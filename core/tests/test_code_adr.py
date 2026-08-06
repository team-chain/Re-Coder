"""
회차1 (FR-02-03/04) — 결정 정규화 + ADR 영속화 단위 테스트.

LLM 호출 없이 순수 로직(정규화·슬러그·번호·마크다운·ops·프롬프트 블록)만 검증한다.
코드리뷰에서 나온 결함(번호 초기화·미선택 결정·프롬프트 인젝션)에 대한
회귀 테스트를 포함한다.
"""
try:  # main.py 스택/패키지 실행 양쪽 지원
    import adr  # type: ignore
    import code_agent as ca  # type: ignore
except ImportError:  # pragma: no cover
    from core import adr  # type: ignore
    from core import code_agent as ca  # type: ignore


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
