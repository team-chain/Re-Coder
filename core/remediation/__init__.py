"""
ReCoder Remediation Subsystem (§32).

PreflightBlocker → RemediationProposal 결정론적 생성 + 적용.

핵심 설계 원칙 (§32.2 결정론적 동치성):
  1. 같은 (ReleaseContract + Blocker + Workspace) 입력 → 같은 fingerprint → 같은 proposal_id
  2. LLM 은 ``rationale`` (자연어 설명) 에만 사용 — 실제 file edit / command 는 Template
     Registry 의 결정론적 치환으로만 생성. (재현성, 감사 가능성 확보)
  3. 적용 전 ``base_sha256`` 으로 파일 무결성 확인 → 다른 도구가 파일을 바꿨으면 거부
  4. 자동 적용 가능한 case 와 가이드만 제공해야 하는 case 를 명시 구분 (auto_apply_available)

Public API
----------
- ``generate_proposals(preflight_run, contract, workspace) -> list[RemediationProposal]``
- ``apply_proposal(proposal, workspace, *, dry_run=False) -> ApplyResult``
- ``compute_fingerprint(...)`` — 결정론적 해시
- ``TEMPLATE_REGISTRY`` — built-in template store
"""

from __future__ import annotations

from .applier import ApplyResult, apply_proposal
from .fingerprint import compute_fingerprint
from .generator import generate_proposals, generate_proposal_for_blocker
from .registry import TEMPLATE_REGISTRY, get_command_template, get_file_template

__all__ = [
    "apply_proposal",
    "ApplyResult",
    "compute_fingerprint",
    "generate_proposals",
    "generate_proposal_for_blocker",
    "TEMPLATE_REGISTRY",
    "get_file_template",
    "get_command_template",
]
