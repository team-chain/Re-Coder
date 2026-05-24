"""
RemediationProposal Applier (§32.3).

RemediationProposal 을 워크스페이스에 안전하게 적용.

설계 원칙:
  - 적용 전 ``base_sha256`` 무결성 확인 (DIFF preview 에 있을 때)
  - 자동 적용 가능한 case 만 실행. MANUAL_ONLY 는 가이드만 반환.
  - 파일 쓰기 전 백업 (.recoder/backups/<timestamp>/<path>) — 실패 시 즉시 복원
  - workspace 밖 경로 차단 (path traversal 방어)
  - dry_run 모드로 시뮬레이션 가능 (실제 변경 안 함)
"""

from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from schemas import (
        RemediationApplyMethod,
        RemediationFallback,
        RemediationPreviewType,
        RemediationProposal,
    )
except ImportError:  # pragma: no cover
    from core.schemas import (  # type: ignore
        RemediationApplyMethod,
        RemediationFallback,
        RemediationPreviewType,
        RemediationProposal,
    )

from .registry import get_command_template, get_file_template, render


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    """적용 결과 — 성공/실패 + 변경된 파일 + rollback 정보."""
    proposal_id: str
    success: bool
    applied_files: list[str] = field(default_factory=list)
    backup_dir: Optional[str] = None
    skipped_reason: Optional[str] = None
    error_message: Optional[str] = None
    dry_run: bool = False
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _safe_resolve(workspace: Path, relative: str) -> Path:
    """workspace 안쪽인지 검증 후 절대 경로 반환. 외부면 ValueError.

    Path traversal (../../etc/passwd) 차단.
    """
    if relative is None:
        raise ValueError("target_path is required for file write")
    candidate = (workspace / relative).resolve()
    workspace_resolved = workspace.resolve()
    try:
        candidate.relative_to(workspace_resolved)
    except ValueError as exc:
        raise ValueError(f"target_path '{relative}' escapes workspace") from exc
    return candidate


def _backup_dir(workspace: Path) -> Path:
    """timestamp 기반 backup 디렉토리. 매 호출마다 새로 생성."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    bd = workspace / ".recoder" / "backups" / ts
    bd.mkdir(parents=True, exist_ok=True)
    return bd


def _file_sha256(path: Path) -> Optional[str]:
    if not path.exists() or not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Apply per method
# ---------------------------------------------------------------------------


def _apply_file_template(
    proposal: RemediationProposal,
    workspace: Path,
    *,
    dry_run: bool,
) -> ApplyResult:
    """FILE_TEMPLATE — Registry 의 FileTemplate 로 파일 생성/덮어쓰기.

    이미 존재하는 파일은:
      - .gitignore 처럼 'append' 의도면 append (template_id 가 .append 로 끝남)
      - 아니면 백업 후 덮어쓰기
    """
    tmpl = get_file_template(proposal.template_id or "")
    if tmpl is None:
        return ApplyResult(
            proposal_id=proposal.proposal_id,
            success=False,
            error_message=f"FileTemplate not found: {proposal.template_id}",
        )
    try:
        rendered = render(tmpl.base_content, proposal.template_variables or {})
    except KeyError as exc:
        return ApplyResult(
            proposal_id=proposal.proposal_id,
            success=False,
            error_message=f"Template variable missing: {exc}",
        )

    try:
        target = _safe_resolve(workspace, proposal.target_path or "")
    except ValueError as exc:
        return ApplyResult(
            proposal_id=proposal.proposal_id,
            success=False,
            error_message=str(exc),
        )

    is_append = (proposal.template_id or "").endswith(".append")

    # base_sha256 검증 (preview 가 DIFF 인 경우)
    if proposal.preview and proposal.preview_type == RemediationPreviewType.DIFF:
        expected = proposal.preview.get("base_sha256")
        actual = _file_sha256(target)
        if expected and actual and expected != actual:
            return ApplyResult(
                proposal_id=proposal.proposal_id,
                success=False,
                error_message=(
                    f"base_sha256 mismatch — target file changed since proposal "
                    f"creation. expected={expected[:8]} actual={actual[:8]}"
                ),
            )

    backup_dir: Optional[Path] = None
    if not dry_run and target.exists():
        backup_dir = _backup_dir(workspace)
        rel_for_backup = proposal.target_path or target.name
        backup_path = backup_dir / rel_for_backup
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup_path)

    if dry_run:
        return ApplyResult(
            proposal_id=proposal.proposal_id,
            success=True,
            applied_files=[proposal.target_path or ""],
            dry_run=True,
            details={"would_write_bytes": len(rendered), "append_mode": is_append},
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if is_append and target.exists():
            with target.open("a", encoding="utf-8") as f:
                f.write(rendered)
        else:
            target.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        # 실패 → 백업 복원
        if backup_dir is not None and (backup_dir / (proposal.target_path or "")).exists():
            try:
                shutil.copy2(backup_dir / (proposal.target_path or ""), target)
            except OSError:
                pass
        return ApplyResult(
            proposal_id=proposal.proposal_id,
            success=False,
            error_message=f"file write failed: {exc}",
            backup_dir=str(backup_dir) if backup_dir else None,
        )

    return ApplyResult(
        proposal_id=proposal.proposal_id,
        success=True,
        applied_files=[proposal.target_path or ""],
        backup_dir=str(backup_dir) if backup_dir else None,
        details={"bytes_written": len(rendered), "append_mode": is_append},
    )


def _apply_command_template(
    proposal: RemediationProposal,
    workspace: Path,
    *,
    dry_run: bool,
) -> ApplyResult:
    """COMMAND_TEMPLATE — 실제 실행은 ``executor`` / ``command_safety`` 로 위임.

    본 모듈은 명령을 렌더링해 반환만 함 (실제 실행 안 함). 호출자가 사용자
    동의를 받은 후 별도로 실행.
    """
    tmpl = get_command_template(proposal.template_id or "")
    if tmpl is None:
        return ApplyResult(
            proposal_id=proposal.proposal_id,
            success=False,
            error_message=f"CommandTemplate not found: {proposal.template_id}",
        )
    try:
        rendered = render(tmpl.command_pattern, proposal.template_variables or {})
    except KeyError as exc:
        return ApplyResult(
            proposal_id=proposal.proposal_id,
            success=False,
            error_message=f"Template variable missing: {exc}",
        )

    return ApplyResult(
        proposal_id=proposal.proposal_id,
        success=True,
        applied_files=[],
        skipped_reason="COMMAND_TEMPLATE — execution delegated to executor module",
        dry_run=dry_run,
        details={"rendered_command": rendered, "template_id": tmpl.template_id},
    )


def _apply_contract_update(
    proposal: RemediationProposal,
    workspace: Path,
    *,
    dry_run: bool,
) -> ApplyResult:
    """CONTRACT_UPDATE — recoder.yml 의 특정 키 값 갱신.

    본 모듈은 사용자 검토를 위해 변경 사항만 반환. 실제 YAML 갱신은
    ``preflight.contract_loader.save_contract()`` 로 위임 (호출자 책임).
    """
    return ApplyResult(
        proposal_id=proposal.proposal_id,
        success=True,
        applied_files=[],
        skipped_reason="CONTRACT_UPDATE — apply via preflight.contract_loader.save_contract()",
        dry_run=dry_run,
        details={
            "target": proposal.target_path or "recoder.yml",
            "variables": proposal.template_variables,
        },
    )


def _apply_manual_only(
    proposal: RemediationProposal,
    workspace: Path,
    *,
    dry_run: bool,
) -> ApplyResult:
    """MANUAL_ONLY — 자동 적용 불가. 가이드 반환만."""
    return ApplyResult(
        proposal_id=proposal.proposal_id,
        success=True,
        applied_files=[],
        skipped_reason="MANUAL_ONLY — user must apply manually",
        dry_run=dry_run,
        details={"preview": proposal.preview},
    )


_APPLIERS = {
    RemediationApplyMethod.FILE_TEMPLATE:    _apply_file_template,
    RemediationApplyMethod.COMMAND_TEMPLATE: _apply_command_template,
    RemediationApplyMethod.CONTRACT_UPDATE:  _apply_contract_update,
    RemediationApplyMethod.MANUAL_ONLY:      _apply_manual_only,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_proposal(
    proposal: RemediationProposal,
    workspace: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> ApplyResult:
    """RemediationProposal 적용 진입점.

    Args:
        proposal:   적용할 제안
        workspace:  프로젝트 루트 (절대 경로)
        dry_run:    True 면 시뮬레이션만 (실제 변경 없음)
        force:      True 면 ``auto_apply_available=False`` 도 시도. **위험.**
                    Approval Level DOUBLE_CONFIRM 이상에서만 사용.

    Returns:
        ApplyResult — 성공 여부 + 변경 파일 + 백업 디렉토리
    """
    if not proposal.auto_apply_available and not force:
        return ApplyResult(
            proposal_id=proposal.proposal_id,
            success=False,
            skipped_reason="auto_apply_available=False — set force=True after DOUBLE_CONFIRM",
            dry_run=dry_run,
        )

    fn = _APPLIERS.get(proposal.apply_method)
    if fn is None:
        return ApplyResult(
            proposal_id=proposal.proposal_id,
            success=False,
            error_message=f"Unknown apply_method: {proposal.apply_method}",
        )
    return fn(proposal, workspace, dry_run=dry_run)
