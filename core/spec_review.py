"""명세 화면(요구사항 · 설계 · 작업) — 아키텍처 계약을 기능 단위로 묶고, 코드·통합 테스트·AI 검토로 상태를 붙인다.

실사용 2026-11-19: 아키텍처 화면이 API 하나씩(dry run)·파일 그래프만 보여 "전체가 요청에 맞게 만들어졌는지" 알 수 없었다.
여기서는 프로젝트 하나를 Kiro 의 명세처럼 본다.
  · 요구사항: 공개 서비스의 API 를 기능(회원·상품·주문 …)으로 묶고, 기능마다 수용 기준(WHEN … 한다)과 통과 여부
  · 설계: 서비스·DB·연결과 전체 API 표
  · 작업: 고칠 것 — 계약 위반·실패한 테스트·인증 빠진 쓰기 API(규칙) + AI 검토가 찾은 빠진 기능
규칙 부분은 코드를 실행하지 않고 즉시 계산한다. AI 검토는 사용자가 누를 때만 한 번 부르고 결과를 저장한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import architecture as arch_mod
    import compose_deploy
    import contract_check
except ImportError:  # pragma: no cover - 패키지 실행
    from core import architecture as arch_mod  # type: ignore
    from core import compose_deploy  # type: ignore
    from core import contract_check  # type: ignore

#: 경로의 첫 이름 → 기능 이름. 모르는 이름은 그대로 쓴다.
_LABELS = {
    "auth": "회원가입 · 로그인", "login": "회원가입 · 로그인", "signup": "회원가입 · 로그인", "register": "회원가입 · 로그인",
    "users": "내 정보", "me": "내 정보", "profile": "내 정보", "accounts": "내 정보", "members": "내 정보",
    "products": "상품", "items": "상품", "catalog": "상품", "categories": "상품",
    "reviews": "리뷰", "ratings": "리뷰", "wishlist": "찜", "wishlists": "찜", "favorites": "찜", "likes": "찜",
    "cart": "장바구니", "carts": "장바구니", "orders": "주문 · 결제", "payments": "주문 · 결제", "checkout": "주문 · 결제",
    "admin": "관리자", "notifications": "알림", "posts": "게시글", "comments": "댓글", "files": "파일", "uploads": "파일",
    "search": "검색", "webhooks": "외부 연동",
}
#: 하위 경로가 이 이름이면 그 기능으로 묶는다(/products/:id/reviews → 리뷰).
_SUBRESOURCES = {"reviews", "ratings", "comments", "likes", "wishlist", "favorites", "cart", "payments"}
_WRITES = {"POST", "PUT", "PATCH", "DELETE"}
#: 로그인 없이 써도 되는 쓰기 API(가입·로그인·토큰 갱신·외부 웹훅)
_PUBLIC_WRITE = re.compile(r"/(auth|login|signup|register|token|refresh|password-reset|webhooks?)(/|$)", re.I)

_UNKNOWN = "unknown"


def _segments(path: str) -> list[str]:
    parts = [p for p in (path or "").split("/") if p and not p.startswith(":") and not p.startswith("{")]
    return parts[1:] if parts and parts[0] == "api" else parts


def feature_key(path: str) -> str:
    segs = [s.lower() for s in _segments(path)]
    if not segs:
        return "root"
    for s in segs[1:]:
        if s in _SUBRESOURCES:
            return s
    return segs[0]


def feature_label(key: str) -> str:
    return _LABELS.get(key, key)


def _criterion(case: dict, result: Optional[dict]) -> dict:
    """통합 테스트 하나 → 읽을 수 있는 수용 기준."""
    expect = str(case.get("expect", ""))
    if expect.startswith("401") or expect.startswith("403"):
        cond, text = "WHEN", "로그인하지 않고 부르면 거절한다"
    elif expect in ("2xx",) and case.get("path") in ("/health", "/"):
        cond, text = "WHEN", "서비스가 떠 있으면 응답한다"
    elif expect.startswith("!5xx"):
        cond, text = "WHEN", "요청을 받으면 서버 오류 없이 응답한다"
    elif expect.isdigit():
        cond, text = "WHEN", f"올바른 요청에 {expect} 로 응답한다"
    else:
        cond, text = "WHEN", f"응답이 {expect} 이다"
    status = _UNKNOWN
    if result is not None:
        status = "ok" if result.get("ok") else "fail"
    return {"cond": cond, "text": text, "status": status, "source": "test", "test": case["id"],
            "api": f"{case['method']} {case['path']}", **({"got": result["status"]} if result and "status" in result else {})}


def _last_results(workspace: str) -> tuple[dict[str, dict], Optional[dict]]:
    """마지막 로컬 실행의 검사별 결과 {test id: {ok, status}} 와 그 기록."""
    try:
        records = compose_deploy.history(workspace)
    except Exception:  # noqa: BLE001 - 기록이 깨져도 화면은 보여 준다
        records = []
    for rec in records:
        rows = rec.get("check_results")
        if isinstance(rows, list) and rows:
            out = {}
            for r in rows:
                name = str(r.get("name", ""))
                if name.startswith("test:"):
                    out[name[5:]] = r
            return out, rec
    return {}, (records[0] if records else None)


def last_deploy(workspace: str, arch: dict) -> Optional[dict]:
    """마지막 로컬 실행(배포 · 롤백) — 프로젝트 요약 카드용. 서비스별 상태는 그때의 검사 결과."""
    try:
        records = compose_deploy.history(workspace)
    except Exception:  # noqa: BLE001
        return None
    if not records:
        return None
    rec = records[0]
    rows = {str(r.get("name", "")): bool(r.get("ok")) for r in rec.get("check_results") or [] if isinstance(r, dict)}
    services = {}
    for s in arch.get("services", []):
        key = f"service:{s['name']}"
        services[s["name"]] = "ok" if rows.get(key) else ("fail" if key in rows else "unknown")
    for d in arch.get("databases", []):
        key = f"db:{d['name']}"
        services[d["name"]] = "ok" if rows.get(key) else ("fail" if key in rows else "unknown")
    public = next((s for s in arch.get("services", []) if s.get("public") and s.get("port")), None)
    status = str(rec.get("status", ""))
    return {"id": str(rec.get("id", "")), "at": str(rec.get("at", "")), "status": status,
            "failures": [str(f) for f in (rec.get("failures") or [])][:5],
            "url": f"http://localhost:{public['port']}" if public and status in ("success", "degraded", "rolled_back") else "",
            "services": services}


# ── AI 검토 저장소 ───────────────────────────────────────────────────────────
def _store_dir() -> Path:
    return Path(os.environ.get("RECODER_SPEC_DIR") or (Path.home() / ".recoder" / "spec"))


def _store_path(workspace: str) -> Path:
    key = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:24]
    return _store_dir() / f"{key}.json"


def load_review(workspace: str) -> Optional[dict]:
    try:
        data = json.loads(_store_path(workspace).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_review(workspace: str, review: dict) -> None:
    path = _store_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def contract_fingerprint(arch: dict) -> str:
    return hashlib.sha256(json.dumps(arch, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


# ── 명세 계산 ────────────────────────────────────────────────────────────────
def build(workspace: str) -> dict:
    ws = str(workspace or "")
    try:
        raw = json.loads((Path(ws) / "architecture.json").read_text(encoding="utf-8"))
        arch, _ = arch_mod.normalize(raw)
    except (OSError, ValueError):
        arch = None
    if not arch:
        return {"available": False, "reason": "아키텍처 계약(architecture.json)이 없는 프로젝트입니다."}

    files = contract_check.read_workspace(ws, arch)
    checked = contract_check.check(arch, files)
    routes: dict[tuple[str, str, str], tuple[str, int]] = {}
    for svc in arch["services"]:
        if svc["kind"] != "api":
            continue
        own = contract_check._service_files(arch, svc, files)  # noqa: SLF001 - 같은 패키지 도우미
        found = contract_check._py_routes(own) if svc["language"] == "python" else contract_check._js_routes(own)  # noqa: SLF001
        for method, path, rel, line in found:
            routes.setdefault((svc["name"], method, contract_check.norm_path(path)), (rel, line))

    results, last = _last_results(ws)
    cases = arch_mod.test_cases(arch)
    case_by_api: dict[tuple[str, str, str], list[dict]] = {}
    for c in cases:
        case_by_api.setdefault((c["service"], c["method"], c["path"]), []).append(c)

    public = {s["name"] for s in arch["services"] if s.get("public") and s["kind"] == "api"}
    if not public:
        public = {s["name"] for s in arch["services"] if s["kind"] == "api"}
    review = load_review(ws)
    fp = contract_fingerprint(arch)
    review_fresh = bool(review and review.get("contract") == fp)

    features: dict[str, dict] = {}
    endpoints_out: list[dict] = []
    for e in arch["endpoints"]:
        test_path = re.sub(r":[A-Za-z_]\w*|\{[^}]+\}", "1", e["path"])
        hit = routes.get((e["service"], e["method"], contract_check.norm_path(e["path"])))
        api = {
            "service": e["service"], "method": e["method"], "path": e["path"], "auth": bool(e["auth"]),
            "admin": "/admin" in e["path"].lower(), "public": e["service"] in public,
            "implemented": hit is not None, "file": hit[0] if hit else "", "line": hit[1] if hit else 0,
        }
        crit = [_criterion(c, results.get(c["id"])) for c in case_by_api.get((e["service"], e["method"], test_path), [])]
        api["tests"] = [{"id": c["test"], "status": c["status"]} for c in crit]
        endpoints_out.append(api)
        key = feature_key(e["path"]) if api["public"] else "internal"
        f = features.setdefault(key, {"key": key, "label": "서비스 간 호출" if key == "internal" else feature_label(key),
                                      "internal": key == "internal", "services": [], "endpoints": [], "criteria": []})
        if e["service"] not in f["services"]:
            f["services"].append(e["service"])
        f["endpoints"].append(api)
        for c in crit:
            c["api"] = f"{e['method']} {e['path']}"
            f["criteria"].append(c)

    # 같은 이름(라벨)의 기능은 합친다(auth · login 이 둘 다 "회원가입 · 로그인").
    merged: dict[str, dict] = {}
    for f in features.values():
        m = merged.get(f["label"])
        if m is None:
            merged[f["label"]] = f
        else:
            m["endpoints"] += f["endpoints"]
            m["criteria"] += f["criteria"]
            m["services"] += [s for s in f["services"] if s not in m["services"]]

    # AI 검토 결과를 기능에 붙인다(사람이 읽는 수용 기준이 앞에).
    ai_by_label: dict[str, list[dict]] = {}
    if review_fresh:
        for req in review.get("requirements") or []:
            label = str(req.get("feature") or "")
            for c in req.get("criteria") or []:
                ai_by_label.setdefault(label, []).append({
                    "cond": str(c.get("cond") or "WHEN")[:8].upper(), "text": str(c.get("text") or "")[:240],
                    "status": c.get("status") if c.get("status") in ("ok", "fail", "missing") else _UNKNOWN,
                    "source": "ai", "note": str(c.get("note") or "")[:200],
                })
    for label, crits in ai_by_label.items():
        target = merged.get(label)
        if target is None:
            target = merged[label] = {"key": label, "label": label, "internal": False, "services": [], "endpoints": [], "criteria": []}
        target["criteria"] = crits + target["criteria"]

    def status_of(f: dict) -> str:
        sts = [c["status"] for c in f["criteria"]]
        if any(not a["implemented"] for a in f["endpoints"] if a["service"] in {s["name"] for s in arch["services"] if s["kind"] == "api"}):
            return "fail"
        if "fail" in sts:
            return "fail"
        if "missing" in sts:
            return "warn"
        if sts and all(s == "ok" for s in sts if s != _UNKNOWN) and any(s == "ok" for s in sts):
            return "ok" if _UNKNOWN not in [c["status"] for c in f["criteria"] if c["source"] == "test"] else "partial"
        return _UNKNOWN

    order = list(_LABELS.values())
    feats = sorted(merged.values(), key=lambda f: (f["internal"], order.index(f["label"]) if f["label"] in order else 99, f["label"]))
    for f in feats:
        f["status"] = status_of(f)
        f["counts"] = {"api": len(f["endpoints"]), "criteria": len(f["criteria"]),
                       "ok": sum(1 for c in f["criteria"] if c["status"] == "ok"),
                       "fail": sum(1 for c in f["criteria"] if c["status"] in ("fail", "missing"))}

    tasks = rule_tasks(arch, endpoints_out, checked["findings"], results, last)
    if review_fresh:
        for t in review.get("tasks") or []:
            title = str(t.get("title") or "").strip()
            if title:
                tasks.append({"id": "ai:" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:10], "title": title[:120],
                              "detail": str(t.get("detail") or "")[:400], "feature": str(t.get("feature") or ""),
                              "severity": t.get("severity") if t.get("severity") in ("high", "medium", "low") else "medium",
                              "source": "ai"})

    totals = {"passed": sum(1 for r in results.values() if r.get("ok")), "total": len(results)}
    services = []
    for s in arch["services"]:
        services.append({"name": s["name"], "path": s.get("path", ""), "kind": s["kind"], "language": s["language"], "port": s["port"],
                         "public": bool(s.get("public")), "databases": s["databases"], "depends_on": s["depends_on"],
                         "purpose": s.get("purpose", ""), "api": sum(1 for e in arch["endpoints"] if e["service"] == s["name"])})
    return {
        "available": True,
        "project": Path(ws).name,
        "features": feats,
        "services": services,
        "databases": [{"name": d["name"], "engine": d["engine"], "tables": d.get("tables", [])} for d in arch["databases"]],
        "endpoints": endpoints_out,
        "tasks": tasks,
        "tests": {**totals, "at": (last or {}).get("at", ""), "status": (last or {}).get("status", "")},
        "review": ({"at": review.get("at", ""), "summary": review.get("summary", ""), "fresh": review_fresh,
                    "missing": review.get("missing") or []} if review else None),
        "contract": fp,
        "deploy": last_deploy(ws, arch),
    }


def rule_tasks(arch: dict, endpoints: list[dict], findings: list[dict], results: dict[str, dict], last: Optional[dict]) -> list[dict]:
    """AI 없이 확실히 말할 수 있는 고칠 것."""
    out: list[dict] = []

    def add(tid: str, title: str, detail: str, severity: str, feature: str = "") -> None:
        out.append({"id": tid, "title": title, "detail": detail, "severity": severity, "feature": feature, "source": "rule"})

    for f in findings:
        if f["severity"] == "info":
            continue
        add(f"contract:{f['code']}:{f['service']}:{f['line']}:{hashlib.sha1(f['message'].encode('utf-8')).hexdigest()[:6]}",
            f["message"], f["fix"], "high" if f["severity"] == "error" else "medium")
    for e in endpoints:
        label = feature_label(feature_key(e["path"])) if e["public"] else "서비스 간 호출"
        if not e["public"]:
            continue
        if e["method"] in _WRITES and not e["auth"] and not _PUBLIC_WRITE.search(e["path"]):
            add(f"auth:{e['method']}:{e['path']}", f"{e['method']} {e['path']} 를 로그인 없이 부를 수 있음",
                "데이터를 바꾸는 API 입니다. 로그인(필요하면 권한)을 확인하세요.", "high", label)
        if e["admin"] and not e["auth"]:
            add(f"admin:{e['method']}:{e['path']}", f"관리자 API {e['path']} 에 로그인 확인 없음",
                "관리자만 부를 수 있게 하세요.", "high", label)
    failed = [k for k, r in results.items() if not r.get("ok")]
    cases = {c["id"]: c for c in arch_mod.test_cases(arch)}
    for tid in failed[:20]:
        c = cases.get(tid)
        if c:
            got = results[tid].get("status")
            add(f"test:{tid}", f"{c['method']} {c['path']} 통합 테스트 실패",
                f"기대 {c['expect']}" + (f" · 실제 {got}" if got is not None else "") + f" — {c.get('why', '')}", "high",
                feature_label(feature_key(c["path"])))
    if not last:
        add("run:local", "로컬 실행으로 통합 테스트 돌리기", "아직 실행한 적이 없어 수용 기준을 확인하지 못했습니다.", "medium")
    return out


# ── AI 검토 ──────────────────────────────────────────────────────────────────
_REVIEW_PROMPT = """너는 시니어 백엔드 리뷰어다. 아래 프로젝트가 "제품으로서" 요청에 맞게 만들어졌는지 검토하라.

[프로젝트 설명]
{intent}

[아키텍처 계약 — 서비스]
{services}

[공개 API 와 코드 구현 여부 · 통합 테스트]
{apis}

할 일:
1. 기능(feature)별 요구사항을 3~6개씩, 사용자가 읽을 짧은 한국어 수용 기준으로 써라.
   cond 는 WHEN/IF/WHILE 중 하나, text 는 "~하면 ~한다" 꼴로 25자 안팎. 위 API·코드로 충족되면 status "ok",
   필요한데 API 가 없으면 "missing", 판단할 수 없으면 "unknown". feature 이름은 다음 중에서 고른다: {labels}
2. 이 종류의 서비스에 실제로 필요한데 빠진 것을 tasks 로(최대 6개). title 은 20자 안팎 명령형, detail 은 한 문장,
   severity 는 high/medium/low. 이미 있는 기능을 다시 적지 마라.
3. summary 는 한 문장(40자 안팎).

JSON 만 출력:
{{"summary": "...", "requirements": [{{"feature": "...", "criteria": [{{"cond": "WHEN", "text": "...", "status": "ok", "note": ""}}]}}],
  "tasks": [{{"title": "...", "detail": "...", "feature": "...", "severity": "medium"}}], "missing": ["..."]}}"""


def review_prompt(workspace: str, spec: dict) -> str:
    intent_parts = []
    for name in ("README.md", "ARCHITECTURE.md"):
        try:
            intent_parts.append((Path(workspace) / name).read_text(encoding="utf-8")[:3500])
        except OSError:
            continue
    services = "\n".join(f"- {s['name']} ({s['kind']}, DB {', '.join(s['databases']) or '없음'}): {s['purpose']}" for s in spec["services"])
    apis = "\n".join(
        f"- {a['method']} {a['path']} · {'로그인' if a['auth'] else '공개'}{' · 관리자' if a['admin'] else ''}"
        f" · {'구현됨' if a['implemented'] else '코드 없음'}"
        + (" · 테스트 " + "/".join(t["status"] for t in a["tests"]) if a["tests"] else "")
        for a in spec["endpoints"] if a["public"])
    labels = ", ".join(f["label"] for f in spec["features"] if not f["internal"])
    return _REVIEW_PROMPT.format(intent="\n\n".join(intent_parts) or "(설명 파일 없음)", services=services,
                                 apis=apis[:9000], labels=labels or "자유")


def parse_review(raw: str) -> dict:
    text = (raw or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("AI 응답에서 JSON 을 찾지 못했습니다")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("AI 응답 형식이 올바르지 않습니다")
    reqs = [r for r in data.get("requirements") or [] if isinstance(r, dict)]
    tasks = [t for t in data.get("tasks") or [] if isinstance(t, dict)][:8]
    return {"summary": str(data.get("summary") or "")[:200], "requirements": reqs[:20], "tasks": tasks,
            "missing": [str(x)[:80] for x in (data.get("missing") or []) if isinstance(x, (str, int, float))][:10]}


async def run_review(workspace: str, complete) -> dict:
    """complete(prompt) → 모델 응답 문자열. 결과를 저장하고 새 명세를 돌려준다."""
    spec = build(workspace)
    if not spec.get("available"):
        return spec
    raw = await complete(review_prompt(workspace, spec))
    review = parse_review(raw)
    review.update(at=datetime.now(timezone.utc).isoformat(), contract=spec["contract"])
    save_review(workspace, review)
    return build(workspace)
