"""The repair pipeline must use the current deployment rules after a develop merge."""
import asyncio
import json

import pytest

from build_readiness import analyze
from grounded_repair.knowledge import KnowledgeIndex
from grounded_repair.pipeline import RepairPipeline
from grounded_repair.store import RepairStore


@pytest.mark.parametrize("code,line", [
    ("DOCKERFILE_NPM_SELF_UPGRADE", "RUN npm install -g npm@latest"),
    ("DOCKERFILE_DEPS_DIR_MISSING", "COPY --from=deps /app/node_modules ./node_modules"),
])
def test_new_readiness_rules_use_copy_and_approval_without_ai(tmp_path, code, line):
    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"name": "demo", "scripts": {"start": "node app.js"}}))
    (root / "app.js").write_text("require('http').createServer((req,res)=>res.end('ok')).listen(3000);\n")
    (root / ".dockerignore").write_text("**/node_modules\n.git\n.env\n.env.*\n")
    original = ('FROM node:22-alpine AS deps\nWORKDIR /app\nCOPY package.json ./\nRUN npm install\n'
                'FROM node:22-alpine\nWORKDIR /app\nCOPY . .\n' + line
                + '\nEXPOSE 3000\nCMD ["node", "app.js"]\n')
    (root / "Dockerfile").write_text(original)
    assert {i.code for i in analyze(root).errors} == {code}

    class NoModel:
        async def call_repair(self, *args, **kwargs):
            pytest.fail("Known readiness failures must not call a model")

    class RuleVerifier:
        # Tests pipeline integration; real Docker verification is a separate smoke.
        identity = "readiness-integration-test"

        def __call__(self, candidate):
            assert candidate != root
            assert (candidate / "Dockerfile").read_text() != original
            return {"passed": not analyze(candidate).errors}

    index = KnowledgeIndex([])
    index.search = lambda *args: pytest.fail("Known rule must not retrieve documents")
    store = RepairStore(tmp_path / "repairs.db")
    pipeline = RepairPipeline(NoModel(), index, store, RuleVerifier())
    result = asyncio.run(pipeline.run(str(root), "Docker build failed"))
    assert result["route"] == "rules" and result["llm_calls"] == 0
    assert result["status"] == "ready_for_approval"
    assert (root / "Dockerfile").read_text() == original
    store.approve(result["id"], str(root))
    assert not analyze(root).errors
