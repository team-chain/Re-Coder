"""Real Docker rebuild smoke; deterministic model fixture, no paid API calls."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from grounded_repair.knowledge import KnowledgeIndex
from grounded_repair.pipeline import RepairPipeline
from grounded_repair.store import RepairStore
from grounded_repair.workspace import DockerVerifier
from llm.base import LLMResponse


class FixtureRouter:
    async def call_repair(self, request, *, tier, run_id):
        supplied = json.loads(request.prompt)["documents"]
        assert any(p["id"] == "docker-copy" for p in supplied)
        data = {"explanation": "COPY must use an existing build-context source", "guidance": [],
                "evidence_ids": ["docker-copy"], "edits": [{"path": "Dockerfile",
                "content": "FROM scratch\nCOPY payload.txt /payload.txt\n", "evidence_ids": ["docker-copy"]}]}
        return LLMResponse(text=json.dumps(data), parsed=data, metadata={"llm_call_record": {
            "model": "fixture-not-a-real-model", "estimated_cost_usd": 0, "token_source": "fixture"}})


async def main():
    with tempfile.TemporaryDirectory(prefix="recoder-smoke-") as tmp:
        root = Path(tmp) / "project"
        root.mkdir()
        original = "FROM scratch\nCOPY paylod.txt /payload.txt\n"
        (root / "Dockerfile").write_text(original)
        (root / "payload.txt").write_text("hello\n")
        (root / ".dockerignore").write_text(".git\n")
        store = RepairStore(Path(tmp) / "runs.db")
        pipeline = RepairPipeline(FixtureRouter(), KnowledgeIndex.load(), store)
        result = await pipeline.run(str(root), 'COPY paylod.txt: not found; failed to compute cache key')
        assert result["status"] == "ready_for_approval", result
        assert result["baseline"]["passed"] is False
        assert result["verification"]["passed"] is True
        assert (root / "Dockerfile").read_text() == original
        store.approve(result["id"])
        assert DockerVerifier()(root)["passed"]
        print(json.dumps({"status": "passed", "baseline_failed": True,
                          "rebuild_passed": True, "original_unchanged_until_approval": True,
                          "model": "fixture", "paid_model_calls": 0}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
