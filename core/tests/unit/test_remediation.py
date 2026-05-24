"""
Unit tests for ReCoder Remediation subsystem (§32).

검증 영역:
  1. 12종 generator 모두 등록 + 호출 가능
  2. 결정론적 동치성 — 같은 입력 → 같은 proposal_id (5회 반복)
  3. Template Registry — 11개 file + 5개 command template
  4. Applier — FILE_TEMPLATE / COMMAND_TEMPLATE / MANUAL_ONLY 각각 정상 작동
  5. Path traversal 차단
  6. base_sha256 무결성 검증
  7. dry_run 모드 — 실제 파일 변경 없음
  8. 보안 — secret 원문 노출 없음
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[2]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from remediation import (  # noqa: E402
    ApplyResult,
    apply_proposal,
    compute_fingerprint,
    generate_proposal_for_blocker,
    generate_proposals,
    get_command_template,
    get_file_template,
)
from remediation.fingerprint import proposal_id_from_fingerprint  # noqa: E402
from remediation.generator import _GENERATORS  # noqa: E402
from remediation.registry import (  # noqa: E402
    TEMPLATE_REGISTRY,
    render,
    required_variables,
)
from schemas import (  # noqa: E402
    ContractProjectMeta,
    ContractRuntime,
    ContractStack,
    PreflightBlocker,
    PreflightCheckCode,
    PreflightRun,
    PreflightSeverity,
    PreflightStatus,
    PreflightWarning,
    ReleaseContract,
    RemediationApplyMethod,
    RemediationPreviewType,
    RemediationTargetType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_contract(
    stack: ContractStack = ContractStack.PYTHON_FASTAPI,
    host_port: int = 8080,
    app_port: int = 8000,
    required_env: list[str] | None = None,
) -> ReleaseContract:
    c = ReleaseContract(
        project=ContractProjectMeta(name="test", stack=stack),
        runtime=ContractRuntime(host_port=host_port, app_port=app_port),
        contract_hash="deadbeef" * 8,  # fixed for determinism
    )
    if required_env is not None:
        c.preflight.required_env = required_env
    return c


def make_blocker(code: PreflightCheckCode) -> PreflightBlocker:
    return PreflightBlocker(code=code, message="test", severity=PreflightSeverity.HIGH)


def make_warning(code: PreflightCheckCode) -> PreflightWarning:
    return PreflightWarning(code=code, message="test", severity=PreflightSeverity.MEDIUM)


# ---------------------------------------------------------------------------
# 1. Generator registry
# ---------------------------------------------------------------------------


def test_generator_registry__has_12_entries() -> None:
    assert len(_GENERATORS) == 12
    all_codes = set(PreflightCheckCode)
    registered = set(_GENERATORS.keys())
    assert registered == all_codes, f"Missing generators: {all_codes - registered}"


@pytest.mark.parametrize("code", list(PreflightCheckCode))
def test_each_generator__returns_proposal(code: PreflightCheckCode, tmp_path: Path) -> None:
    """각 12종 generator 가 PreflightBlocker 를 받아 RemediationProposal 반환."""
    blocker = make_blocker(code)
    contract = make_contract(required_env=["DATABASE_URL", "API_KEY"])
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    assert proposal is not None
    assert proposal.source_blocker_code == code
    assert proposal.proposal_id.startswith("rem_")
    assert len(proposal.proposal_id) == 12  # rem_ + 8 hex
    assert proposal.summary
    assert proposal.rationale


# ---------------------------------------------------------------------------
# 2. Determinism — same input → same proposal_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", list(PreflightCheckCode))
def test_determinism__same_input_5_calls_same_proposal_id(code: PreflightCheckCode, tmp_path: Path) -> None:
    blocker = make_blocker(code)
    contract = make_contract(required_env=["DATABASE_URL", "API_KEY"])
    ids = {generate_proposal_for_blocker(blocker, contract, tmp_path).proposal_id for _ in range(5)}
    assert len(ids) == 1, f"Non-deterministic: {ids}"


def test_determinism__different_contract_hash_different_id(tmp_path: Path) -> None:
    blocker = make_blocker(PreflightCheckCode.MISSING_DOCKERFILE)
    c1 = make_contract()
    c1.contract_hash = "aaaa" * 16
    c2 = make_contract()
    c2.contract_hash = "bbbb" * 16
    p1 = generate_proposal_for_blocker(blocker, c1, tmp_path)
    p2 = generate_proposal_for_blocker(blocker, c2, tmp_path)
    assert p1.proposal_id != p2.proposal_id


def test_fingerprint__keys_normalized() -> None:
    """dict 키 순서 달라도 같은 fingerprint."""
    f1 = compute_fingerprint(
        blocker_code="X", target_path="a", template_id="t",
        template_variables={"a": 1, "b": 2}
    )
    f2 = compute_fingerprint(
        blocker_code="X", target_path="a", template_id="t",
        template_variables={"b": 2, "a": 1}
    )
    assert f1 == f2
    assert len(f1) == 64  # SHA256 hex


def test_proposal_id_from_fingerprint__format() -> None:
    fp = "a" * 64
    pid = proposal_id_from_fingerprint(fp)
    assert pid == "rem_aaaaaaaa"
    assert len(pid) == 12


# ---------------------------------------------------------------------------
# 3. Template registry
# ---------------------------------------------------------------------------


def test_template_registry__file_templates_present() -> None:
    expected = {
        "env.example.create",
        "gitignore.env.append",
        "dockerfile.fastapi",
        "dockerfile.flask",
        "dockerfile.node",
        "health.fastapi.snippet",
        "health.flask.snippet",
        "health.express.snippet",
        "dockerfile.expose.append",
        "recoder.yml.port.update",
    }
    actual = {t.template_id for t in TEMPLATE_REGISTRY.all_files()}
    assert expected.issubset(actual), f"Missing: {expected - actual}"


def test_template_registry__command_templates_present() -> None:
    expected = {
        "port.kill.windows",
        "port.kill.unix",
        "pip.compile.requirements",
        "npm.audit.fix",
        "pip.audit.run",
    }
    actual = {t.template_id for t in TEMPLATE_REGISTRY.all_commands()}
    assert expected == actual


def test_render__substitutes_vars() -> None:
    result = render("Hello {{name}}, port {{port}}", {"name": "World", "port": 8000})
    assert result == "Hello World, port 8000"


def test_render__missing_var_raises() -> None:
    with pytest.raises(KeyError):
        render("Hello {{name}}", {})


def test_required_variables__detected() -> None:
    body = "FROM python:{{python_version}}\nEXPOSE {{app_port}}\nCMD {{cmd}}"
    assert required_variables(body) == ["python_version", "app_port", "cmd"]


# ---------------------------------------------------------------------------
# 4. Applier — FILE_TEMPLATE
# ---------------------------------------------------------------------------


def test_apply_file_template__missing_dockerfile__writes_file(tmp_path: Path) -> None:
    blocker = make_blocker(PreflightCheckCode.MISSING_DOCKERFILE)
    contract = make_contract(stack=ContractStack.PYTHON_FASTAPI, app_port=8000)
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    assert proposal.auto_apply_available is True
    assert proposal.apply_method == RemediationApplyMethod.FILE_TEMPLATE

    result = apply_proposal(proposal, tmp_path)
    assert result.success
    assert (tmp_path / "Dockerfile").exists()
    content = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.11-slim" in content
    assert "EXPOSE 8000" in content


def test_apply_file_template__gitignore_append__appends_correctly(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    blocker = make_blocker(PreflightCheckCode.ENV_FILE_NOT_GITIGNORED)
    contract = make_contract()
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    result = apply_proposal(proposal, tmp_path)
    assert result.success
    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "node_modules/" in content  # original preserved
    assert ".env" in content
    assert ".env.local" in content
    assert result.backup_dir is not None


def test_apply_file_template__env_example_create(tmp_path: Path) -> None:
    blocker = make_blocker(PreflightCheckCode.MISSING_REQUIRED_ENV)
    contract = make_contract(required_env=["DATABASE_URL", "API_KEY", "SECRET_TOKEN"])
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    result = apply_proposal(proposal, tmp_path)
    assert result.success
    content = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert "DATABASE_URL=" in content
    assert "API_KEY=" in content
    assert "SECRET_TOKEN=" in content


# ---------------------------------------------------------------------------
# 5. Applier — dry_run & path traversal
# ---------------------------------------------------------------------------


def test_apply__dry_run_does_not_write(tmp_path: Path) -> None:
    blocker = make_blocker(PreflightCheckCode.MISSING_DOCKERFILE)
    contract = make_contract()
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    result = apply_proposal(proposal, tmp_path, dry_run=True)
    assert result.success
    assert result.dry_run
    assert not (tmp_path / "Dockerfile").exists()


def test_apply__path_traversal_rejected(tmp_path: Path) -> None:
    blocker = make_blocker(PreflightCheckCode.MISSING_DOCKERFILE)
    contract = make_contract()
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    # Tamper with target_path
    proposal.target_path = "../escape.txt"
    result = apply_proposal(proposal, tmp_path)
    assert not result.success
    assert "escape" in (result.error_message or "").lower()


# ---------------------------------------------------------------------------
# 6. Applier — MANUAL_ONLY and COMMAND_TEMPLATE
# ---------------------------------------------------------------------------


def test_apply__manual_only_returns_guidance(tmp_path: Path) -> None:
    blocker = make_blocker(PreflightCheckCode.SECRET_LEAK_RISK)
    contract = make_contract()
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    assert proposal.apply_method == RemediationApplyMethod.MANUAL_ONLY
    assert proposal.auto_apply_available is False
    # Without force, refuses
    result = apply_proposal(proposal, tmp_path)
    assert not result.success
    assert "auto_apply_available" in (result.skipped_reason or "")
    # With force, returns guidance (skipped_reason set)
    result_forced = apply_proposal(proposal, tmp_path, force=True)
    assert result_forced.success
    assert "MANUAL_ONLY" in (result_forced.skipped_reason or "")


def test_apply__command_template_returns_rendered_command(tmp_path: Path) -> None:
    blocker = make_blocker(PreflightCheckCode.UNPINNED_DEPENDENCIES)
    contract = make_contract()
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    result = apply_proposal(proposal, tmp_path, force=True)
    assert result.success
    assert "rendered_command" in result.details
    assert result.details["rendered_command"]  # non-empty


# ---------------------------------------------------------------------------
# 7. generate_proposals — PreflightRun integration
# ---------------------------------------------------------------------------


def test_generate_proposals__dedup_blocker_warning_same_code(tmp_path: Path) -> None:
    """같은 code 가 blocker + warning 양쪽에 있으면 blocker 만 살아남음."""
    run = PreflightRun(
        status=PreflightStatus.BLOCKED,
        blockers=[make_blocker(PreflightCheckCode.MISSING_DOCKERFILE)],
        warnings=[make_warning(PreflightCheckCode.MISSING_DOCKERFILE)],
    )
    contract = make_contract()
    proposals = generate_proposals(run, contract, tmp_path)
    codes = [p.source_blocker_code for p in proposals]
    assert codes.count(PreflightCheckCode.MISSING_DOCKERFILE) == 1


def test_generate_proposals__multiple_blockers(tmp_path: Path) -> None:
    run = PreflightRun(
        status=PreflightStatus.BLOCKED,
        blockers=[
            make_blocker(PreflightCheckCode.MISSING_DOCKERFILE),
            make_blocker(PreflightCheckCode.ENV_FILE_NOT_GITIGNORED),
        ],
    )
    contract = make_contract()
    proposals = generate_proposals(run, contract, tmp_path)
    assert len(proposals) == 2
    ids = {p.proposal_id for p in proposals}
    assert len(ids) == 2  # no duplicates


# ---------------------------------------------------------------------------
# 8. Security — no secret leakage
# ---------------------------------------------------------------------------


def test_secret_leak_proposal__no_raw_value(tmp_path: Path) -> None:
    """SECRET_LEAK_RISK proposal 의 어디에도 secret 원문 패턴이 들어가지 않아야 함."""
    blocker = make_blocker(PreflightCheckCode.SECRET_LEAK_RISK)
    contract = make_contract()
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    text = proposal.model_dump_json()
    # 가짜 secret 패턴들이 proposal에 안 들어가는지
    assert "AKIA" not in text  # AWS access key prefix
    assert "ghp_" not in text  # GitHub personal access token
    assert "sk_live" not in text  # Stripe live key


# ---------------------------------------------------------------------------
# 9. Preview types
# ---------------------------------------------------------------------------


def test_preview_types__file_content_for_writable(tmp_path: Path) -> None:
    """auto_apply_available 인 file template 은 FILE_CONTENT preview 가져야 함."""
    blocker = make_blocker(PreflightCheckCode.MISSING_DOCKERFILE)
    contract = make_contract()
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    assert proposal.preview_type == RemediationPreviewType.FILE_CONTENT
    assert proposal.preview is not None
    assert "content" in proposal.preview


def test_preview_types__guidance_for_manual(tmp_path: Path) -> None:
    blocker = make_blocker(PreflightCheckCode.APP_ENTRYPOINT_NOT_FOUND)
    contract = make_contract()
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    assert proposal.preview_type == RemediationPreviewType.GUIDANCE
    assert proposal.preview is not None
    assert "steps" in proposal.preview
    assert len(proposal.preview["steps"]) > 0


def test_preview_types__command_for_command_template(tmp_path: Path) -> None:
    blocker = make_blocker(PreflightCheckCode.UNPINNED_DEPENDENCIES)
    contract = make_contract()
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    assert proposal.preview_type == RemediationPreviewType.COMMAND
    assert proposal.preview is not None
    assert "command" in proposal.preview


# ---------------------------------------------------------------------------
# 10. Stack-aware Dockerfile generation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stack, expected_template_id, expected_in_content",
    [
        (ContractStack.PYTHON_FASTAPI, "dockerfile.fastapi", "uvicorn"),
        (ContractStack.PYTHON_FLASK,   "dockerfile.flask",   "gunicorn"),
        (ContractStack.NODE_EXPRESS,   "dockerfile.node",    "node:"),
    ],
)
def test_dockerfile_stack_aware(
    stack: ContractStack,
    expected_template_id: str,
    expected_in_content: str,
    tmp_path: Path,
) -> None:
    blocker = make_blocker(PreflightCheckCode.MISSING_DOCKERFILE)
    contract = make_contract(stack=stack)
    proposal = generate_proposal_for_blocker(blocker, contract, tmp_path)
    assert proposal.template_id == expected_template_id
    result = apply_proposal(proposal, tmp_path)
    assert result.success
    content = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert expected_in_content in content
