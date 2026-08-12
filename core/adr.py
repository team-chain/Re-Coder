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

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

ADR_DIR = "docs/adr"

#: ADR 번호 예약 장부의 경로를 덮는 환경변수. `RECODER_ECS_STORE` 와 같은
#: 규약이며, 테스트는 `tests/conftest.py` 에서 임시 경로로 덮는다.
ENV_ADR_STORE = "RECODER_ADR_STORE"

#: 장부 읽기-수정-쓰기를 감싸는 잠금.
#:
#: Core 는 `singleton.py` 가 프로세스를 하나로 강제하므로 **프로세스 안의
#: 동시성만** 막으면 된다. 여러 Core 가 같은 워크스페이스를 동시에 보는
#: 상황까지는 막지 못한다 — 그때도 장부가 깨지지는 않지만(원자적 교체)
#: 번호는 겹칠 수 있다.
_reservation_lock = threading.Lock()

#: 프로세스 안에서만 유지하는 2차 방어선. **디스크 장부가 실패해도**
#: 이 Core 가 살아 있는 동안은 같은 번호를 두 번 주지 않는다.
#:
#: 필요한 이유: Windows 에서 `os.replace` 는 대상 파일이 열려 있으면
#: `PermissionError` 를 낸다(백신 실시간 검사·OneDrive 동기화·편집기).
#: 그러면 장부가 한 번도 갱신되지 않아 발급이 계속 1, 1, 1 로 나오고,
#: 이 장치가 막으려던 덮어쓰기 사고가 사용자 모르게 그대로 재발한다.
#: 데모용 워크스페이스가 OneDrive 폴더 아래에 있으면 실제로 밟는 경로다.
_memory_reservations: dict[str, int] = {}

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
    """비신뢰 입력을 한 줄로 눕히고 길이를 제한한다. **멱등이어야 한다.**

    개행을 남기면 프롬프트의 다른 섹션인 척하거나(프롬프트 인젝션)
    ADR 마크다운 구조를 깨뜨릴 수 있어 공백으로 접는다.

    자른 **뒤에** 다시 strip 하는 순서가 중요하다. 먼저 strip 하고 자르면
    상한 경계가 공백에 걸릴 때 결과 끝에 공백이 남고, 한 번 더 적용했을 때
    그 공백이 사라져 **값이 달라진다**(`f(f(x)) != f(x)`).
    이 함수는 발급(generate_plan)·검증(승인 게이트)·기록(normalize_decisions)
    세 곳에서 각각 호출되므로, 멱등이 아니면 "발급 때 서로 다른 두 값이
    검증 때 같아지는" 어긋남이 생긴다 — 정상 승인이 거절되는 원인이었다.
    """
    text = str(value or "")
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()[:limit].strip()


def canonical_key(value: object) -> str:
    """결정 id·선택지 key 의 **정규형**. 비교와 기록이 반드시 이걸 통해야 한다.

    승인 게이트(code_agent)와 기록(normalize_decisions)이 서로 다른 방식으로
    키를 비교하면, 게이트에서는 서로 다른 두 키가 기록 단계에서는 같아진다.
    그러면 사용자가 고른 것과 **다른 선택지**의 라벨·근거가 ADR 에 남는다.
    (예: 앞 200자가 같은 두 키, 내부 공백만 다른 두 키)

    그래서 정규형을 한 곳에서만 정의하고 양쪽이 같은 함수를 쓴다.
    """
    return _clean(value)


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
        offered = False
        for opt in raw_options:
            if not isinstance(opt, dict):
                continue
            key = _clean(opt.get("key"))
            if not key:
                continue
            offered = True
            if key == chosen_key:
                chosen_opt = opt
            elif len(alternatives) < MAX_ALTERNATIVES:
                alternatives.append({
                    "label": _clean(opt.get("label")) or key,
                    "summary": _clean(opt.get("summary")),
                    "cons": _clean_list(opt.get("cons")),
                })

        if offered and not chosen_opt:
            # 선택지를 제시해 놓고 그중 어느 것도 아닌 값이 왔다.
            # 이걸 살려두면 아래 `or chosen_key` 폴백이 알 수 없는 키를 그대로
            # 선택 라벨로 삼아, 아무도 승인하지 않은 결정이 프롬프트와 ADR 에
            # 실린다. 승인 게이트(code_agent._approval_state)가 이미 막지만,
            # 기록을 만드는 이 지점에서도 한 번 더 끊는다.
            continue
        # 선택지 자체가 없는 최소 형태({"id","chosen_key"})는 종전대로 허용한다
        # — 대조할 목록이 없으므로 chosen_key 를 라벨로 쓰는 것이 유일한 해석이다.

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


def _scanned_max_index(root: Path, target_folder: str = "") -> int:
    """디스크에 **이미 기록된** ADR 중 가장 큰 번호. 없으면 0."""
    adr_dir = adr_output_dir(root, target_folder)
    max_n = 0
    if adr_dir.is_dir():
        for p in adr_dir.glob("ADR-*.md"):
            m = re.match(r"ADR-(\d+)", p.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
    return max_n


# ── 번호 예약 장부 ────────────────────────────────────────────────────
#
# **왜 장부가 필요한가.**
#
# 이 프로젝트의 신뢰 모델상 Core 는 워크스페이스에 직접 쓰지 않는다. 결정을
# ADR ops 로 만들어 돌려주면 **사용자가 승인한 뒤** 확장이 파일을 쓴다(D6).
# 그래서 "번호를 정하는 시점"과 "파일이 생기는 시점" 사이에 사람의 승인이
# 끼어들고, 그 사이에 다음 요청이 들어올 수 있다.
#
#   1. 요청 A 생성 → 디스크에 ADR 0개 → ADR-001 로 만들어 제안 (파일 없음)
#   2. 요청 B 생성 → 디스크는 **여전히** 0개 → 또 ADR-001 로 제안
#   3. A 승인 → ADR-001 기록
#   4. B 승인 → ADR-001 **덮어씀** → A 의 결정 기록이 사라진다
#
# 디스크만 스캔하면 이 경로를 절대 막을 수 없다. 아직 존재하지 않는 파일이
# 근거이기 때문이다. 그래서 **발급한 번호를 따로 적어 둔다.**
#
# 번호에 구멍이 생기는 것은 허용한다 — 사용자가 제안을 버리면 그 번호는
# 비게 된다. 카드의 완료 기준은 "번호가 연속"이 아니라 **"ADR 유실 0"** 이고,
# 둘 중 하나를 골라야 한다면 구멍이 덮어쓰기보다 압도적으로 낫다.


def _reservation_store() -> Path:
    """장부 파일 경로. 호출할 때마다 환경변수를 다시 읽는다.

    임포트 시점에 고정하면 테스트가 `monkeypatch.setenv` 로 덮을 수 없다
    (`sbom.py` 가 그 형태라 같은 실수를 반복하지 않는다).
    """
    raw = (os.environ.get(ENV_ADR_STORE) or "").strip()
    if raw:
        return Path(raw)
    home = (os.environ.get("RECODER_HOME") or "").strip()
    base = Path(home) if home else (Path.home() / ".recoder")
    return base / "adr_reservations.json"


def _store_key(adr_dir: Path) -> str:
    """장부의 키. 워크스페이스마다 번호가 독립이어야 한다.

    `normcase` 를 거치는 이유는 Windows 다 — 같은 폴더가 `C:\\proj` 와
    `c:\\proj` 로 들어오면 키가 갈라져 번호가 겹친다.
    """
    try:
        resolved = str(adr_dir.resolve())
    except Exception:  # noqa: BLE001 — 존재하지 않는 경로 등
        resolved = str(adr_dir)
    return os.path.normcase(resolved)


def _read_reservations() -> dict:
    """장부 전체. 읽지 못하면 **빈 장부로 간주하고 계속 간다.**

    장부가 깨졌다고 ADR 생성 자체를 실패시키지는 않는다. 그 경우 동작은
    장부가 없던 예전과 같아질 뿐이고, 그 사실은 로그로 남긴다.
    """
    path = _reservation_store()
    try:
        with path.open(encoding="utf-8") as fp:
            data = json.load(fp)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("ADR 예약 장부를 읽지 못했습니다(%s): %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write_reservations(data: dict) -> bool:
    """장부 저장. 임시 파일에 쓴 뒤 원자적으로 교체한다.

    바로 덮어쓰면 쓰는 도중 죽었을 때 **잘린 JSON** 이 남고, 그다음부터
    장부를 못 읽어 예약이 통째로 사라진다.
    """
    path = _reservation_store()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ADR 예약 장부를 저장하지 못했습니다(%s): %s", path, exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def reserved_adr_index(root: Path, target_folder: str = "") -> int:
    """이 워크스페이스에서 **이미 발급한** 가장 큰 번호. 없으면 0.

    디스크 장부와 프로세스 안 기억 중 **큰 쪽**을 쓴다. 장부 저장이 막혀도
    이 Core 가 살아 있는 동안은 번호가 겹치지 않게 하기 위해서다.
    """
    key = _store_key(adr_output_dir(root, target_folder))
    raw = _read_reservations().get(key)
    try:
        on_disk = max(0, int(raw))
    except (TypeError, ValueError):
        on_disk = 0
    return max(on_disk, _memory_reservations.get(key, 0))


def next_adr_index(root: Path, target_folder: str = "") -> int:
    """다음에 쓸 ADR 번호.

    디스크에 기록된 파일과 **아직 승인되지 않은 발급분**을 모두 넘어선
    번호를 돌려준다. 예약하지는 않는다 — 조회 전용이다.
    """
    return max(
        _scanned_max_index(root, target_folder),
        reserved_adr_index(root, target_folder),
    ) + 1


def allocate_adr_indexes(root: Path, target_folder: str = "", count: int = 1) -> int:
    """`count` 개의 연속된 번호를 예약하고 **시작 번호**를 돌려준다.

    장부에 쓰지 못해도 번호는 정상적으로 돌려준다. 그 경우 동작이 장부가
    없던 예전으로 되돌아갈 뿐이며, 발급을 실패시켜 ADR 을 아예 못 만들게
    하는 것보다 낫다.
    """
    if count <= 0:
        return next_adr_index(root, target_folder)

    # 스캔은 감싸지 않는다. 디스크에 있는 파일을 못 읽었는데 1 부터 다시
    # 주면 **기존 기록을 덮어쓴다** — 장부가 막으려던 바로 그 사고다.
    # 여기서 터지면 터지는 게 맞다.
    scanned = _scanned_max_index(root, target_folder)

    # 장부는 개선 장치다. **새로 들인 의존이 새 실패 모드를 만들면 안 된다.**
    # 장부 계층에서 무슨 일이 나든 번호는 정상적으로 발급하고, 그 경우
    # 동작이 장부가 없던 예전으로 되돌아갈 뿐이게 한다.
    key = _store_key(adr_output_dir(root, target_folder))
    with _reservation_lock:
        try:
            data = _read_reservations()
            reserved = max(0, int(data.get(key, 0) or 0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ADR 예약 장부를 읽지 못해 파일 스캔만으로 진행합니다: %s", exc)
            data, reserved = {}, 0

        start = max(scanned, reserved, _memory_reservations.get(key, 0)) + 1
        high_water = start + count - 1

        # **기억부터 올린다.** 디스크 쓰기가 실패해도 이 프로세스 안에서는
        # 번호가 절대 겹치지 않아야 한다.
        _memory_reservations[key] = high_water

        try:
            data[key] = high_water
            _write_reservations(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ADR 예약 장부를 갱신하지 못했습니다 — Core 를 다시 켜면 승인 전 "
                "제안끼리 번호가 겹칠 수 있습니다: %s", exc,
            )
    return start


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
    # 요청문은 프롬프트에는 전문이 들어가고 ADR 에는 상한까지만 들어간다.
    # 잘렸다는 표시가 없으면, ADR 만 읽는 사람은 잘린 문장을 요청 전문으로
    # 오해한다(뒤에 붙어 있던 제약이 사라진 것처럼 보인다).
    full_req = _clean(instruction, MAX_INSTRUCTION_CHARS * 4)
    req = _clean(instruction, MAX_INSTRUCTION_CHARS)
    if len(full_req) > len(req):
        req = f"{req} …(이하 생략, 전문은 코드 생성에 사용됨)"

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

    번호는 **여기서 예약한다.** 조회(`next_adr_index`)로 끝내면, 승인 전인
    제안이 여럿 있을 때 모두 같은 번호를 받아 나중에 적용한 것이 앞의
    기록을 덮어쓴다.
    """
    ops: list[dict] = []
    n = allocate_adr_indexes(root, target_folder, len(decisions))
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
