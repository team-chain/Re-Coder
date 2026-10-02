"""Aggregate real runs without turning missing prices or unreviewed citations into zero."""
from __future__ import annotations

import statistics


def report(runs: list[dict]) -> dict:
    results = {}
    for condition in "ABCD":
        group = [r for r in runs if r["strategy"] == condition]
        if not group:
            continue
        # Rules are a separate stratum, not evidence for a model's solve rate.
        invalid = {"failure_not_reproduced", "verification_unavailable", "workspace_changed"}
        ai = [r for r in group if r.get("route") != "rules" and r["status"] not in invalid]
        solved = [r for r in ai if r["status"] in {"ready_for_approval", "applied"}]
        known_cost = all(r.get("cost_complete", False) for r in ai)
        total = sum(r.get("estimated_cost_usd") or 0 for r in ai) if known_cost else None
        reviewed = [r for r in ai if r.get("citation_review") in {"correct", "incorrect"}]
        results[condition] = {
            "tasks": len(ai), "rules_tasks": sum(r.get("route") == "rules" for r in group),
            "invalid_tasks": sum(r["status"] in invalid for r in group), "solved": len(solved),
            "solve_rate": len(solved) / len(ai) if ai else None,
            "first_attempt_solved": sum(r.get("llm_calls") == 1 for r in solved),
            "total_generation_calls": sum(r.get("llm_calls", 0) for r in ai),
            "mean_calls_to_solve": statistics.mean(r["llm_calls"] for r in solved) if solved else None,
            "total_estimated_cost_usd": total,
            # Include the cost of failed tasks in the numerator.
            "estimated_cost_per_solved_usd": total / len(solved) if total is not None and solved else None,
            "median_elapsed_ms": statistics.median(r["elapsed_ms"] for r in ai) if ai else None,
            "citation_accuracy": sum(r["citation_review"] == "correct" for r in reviewed) / len(reviewed) if reviewed else None,
            "citations_reviewed": len(reviewed),
            "retrieval_modes": sorted({r.get("retrieval_mode", "unknown") for r in ai}),
            "limitations": "Model generation calls exclude SDK transport retries; costs retain provider token_source.",
        }
    return results
