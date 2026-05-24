"""
Unit tests for ReCoder IncidentMemory (§35).

검증 영역:
  1. Fingerprint — 결정론, 마스킹, stack normalize
  2. Memory store — CRUD + (fingerprint, project_id) composite PK
  3. Learner — success/consent gating, 재발 시 success_count 증가
  4. Matcher — exact project match (1.0) vs cross-project fallback (0.7)
  5. Privacy — user_consent=False 면 학습 안 됨, delete_incident_memory 동작
  6. Ranking
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[2]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from incident_memory import (  # noqa: E402
    build_incident_fingerprint,
    delete_incident_memory,
    init_incident_memory_table,
    learn_from_remediation,
    list_incident_memories,
    load_incident_memory,
    mask_for_fingerprint,
    match_incident,
    normalize_stack_trace,
    rank_matches,
    save_incident_memory,
    touch_incident_memory,
)
from persistence import RecoderDB  # noqa: E402
from schemas import (  # noqa: E402
    IncidentMemoryMatch,
    IncidentMemoryRecord,
    RemediationRun,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> RecoderDB:
    d = RecoderDB(tmp_path / "test.db", check_same_thread=True)
    init_incident_memory_table(d)
    return d


def _mk_record(
    fingerprint: str = "abc" * 22,  # 66 chars ≥ min_length=8
    project_id: str | None = "proj_demo",
    successful_fix: str = "downgraded dependency",
    success_count: int = 1,
    consent: bool = True,
    last_seen: datetime | None = None,
) -> IncidentMemoryRecord:
    return IncidentMemoryRecord(
        fingerprint=fingerprint[:64],
        project_id=project_id,
        symptom="ImportError: cannot import name 'X' from 'Y'",
        root_cause="version mismatch after package update",
        successful_fix=successful_fix,
        applied_proposal_id="rem_aaaaaaaa",
        success_count=success_count,
        last_seen_at=last_seen or datetime.now(timezone.utc),
        user_consent=consent,
    )


# ---------------------------------------------------------------------------
# 1. Fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint__deterministic_same_input() -> None:
    f1 = build_incident_fingerprint(
        error_type="ModuleNotFoundError",
        error_message="No module named 'foo'",
        last_file="app/main.py",
    )
    f2 = build_incident_fingerprint(
        error_type="ModuleNotFoundError",
        error_message="No module named 'foo'",
        last_file="app/main.py",
    )
    assert f1 == f2
    assert len(f1) == 64


def test_fingerprint__different_error_type_different_fp() -> None:
    f1 = build_incident_fingerprint(
        error_type="ModuleNotFoundError",
        error_message="No module named 'foo'",
    )
    f2 = build_incident_fingerprint(
        error_type="ConnectionRefusedError",
        error_message="No module named 'foo'",
    )
    assert f1 != f2


def test_fingerprint__masks_workspace_path() -> None:
    f1 = build_incident_fingerprint(
        error_type="FileNotFoundError",
        error_message="File not found: C:\\Users\\alice\\project\\app.py",
    )
    f2 = build_incident_fingerprint(
        error_type="FileNotFoundError",
        error_message="File not found: C:\\Users\\bob\\project\\app.py",
    )
    # 같은 패턴이라 마스킹 후엔 동일해야 함
    assert f1 == f2


def test_fingerprint__masks_quoted_values() -> None:
    """따옴표 안 값은 마스킹되어 결정론적."""
    f1 = build_incident_fingerprint(
        error_type="ValueError",
        error_message='Invalid token: "ABC123"',
    )
    f2 = build_incident_fingerprint(
        error_type="ValueError",
        error_message='Invalid token: "XYZ789"',
    )
    assert f1 == f2


def test_fingerprint__masks_numbers_and_hashes() -> None:
    """숫자와 hash 는 fingerprint 결정성을 깨므로 마스킹."""
    f1 = build_incident_fingerprint(
        error_type="SqlError",
        error_message="Query failed at row 12345, hash deadbeef0123",
    )
    f2 = build_incident_fingerprint(
        error_type="SqlError",
        error_message="Query failed at row 99999, hash cafef00d4567",
    )
    assert f1 == f2


def test_mask_for_fingerprint__empty_returns_empty() -> None:
    assert mask_for_fingerprint("") == ""


def test_normalize_stack_trace__top_3() -> None:
    stack = (
        'Traceback (most recent call last):\n'
        '  File "/app/main.py", line 42, in handler\n'
        '    do_work()\n'
        '  File "/app/lib/work.py", line 100, in do_work\n'
        '    raise ValueError\n'
        '  File "/app/lib/util.py", line 5, in helper\n'
        '    pass\n'
        '  File "/app/lib/other.py", line 9, in extra\n'
        '    pass\n'
    )
    frames = normalize_stack_trace(stack, top_n=3)
    assert frames == [
        "main.py::handler",
        "work.py::do_work",
        "util.py::helper",
    ]


def test_fingerprint__stack_trace_affects_fp() -> None:
    base = dict(error_type="RuntimeError", error_message="boom")
    f_no_stack = build_incident_fingerprint(**base)
    f_with_stack = build_incident_fingerprint(
        **base, stack_trace='  File "/a.py", line 1, in foo\n'
    )
    assert f_no_stack != f_with_stack


# ---------------------------------------------------------------------------
# 2. Memory store CRUD
# ---------------------------------------------------------------------------


def test_save_and_load(db: RecoderDB) -> None:
    rec = _mk_record()
    fp = save_incident_memory(db, rec)
    loaded = load_incident_memory(db, fp, "proj_demo")
    assert loaded is not None
    assert loaded.fingerprint == rec.fingerprint
    assert loaded.successful_fix == rec.successful_fix


def test_load_missing_returns_none(db: RecoderDB) -> None:
    assert load_incident_memory(db, "nonexistent" * 7, "proj_demo") is None


def test_save_replaces_same_pk(db: RecoderDB) -> None:
    """(fingerprint, project_id) 같은 PK 면 REPLACE."""
    rec1 = _mk_record(successful_fix="fix v1", success_count=1)
    save_incident_memory(db, rec1)

    rec2 = _mk_record(successful_fix="fix v2 (better)", success_count=3)
    save_incident_memory(db, rec2)

    loaded = load_incident_memory(db, rec2.fingerprint, "proj_demo")
    assert loaded.successful_fix == "fix v2 (better)"
    assert loaded.success_count == 3


def test_different_projects_same_fingerprint_coexist(db: RecoderDB) -> None:
    fp = "abc" * 22
    save_incident_memory(db, _mk_record(fingerprint=fp, project_id="A"))
    save_incident_memory(db, _mk_record(fingerprint=fp, project_id="B"))
    a = load_incident_memory(db, fp[:64], "A")
    b = load_incident_memory(db, fp[:64], "B")
    assert a is not None
    assert b is not None
    assert a.project_id == "A"
    assert b.project_id == "B"


def test_list_consent_only_filter(db: RecoderDB) -> None:
    save_incident_memory(db, _mk_record(fingerprint="aaa" * 22, consent=True))
    save_incident_memory(db, _mk_record(fingerprint="bbb" * 22, consent=False))
    consented = list_incident_memories(db, consent_only=True)
    all_items = list_incident_memories(db, consent_only=False)
    assert len(consented) == 1
    assert len(all_items) == 2


def test_touch_increments_count(db: RecoderDB) -> None:
    rec = _mk_record()
    save_incident_memory(db, rec)
    updated = touch_incident_memory(db, rec.fingerprint, "proj_demo")
    assert updated is not None
    assert updated.success_count == 2
    # second touch
    updated2 = touch_incident_memory(db, rec.fingerprint, "proj_demo")
    assert updated2.success_count == 3


def test_touch_nonexistent_returns_none(db: RecoderDB) -> None:
    assert touch_incident_memory(db, "missing" * 10, "X") is None


def test_delete_removes_record(db: RecoderDB) -> None:
    rec = _mk_record()
    save_incident_memory(db, rec)
    assert delete_incident_memory(db, rec.fingerprint, "proj_demo") is True
    assert load_incident_memory(db, rec.fingerprint, "proj_demo") is None
    assert delete_incident_memory(db, rec.fingerprint, "proj_demo") is False  # already gone


# ---------------------------------------------------------------------------
# 3. Learner — gating
# ---------------------------------------------------------------------------


def _mk_remediation_run(success: bool = True) -> RemediationRun:
    return RemediationRun(
        preflight_run_id="pre_xxxxxxxx",
        proposal_id="rem_yyyyyyyy",
        success=success,
    )


def test_learner__no_consent_no_store(db: RecoderDB) -> None:
    r = learn_from_remediation(
        db,
        remediation_run=_mk_remediation_run(success=True),
        fingerprint="zzz" * 22,
        symptom="s", root_cause="rc", successful_fix="fix",
        user_consent=False,
    )
    assert r.stored is False
    assert "consent" in (r.skipped_reason or "")


def test_learner__failed_remediation_not_learned(db: RecoderDB) -> None:
    r = learn_from_remediation(
        db,
        remediation_run=_mk_remediation_run(success=False),
        fingerprint="zzz" * 22,
        symptom="s", root_cause="rc", successful_fix="fix",
        user_consent=True,
    )
    assert r.stored is False
    assert "failed" in (r.skipped_reason or "")


def test_learner__stores_on_success_with_consent(db: RecoderDB) -> None:
    fp = ("0" * 32 + "1" * 32)[:64]
    r = learn_from_remediation(
        db,
        remediation_run=_mk_remediation_run(success=True),
        fingerprint=fp,
        symptom="ImportError",
        root_cause="missing dep",
        successful_fix="pip install foo",
        project_id="my_proj",
        user_consent=True,
    )
    assert r.stored is True
    assert r.success_count == 1
    rec = load_incident_memory(db, fp, "my_proj")
    assert rec is not None
    assert rec.symptom == "ImportError"


def test_learner__recurrence_increments_count(db: RecoderDB) -> None:
    fp = ("1" * 32 + "2" * 32)[:64]
    args = dict(
        fingerprint=fp,
        symptom="s", root_cause="rc", successful_fix="fix",
        project_id="p1",
        user_consent=True,
    )
    learn_from_remediation(db, remediation_run=_mk_remediation_run(), **args)
    r = learn_from_remediation(db, remediation_run=_mk_remediation_run(), **args)
    assert r.success_count == 2
    r2 = learn_from_remediation(db, remediation_run=_mk_remediation_run(), **args)
    assert r2.success_count == 3


# ---------------------------------------------------------------------------
# 4. Matcher
# ---------------------------------------------------------------------------


def test_match__exact_project_match_confidence_1_0(db: RecoderDB) -> None:
    fp = ("3" * 32 + "4" * 32)[:64]
    save_incident_memory(db, _mk_record(fingerprint=fp, project_id="my_proj"))
    matches = match_incident(db, fingerprint=fp, project_id="my_proj")
    assert len(matches) == 1
    assert matches[0].confidence == 1.0
    assert matches[0].entry.project_id == "my_proj"


def test_match__no_match_returns_empty(db: RecoderDB) -> None:
    save_incident_memory(db, _mk_record(fingerprint="aaa" * 22, project_id="x"))
    matches = match_incident(db, fingerprint="bbb" * 22, project_id="x")
    assert matches == []


def test_match__cross_project_fallback(db: RecoderDB) -> None:
    """본 프로젝트에 없으면 다른 프로젝트의 같은 fingerprint fix 를 0.7 로 제안."""
    fp = ("5" * 32 + "6" * 32)[:64]
    save_incident_memory(db, _mk_record(fingerprint=fp, project_id="other_proj"))
    matches = match_incident(db, fingerprint=fp, project_id="my_proj")
    assert len(matches) == 1
    assert matches[0].confidence == 0.7
    assert matches[0].entry.project_id == "other_proj"


def test_match__cross_project_fallback_disabled(db: RecoderDB) -> None:
    fp = ("7" * 32 + "8" * 32)[:64]
    save_incident_memory(db, _mk_record(fingerprint=fp, project_id="other_proj"))
    matches = match_incident(db, fingerprint=fp, project_id="my_proj", cross_project_fallback=False)
    assert matches == []


def test_match__no_consent_excluded_from_fallback(db: RecoderDB) -> None:
    """consent=False 인 record 는 cross-project fallback 에 등장하면 안 됨."""
    fp = ("9" * 32 + "a" * 32)[:64]
    save_incident_memory(db, _mk_record(fingerprint=fp, project_id="other_proj", consent=False))
    matches = match_incident(db, fingerprint=fp, project_id="my_proj")
    assert matches == []


def test_rank_matches__by_confidence_then_success_count() -> None:
    a = IncidentMemoryMatch(
        entry=_mk_record(fingerprint="aaa" * 22, success_count=5),
        confidence=0.7,
    )
    b = IncidentMemoryMatch(
        entry=_mk_record(fingerprint="bbb" * 22, success_count=2),
        confidence=1.0,
    )
    c = IncidentMemoryMatch(
        entry=_mk_record(fingerprint="ccc" * 22, success_count=10),
        confidence=0.7,
    )
    ranked = rank_matches([a, b, c])
    # b first (highest confidence), then c (same conf as a but higher success_count)
    assert ranked[0] is b
    assert ranked[1] is c
    assert ranked[2] is a


# ---------------------------------------------------------------------------
# 5. End-to-end: learn → match → touch
# ---------------------------------------------------------------------------


def test_e2e__learn_then_match_then_touch(db: RecoderDB) -> None:
    fp = build_incident_fingerprint(
        error_type="ModuleNotFoundError",
        error_message="No module named 'requests'",
        last_file="api/client.py",
    )
    # 1. Learn
    learn_from_remediation(
        db,
        remediation_run=_mk_remediation_run(),
        fingerprint=fp,
        symptom="missing requests package",
        root_cause="not in requirements.txt",
        successful_fix="add 'requests==2.31.0' to requirements.txt",
        project_id="proj1",
        user_consent=True,
    )

    # 2. Match — same fingerprint again
    matches = match_incident(db, fingerprint=fp, project_id="proj1")
    assert len(matches) == 1
    assert matches[0].confidence == 1.0
    assert matches[0].entry.success_count == 1

    # 3. Touch on recurrence
    touched = touch_incident_memory(db, fp, "proj1")
    assert touched.success_count == 2

    # 4. Match again — count reflects
    matches2 = match_incident(db, fingerprint=fp, project_id="proj1")
    assert matches2[0].entry.success_count == 2
