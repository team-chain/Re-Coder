#!/usr/bin/env python3
"""고아 코드 감사 — docs/orphan-code-audit.md 생성.

보드 이슈 「고아 코드 5~6천 줄 — 지울지 연결할지 결정 필요」의 결정 자료.

무엇을 하나
    1. core 를 AST 로 훑어 엔트리포인트(main + api/*)에서 import 로 닿지
       않는 모듈을 찾는다 (동적 import 는 못 본다 — 한계에 명시).
    2. 각 고아를 tests / scripts / gateway / extension 참조와 교차검사해
       삭제 1순위 / 테스트만 사용 / 배선 후보로 나눈다.
    3. 고아끼리의 의존 클러스터를 묶는다 — 지울 거면 클러스터째 지워야 한다.

사용
    python3 scripts/orphan_audit.py          # 문서 재생성
"""
from __future__ import annotations

import ast
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
OUT = ROOT / "docs" / "orphan-code-audit.md"

#: 빈 패키지 __init__ 는 자식 판정에 묶는다.
_SKIP = {"__init__", "api", "collectors", "registries", "registry.file_templates",
         "eval", "cv", "forecast", "standup", "replay", "visual_diff", "chunker",
         "incident_memory", "preflight.checks", "eval.v10"}


def _module_map() -> dict[str, Path]:
    mods: dict[str, Path] = {}
    for p in CORE.rglob("*.py"):
        if "__pycache__" in p.parts or (len(p.parts) > len(CORE.parts) and p.parts[len(CORE.parts)] == "tests"):
            continue
        rel = p.relative_to(CORE).with_suffix("")
        name = ".".join(rel.parts)
        if name.endswith(".__init__"):
            name = name[:-9]
        mods[name] = p
    return mods


def _imports_of(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            for a in node.names:
                out.add(f"{node.module}.{a.name}")
    return out


def _resolve(imp: str, mods: dict[str, Path]) -> str | None:
    cand = imp[5:] if imp.startswith("core.") else imp
    while cand and cand not in mods and "." in cand:
        cand = cand.rsplit(".", 1)[0]
    return cand if cand in mods else None


def _reachable(mods: dict[str, Path]) -> set[str]:
    entries = [m for m in mods if m == "main" or m.startswith("api.")]
    seen = set(entries)
    stack = list(entries)
    while stack:
        m = stack.pop()
        for imp in _imports_of(mods[m]):
            cand = _resolve(imp, mods)
            if cand and cand not in seen:
                seen.add(cand)
                stack.append(cand)
    return seen


def _external_import_refs(mods: dict[str, Path], orphans: set[str]) -> dict[str, list[str]]:
    """tests / scripts / gateway 의 **실제 import 문** 기준 참조 (AST — 오탐 없음)."""
    refs: dict[str, list[str]] = defaultdict(list)
    roots = [CORE / "tests", ROOT / "scripts", ROOT / "gateway"]
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            for imp in _imports_of(p):
                cand = _resolve(imp, mods)
                if cand in orphans:
                    rel = p.relative_to(ROOT).as_posix()
                    if rel not in refs[cand]:
                        refs[cand].append(rel)
    return refs


def _name_refs_in_extension(name: str) -> list[str]:
    tail = name.split(".")[-1]
    try:
        out = subprocess.run(
            ["grep", "-rl", tail, "extension/src", "extension/webview-src"],
            capture_output=True, text=True, cwd=ROOT,
        )
        return [f for f in out.stdout.splitlines()][:4]
    except Exception:
        return []


def _first_doc_line(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        doc = ast.get_docstring(tree) or ""
        return doc.strip().splitlines()[0][:90] if doc.strip() else "(docstring 없음)"
    except Exception:
        return "(파싱 실패)"


def _clusters(orphans: set[str], mods: dict[str, Path]) -> list[set[str]]:
    """고아끼리의 의존을 무향으로 묶은 연결 성분 — 삭제 단위."""
    adj: dict[str, set[str]] = defaultdict(set)
    for m in orphans:
        for imp in _imports_of(mods[m]):
            cand = _resolve(imp, mods)
            if cand in orphans and cand != m:
                adj[m].add(cand)
                adj[cand].add(m)
    seen: set[str] = set()
    comps: list[set[str]] = []
    for m in sorted(orphans):
        if m in seen:
            continue
        comp = {m}
        stack = [m]
        while stack:
            cur = stack.pop()
            for nxt in adj[cur]:
                if nxt not in comp:
                    comp.add(nxt)
                    stack.append(nxt)
        seen |= comp
        comps.append(comp)
    return sorted(comps, key=lambda c: -sum(1 for _ in c))


def main() -> None:
    mods = _module_map()
    seen = _reachable(mods)
    orphans = {m for m in mods if m not in seen and m not in _SKIP}
    ext_refs = _external_import_refs(mods, orphans)

    lines_of = {m: sum(1 for _ in open(mods[m], encoding="utf-8")) for m in orphans}
    total = sum(lines_of.values())

    md: list[str] = []
    md.append("# 고아 코드 감사 (자동 생성)\n")
    md.append(f"생성: `python3 scripts/orphan_audit.py` · 기준 커밋 시점의 core {len(mods)}개 모듈 중 "
              f"엔트리포인트(main + api/*)에서 **닿지 않는 모듈 {len(orphans)}개, {total:,}줄**.\n")
    md.append("한계: 정적 import 만 본다 — 문자열 기반 동적 import, 외부에서 직접 실행하는 "
              "스크립트성 모듈은 아래 참조 열로만 잡힌다. 삭제 전 반드시 전체 테스트를 돌릴 것.\n")

    md.append("\n## 판정 요약\n")
    md.append("| 분류 | 기준 | 처리 |\n|---|---|---|\n")
    md.append("| T1 삭제 후보 | 어디서도 import 되지 않음 (클러스터째 죽어 있음) | 팀 확인 후 클러스터 단위 삭제 |\n")
    md.append("| T2 테스트만 | core/tests 에서만 import | 기능을 살릴 게 아니면 테스트와 함께 삭제 |\n")
    md.append("| T3 배선 후보 | 설계서 기능으로 보이는 에이전트/저장소 | 지울지 연결할지 **팀 결정** — 결정 전 삭제 금지 |\n")

    #: T3 후보 — 이름이 설계서 기능(에이전트·인시던트·복구)을 가리키는 것들.
    t3_hint = {"rca_agent", "postmortem_agent", "gitops_agent", "rollback_pr_agent",
               "rollback_policy", "sbom_agent", "incident_correlator", "incident_timeline",
               "local_deploy_agent", "mcp_server", "agents.code_agent", "risk_validator",
               "plan_execute_verify", "planner", "executor", "verifier", "quality_runner"}

    def tier(m: str) -> str:
        if m in t3_hint:
            return "T3"
        if ext_refs.get(m) and all(r.startswith("core/tests") for r in ext_refs[m]):
            return "T2"
        if ext_refs.get(m):
            return "T2*"  # scripts/gateway 참조 — 외부 진입점일 수 있음
        return "T1"

    md.append("\n## 클러스터 (삭제 단위)\n")
    md.append("고아끼리 서로 import 하는 묶음이다. 지우려면 묶음째 지워야 한다.\n")
    for i, comp in enumerate(_clusters(orphans, mods), 1):
        comp_lines = sum(lines_of[m] for m in comp)
        tiers = {tier(m) for m in comp}
        verdict = "T3 포함 — 팀 결정 필요" if "T3" in tiers else (
            "테스트 정리 동반" if "T2" in tiers or "T2*" in tiers else "T1 — 삭제 후보")
        md.append(f"\n### 클러스터 {i} — {comp_lines:,}줄 · {verdict}\n")
        md.append("| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |\n|---|---|---|---|---|\n")
        for m in sorted(comp):
            refs = ext_refs.get(m, [])
            ext = _name_refs_in_extension(m) if m in {"mcp_server", "switch_aws"} else []
            ref_txt = "<br>".join(refs + [f"(이름 언급) {e}" for e in ext]) or "—"
            md.append(f"| `{m}` | {lines_of[m]} | {tier(m)} | {ref_txt} | {_first_doc_line(mods[m])} |\n")

    md.append("\n## 다음 단계 (제안)\n")
    md.append("1. T1 클러스터: 이 문서 리뷰에서 이견 없으면 클러스터 단위 삭제 PR — 삭제 후 전체 테스트로 검증.\n")
    md.append("2. T2: 대응 테스트 파일과 함께 지우거나, 살릴 기능이면 T3 로 옮겨 결정.\n")
    md.append("3. T3: 카드별로 「연결(회차 배정)」 또는 「삭제(설계서에 미구현 명시)」 결정 — 보드 이슈 카드의 DoD.\n")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(md), encoding="utf-8")
    print(f"쓴 파일: {OUT} (고아 {len(orphans)}개, {total:,}줄)")


if __name__ == "__main__":
    sys.exit(main())
