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


def test_approval_missing_when_cards_present_but_unchosen():
    """카드는 왔는데 아무것도 고르지 않았으면 승인이 아니다."""
    assert ca._approval_state([{"id": "storage", "options": []}]) == ca.APPROVAL_MISSING


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


def test_one_valid_choice_is_enough_even_with_a_bad_sibling():
    """유효한 승인이 하나라도 있으면 통과 — 잘못된 항목은 정규화가 걸러낸다."""
    assert ca._approval_state([{"id": "x", "chosen_key": "x"}, _chose("local")]) == ca.APPROVAL_APPROVED


def test_generate_code_rejects_choice_outside_offered_options(monkeypatch, tmp_path):
    called: list[int] = []
    monkeypatch.setattr(ca, "get_router", lambda: called.append(1))
    with pytest.raises(ValueError, match="제시된 선택지에 없어"):
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
        "options": [{"key": "cancel", "label": "취소처럼 보이는 선택지"}],
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
