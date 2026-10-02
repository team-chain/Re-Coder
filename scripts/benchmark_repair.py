"""Run A/B/C/D on prepared fixtures. --live explicitly enables paid model calls."""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from grounded_repair.benchmark import report


async def execute(args):
    from grounded_repair.knowledge import KnowledgeIndex
    from grounded_repair.pipeline import RepairPipeline
    from grounded_repair.store import RepairStore
    from llm.provider_router import get_provider_router

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    prepared = [c for c in cases if c.get("status") == "ready"]
    if not prepared:
        raise SystemExit("No runnable fixtures. Materialize and verify cases before measuring.")
    if not args.live:
        print(json.dumps({"ready": len(prepared), "planned": len(cases)-len(prepared),
                          "generation_call_budget": len(prepared) * 8,
                          "message": "Use --live to run paid models and Docker builds."}, indent=2))
        return
    args.output.mkdir(parents=True, exist_ok=True)
    pipeline = RepairPipeline(get_provider_router(), KnowledgeIndex.load(), RepairStore(args.output / "runs.db"))
    runs = []
    rng = random.Random(args.seed)
    for case in prepared:
        if case["stage"] != "build":
            raise SystemExit("Cloud/runtime fixtures require environment verifiers before evaluation: " + case["id"])
        conditions = list("ABCD")
        rng.shuffle(conditions)
        for condition in conditions:
            workspace = (args.cases.parent / case["workspace"]).resolve()
            log = (args.cases.parent / case["log_file"]).read_text(encoding="utf-8")
            result = await pipeline.run(str(workspace), log, case["stage"], condition, use_cache=False)
            result.update(case_id=case["id"], category=case["category"], order_seed=args.seed)
            runs.append(result)
            # Save after each task so interrupted experiments can be inspected.
            (args.output / "runs.json").write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")
            (args.output / "report.json").write_text(json.dumps(report(runs), indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("work/repair-benchmark"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--live", action="store_true")
    asyncio.run(execute(parser.parse_args()))
