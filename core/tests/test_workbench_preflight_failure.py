"""Regression coverage for fail-closed workbench preflight responses."""
from __future__ import annotations

import asyncio


def test_preflight_exception_reports_maximum_risk(monkeypatch):
    from api.routes import workbench
    import preflight
    import preflight.contract_loader as contract_loader

    class BrokenRunner:
        def __init__(self, *args, **kwargs):
            pass

        def run_sync(self):
            raise RuntimeError("preflight unavailable")

    saved = []
    monkeypatch.setattr(preflight, "StaticPreflightRunner", BrokenRunner)
    monkeypatch.setattr(contract_loader, "load_contract", lambda _ws: None)
    monkeypatch.setattr(contract_loader, "build_default_contract", lambda _stack: object())
    monkeypatch.setattr(workbench, "_get_db", lambda: object())
    monkeypatch.setattr(workbench, "save_preflight_run", lambda _db, run: saved.append(run))

    response = asyncio.run(
        workbench.trigger_preflight(
            workbench.PreflightRunRequest(
                project_id="project-1",
                workspace_path=".",
                source="core",
            )
        )
    )

    assert response["status"].lower() == "blocked"
    assert response["score"] == 0
    assert response["risk_score"] == 100
    assert saved and saved[0].score == 0
