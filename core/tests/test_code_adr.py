"""
회차1 (FR-02-03/04) — 결정 정규화 + ADR 영속화 단위 테스트.

LLM 호출 없이 순수 로직(정규화·슬러그·번호·마크다운·ops·프롬프트 블록)만 검증한다.
코드리뷰에서 나온 결함(번호 초기화·미선택 결정·프롬프트 인젝션·승인 게이트 우회·
예약 네임스페이스 침범)에 대한 회귀 테스트를 포함한다.
"""
import json

import pytest

try:  # main.py 스택/패키지 실행 양쪽 지원
    import adr  # type: ignore
    import code_agent as ca  # type: ignore
except ImportError:  # pragma: no cover
    from core import adr  # type: ignore
    from core import code_agent as ca  # type: ignore


class _FakeRouter:
    """LLM 대신 미리 정해둔 raw 텍스트를 돌려주는 가짜 라우터."""

    def __init__(self, texts: list[str]):
        self._texts = list(texts)

    def call(self, request, agent=None, operation=None):
        text = self._texts.pop(0) if self._texts else json.dumps({"decisions": []})
        return type("_Resp", (), {"text": text, "model_used": "fake-model"})()


PLAN_DECISION = {
    "id": "storage",
    "question": "데이터를 어디에 저장할까요?",
    "impact": "앱 구조에 영향",
    "chosen_key": "local",
    "options": [
        {"key": "local", "label": "브라우저 로컬 저장", "summary": "서버 없이 바로 동작",
         "pros": ["간단", "설정 불필요"], "cons": ["기기 간 공유 불가"], "recommended": True},
        {"key": "file", "label": "파일(JSON)", "summary": "서버 파일시스템에 저장",
         "pros": ["영속성"], "cons": ["동시성 처리 필요"], "recommended": False},
    ],
}


# ── 정규화 ────────────────────────────────────────────────────────────

def test_normalize_full_decision():
    out = adr.normalize_decisions([PLAN_DECISION])
    assert len(out) == 1
    d = out[0]
    assert d["chosen_key"] == "local"
    assert d["chosen_label"] == "브라우저 로컬 저장"
    assert d["pros"] == ["간단", "설정 불필요"]
    assert d["impact"] == "앱 구조에 영향"
    # 선택 안 한 옵션은 대안으로 분류
    assert [a["label"] for a in d["alternatives"]] == ["파일(JSON)"]


def test_normalize_minimal_decision():
    out = adr.normalize_decisions([{"id": "auth", "chosen_key": "jwt"}])
    assert out[0]["id"] == "auth"
    assert out[0]["chosen_key"] == "jwt"
    assert out[0]["chosen_label"] == "jwt"   # 라벨 없으면 key 로 대체
    assert out[0]["alternatives"] == []


def test_normalize_accepts_choice_aliases():
    assert adr.normalize_decisions([{"id": "a", "choice": "x"}])[0]["chosen_key"] == "x"
    assert adr.normalize_decisions([{"id": "b", "chosen": "y"}])[0]["chosen_key"] == "y"


def test_normalize_ignores_garbage():
    assert adr.normalize_decisions([None, {}, "x", 3]) == []
    assert adr.normalize_decisions(None) == []


def test_normalize_drops_unchosen_decision():
    """[회귀] 사용자가 고르지 않은 결정은 버린다.

    예전에는 '(미지정)' 으로 ADR 이 만들어져, 하지도 않은 결정이 기록됐다.
    """
    out = adr.normalize_decisions([
        {"id": "storage", "question": "저장?", "options": [{"key": "a", "label": "A"}]},
    ])
    assert out == []


def test_normalize_collapses_newlines_and_truncates():
    """[회귀] 프롬프트 인젝션·마크다운 파괴 방지 — 개행 제거 + 길이 제한."""
    out = adr.normalize_decisions([{
        "id": "x", "chosen_key": "k",
        "question": "줄1\n## 가짜 헤더\n줄2",
        "impact": "가" * 500,
    }])
    d = out[0]
    assert "\n" not in d["question"]
    assert "## 가짜 헤더" in d["question"]      # 내용은 남되 한 줄로
    assert len(d["impact"]) <= adr.MAX_FIELD_CHARS


def test_normalize_caps_decision_count():
    many = [{"id": f"d{i}", "chosen_key": "k"} for i in range(50)]
    assert len(adr.normalize_decisions(many)) == adr.MAX_DECISIONS


# ── 슬러그 / 번호 ─────────────────────────────────────────────────────

def test_slugify():
    assert adr.slugify("storage") == "storage"
    assert adr.slugify("데이터 저장!!") == "데이터-저장"
    assert adr.slugify("") == "decision"


def test_slugify_blocks_path_traversal():
    slug = adr.slugify("../../etc/passwd")
    assert "/" not in slug and ".." not in slug


def test_slugify_avoids_windows_reserved_name():
    assert adr.slugify("con") != "con"


def test_next_adr_index(tmp_path):
    assert adr.next_adr_index(tmp_path) == 1
    d = tmp_path / "docs" / "adr"
    d.mkdir(parents=True)
    (d / "ADR-001-a.md").write_text("x", encoding="utf-8")
    (d / "ADR-004-b.md").write_text("x", encoding="utf-8")
    assert adr.next_adr_index(tmp_path) == 5


def test_next_adr_index_respects_target_folder(tmp_path):
    """[회귀] 확장이 target_folder 를 붙여 기록하므로 스캔도 같은 위치를 봐야 한다.

    예전에는 <root>/docs/adr 만 봐서 번호가 매번 001 로 초기화되고
    기존 ADR 을 덮어썼다.
    """
    nested = tmp_path / "myapp" / "docs" / "adr"
    nested.mkdir(parents=True)
    (nested / "ADR-007-x.md").write_text("x", encoding="utf-8")

    assert adr.next_adr_index(tmp_path) == 1                     # 루트엔 없음
    assert adr.next_adr_index(tmp_path, "myapp") == 8            # 실제 기록 위치
    assert adr.next_adr_index(tmp_path, "/myapp/") == 8          # 슬래시 허용


# ── ADR 마크다운 / ops ────────────────────────────────────────────────

def test_build_adr_markdown_structure():
    d = adr.normalize_decisions([PLAN_DECISION])[0]
    md = adr.build_adr_markdown(1, d, "할 일 목록 앱 만들어줘")
    assert md.startswith("# ADR-001: 데이터를 어디에 저장할까요?")
    assert "## 결정" in md and "브라우저 로컬 저장" in md
    assert "## 검토한 대안" in md and "파일(JSON)" in md
    assert "## 영향" in md and "감수: 기기 간 공유 불가" in md
    assert "앱 구조에 영향" in md


def test_build_adr_ops_numbering(tmp_path):
    ds = adr.normalize_decisions([
        PLAN_DECISION,
        {"id": "auth", "question": "인증 방식은?", "chosen_key": "jwt"},
    ])
    ops = adr.build_adr_ops(ds, "요청", tmp_path)
    assert [o["file"] for o in ops] == [
        "docs/adr/ADR-001-storage.md",
        "docs/adr/ADR-002-auth.md",
    ]
    assert all(o["action"] == "create" and o["is_adr"] for o in ops)


def test_build_adr_ops_continues_numbering_in_target_folder(tmp_path):
    nested = tmp_path / "app" / "docs" / "adr"
    nested.mkdir(parents=True)
    (nested / "ADR-003-prev.md").write_text("x", encoding="utf-8")
    ops = adr.build_adr_ops(
        adr.normalize_decisions([PLAN_DECISION]), "요청", tmp_path, "app"
    )
    # 경로는 워크스페이스 상대(확장이 target_folder 를 붙임), 번호는 이어서
    assert ops[0]["file"] == "docs/adr/ADR-004-storage.md"


def test_build_adr_ops_empty_when_no_decisions(tmp_path):
    assert adr.build_adr_ops([], "요청", tmp_path) == []


# ── 프롬프트 블록 ─────────────────────────────────────────────────────

def test_decisions_prompt_block():
    d = adr.normalize_decisions([PLAN_DECISION])
    block = ca._decisions_prompt_block(d)
    assert "사용자가 확정한 설계 결정" in block
    assert "브라우저 로컬 저장" in block
    assert "(local)" in block
    assert ca._decisions_prompt_block([]) == ""


def test_prompt_block_and_adr_agree_on_choice():
    """프롬프트와 ADR 이 같은 정규화 결과를 쓰므로 선택이 어긋나지 않는다."""
    d = adr.normalize_decisions([PLAN_DECISION])
    block = ca._decisions_prompt_block(d)
    md = adr.build_adr_markdown(1, d[0], "요청")
    assert d[0]["chosen_label"] in block
    assert d[0]["chosen_label"] in md


def test_prompt_block_survives_raw_input():
    """정규화를 거치지 않은 입력이 흘러들어와도 예외 없이 처리한다."""
    assert ca._decisions_prompt_block([None, "x", 3, {}]) == ""
    assert ca._decisions_prompt_block([{"id": "a"}]) == ""          # chosen_key 없음
    raw = [{"id": "s", "question": "q", "chosen_key": "k", "options": []}]
    assert "q: k (k)" in ca._decisions_prompt_block(raw)


# ── 요청별 워크스페이스 격리 ─────────────────────────────────────────

def test_resolve_root_prefers_explicit_over_env(tmp_path, monkeypatch):
    """[회귀] 워크스페이스는 전역 env 가 아니라 인자로 결정되어야 한다.

    to_thread 로 넘기는 사이 다른 요청이 env 를 덮어써도, 명시 경로를 받은
    요청은 자기 워크스페이스를 그대로 쓴다.
    """
    mine = tmp_path / "mine"; mine.mkdir()
    other = tmp_path / "other"; other.mkdir()
    monkeypatch.setenv("RECODER_PROJECT_ROOT", str(other))   # 남이 덮어쓴 상태

    assert ca._resolve_root(str(mine)) == mine               # 명시값 우선
    assert ca._resolve_root("") == other                     # 없으면 기존 동작


def test_resolve_root_falls_back_when_path_invalid(tmp_path, monkeypatch):
    valid = tmp_path / "v"; valid.mkdir()
    monkeypatch.setenv("RECODER_PROJECT_ROOT", str(valid))
    assert ca._resolve_root(str(tmp_path / "does-not-exist")) == valid


# ── FR-02-05 항상 선택지·사람 승인 ───────────────────────────────────

def test_confirm_decision_shape_matches_plan_schema():
    """확인 카드는 plan 결과 스키마를 그대로 따라야 확장이 수정 없이 렌더한다."""
    d = ca._build_confirm_decision("오타 고쳐줘")
    assert d["id"] == adr.CONFIRM_DECISION_ID
    assert "이대로 진행할까요?" in d["question"]
    assert "오타 고쳐줘" in d["question"]
    assert [o["key"] for o in d["options"]] == ["proceed", "cancel"]
    for o in d["options"]:
        assert {"key", "label", "summary", "pros", "cons", "recommended"} <= set(o)
    assert sum(1 for o in d["options"] if o["recommended"]) == 1


def test_confirm_decision_handles_empty_instruction():
    assert ca._build_confirm_decision("")["question"] == "이대로 진행할까요?"


def test_confirm_decision_collapses_newlines():
    assert "\n" not in ca._build_confirm_decision("첫줄\n둘째줄")["question"]


def test_confirm_decision_is_not_recorded_as_adr(tmp_path):
    """[핵심] 확인 카드는 승인 흐름에만 쓰이고 ADR 로는 남지 않는다."""
    confirmed = [{
        "id": adr.CONFIRM_DECISION_ID,
        "question": "이대로 진행할까요?",
        "chosen_key": "proceed",
        "options": [{"key": "proceed", "label": "진행"}],
    }]
    assert adr.normalize_decisions(confirmed) == []
    assert adr.build_adr_ops(adr.normalize_decisions(confirmed), "req", tmp_path) == []


def test_reserved_ids_filtered_but_real_decisions_survive():
    out = adr.normalize_decisions([
        {"id": adr.CONFIRM_DECISION_ID, "chosen_key": "proceed"},
        PLAN_DECISION,
    ])
    assert [d["id"] for d in out] == ["storage"]


def test_cancel_blocks_generation():
    assert ca._is_cancelled([{"id": adr.CONFIRM_DECISION_ID, "chosen_key": "cancel"}]) is True


def test_proceed_does_not_block():
    assert ca._is_cancelled([{"id": adr.CONFIRM_DECISION_ID, "chosen_key": "proceed"}]) is False
    assert ca._is_cancelled([]) is False
    assert ca._is_cancelled(None) is False
    assert ca._is_cancelled([PLAN_DECISION]) is False


def test_cancel_key_on_normal_decision_is_not_cancellation():
    """일반 결정에서 'cancel' 이라는 key 를 골라도 중단으로 오인하지 않는다."""
    assert ca._is_cancelled([{"id": "storage", "chosen_key": "cancel"}]) is False


# ── 승인 게이트: '취소 아님'이 아니라 '고른 것이 있음'을 확인 (Codex P1) ──

def test_approval_missing_when_no_decisions():
    """decisions 를 빼고 부르는 경로가 승인 게이트를 통과하면 안 된다."""
    assert ca._approval_state([]) == ca.APPROVAL_MISSING
    assert ca._approval_state(None) == ca.APPROVAL_MISSING


def test_unchosen_card_is_invalid_not_merely_missing():
    """카드는 제시됐는데 고르지 않았다 — 승인이 아닐 뿐 아니라 **거절 대상**이다.

    정규화가 미선택 카드를 조용히 버리므로, 통과시키면 "승인받으라고 내놓은
    결정"이 코드에도 ADR 에도 반영되지 않은 채 생성이 진행된다.
    """
    assert ca._approval_state([{"id": "storage", "options": []}]) == ca.APPROVAL_INVALID
    # MISSING 은 결정이 아예 실려오지 않은 경우로만 좁힌다.
    assert ca._approval_state([]) == ca.APPROVAL_MISSING


def _chose(key: str, *, id: str = "storage", keys=("local", "db")) -> dict:
    """제시된 선택지 중 하나를 고른 정상 결정 페이로드."""
    return {"id": id, "question": "q", "chosen_key": key,
            "options": [{"key": k, "label": k} for k in keys]}


def test_approval_granted_by_offered_choice():
    assert ca._approval_state([_chose("local")]) == ca.APPROVAL_APPROVED
    assert ca._approval_state([
        {"id": adr.CONFIRM_DECISION_ID, "chosen_key": "proceed"}
    ]) == ca.APPROVAL_APPROVED


def test_cancel_outranks_other_approvals():
    """여러 결정 중 하나라도 명시적 취소면 전체가 취소다 (순서 무관)."""
    cancel = {"id": adr.CONFIRM_DECISION_ID, "chosen_key": "cancel"}
    ok = _chose("local")
    assert ca._approval_state([ok, cancel]) == ca.APPROVAL_CANCELLED
    assert ca._approval_state([cancel, ok]) == ca.APPROVAL_CANCELLED


# ── 승인 키가 실제 제시된 선택지인지 검증 (Codex P2 · 3차) ──────────

def test_choice_not_among_offered_options_is_not_approval():
    """[핵심] 제시된 적 없는 값을 chosen_key 로 보내면 승인이 아니다."""
    assert ca._approval_state([{"id": "x", "chosen_key": "x"}]) == ca.APPROVAL_INVALID
    assert ca._approval_state([_chose("없는키")]) == ca.APPROVAL_INVALID


def test_confirm_card_requires_exactly_proceed():
    """확인 카드는 서버가 만든 것이므로 허용 키가 proceed/cancel 로 고정이다."""
    assert ca._approval_state([
        {"id": adr.CONFIRM_DECISION_ID, "chosen_key": "yes"}
    ]) == ca.APPROVAL_INVALID
    assert ca._approval_state([
        {"id": adr.CONFIRM_DECISION_ID, "chosen_key": "PROCEED"}
    ]) == ca.APPROVAL_APPROVED


# ── [불변식] 게이트와 정규화는 어긋나선 안 된다 ──────────────────────
#
# 지금까지 리뷰에서 나온 결함은 전부 같은 형태였다:
#   "게이트는 통과시켰는데 정규화가 조용히 버리거나 엉뚱하게 해석한다"
#   → 승인받으라고 내놓은 결정이 코드·ADR 에 반영되지 않는다.
# 개별 사례를 하나씩 막는 대신, 규칙 자체를 불변식으로 고정한다.

_CONFIRM = {"id": adr.CONFIRM_DECISION_ID, "chosen_key": "proceed"}


def _card(i: int, chosen: str = "a") -> dict:
    return {"id": f"d{i}", "question": f"q{i}", "chosen_key": chosen,
            "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]}


ACCEPTED_PAYLOADS = [
    [_card(1)],
    [_card(1), _card(2), _card(3)],
    [_CONFIRM],
    [_card(i) for i in range(adr.MAX_DECISIONS)],          # 상한 정확히
]

REJECTED_PAYLOADS = [
    ["문자열"],                                             # dict 아님
    [{"question": "q", "chosen_key": "a",                   # id 없음
      "options": [{"key": "a"}]}],
    [dict(_card(1), chosen_key="")],                        # 미선택 카드
    [_card(1), dict(_card(2), chosen_key="")],              # 일부만 미선택
    [dict(_card(1), chosen_key="없는키")],                   # 제시 안 된 키
    [_card(1), dict(_card(2), chosen_key="없는키")],         # 일부만 엉터리
    [{"id": adr.CONFIRM_DECISION_ID, "chosen_key": "yes"}],  # 확인 카드 잘못된 키
    [_card(1), dict(_card(2), id="d1")],                    # id 중복
    [_card(i) for i in range(adr.MAX_DECISIONS + 1)],       # 상한 초과
    # ↓ 원문으로는 서로 다르지만 정규형(adr.canonical_key)으로는 같아지는 키.
    #   게이트가 원문으로 비교하면 통과하는데, 기록 단계는 정규형으로 비교해
    #   **고르지 않은 선택지**의 라벨·근거를 ADR 에 남긴다.
    [{"id": "auth", "question": "q", "chosen_key": "k" * 200 + "AAA",
      "options": [{"key": "k" * 200 + "AAA", "label": "고른 것"},
                  {"key": "k" * 200 + "BBB", "label": "안 고른 것"}]}],   # 200자 절단 충돌
    [{"id": "auth", "question": "q", "chosen_key": "a b",
      "options": [{"key": "a b", "label": "고른 것"},
                  {"key": "a  b", "label": "안 고른 것"}]}],              # 공백 접힘 충돌
    [{"id": "auth", "question": "q", "chosen_key": "a\tb",
      "options": [{"key": "a\tb", "label": "고른 것"},
                  {"key": "a b", "label": "안 고른 것"}]}],               # 탭→공백 충돌
    [_card(1), dict(_card(2), id="d1 ")],                   # 공백만 다른 id → 정규형 중복
    # ↓ plan 이 지키는 구조 제약을 벗어난 카드. 낡거나 망가진 클라이언트가 보내면
    #   정규화가 초과분을 말없이 잘라내 사용자가 본 것과 기록이 어긋난다.
    [dict(_card(1), options=[{"key": "a", "label": "A"}])],  # 선택지 1개 → 고를 게 없음
    [dict(_card(1), chosen_key="k0",
          options=[{"key": f"k{i}", "label": f"L{i}"} for i in range(7)])],  # 선택지 7개
    [dict(_card(1), options=[{"key": "a", "label": "A", "pros": [f"p{i}" for i in range(9)]},
                             {"key": "b", "label": "B"}])],  # 장점 9개 → 5개로 잘림
    [{"id": "d1", "chosen_key": "a",                        # 질문 없음 → ADR 제목이 id 로 대체
      "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]}],
]


@pytest.mark.parametrize("payload", ACCEPTED_PAYLOADS,
                         ids=[f"ok{i}" for i in range(len(ACCEPTED_PAYLOADS))])
def test_gate_accepts_only_fully_valid_payloads(payload):
    assert ca._approval_state(payload) == ca.APPROVAL_APPROVED


@pytest.mark.parametrize("payload", REJECTED_PAYLOADS,
                         ids=[f"bad{i}" for i in range(len(REJECTED_PAYLOADS))])
def test_gate_rejects_partially_valid_payloads(payload):
    assert ca._approval_state(payload) == ca.APPROVAL_INVALID


@pytest.mark.parametrize("payload", ACCEPTED_PAYLOADS,
                         ids=[f"ok{i}" for i in range(len(ACCEPTED_PAYLOADS))])
def test_everything_the_gate_accepts_survives_normalization(payload):
    """[불변식] 게이트를 통과한 결정은 확인 카드를 빼고 **하나도 사라지지 않는다**.

    이게 깨지면 "승인은 받았는데 반영은 안 된 결정"이 생긴다 — 지금까지 나온
    결함들이 전부 이 불변식의 위반이었다.
    """
    expected = [str(d["id"]) for d in payload if d["id"] != adr.CONFIRM_DECISION_ID]
    assert [d["id"] for d in adr.normalize_decisions(payload)] == expected


def test_canonical_key_is_idempotent():
    """[근본] `canonical_key(canonical_key(x)) == canonical_key(x)`.

    이 함수는 발급(generate_plan)·검증(게이트)·기록(normalize_decisions)
    세 곳에서 각각 호출된다. 멱등이 아니면 발급 때 서로 다르던 두 값이
    검증 때 같아져 **정상 승인이 거절**된다 — 실제로 그랬다.
    경계는 상한 근처에서 공백이 잘릴 때다.
    """
    for n in range(195, 210):
        for pos in range(195, n):
            for ch in (" ", "\t", "\n"):
                s = "a" * n
                s = s[:pos] + ch + s[pos + 1:]
                assert adr.canonical_key(adr.canonical_key(s)) == adr.canonical_key(s), \
                    f"멱등 아님: len={n} pos={pos} ch={ch!r}"
    assert all(len(adr.canonical_key("x" * n)) <= adr.MAX_FIELD_CHARS for n in range(300))


def test_unique_canonical_id_stays_within_the_length_limit():
    """중복 회피 접미사를 붙여도 정규형이 다시 잘려 원래 id 로 돌아가면 안 된다."""
    base = "a" * adr.MAX_FIELD_CHARS
    seen = {base}
    for _ in range(5):
        new = ca._unique_canonical_id(base, seen)
        assert adr.canonical_key(new) == new, "발급한 id 가 정규형과 다름"
        assert new not in seen, "정규형 기준으로 중복"
        seen.add(new)


# plan 이 발급한 값은 기록 단계가 손대지 않아야 한다 (발급형 == 기록형).
_PLAN_STRESS = [
    ("상한 근처 id", {"id": "a" * 199 + "  b", "question": "q",
                      "options": [{"key": "x", "label": "X"}, {"key": "y", "label": "Y"}]}),
    ("상한 근처 key", {"id": "d", "question": "q",
                       "options": [{"key": "k" * 199 + "  b", "label": "X"},
                                   {"key": "k" * 199, "label": "Y"},
                                   {"key": "z", "label": "Z"}]}),
    ("긴 질문", {"id": "d", "question": "질" * 260,
                 "options": [{"key": "x", "label": "X"}, {"key": "y", "label": "Y"}]}),
    ("긴 라벨·요약", {"id": "d", "question": "q",
                      "options": [{"key": "x", "label": "라" * 260, "summary": "요" * 260},
                                  {"key": "y", "label": "Y"}]}),
    ("장단점 과다", {"id": "d", "question": "q",
                     "options": [{"key": "x", "label": "X", "pros": [f"p{i}" for i in range(9)],
                                  "cons": [f"c{i}" for i in range(9)]},
                                 {"key": "y", "label": "Y"}]}),
    ("옵션 과다", {"id": "d", "question": "q",
                   "options": [{"key": f"k{i}", "label": f"L{i}"} for i in range(9)]}),
    ("타입 이상", {"id": 7, "question": True, "impact": ["x"],
                   "options": [{"key": 1, "label": 2, "summary": None, "pros": [3], "cons": False},
                               {"key": 2, "label": 3}]}),
]


@pytest.mark.parametrize("name,raw", _PLAN_STRESS, ids=[n for n, _ in _PLAN_STRESS])
def test_plan_emits_values_normalization_will_not_change(monkeypatch, name, raw):
    """[불변식] 발급한 문자열은 전부 정규형이고, 개수 상한도 기록 단계와 같다.

    이걸 어기면 사용자가 카드에서 본 문구·항목이 ADR 에서 말없이 달라지거나
    사라진다. 지금까지 나온 결함 상당수가 이 불변식의 위반이었다.
    """
    monkeypatch.setattr(ca, "get_router",
                        lambda: _FakeRouter([json.dumps({"decisions": [raw]})]))
    for d in ca.generate_plan("뭔가 만들어줘")["decisions"]:
        for value in (d["id"], d["question"], d["impact"]):
            assert adr.canonical_key(value) == value, f"{name}: 발급형≠정규형 {value!r}"
        assert ca.MIN_OPTIONS_PER_DECISION <= len(d["options"]) <= ca.MAX_OPTIONS_PER_DECISION
        for o in d["options"]:
            for value in (o["key"], o["label"], o["summary"], *o["pros"], *o["cons"]):
                assert adr.canonical_key(value) == value, f"{name}: 발급형≠정규형 {value!r}"
            assert len(o["pros"]) <= adr.MAX_LIST_ITEMS
            assert len(o["cons"]) <= adr.MAX_LIST_ITEMS


@pytest.mark.parametrize("name,raw", _PLAN_STRESS, ids=[n for n, _ in _PLAN_STRESS])
def test_plan_output_approved_normally_always_passes_and_is_recorded(monkeypatch, name, raw):
    """[불변식] plan 이 준 선택지를 그대로 고르면 **반드시** 통과하고, 고른 그대로 기록된다."""
    monkeypatch.setattr(ca, "get_router",
                        lambda: _FakeRouter([json.dumps({"decisions": [raw]})]))
    offered = ca.generate_plan("뭔가 만들어줘")["decisions"]
    if any(d["id"] == adr.CONFIRM_DECISION_ID for d in offered):
        return  # 확인 카드는 별도 테스트에서 다룬다

    for index in range(len(offered[0]["options"])):
        picked = [dict(d, chosen_key=d["options"][index % len(d["options"])]["key"])
                  for d in offered]
        assert ca._approval_state(picked) == ca.APPROVAL_APPROVED, f"{name}: 정상 승인이 거절됨"
        kept = adr.normalize_decisions(picked)
        assert len(kept) == len(picked), f"{name}: 승인한 결정이 기록에서 사라짐"
        for d, k in zip(picked, kept):
            want = next(o for o in d["options"] if o["key"] == d["chosen_key"])
            assert k["chosen_label"] == (want["label"] or want["key"])
            assert k["chosen_summary"] == want.get("summary", "")
            assert k["impact"] == d["impact"]
            assert k["question"] == d["question"]
            assert len(k["alternatives"]) == len(d["options"]) - 1, "본 대안이 기록에서 누락"


def test_adr_marks_a_truncated_request_instead_of_hiding_it():
    """요청문이 잘렸으면 잘렸다고 적는다 — ADR 만 읽는 사람이 오해하지 않도록."""
    long_req = "이 요청은 아주 깁니다. " * 40
    md = adr.build_adr_markdown(1, adr.normalize_decisions([PLAN_DECISION])[0], long_req)
    assert "생략" in md
    short = adr.build_adr_markdown(1, adr.normalize_decisions([PLAN_DECISION])[0], "짧은 요청")
    assert "생략" not in short


def test_gate_enforces_the_same_option_limits_plan_does():
    """[불변식] 발급 쪽 구조 제약과 검증 쪽 제약은 같아야 한다.

    게이트가 개수를 확인하지 않으면, 제약을 벗어난 카드를 보낸 클라이언트의
    요청이 통과하고 정규화가 초과분을 말없이 잘라낸다 — 사용자가 봤다는
    대안·근거가 ADR 에서 사라지거나, 고를 것이 하나뿐인 카드가 승인이 된다.
    """
    too_few = dict(_card(1), options=[{"key": "a", "label": "A"}])
    too_many = dict(_card(1), chosen_key="k0",
                    options=[{"key": f"k{i}", "label": f"L{i}"}
                             for i in range(ca.MAX_OPTIONS_PER_DECISION + 2)])
    assert ca._approval_state([too_few]) == ca.APPROVAL_INVALID
    assert ca._approval_state([too_many]) == ca.APPROVAL_INVALID

    # 경계값은 통과해야 한다 (과도한 조임 방지).
    for n in range(ca.MIN_OPTIONS_PER_DECISION, ca.MAX_OPTIONS_PER_DECISION + 1):
        ok = dict(_card(1), chosen_key="k0",
                  options=[{"key": f"k{i}", "label": f"L{i}"} for i in range(n)])
        assert ca._approval_state([ok]) == ca.APPROVAL_APPROVED, f"선택지 {n}개가 거절됨"


def test_gate_rejects_pros_cons_that_normalization_would_truncate():
    over = dict(_card(1), options=[
        {"key": "a", "label": "A", "cons": [f"c{i}" for i in range(adr.MAX_LIST_ITEMS + 1)]},
        {"key": "b", "label": "B"}])
    assert ca._approval_state([over]) == ca.APPROVAL_INVALID
    exact = dict(_card(1), options=[
        {"key": "a", "label": "A", "cons": [f"c{i}" for i in range(adr.MAX_LIST_ITEMS)]},
        {"key": "b", "label": "B"}])
    assert ca._approval_state([exact]) == ca.APPROVAL_APPROVED


def test_gate_requires_the_question_the_user_approved():
    """질문이 없으면 ADR 제목이 id 로 대체돼 승인한 질문이 기록에서 사라진다."""
    no_q = {"id": "d1", "chosen_key": "a",
            "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]}
    assert ca._approval_state([no_q]) == ca.APPROVAL_INVALID


def test_generated_code_ops_cannot_overwrite_the_adr_record(monkeypatch, tmp_path):
    """LLM 이 ADR 기록 파일을 만들어도 결정 기록을 덮어쓰지 못한다."""
    payload = json.dumps({"ops": [
        {"action": "create", "file": "docs/adr/ADR-001-storage.md", "content": "가짜"},
        {"action": "create", "file": "app.py", "content": "x=1"},
    ], "summary": "s"})
    monkeypatch.setattr(ca, "get_router", lambda: _FakeRouter([payload]))

    result = ca.generate_code("만들어줘", decisions=[PLAN_DECISION], project_root=str(tmp_path))
    files = [op["file"] for op in result["ops"]]
    assert files.count("docs/adr/ADR-001-storage.md") == 1
    assert "app.py" in files
    adr_op = next(op for op in result["ops"] if op["file"].startswith(adr.ADR_DIR))
    assert adr_op.get("is_adr") is True, "남은 것은 진짜 ADR 기록이어야 함"


def test_filtering_every_op_reports_failure_instead_of_empty_success(monkeypatch, tmp_path):
    """[핵심] 걸러내고 나니 적용할 게 없으면 성공으로 반환하면 안 된다.

    그대로 두면 확장은 "생성 완료 · 변경 0건"을 띄우고, 사용자는 요청이
    처리된 줄 안다. 실제로는 요청한 변경이 통째로 사라진 것이다.
    """
    payload = json.dumps({"ops": [
        {"action": "create", "file": "docs/adr/ADR-001-storage.md", "content": "덮어쓰기"},
    ], "summary": "s"})
    monkeypatch.setattr(ca, "get_router", lambda: _FakeRouter([payload]))
    confirm = dict(ca._build_confirm_decision("ADR 고쳐줘"), chosen_key="proceed")

    with pytest.raises(ValueError, match="적용할 수 있는 것이 없습니다"):
        ca.generate_code("ADR 고쳐줘", decisions=[confirm], project_root=str(tmp_path))


@pytest.mark.parametrize("path", [
    "docs/adr/ADR-001-storage.md",       # 그대로
    "docs/adr/adr-001-storage.md",       # 파일명 소문자 — Windows·macOS 에선 같은 파일
    "Docs/ADR/ADR-001-storage.md",       # 디렉터리 대소문자
    "DOCS/ADR/ADR-001-STORAGE.MD",       # 전부 대문자
    "./docs/adr/ADR-001-storage.md",     # 앞에 ./
    "docs/adr/../adr/ADR-001-storage.md",  # 돌아가는 경로
    "docs\\adr\\ADR-001-storage.md",     # 윈도우 구분자
], ids=["as-is", "lower-file", "mixed-dir", "upper", "dot-slash", "dot-dot", "backslash"])
def test_adr_record_protection_ignores_path_spelling(monkeypatch, tmp_path, path):
    """[핵심] 같은 파일을 가리키는 다른 표기로 보호를 우회할 수 없다.

    확장은 op 를 하나씩 독립적으로 쓴다. 대소문자만 다른 op 가 통과해 나중에
    쓰이면, 대소문자를 구분하지 않는 파일시스템(Windows·macOS)에서는 승인된
    기록을 그대로 덮어쓴다 — 이 필터가 막으려던 사고가 그대로 난다.
    """
    payload = json.dumps({"ops": [
        {"action": "create", "file": path, "content": "가짜"},
        {"action": "create", "file": "app.py", "content": "x=1"},
    ], "summary": "s"})
    monkeypatch.setattr(ca, "get_router", lambda: _FakeRouter([payload]))

    result = ca.generate_code("만들어줘", decisions=[PLAN_DECISION], project_root=str(tmp_path))
    # 가짜 내용이 op 로 남아있으면 안 된다 (경로 표기가 무엇이든).
    assert all(op["content"] != "가짜" for op in result["ops"]), f"{path} 가 걸러지지 않음"
    assert "app.py" in [op["file"] for op in result["ops"]]
    adr_ops = [op for op in result["ops"] if op.get("is_adr")]
    assert len(adr_ops) == 1, "진짜 ADR 기록 하나만 남아야 함"


def test_hand_written_docs_in_the_adr_folder_stay_editable(monkeypatch, tmp_path):
    """생성 기록(ADR-NNN-*.md)만 보호한다 — README 같은 손 문서는 수정 가능해야 한다.

    디렉터리 전체를 막으면 "ADR 규칙 문서 고쳐줘"가 조용히 아무것도 안 하고
    성공으로 끝난다.
    """
    payload = json.dumps({"ops": [
        {"action": "update", "file": "docs/adr/README.md", "content": "규칙 보강"},
    ], "summary": "s"})
    monkeypatch.setattr(ca, "get_router", lambda: _FakeRouter([payload]))
    confirm = dict(ca._build_confirm_decision("ADR 규칙 문서 보강"), chosen_key="proceed")

    result = ca.generate_code("ADR 규칙 문서 보강", decisions=[confirm], project_root=str(tmp_path))
    assert [op["file"] for op in result["ops"]] == ["docs/adr/README.md"]


def test_gate_and_normalization_compare_keys_the_same_way():
    """[핵심] 게이트와 기록이 같은 정규형으로 비교해야 한다.

    원문 비교와 정규형 비교가 섞이면, 게이트에선 서로 다른 두 키가 기록에선
    같아져 **사용자가 고르지 않은 선택지**의 라벨·근거가 ADR 에 남는다.
    """
    long_a, long_b = "k" * 200 + "AAA", "k" * 200 + "BBB"
    d = {"id": "auth", "question": "q", "chosen_key": long_a,
         "options": [{"key": long_a, "label": "고른 것", "summary": "A안"},
                     {"key": long_b, "label": "안 고른 것", "summary": "B안"}]}
    assert ca._approval_state([d]) == ca.APPROVAL_INVALID
    assert ca._chosen_key(d) == adr.canonical_key(long_a)


def test_plan_never_issues_options_that_collide_after_canonicalization(monkeypatch):
    """제시 단계에서 정규형이 겹치는 선택지를 아예 내보내지 않는다."""
    long_a, long_b = "k" * 200 + "AAA", "k" * 200 + "BBB"
    body = json.dumps({"decisions": [{
        "id": "auth", "question": "인증?",
        "options": [{"key": long_a, "label": "A"}, {"key": long_b, "label": "B"},
                    {"key": "sane", "label": "C"}],
        "impact": "",
    }]})
    monkeypatch.setattr(ca, "get_router", lambda: _FakeRouter([body]))

    offered = ca.generate_plan("뭔가 만들어줘")["decisions"][0]["options"]
    keys = [o["key"] for o in offered]
    assert len(set(keys)) == len(keys), f"정규형 충돌이 제시됨: {keys}"

    # 제시된 어느 것을 골라도 게이트 통과 + 기록이 그 선택과 일치해야 한다.
    for opt in offered:
        d = {"id": "auth", "question": "인증?", "chosen_key": opt["key"], "options": offered}
        assert ca._approval_state([d]) == ca.APPROVAL_APPROVED
        assert adr.normalize_decisions([d])[0]["chosen_label"] == opt["label"]


def test_plan_never_offers_more_than_normalization_keeps(monkeypatch):
    """제시 단계에서도 같은 상한을 지킨다 — 승인 후 잘려나가는 결정이 없도록."""
    over = adr.MAX_DECISIONS + 5
    body = json.dumps({"decisions": [
        {"id": f"d{i}", "question": f"q{i}",
         "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}], "impact": ""}
        for i in range(over)
    ]})
    monkeypatch.setattr(ca, "get_router", lambda: _FakeRouter([body]))

    offered = ca.generate_plan("뭔가 만들어줘")["decisions"]
    assert len(offered) == adr.MAX_DECISIONS

    for d in offered:
        d["chosen_key"] = "a"
    assert ca._approval_state(offered) == ca.APPROVAL_APPROVED
    assert len(adr.normalize_decisions(offered)) == len(offered)


def test_invalid_sibling_rejects_the_whole_payload():
    """[핵심] 유효한 선택 하나가 섞여 있어도, 엉터리 항목이 있으면 전체를 거절한다.

    정규화는 알 수 없는 `chosen_key` 를 버리지 않고 그대로 선택 라벨로 삼는다.
    그래서 통과시키면 **아무도 승인하지 않은 결정**이 프롬프트와 ADR 에 실린다.
    (3차 리뷰에서 내가 반대로 단정했던 부분 — 회귀로 고정한다.)
    """
    bad = _chose("없는키", id="auth", keys=("jwt", "session"))
    assert ca._approval_state([_chose("local"), bad]) == ca.APPROVAL_INVALID
    assert ca._approval_state([bad, _chose("local")]) == ca.APPROVAL_INVALID


def test_cancel_still_outranks_an_invalid_sibling():
    cancel = {"id": adr.CONFIRM_DECISION_ID, "chosen_key": "cancel"}
    bad = _chose("없는키")
    assert ca._approval_state([bad, cancel]) == ca.APPROVAL_CANCELLED


def test_normalize_drops_choice_that_matches_no_offered_option():
    """게이트가 뚫려도 근거 없는 ADR 이 남지 않도록 정규화에서도 끊는다."""
    out = adr.normalize_decisions([{
        "id": "auth", "question": "인증?", "chosen_key": "없는키",
        "options": [{"key": "jwt", "label": "JWT"}, {"key": "session", "label": "세션"}],
    }])
    assert out == []


def test_normalize_still_accepts_minimal_shape_without_options():
    """선택지 목록이 아예 없는 최소 형태는 종전대로 허용 (대조할 대상이 없음)."""
    out = adr.normalize_decisions([{"id": "auth", "question": "인증?", "chosen_key": "jwt"}])
    assert [d["chosen_label"] for d in out] == ["jwt"]


def test_generate_code_rejects_choice_outside_offered_options(monkeypatch, tmp_path):
    called: list[int] = []
    monkeypatch.setattr(ca, "get_router", lambda: called.append(1))
    with pytest.raises(ValueError, match="승인 내용이 온전하지 않아"):
        ca.generate_code("만들어줘", decisions=[{"id": "x", "chosen_key": "x"}],
                         project_root=str(tmp_path))
    assert called == []


def test_server_issued_confirm_card_passes_its_own_gate():
    """서버가 만든 확인 카드를 그대로 승인하면 반드시 통과해야 한다 (자기모순 방지)."""
    card = ca._build_confirm_decision("오타 고쳐줘")
    for opt in card["options"]:
        card["chosen_key"] = opt["key"]
        expected = ca.APPROVAL_CANCELLED if opt["key"] == "cancel" else ca.APPROVAL_APPROVED
        assert ca._approval_state([card]) == expected


def test_generate_code_rejects_request_without_approval(monkeypatch, tmp_path):
    """[핵심] decisions 없이 generate_code 를 부르면 LLM 호출 전에 막힌다."""
    called: list[int] = []
    monkeypatch.setattr(ca, "get_router", lambda: called.append(1))

    with pytest.raises(ValueError, match="승인된 선택이 없어"):
        ca.generate_code("아무거나 만들어줘", decisions=[], project_root=str(tmp_path))
    with pytest.raises(ValueError, match="승인된 선택이 없어"):
        ca.generate_code("아무거나 만들어줘", project_root=str(tmp_path))

    assert called == [], "승인 없이 LLM 을 호출하면 안 됨 (게이트가 뒤에 있다는 뜻)"


# ── 예약 네임스페이스 침범 방지 (Codex P2) ──────────────────────────

def test_normalize_keeps_reserved_looking_ids_other_than_confirm():
    """`__` 로 시작해도 확인 카드가 아니면 버리지 않는다.

    접두사 전체를 걸러내면 사용자가 고른 진짜 결정이 조용히 사라져
    프롬프트에도 ADR 에도 안 실린 채 코드가 생성된다.
    """
    out = adr.normalize_decisions([{"id": "__rogue", "question": "진짜 결정", "chosen_key": "a"}])
    assert [d["id"] for d in out] == ["__rogue"]


def test_reserved_id_slug_is_still_a_safe_filename(tmp_path):
    ops = adr.build_adr_ops(
        adr.normalize_decisions([{"id": "__rogue", "question": "q", "chosen_key": "a"}]),
        "req", tmp_path,
    )
    assert ops[0]["file"] == "docs/adr/ADR-001-rogue.md"


def test_generate_plan_remaps_model_ids_that_invade_reserved_namespace(monkeypatch):
    """[핵심] 모델이 `__` id 를 뱉어도 일반 id 로 옮겨 붙여 UI·ADR 이 어긋나지 않게 한다."""
    body = json.dumps({"decisions": [{
        "id": "__confirm__", "question": "진짜 설계 결정입니다",
        "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
        "impact": "",
    }]})
    monkeypatch.setattr(ca, "get_router", lambda: _FakeRouter([body]))

    decision = ca.generate_plan("뭔가 만들어줘")["decisions"][0]
    assert decision["id"] == "d-confirm"
    assert decision["id"] != adr.CONFIRM_DECISION_ID

    # 옮겨 붙인 id 로 승인해도 정규화·ADR 이 정상 동작해야 한다.
    decision["chosen_key"] = "a"
    assert [d["id"] for d in adr.normalize_decisions([decision])] == ["d-confirm"]


def _plan_ids(monkeypatch, *raw_ids: str) -> list[str]:
    """주어진 id 들로 plan 을 태우고, 최종 결정 id 목록을 돌려준다."""
    body = json.dumps({"decisions": [
        {"id": rid, "question": f"{rid} 를 어떻게 할까요?",
         "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}], "impact": ""}
        for rid in raw_ids
    ]})
    monkeypatch.setattr(ca, "get_router", lambda: _FakeRouter([body]))
    return [d["id"] for d in ca.generate_plan("뭔가 만들어줘")["decisions"]]


def test_remap_does_not_collide_with_existing_id(monkeypatch):
    """[핵심] `__auth` 재배치 결과가 이미 있는 `d-auth` 를 덮어쓰면 안 된다.

    웹뷰는 선택을 `selections[decision.id]` 로 저장한다. id 가 겹치면 뒤 카드가
    앞 카드의 선택을 덮어써, 고르지도 않은 선택이 코드 생성과 ADR 에 실린다.
    """
    ids = _plan_ids(monkeypatch, "__auth", "d-auth")
    assert len(set(ids)) == 2, f"id 가 충돌함: {ids}"


def test_duplicate_ids_from_model_are_separated(monkeypatch):
    """재배치와 무관하게, 모델이 같은 id 를 두 번 줘도 유일해야 한다."""
    ids = _plan_ids(monkeypatch, "auth", "auth", "auth")
    assert len(set(ids)) == 3, f"id 가 충돌함: {ids}"
    assert ids[0] == "auth"


def test_repeated_reserved_ids_stay_unique(monkeypatch):
    ids = _plan_ids(monkeypatch, "__auth", "__auth")
    assert len(set(ids)) == 2, f"id 가 충돌함: {ids}"
    assert not any(i.startswith(adr.RESERVED_ID_PREFIX) for i in ids)


def test_remapped_decision_cannot_impersonate_cancellation(monkeypatch):
    """`__confirm__` + 'cancel' 을 유도해도 생성 중단을 흉내 낼 수 없다."""
    body = json.dumps({"decisions": [{
        "id": "__confirm__", "question": "진짜 결정",
        # 선택지는 2개 이상이어야 제시된다(MIN_OPTIONS_PER_DECISION).
        "options": [{"key": "cancel", "label": "취소처럼 보이는 선택지"},
                    {"key": "keep", "label": "다른 선택지"}],
        "impact": "",
    }]})
    monkeypatch.setattr(ca, "get_router", lambda: _FakeRouter([body]))

    decision = ca.generate_plan("뭔가 만들어줘")["decisions"][0]
    decision["chosen_key"] = "cancel"
    assert ca._approval_state([decision]) == ca.APPROVAL_APPROVED


def test_concurrent_requests_do_not_share_root(tmp_path, monkeypatch):
    """두 요청이 스레드에서 겹쳐 돌아도 각자 자기 루트를 본다."""
    import threading

    a = tmp_path / "wsA"; a.mkdir()
    b = tmp_path / "wsB"; b.mkdir()
    seen: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, path):
        # 두 스레드가 동시에 env 를 덮어쓰는 최악의 상황을 재현
        monkeypatch.setenv("RECODER_PROJECT_ROOT", str(path))
        barrier.wait()
        seen[name] = ca._resolve_root(str(path))

    t1 = threading.Thread(target=worker, args=("A", a))
    t2 = threading.Thread(target=worker, args=("B", b))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert seen["A"] == a and seen["B"] == b
