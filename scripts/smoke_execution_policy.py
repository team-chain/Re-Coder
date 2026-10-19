"""Real OPA REST + Docker repair, revocation and outage checks. No paid model calls."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from execution_policy import ExecutionDeniedError, ExecutionPolicy  # noqa: E402
from grounded_repair.knowledge import KnowledgeIndex  # noqa: E402
from grounded_repair.pipeline import RepairPipeline  # noqa: E402
from grounded_repair.store import RepairStore  # noqa: E402
from grounded_repair.workspace import DockerVerifier  # noqa: E402
from opa_gate import OPAGate  # noqa: E402
from smoke_grounded_repair import FixtureRouter  # noqa: E402


class CountingVerifier(DockerVerifier):
    def __init__(self):
        self.calls = 0

    def __call__(self, root):
        self.calls += 1
        return super().__call__(root)


def request(url, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        method="PUT" if data is not None else "GET",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        req, timeout=2
    ) as response:
        return response.status


async def exercise(root, url):
    project = root / "project"
    project.mkdir()
    original = (
        "FROM scratch\nCOPY payload.txt /payload.txt\nCOPY payload.txt /payload.txt/child.txt\n"
    )
    fixed = "FROM scratch\nCOPY payload.txt /payload.txt\n"
    (project / "Dockerfile").write_text(original, encoding="utf-8")
    (project / "payload.txt").write_text("hello\n", encoding="utf-8")
    (project / ".dockerignore").write_text(".git\n", encoding="utf-8")
    policy = ExecutionPolicy("opa", OPAGate(opa_url=url))
    store = RepairStore(root / "runs.db", policy)
    verifier = CountingVerifier()
    pipeline = RepairPipeline(
        FixtureRouter(), KnowledgeIndex.load(model_path="", reranker_path=""), store, verifier
    )
    log = "COPY payload.txt /payload.txt/child.txt: not a directory"
    prepared = await pipeline.run(str(project), log, use_cache=False)
    assert prepared["status"] == "ready_for_approval", prepared
    assert prepared["baseline"]["passed"] is False and prepared["verification"]["passed"] is True
    assert all(check["allowed"] for check in prepared["policy_checks"])
    assert verifier.calls == 2

    request(url + "/v1/data/recoder_execution", {"blocked_actions": ["repair.apply"]})
    try:
        store.approve(prepared["id"], str(project))
    except ExecutionDeniedError:
        pass
    else:
        raise AssertionError("Revoked approval must fail")
    assert (project / "Dockerfile").read_text(encoding="utf-8") == original

    request(url + "/v1/data/recoder_execution", {"blocked_actions": ["repair.verify"]})
    denied = await pipeline.run(str(project), log, use_cache=False)
    assert denied["status"] == "policy_denied" and denied["llm_calls"] == 0
    assert verifier.calls == 2, "OPA denial must precede Docker execution"

    request(url + "/v1/data/recoder_execution", {"blocked_actions": []})
    assert store.approve(prepared["id"], str(project))["status"] == "applied"
    assert (project / "Dockerfile").read_text(encoding="utf-8") == fixed
    return policy, project


def main():
    opa = shutil.which("opa")
    if not opa:
        raise SystemExit("opa is required; this smoke cannot be skipped")
    # Test process only: do not send an operator's token to our disposable local server.
    os.environ["OPA_TOKEN"] = ""
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="recoder-opa-smoke-") as tmp:
        root = Path(tmp)
        with (root / "opa.log").open("wb") as output:
            server = subprocess.Popen(
                [
                    opa,
                    "run",
                    "--server",
                    "--addr",
                    f"127.0.0.1:{port}",
                    "--skip-version-check",
                    "--log-level",
                    "error",
                    str(ROOT / "policies/recoder/execution.rego"),
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        if request(url + "/health") == 200:
                            break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.05)
                else:
                    raise RuntimeError(
                        "OPA failed to start: "
                        + (root / "opa.log").read_text(encoding="utf-8")[:2000]
                    )
                policy, project = asyncio.run(exercise(root, url))
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
        try:
            policy.authorize("repair.generate", project, snapshot=True)
        except ExecutionDeniedError:
            pass
        else:
            raise AssertionError("Stopped OPA must fail closed")
    print(  # noqa: T201 - CLI verification report
        json.dumps(
            {
                "status": "passed",
                "real_opa_and_docker": True,
                "revoked_apply_preserved_original": True,
                "denied_verification_did_not_run_docker": True,
                "new_allow_applied_verified_contents": True,
                "opa_outage_failed_closed": True,
                "paid_model_calls": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
