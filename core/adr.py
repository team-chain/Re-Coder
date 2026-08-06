"""
ADR (Architecture Decision Record) — 설계 결정 정규화 + 영속화.

회차1 FR-02-03/04 · DoD "코드 + ADR 동시 산출".

/api/code/plan 이 제시한 설계 결정을 사용자가 선택·승인하면
  (1) 그 결정을 generate 프롬프트에 주입해 결정을 따르는 코드를 생성하고,
  (2) 동시에 각 결정을 구조화 ADR(docs/adr/ADR-NNN-slug.md)로 영속화한다.

ADR 은 생성 코드와 함께 ops 로 반환되어 확장이 한 번에 기록한다.
(Core 가 워크스페이스 파일시스템을 직접 쓰지 않는 것이 이 프로젝트의 신뢰 모델)

정규화는 프롬프트와 ADR 이 **같은 데이터**를 보도록 하는 단일 창구다.
여기서 한 번만 파싱해야 "프롬프트가 말하는 결정"과 "ADR 이 기록한 결정"이
어긋나지 않는다.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import TypedDict

ADR_DIR = "docs/adr"

# 내부용 예약 결정 id 접두사.
# FR-02-05(항상 선택지·사람 승인)를 지키려면 설계 결정이 없는 요청에도
# 확인 카드를 띄워야 한다. 그 확인 카드는 "설계 결정"이 아니므로 ADR 로
# 남기지 않는다 — 이 접두사로 시작하는 결정은 정규화 단계에서 걸러진다.
RESERVED_ID_PREFIX = "__"
CONFIRM_DECISION_ID = "__confirm__"

# 프롬프트 오염·비대화 방지 상한 (결정 텍스트는 웹뷰를 거친 비신뢰 입력)
MAX_DECISIONS = 10
MAX_FIELD_CHARS = 200
MAX_LIST_ITEMS = 5
MAX_ALTERNATIVES = 4
MAX_INSTRUCTION_CHARS = 200


class Alternative(TypedDict):
    label: str
    summary: str
    cons: list[str]


class NormalizedDecision(TypedDict):
    id: str
    question: str
    chosen_key: str
    chosen_label: str
    chosen_summary: str
    pros: list[str]
    cons: list[str]
    impact: str
    alternatives: list[Alternative]


def _clean(value: object, limit: int = MAX_FIELD_CHARS) -> str:
    """비신뢰 입력을 한 줄로 눕히고 길이를 제한한다.

    개행을 남기면 프롬프트의 다른 섹션인 척하거나(프롬프트 인젝션)
    ADR 마크다운 구조를 깨뜨릴 수 있어 공백으로 접는다.
    """
    text = str(value or "")
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:limit]


def _clean_list(values: object, limit: int = MAX_LIST_ITEMS) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for v in values:
        cleaned = _clean(v)
        if cleaned:
            out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def normalize_decisions(decisions: list | None) -> list[NormalizedDecision]:
    """
    webview/plan 이 넘겨주는 다양한 형태의 결정 입력을 통일된 형태로 정리한다.

    허용 입력(유연 — plan 결과 형태와 최소 형태 모두 수용):
      - {"id","question","options":[{key,label,summary,pros,cons}],"impact","chosen_key"}
      - {"id","chosen_key"} / {"id","choice"} / {"id","chosen"}

    `chosen_key` 가 없는 항목은 **버린다**. 사용자가 고르지 않은 결정을
    "(미지정)" 으로 ADR 에 남기면 하지도 않은 결정을 기록하는 셈이 된다.
    """
    out: list[NormalizedDecision] = []
    for d in (decisions or []):
        if not isinstance(d, dict):
            continue

        chosen_key = _clean(
            d.get("chosen_key") or d.get("choice") or d.get("chosen") or ""
        )
        if not chosen_key:
            # 미선택 결정 — 프롬프트에도 ADR 에도 넣지 않는다.
            continue

        did = _clean(d.get("id")) or chosen_key
        if did == CONFIRM_DECISION_ID:
            # 확인 카드 — 승인 흐름에는 쓰이되 설계 결정이 아니므로 ADR 로 남기지 않는다.
            #
            # 여기서 접두사(`__`) 전체를 거르지 않는 이유: 그렇게 하면 모델이
            # 만들어낸 `__` 로 시작하는 **진짜 결정**까지 조용히 사라져,
            # 사용자가 고른 선택이 프롬프트에도 ADR 에도 안 실린 채 코드가 생성된다.
            # 예약 네임스페이스 침범은 code_agent.generate_plan 이 파싱 시점에
            # 일반 id 로 옮겨 붙여 막는다(RESERVED_ID_PREFIX). 이 필터는 실제
            # 확인 카드 하나만 정확히 집어낸다.
            continue

        raw_options = d.get("options") if isinstance(d.get("options"), list) else []

        chosen_opt: dict = {}
        alternatives: list[Alternative] = []
        for opt in raw_options:
            if not isinstance(opt, dict):
                continue
            key = _clean(opt.get("key"))
            if not key:
                continue
            if key == chosen_key:
                chosen_opt = opt
            elif len(alternatives) < MAX_ALTERNATIVES:
                alternatives.append({
                    "label": _clean(opt.get("label")) or key,
                    "summary": _clean(opt.get("summary")),
                    "cons": _clean_list(opt.get("cons")),
                })

        out.append({
            "id": did,
            "question": _clean(d.get("question")),
            "chosen_key": chosen_key,
            "chosen_label": _clean(chosen_opt.get("label")) or chosen_key,
            "chosen_summary": _clean(chosen_opt.get("summary")),
            "pros": _clean_list(chosen_opt.get("pros")),
            "cons": _clean_list(chosen_opt.get("cons")),
            "impact": _clean(d.get("impact")),
            "alternatives": alternatives,
        })

        if len(out) >= MAX_DECISIONS:
            break

    return out


def slugify(text: str, fallback: str = "decision") -> str:
    """ADR 파일명용 슬러그 — 영숫자/한글만 남기고 하이픈으로.

    경로 구분자와 점이 제거되므로 경로 탈출(../)이 성립하지 않는다.
    """
    text = (text or "").strip().lower()
    text = re.sub(r"[^\w가-힣\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    slug = (text or fallback)[:40].strip("-") or fallback
    # Windows 예약어 방지 (ADR-001-con.md 같은 파일명은 생성 불가)
    if slug.split("-")[0] in {
        "con", "prn", "aux", "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }:
        slug = f"d-{slug}"
    return slug


def adr_output_dir(root: Path, target_folder: str = "") -> Path:
    """ADR 이 실제로 기록될 디렉터리.

    확장은 모든 op 경로 앞에 `target_folder` 를 붙여 워크스페이스에 쓴다.
    번호 스캔도 **같은 위치**를 봐야 ADR-001 이 매번 다시 생성되어
    기존 기록을 덮어쓰는 사고를 막을 수 있다.
    """
    base = root
    tf = (target_folder or "").strip().strip("/\\")
    if tf:
        base = base / tf
    return base / ADR_DIR


def next_adr_index(root: Path, target_folder: str = "") -> int:
    """기존 ADR-NNN 파일을 스캔해 다음 번호를 돌려준다."""
    adr_dir = adr_output_dir(root, target_folder)
    max_n = 0
    if adr_dir.is_dir():
        for p in adr_dir.glob("ADR-*.md"):
            m = re.match(r"ADR-(\d+)", p.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def build_adr_markdown(index: int, decision: NormalizedDecision, instruction: str) -> str:
    """정규화된 결정 하나 → 구조화 ADR 마크다운(결정·근거·대안·영향)."""
    today = datetime.now().strftime("%Y-%m-%d")
    title = decision.get("question") or decision.get("id") or "설계 결정"
    chosen = decision.get("chosen_label") or decision.get("chosen_key")
    summary = decision.get("chosen_summary")
    pros = decision.get("pros") or []
    cons = decision.get("cons") or []
    impact = decision.get("impact")
    alts = decision.get("alternatives") or []
    req = _clean(instruction, MAX_INSTRUCTION_CHARS)

    lines = [
        f"# ADR-{index:03d}: {title}",
        "",
        "- 상태: 승인됨",
        f"- 날짜: {today}",
        f"- 요청: {req}" if req else "- 요청: (미기재)",
        "",
        "## 결정",
        f"**{chosen}**" + (f" — {summary}" if summary else ""),
    ]
    if pros:
        lines += ["", "근거(장점):"] + [f"- {p}" for p in pros]

    lines += ["", "## 검토한 대안"]
    if alts:
        for a in alts:
            reason = f" → {', '.join(a['cons'])} 이유로 제외" if a.get("cons") else " 제외"
            summ = f" ({a['summary']})" if a.get("summary") else ""
            lines.append(f"- {a['label']}{summ}{reason}")
    else:
        lines.append("- (기록된 대안 없음)")

    lines += ["", "## 영향"]
    impact_bits = []
    if impact:
        impact_bits.append(impact)
    if cons:
        impact_bits.append("감수: " + ", ".join(cons))
    lines.append(" · ".join(impact_bits) if impact_bits else "(영향 미기재)")
    lines.append("")
    return "\n".join(lines)


def build_adr_ops(
    decisions: list[NormalizedDecision],
    instruction: str,
    root: Path,
    target_folder: str = "",
) -> list[dict]:
    """정규화된 결정 목록 → docs/adr/ADR-NNN-slug.md 파일 ops 목록.

    경로는 워크스페이스 상대(`docs/adr/...`)로 둔다. 확장이 `target_folder`
    를 붙여 기록하므로, 번호 스캔에도 동일한 `target_folder` 를 넘긴다.
    """
    ops: list[dict] = []
    n = next_adr_index(root, target_folder)
    for d in decisions:
        slug = slugify(d.get("id") or d.get("question") or "")
        ops.append({
            "action": "create",
            "file": f"{ADR_DIR}/ADR-{n:03d}-{slug}.md",
            "language": "markdown",
            "content": build_adr_markdown(n, d, instruction),
            "rationale": "설계 결정 근거 기록 (ADR)",
            "is_adr": True,
        })
        n += 1
    return ops
