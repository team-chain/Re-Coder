"""팀 벤치마크(2.0.45) — 멀티 에이전트·테스트 리코더가 실제로 나아지는지 숫자로 잰다.

같은 과제(benchmarks/team/tasks.json)를 여러 구성으로 만들고, **사람이 쓴 판정 시험(oracle)** 으로 잰다.

  A 혼자          — mode=auto, 테스트 끔(요청이 크면 코어가 알아서 나눌 수 있다 — 기록에 engine 으로 남긴다)
  B 팀            — mode=team, 개발 3명, 테스트 끔
  C 팀+테스트     — mode=team, 개발 3명, 테스트 리코더 켬(만든 앱을 띄워 완성 기준을 시험하고 실패를 고친다)
  D 팀+테스트+역할 — C 와 같고 검토·테스트만 빠른 모델(균형 프리셋)

생성 쪽에는 과제의 prompt 와 criteria 만 보인다. oracle 은 생성이 끝난 뒤에만 쓴다(시험에 맞춰 만드는 것을 막는다).

재는 것(실행마다 · 구성마다):
  통과율       oracle 이 있는 조건 중 통과한 비율(앱이 뜨지 않으면 모두 실패)
  실행 성공    빌드가 되고 앱이 떠서 요청에 답했는가
  서버 오류    기본 흐름 요청(runtime_check.scenario)에서 5xx 가 난 경로 수
  연결 오류    화면 코드가 부르는 /api 경로 중 서버에 없는 것(정적 검사)
  보안         code_security 점수·발견 수
  시간·비용    걸린 시간, AI 호출 수·토큰·비용(단가를 아는 호출만 더하고 모르는 호출 수를 따로 적는다)

  python scripts/team_benchmark.py --check-oracle            # 판정 시험이 기준 구현에서 통과·버그를 잡는지(무료)
  python scripts/team_benchmark.py                           # 계획과 최대 호출 수만 보여 준다(무료)
  python scripts/team_benchmark.py --live --budget 5         # 실제로 만든다(AI 요금 · Docker 필요)
  python scripts/team_benchmark.py --report benchmarks/team/results   # 저장된 결과로 보고서만 다시

결과는 --output 폴더의 runs.json(실행마다), summary.json, report.md. 중간에 멈춰도 같은 폴더로 다시 실행하면 이어서 한다.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

TASKS = ROOT / "benchmarks" / "team" / "tasks.json"

CONFIGS: dict[str, dict] = {
    "A": {"label": "혼자", "mode": "auto", "agents": 1, "test": False, "models": None},
    "B": {"label": "팀", "mode": "team", "agents": 3, "test": False, "models": None},
    "C": {"label": "팀+테스트", "mode": "team", "agents": 3, "test": True, "models": None},
    "D": {"label": "팀+테스트+역할별 모델", "mode": "team", "agents": 3, "test": True, "models": {"review": "fast", "test": "fast"}},
}
#: 실행 하나의 최대 AI 호출(대략) — 계획 단계에서 비용 상한을 가늠하는 데만 쓴다.
MAX_CALLS = {"A": 12, "B": 40, "C": 60, "D": 60}


def load_tasks(path: Path = TASKS) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    tasks = doc["tasks"] if isinstance(doc, dict) else doc
    for t in tasks:
        ids = {c["id"] for c in t["criteria"]}
        for chk in t["oracle"]:
            if chk["criterion"] not in ids:
                raise SystemExit(f"{t['id']}: oracle 이 없는 조건 {chk['criterion']} 을 가리킵니다")
    return tasks


def spec_of(task: dict) -> dict:
    """생성 요청에 싣는 명세 — 사람이 승인한 것처럼(벤치마크라 승인 창은 건너뛴다)."""
    return {"goal": task["title"], "in_scope": [], "out_of_scope": [],
            "criteria": [{"id": c["id"], "text": c["text"], "kind": c.get("kind", "api"), "check": c.get("check", ""), "source": "human"}
                         for c in task["criteria"]]}


# ── 판정(oracle) ──────────────────────────────────────────────────────────

class OracleSession:
    """runtime_check 이 앱을 띄운 뒤 부르는 자리(acceptance)에 끼워 넣는 판정 시험.

    테스트 리코더(code_test.Session)와 달리 경로표로 시험을 거르지 않는다 — 앱에 없는 경로도 실패로 센다.
    """

    def __init__(self, task: dict):
        self.task = task
        self.criteria = [{"id": c["id"], "text": c["text"]} for c in task["criteria"]]
        self.plan = {"checks": [dict(c) for c in task["oracle"]], "unverifiable": []}
        self.outcome: Optional[dict] = None

    def execute(self, request, routes=None, files=None, logs=None) -> dict:
        import code_test
        self.outcome = code_test.run_checks(self.plan, request, logs, self.criteria)
        return self.outcome


def oracle_score(outcome: Optional[dict], task: dict) -> dict:
    """판정 결과 → 숫자. oracle 이 있는 조건만 분모. 결과가 없으면(앱이 안 뜸) 모두 실패."""
    with_checks = sorted({c["criterion"] for c in task["oracle"]})
    if not outcome:
        return {"passed": 0, "failed": len(with_checks), "total": len(with_checks), "rate": 0.0, "criteria": {c: "failed" for c in with_checks}}
    verdicts = {v["id"]: v["verdict"] for v in outcome.get("criteria") or []}
    passed = sum(1 for c in with_checks if verdicts.get(c) == "passed")
    return {"passed": passed, "failed": len(with_checks) - passed, "total": len(with_checks),
            "rate": round(passed / len(with_checks), 4) if with_checks else 0.0,
            "criteria": {c: verdicts.get(c, "failed") for c in with_checks}}


# ── 연결 오류(정적) ──────────────────────────────────────────────────────

_CALL = re.compile(
    r"""(?:fetch|axios(?:\.(?P<am>get|post|put|patch|delete))?|api(?:\.(?P<pm>get|post|put|patch|delete)))\s*\(\s*
        (?P<q>[`'"])(?P<url>(?:(?!(?P=q)).){1,200})(?P=q)(?P<rest>[^;]{0,300})""", re.X | re.I)
_METHOD_OPT = re.compile(r"""method\s*:\s*[`'"](GET|POST|PUT|PATCH|DELETE)[`'"]""", re.I)


def client_calls(files: dict[str, str], server: str) -> list[tuple[str, str, str]]:
    """화면 코드(서버 폴더 밖 · public/)가 부르는 (메서드, /api 경로, 파일). ${…} 는 :p 로 바꾼다."""
    out = []
    srv = server.strip("/").rstrip("/")
    for path, text in files.items():
        if not re.search(r"\.(m?js|jsx|tsx?|html)$", path) or "node_modules" in path:
            continue
        in_server = srv not in ("", ".") and path.startswith(srv + "/") and "/public/" not in path
        if in_server or (srv in ("", ".") and not re.search(r"(^|/)(public|client|web|frontend|src/pages|src/components)/", path)):
            continue
        for m in _CALL.finditer(text):
            url = m.group("url")
            url = re.sub(r"\$\{[^}]*\}", ":p", url)
            url = re.sub(r"^(?:https?://[^/]+|:p)", "", url)
            url = url.split("?", 1)[0]
            if not url.startswith("/api"):
                continue
            method = (m.group("am") or m.group("pm") or "").upper()
            if not method:
                opt = _METHOD_OPT.search(m.group("rest") or "")
                method = opt.group(1).upper() if opt else "GET"
            out.append((method, url.rstrip("/") or "/", path))
    return list(dict.fromkeys(out))


def connection_errors(files: dict[str, str], server: str) -> list[dict]:
    """화면이 부르는데 서버에 없는 경로(메서드까지 같은 경로가 없음)."""
    import runtime_check
    routes = runtime_check.route_table(files, server)
    if not routes:
        return []

    def matches(method: str, url: str) -> bool:
        for m, p, _ in routes:
            pat = re.sub(r":[A-Za-z_]\w*", "[^/]+", re.escape(p).replace("\\:", ":"))
            if m == method and re.fullmatch(pat, url.replace(":p", "x1")):
                return True
        return False

    return [{"method": m, "path": u, "file": f} for m, u, f in client_calls(files, server) if not matches(m, u)]


# ── 실행 하나 ─────────────────────────────────────────────────────────────

def _approval(instruction: str) -> list[dict]:
    import model_compare
    return model_compare._approval(instruction)


def default_generate(**kwargs: Any) -> dict:
    from code_agent import generate_code
    return generate_code(**kwargs)


def default_verify(workspace: Path, ops: list[dict], oracle: OracleSession) -> dict:
    import generated_validation
    return generated_validation.verify_proposal(workspace, ops, run_app=True, acceptance=oracle)


def project_files(ops: list[dict]) -> dict[str, str]:
    return {op["file"]: op.get("content") or "" for op in ops if isinstance(op, dict) and op.get("file")}


def server_dir(workspace: Path) -> str:
    try:
        import docker_kit
        from build_readiness import ProjectFiles
        info = docker_kit.layout(ProjectFiles(workspace)) or {}
        return info.get("server") or "."
    except Exception:  # noqa: BLE001
        return "."


def run_one(task: dict, key: str, workspace: Path, run_id: str, *,
            generate: Callable[..., dict] = default_generate,
            verify: Callable[[Path, list[dict], OracleSession], dict] = default_verify) -> dict:
    """과제 하나를 구성 key 로 만들고 잰다. 실패도 결과다(error 에 남긴다)."""
    import generation_progress
    import model_compare
    from llm import cost_ledger, task_tier

    cfg = CONFIGS[key]
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    #: 같은 run_id 를 다시 돌려도(결과를 지우고 다시) 원장 기록이 섞이지 않게 실행마다 새 표시.
    tag = f"teambench-{run_id}-{uuid.uuid4().hex[:8]}"
    events: list[dict] = []
    token = generation_progress.bind(lambda e: events.append({k: e.get(k) for k in ("step", "agent", "phase", "passed", "failed", "total", "round")}))
    started = time.monotonic()
    result, error = None, ""
    try:
        with task_tier.tagged(tag):
            result = generate(instruction=task["prompt"], session_id=run_id[-8:], project_root=str(workspace), decisions=_approval(task["prompt"]),
                              mode=cfg["mode"], agents=cfg["agents"], spec=spec_of(task), models=cfg["models"], test=cfg["test"])
    except Exception as exc:  # noqa: BLE001
        error = (str(exc) or exc.__class__.__name__)[:400]
    finally:
        generation_progress._reporter.reset(token)
    seconds = round(time.monotonic() - started, 1)
    records = [r for r in cost_ledger.all_records() if r.get("run") == tag]
    metrics = model_compare.measure(records, result)
    ops = [op for op in ((result or {}).get("ops") or []) if isinstance(op, dict)]
    for op in ops:
        target = workspace / op["file"]
        if workspace.resolve() not in target.resolve().parents:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(op.get("content") or "", encoding="utf-8", newline="")
    oracle = OracleSession(task)
    verification: dict = {}
    if ops:
        try:
            verification = verify(workspace, ops, oracle) or {}
        except Exception as exc:  # noqa: BLE001
            verification = {"status": "unavailable", "output": str(exc)[:300]}
    runtime = verification.get("runtime") or {}
    failures = runtime.get("failures") or []
    started_ok = bool(verification.get("passed")) and runtime.get("status") in ("passed", "failed") \
        and not any(str(f.get("request", "")).startswith("앱 시작") for f in failures)
    files = project_files(ops)
    server = server_dir(workspace)
    conn = connection_errors(files, server) if files else []
    acc = ((result or {}).get("spec") or {}).get("acceptance") or {}
    return {
        "run_id": run_id, "task": task["id"], "config": key, "label": cfg["label"], "error": error,
        "seconds": seconds, "files": len(ops),
        "engine": any(e.get("step") in ("planned", "wave", "file_start") for e in events),
        "build": verification.get("status") or ("skipped" if not ops else "unavailable"),
        "run_ok": started_ok,
        "server_errors": sum(1 for f in failures if not f.get("acceptance") and not str(f.get("request", "")).startswith("앱 시작")),
        "connection_errors": len(conn), "connection_detail": conn[:10],
        "oracle": oracle_score(oracle.outcome if started_ok else None, task),
        "self_test": {k: acc.get(k) for k in ("status", "passed", "failed", "unverifiable", "total", "rounds")} if acc else None,
        "calls": metrics["calls"], "input_tokens": metrics["input_tokens"], "output_tokens": metrics["output_tokens"],
        "cost_usd": metrics["cost_usd"], "unknown_cost_calls": metrics["unknown_cost_calls"],
        "security_score": metrics["security_score"], "security_findings": metrics["security_findings"],
        "remaining_errors": metrics["remaining_errors"],
    }


# ── 모으기 · 보고서 ──────────────────────────────────────────────────────

def _mean(xs: list[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(statistics.fmean(xs), 4) if xs else None


def _median(xs: list[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def summarize(runs: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for key in CONFIGS:
        mine = [r for r in runs if r["config"] == key]
        if not mine:
            continue
        passed = sum(r["oracle"]["passed"] for r in mine)
        total = sum(r["oracle"]["total"] for r in mine)
        known = [r["cost_usd"] for r in mine if r["cost_usd"] is not None]
        out[key] = {
            "label": CONFIGS[key]["label"], "runs": len(mine), "errors": sum(1 for r in mine if r["error"]),
            "pass_rate": round(passed / total, 4) if total else None, "criteria_passed": passed, "criteria_total": total,
            "tasks_fully_passed": sum(1 for r in mine if r["oracle"]["total"] and r["oracle"]["passed"] == r["oracle"]["total"]),
            "run_ok_rate": round(sum(1 for r in mine if r["run_ok"]) / len(mine), 4),
            "server_errors": _mean([r["server_errors"] for r in mine]),
            "connection_errors": _mean([r["connection_errors"] for r in mine]),
            "security_findings": _mean([r["security_findings"] for r in mine]),
            "security_score": _mean([r["security_score"] for r in mine]),
            "median_seconds": _median([r["seconds"] for r in mine]),
            "calls": _mean([r["calls"] for r in mine]),
            "cost_usd": round(sum(known), 6) if known else None,
            "unknown_cost_calls": sum(r["unknown_cost_calls"] for r in mine),
            "cost_per_passed_criterion": round(sum(known) / passed, 6) if known and passed and len(known) == len(mine) else None,
            "engine_runs": sum(1 for r in mine if r["engine"]),
        }
    return out


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _num(v: Optional[float], digits: int = 2) -> str:
    return "—" if v is None else (f"{v:.{digits}f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v))


def _usd(v: Optional[float]) -> str:
    return "—" if v is None else f"${v:.4f}"


def paired(runs: list[dict], a: str, b: str) -> dict:
    """같은 과제·같은 반복끼리 비교 — b 가 a 보다 통과 조건이 많은/같은/적은 쌍의 수."""
    by = {}
    for r in runs:
        by.setdefault((r["task"], r["run_id"].rsplit("-", 1)[-1]), {})[r["config"]] = r
    better = same = worse = 0
    for pair in by.values():
        if a in pair and b in pair:
            x, y = pair[a]["oracle"]["passed"], pair[b]["oracle"]["passed"]
            better += y > x
            same += y == x
            worse += y < x
    return {"better": better, "same": same, "worse": worse}


def report_md(runs: list[dict], meta: dict) -> str:
    s = summarize(runs)
    lines = ["# 팀 벤치마크 결과", "",
             f"실행일: {meta.get('date', '')} · 과제 {meta.get('tasks', '?')}개 · 반복 {meta.get('repeat', 1)}회 · 모델: {meta.get('models', '기록 없음')}", ""]
    lines += ["## 구성별", "",
              "| 구성 | 실행 | 판정 통과율 | 모두 통과한 과제 | 실행 성공 | 서버 오류(평균) | 연결 오류(평균) | 보안 발견(평균) | 중앙 시간 | AI 호출(평균) | 비용 | 통과 조건 1개당 비용 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for key, v in s.items():
        cost = _usd(v["cost_usd"]) + (f" (+단가 모름 {v['unknown_cost_calls']}회)" if v["unknown_cost_calls"] else "")
        lines.append(f"| {key} {v['label']} | {v['runs']} | {_pct(v['pass_rate'])} ({v['criteria_passed']}/{v['criteria_total']}) | {v['tasks_fully_passed']}/{v['runs']} "
                     f"| {_pct(v['run_ok_rate'])} | {_num(v['server_errors'])} | {_num(v['connection_errors'])} | {_num(v['security_findings'])} "
                     f"| {_num(v['median_seconds'], 1)}초 | {_num(v['calls'], 1)} | {cost} | {_usd(v['cost_per_passed_criterion'])} |")
    keys = list(s)
    if "A" in s and len(keys) > 1:
        lines += ["", "## 같은 과제끼리 (A 대비)", "", "| 비교 | 더 많이 통과 | 같음 | 더 적게 통과 |", "|---|---:|---:|---:|"]
        for k in keys:
            if k != "A":
                p = paired(runs, "A", k)
                lines.append(f"| {k} vs A | {p['better']} | {p['same']} | {p['worse']} |")
    lines += ["", "## 과제별 판정 통과(통과/조건)", "", "| 과제 | " + " | ".join(keys) + " |", "|---|" + "---:|" * len(keys)]
    for tid in dict.fromkeys(r["task"] for r in runs):
        row = []
        for k in keys:
            mine = [r for r in runs if r["task"] == tid and r["config"] == k]
            row.append(", ".join(f"{r['oracle']['passed']}/{r['oracle']['total']}" + ("·실행✗" if not r["run_ok"] else "") for r in mine) or "—")
        lines.append(f"| {tid} | " + " | ".join(row) + " |")
    n = len({r['task'] for r in runs})
    lines += ["", "## 읽는 법", "",
              "- 판정 통과율은 사람이 쓴 시험(benchmarks/team/tasks.json 의 oracle)으로만 잰다. 생성 쪽은 oracle 을 보지 못한다.",
              "- C·D 의 테스트 리코더는 같은 완성 기준으로 **스스로 만든** 시험을 돌린다 — oracle 과 다른 시험이다.",
              "- 앱이 뜨지 않으면(실행✗) 그 과제의 조건은 모두 실패로 센다.",
              "- 비용은 단가를 아는 호출만 더한다. 단가를 모르는 호출이 있으면 '통과 조건 1개당 비용'은 비운다.",
              f"- 과제 {n}개는 작은 표본이다. 차이가 몇 조건 수준이면 반복(--repeat)을 늘려 다시 확인하라.",
              "- A 는 요청이 크면 코어가 알아서 나눠 만들 수 있다(engine 열). 이번 과제들은 작아 대부분 한 번에 만든다."]
    eng = {k: v["engine_runs"] for k, v in s.items()}
    lines.append("- 나눠 만든(엔진) 실행 수: " + ", ".join(f"{k} {v}" for k, v in eng.items()))
    return "\n".join(lines) + "\n"


# ── 판정 시험 점검(무료) ────────────────────────────────────────────────

def check_oracle(tasks: list[dict]) -> list[str]:
    """기준 구현에서 모두 통과하고, 넣은 버그는 정확히 그 조건에서 잡는지. 문제 목록(비면 통과)."""
    sys.path.insert(0, str(ROOT / "benchmarks" / "team"))
    import reference_app
    import runtime_check
    problems = []
    for task in tasks:
        bugs = reference_app.BUGS.get(task["id"], {})
        for bug in [None, *bugs]:
            srv = reference_app.serve(task["id"], frozenset([bug]) if bug else frozenset())
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            try:
                oracle = OracleSession(task)
                out = oracle.execute(lambda m, p, b, t: runtime_check._request(base, m, p, b, t))
                score = oracle_score(out, task)
            finally:
                srv.shutdown()
            if bug is None and score["failed"]:
                bad = [c for c, v in score["criteria"].items() if v != "passed"]
                problems.append(f"{task['id']}: 기준 구현에서 실패 {bad}")
            if bug is not None:
                want = bugs[bug] if isinstance(bugs[bug], (tuple, list)) else (bugs[bug],)
                for w in want:
                    if score["criteria"].get(w) != "failed":
                        problems.append(f"{task['id']}: 버그 {bug} 를 {w} 가 잡지 못함")
                extra = [c for c, v in score["criteria"].items() if v == "failed" and c not in want]
                if extra:
                    problems.append(f"{task['id']}: 버그 {bug} 가 상관없는 조건 {extra} 까지 실패시킴")
    return problems


# ── 실행 ──────────────────────────────────────────────────────────────────

def _save(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def execute(args, *, generate=default_generate, verify=default_verify) -> dict:
    tasks = load_tasks(args.tasks)
    if args.only:
        tasks = [t for t in tasks if t["id"] in args.only]
    keys = [k for k in args.configs if k in CONFIGS]
    plan = [(t, k, rep) for rep in range(1, args.repeat + 1) for t in tasks for k in keys]
    if not args.live:
        print(json.dumps({"runs": len(plan), "configs": keys, "tasks": len(tasks), "repeat": args.repeat,
                          "max_ai_calls": sum(MAX_CALLS[k] for _, k, _ in plan), "budget_usd": args.budget,
                          "message": "실제로 만들려면 --live (AI 요금이 들고 Docker 가 필요합니다)."}, ensure_ascii=False, indent=1))
        return {}
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    runs: list[dict] = json.loads((out / "runs.json").read_text(encoding="utf-8")) if (out / "runs.json").exists() else []
    done = {r["run_id"] for r in runs}
    for task, key, rep in plan:
        run_id = f"{task['id']}-{key}-{rep}"
        if run_id in done:
            continue
        spent = sum(r["cost_usd"] or 0 for r in runs)
        if spent >= args.budget:
            print(f"예산 ${args.budget} 에 닿아 멈춥니다(지금까지 ${spent:.4f}).", flush=True)
            break
        r = run_one(task, key, out / "work" / run_id, run_id, generate=generate, verify=verify)
        runs.append(r)
        _save(out / "runs.json", runs)
        print(f"{len(runs)}/{len(plan)} {run_id} 판정 {r['oracle']['passed']}/{r['oracle']['total']} 실행 {'✓' if r['run_ok'] else '✗'} "
              f"{r['seconds']}초 ${r['cost_usd'] or 0:.4f}{' · ' + r['error'] if r['error'] else ''}", flush=True)
        if not args.keep_work:
            shutil.rmtree(out / "work" / run_id, ignore_errors=True)
    meta = {"date": time.strftime("%Y-%m-%d"), "tasks": len(tasks), "repeat": args.repeat, "models": args.models_note or _models_note()}
    _save(out / "summary.json", {"meta": meta, "configs": summarize(runs)})
    (out / "report.md").write_text(report_md(runs, meta), encoding="utf-8")
    return {"runs": runs, "summary": summarize(runs)}


def _models_note() -> str:
    try:
        from llm.provider_router import get_provider_router
        r = get_provider_router()
        q, f = r._spec_provider("quality"), r._spec_provider("fast")
        return f"품질 {q.provider_name}/{q.model_id}" + (f" · 빠른 {f.provider_name}/{f.model_id}" if f else "")
    except Exception:  # noqa: BLE001
        return "기록 없음"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tasks", type=Path, default=TASKS)
    p.add_argument("--output", type=Path, default=ROOT / "benchmarks" / "team" / "results")
    p.add_argument("--configs", type=lambda s: [x.strip().upper() for x in s.split(",") if x.strip()], default=["A", "B", "C"])
    p.add_argument("--only", type=lambda s: [x.strip() for x in s.split(",") if x.strip()], default=None, help="과제 id 몇 개만")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--budget", type=float, default=5.0, help="USD 상한 — 넘으면 다음 실행을 시작하지 않는다")
    p.add_argument("--live", action="store_true", help="실제 AI 호출(요금 발생)")
    p.add_argument("--keep-work", action="store_true", help="만든 프로젝트 폴더를 남긴다")
    p.add_argument("--models-note", default="", help="보고서에 적을 모델 이름")
    p.add_argument("--check-oracle", action="store_true", help="판정 시험을 기준 구현으로 점검(무료)")
    p.add_argument("--report", type=Path, help="저장된 runs.json 으로 보고서만 다시 만든다")
    args = p.parse_args(argv)
    if args.check_oracle:
        problems = check_oracle(load_tasks(args.tasks))
        print("\n".join(problems) if problems else "판정 시험 점검 통과 — 기준 구현은 모두 통과, 넣은 버그는 모두 해당 조건에서 잡음")
        return 1 if problems else 0
    if args.report:
        runs = json.loads((args.report / "runs.json").read_text(encoding="utf-8"))
        meta = json.loads((args.report / "summary.json").read_text(encoding="utf-8")).get("meta", {}) if (args.report / "summary.json").exists() else {}
        (args.report / "report.md").write_text(report_md(runs, meta), encoding="utf-8")
        print(args.report / "report.md")
        return 0
    if args.live and not 0 < args.budget <= 50:
        raise SystemExit("--budget 는 0 보다 크고 50 이하(USD)")
    execute(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
