"""팀 벤치마크(scripts/team_benchmark.py) — 가짜 AI 로 측정·집계·보고서가 맞는지, 판정 시험이 버그를 잡는지.

실제 AI 로 돌리는 것은 사용자가 한다(요금·키 필요). 여기서는 돈이 들지 않는 부분을 끝까지 확인한다.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("team_benchmark", ROOT / "scripts" / "team_benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["team_benchmark"] = mod
    spec.loader.exec_module(mod)
    sys.path.insert(0, str(ROOT / "benchmarks" / "team"))
    return mod


def test_tasks_are_well_formed_and_hide_the_oracle(bench):
    tasks = bench.load_tasks()
    assert len(tasks) >= 10
    for t in tasks:
        assert t["prompt"].startswith(t["prompt"].split(".")[0]) and "/api/" in t["prompt"]
        spec = bench.spec_of(t)
        dumped = json.dumps(spec, ensure_ascii=False)
        #: 생성 쪽에 가는 명세에는 판정 시험(oracle)의 값이 들어가지 않는다
        assert "{{rand}}" not in dumped and "oracle" not in dumped
        assert {c["id"] for c in spec["criteria"]} == {c["id"] for c in t["criteria"]}
        assert {c["criterion"] for c in t["oracle"]} <= {c["id"] for c in t["criteria"]}
    assert sum(1 for t in tasks if t["frontend"]) >= 3, "연결 오류를 잴 화면 과제"


def test_oracle_passes_the_reference_and_catches_every_planted_bug(bench):
    assert bench.check_oracle(bench.load_tasks()) == []


def test_connection_errors_find_client_calls_without_a_server_route(bench):
    files = {
        "server/src/index.js": "const express = require('express');\nconst todos = require('./routes/todos');\nconst app = express();\n"
                               "app.use('/api/todos', todos);\napp.use(express.static('public'));\n",
        "server/src/routes/todos.js": "const router = require('express').Router();\nrouter.get('/', (req, res) => res.json([]));\n"
                                      "router.post('/', (req, res) => res.status(201).json({}));\nrouter.patch('/:id', (req, res) => res.json({}));\nmodule.exports = router;\n",
        "server/public/app.js": "fetch('/api/todos').then(r => r.json());\n"
                                "fetch(`/api/todos/${id}`, { method: 'PATCH', body: '{}' });\n"
                                "fetch('/api/todo', { method: 'POST' });\n"
                                "fetch(`/api/todos/${id}`, { method: 'DELETE' });\n"
                                "fetch('https://cdn.example.com/lib.js');\n",
    }
    calls = bench.client_calls(files, "server")
    assert ("GET", "/api/todos", "server/public/app.js") in calls and ("PATCH", "/api/todos/:p", "server/public/app.js") in calls
    errs = bench.connection_errors(files, "server")
    assert [(e["method"], e["path"]) for e in errs] == [("POST", "/api/todo"), ("DELETE", "/api/todos/:p")]
    #: 서버 코드 안의 fetch(다른 서비스 호출)는 화면 호출로 세지 않는다
    files["server/src/routes/todos.js"] += "fetch('/api/elsewhere');\n"
    assert len(bench.connection_errors(files, "server")) == 2


def _fake_world(bench, reference_app, runtime_check, bugs_by_config):
    """가짜 AI(generate)와 가짜 실행(verify): 구성마다 기준 구현에 버그를 다르게 넣어 띄우고 oracle 을 돌린다."""
    seen = []

    def generate(**kw):
        seen.append(kw)
        key = {("auto", False): "A", ("team", False): "B", ("team", True): "C"}[(kw["mode"], kw["test"])]
        from llm import cost_ledger, task_tier
        cost_ledger.record({"provider": "fake", "model": "fake-1", "operation": "generate_code", "input_tokens": 100, "output_tokens": 50,
                            "estimated_cost_usd": {"A": 0.01, "B": 0.03, "C": 0.05}[key], "run": task_tier.run_tag(), "token_source": "api"})
        ops = [{"file": "server/src/index.js", "content": f"// {key}\n", "action": "create"},
               {"file": "Dockerfile", "content": "FROM node:20\n", "action": "create"}]
        return {"ops": ops, "consistency_issues": [], "spec": {"acceptance": {"status": "passed", "passed": 3, "failed": 0, "total": 3, "rounds": 2}} if key == "C" else {}}

    def verify(workspace, ops, oracle):
        key = ops[0]["content"].split()[1]
        srv = reference_app.serve(oracle.task["id"], frozenset(bugs_by_config[key].get(oracle.task["id"], ())))
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            out = oracle.execute(lambda m, p, b, t: runtime_check._request(base, m, p, b, t))
        finally:
            srv.shutdown()
        return {"status": "passed", "passed": True, "runtime": {"status": "failed" if out["failed"] else "passed",
                                                               "failures": [{"request": "x", "acceptance": True}] * out["failed"]}}
    return generate, verify, seen


def test_benchmark_measures_compares_and_reports_with_a_fake_ai(bench, tmp_path, monkeypatch):
    import reference_app
    import runtime_check
    from llm import cost_ledger
    monkeypatch.setattr(cost_ledger, "ledger_file", lambda: None)
    tasks = [t for t in bench.load_tasks() if t["id"] in ("T01-notes", "T03-likes", "T07-stock")]
    bugs = {"A": {"T01-notes": ["oldest_first", "delete_noop"], "T03-likes": ["count_every_click"], "T07-stock": ["oversell"]},
            "B": {"T03-likes": ["count_every_click"]},
            "C": {}}
    generate, verify, seen = _fake_world(bench, reference_app, runtime_check, bugs)
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8")
    args = SimpleNamespace(tasks=tasks_file, only=None, configs=["A", "B", "C"], repeat=1, live=True, budget=5.0,
                           output=tmp_path / "out", keep_work=False, models_note="가짜 모델")
    out = bench.execute(args, generate=generate, verify=verify)
    runs = out["runs"]
    assert len(runs) == 9 and not any(r["error"] for r in runs)
    #: 생성 요청 — 구성마다 모드·인원·테스트가 다르고, oracle 은 보이지 않는다
    assert {(k["mode"], k["agents"], k["test"]) for k in seen} == {("auto", 1, False), ("team", 3, False), ("team", 3, True)}
    assert all("{{rand}}" not in json.dumps(k["spec"], ensure_ascii=False) for k in seen)
    s = out["summary"]
    assert s["A"]["criteria_passed"] == 12 - 4 and s["A"]["criteria_total"] == 12, "A: 버그 4개 → 조건 4개 실패"
    assert s["B"]["criteria_passed"] == 11 and s["C"]["criteria_passed"] == 12
    assert s["C"]["pass_rate"] == 1.0 and s["C"]["tasks_fully_passed"] == 3
    assert s["A"]["cost_usd"] == pytest.approx(0.03) and s["C"]["cost_usd"] == pytest.approx(0.15)
    assert s["C"]["cost_per_passed_criterion"] == pytest.approx(0.15 / 12, abs=1e-6)
    c_run = next(r for r in runs if r["config"] == "C")
    assert c_run["self_test"]["rounds"] == 2 and c_run["calls"] == 1 and c_run["run_ok"]
    assert bench.paired(runs, "A", "C") == {"better": 3, "same": 0, "worse": 0}
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "| C 팀+테스트 | 3 | 100.0% (12/12) | 3/3 |" in report
    assert "| A 혼자 | 3 | 66.7% (8/12) | 0/3 |" in report
    assert "| C vs A | 3 | 0 | 0 |" in report and "가짜 모델" in report
    #: 같은 폴더로 다시 실행하면 이미 한 것은 건너뛴다(이어서 하기)
    seen.clear()
    bench.execute(args, generate=generate, verify=verify)
    assert seen == []
    #: 예산에 닿으면 다음 실행을 시작하지 않는다
    args2 = SimpleNamespace(**{**vars(args), "output": tmp_path / "out2", "budget": 0.02})
    seen.clear()
    out2 = bench.execute(args2, generate=generate, verify=verify)
    assert len(out2["runs"]) == 2, "0.01 + 0.03 = 0.04 ≥ 0.02 에서 멈춤"


def test_app_that_does_not_start_counts_every_criterion_as_failed(bench, tmp_path):
    task = next(t for t in bench.load_tasks() if t["id"] == "T01-notes")

    def generate(**kw):
        return {"ops": [{"file": "server/src/index.js", "content": "x"}]}

    def verify(workspace, ops, oracle):
        return {"status": "passed", "passed": True, "runtime": {"status": "failed", "failures": [{"request": "앱 시작(/)", "status": None}]}}

    r = bench.run_one(task, "A", tmp_path / "w", "T01-notes-A-1", generate=generate, verify=verify)
    assert r["run_ok"] is False and r["oracle"] == {"passed": 0, "failed": 4, "total": 4, "rate": 0.0,
                                                   "criteria": {"AC-1": "failed", "AC-2": "failed", "AC-3": "failed", "AC-4": "failed"}}
    assert r["server_errors"] == 0, "앱 시작 실패는 서버 오류(5xx) 수에 넣지 않는다"
    #: 생성이 실패해도 결과로 남는다
    r2 = bench.run_one(task, "B", tmp_path / "w2", "T01-notes-B-1", generate=lambda **kw: (_ for _ in ()).throw(RuntimeError("한도")), verify=verify)
    assert r2["error"] == "한도" and r2["files"] == 0 and r2["build"] == "skipped"


def test_dry_run_spends_nothing(bench, capsys, tmp_path):
    assert bench.main(["--output", str(tmp_path / "o")]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["runs"] == 30 and plan["configs"] == ["A", "B", "C"] and "--live" in plan["message"]
    assert not (tmp_path / "o").exists()
