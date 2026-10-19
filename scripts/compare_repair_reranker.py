"""Compare local retrieval quality/latency on fixed development queries; no LLM calls."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from grounded_repair.knowledge import KnowledgeIndex  # noqa: E402 - standalone script
from grounded_repair.reranker import LocalReranker  # noqa: E402


def matches(passage_id: str, prefixes: list[str]) -> bool:
    return any(passage_id == prefix or passage_id.startswith(prefix + ":") for prefix in prefixes)


def metrics(rows: list[dict], variant: str) -> dict:
    positive = [row for row in rows if row["expected"]]
    times = [sample for row in rows for sample in row[variant]["latency_ms"]]
    ranks = [
        next(
            (i for i, name in enumerate(row[variant]["ids"], 1) if matches(name, row["expected"])),
            0,
        )
        for row in positive
    ]
    return {
        "positive_queries": len(positive),
        "hit_at_1": sum(rank == 1 for rank in ranks) / len(ranks),
        "hit_at_4": sum(rank > 0 for rank in ranks) / len(ranks),
        "mrr_at_4": sum(1 / rank if rank else 0 for rank in ranks) / len(ranks),
        "median_ms": statistics.median(times),
        "p95_ms": sorted(times)[max(0, (95 * len(times) + 99) // 100 - 1)],
        "forbidden_hits": sum(
            matches(name, row.get("forbidden", [])) for row in rows for name in row[variant]["ids"]
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=ROOT / "benchmarks/repair/corpus.json")
    parser.add_argument(
        "--cases", type=Path, default=ROOT / "benchmarks/repair/reranker-cases.json"
    )
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--reranker-model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    if not cases or not any(case.get("expected") for case in cases) or not 1 <= args.repeats <= 5:
        parser.error("Need positive cases and repeats between 1 and 5")
    started = time.perf_counter()
    base = KnowledgeIndex.load(
        args.corpus, args.embedding_model, reranker_path="", include_summaries=True
    )
    ranked = KnowledgeIndex(base.passages, base.embed, LocalReranker(args.reranker_model))
    setup_seconds = time.perf_counter() - started
    indexes = {"baseline": base, "reranked": ranked}
    # Exclude one explicit warm-up from timings; alternate order on measured cases.
    for index in indexes.values():
        index.search(
            cases[0]["query"],
            cases[0].get("keywords", []),
            4,
            project_versions=cases[0].get("versions", {}),
        )
    rows = []
    for number, case in enumerate(cases):
        row = {**case}
        order = list(indexes) if number % 2 == 0 else list(reversed(indexes))
        for name in order:
            elapsed = []
            for _ in range(args.repeats):
                start = time.perf_counter()
                found = indexes[name].search(
                    case["query"],
                    case.get("keywords", []),
                    4,
                    project_versions=case.get("versions", {}),
                )
                elapsed.append(round((time.perf_counter() - start) * 1000, 3))
            row[name] = {"ids": [p.id for p in found], "latency_ms": elapsed}
        rows.append(row)
        print(f"{number + 1}/{len(cases)} {case['id']}", flush=True)  # noqa: T201
    report = {
        "limitation": "Document-topic labels on development queries, including four existing smoke queries; not held-out, passage entailment, or end-to-end repair accuracy. No paid LLM calls.",
        "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "environment": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "device": os.getenv("RECODER_REPAIR_RERANKER_DEVICE", "cpu"),
            "omp_num_threads": os.getenv("OMP_NUM_THREADS", "default"),
            "libraries": {
                name: importlib.metadata.version(name)
                for name in ("sentence-transformers", "torch", "transformers", "huggingface-hub")
            },
        },
        "mode": base.mode,
        "reranker_profile": ranked.retrieval_profile,
        "setup_seconds": round(setup_seconds, 3),
        "repeats": args.repeats,
        "metrics": {name: metrics(rows, name) for name in indexes},
        "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["metrics"], indent=2))  # noqa: T201
    if any(report["metrics"][name]["forbidden_hits"] for name in indexes):
        raise SystemExit("Version-incompatible evidence was retrieved")


if __name__ == "__main__":
    main()
