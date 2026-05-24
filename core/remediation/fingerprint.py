"""
Deterministic fingerprint for RemediationProposal (§32.2).

같은 (contract + blocker_code + target_path + template_id + template_variables) 입력
→ 항상 같은 fingerprint (SHA256). proposal_id 도 fingerprint 기반으로 만들면
재현 가능한 빌드 & 중복 제안 제거가 가능하다.

설계 결정:
  - JSON 직렬화 후 SHA256 (sort_keys=True, ensure_ascii=False)
  - workspace path 같은 머신 종속 값은 정규화 (project-relative)
  - 시간 정보는 fingerprint 입력에서 제외 (created_at 등)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _normalize_for_hash(value: Any) -> Any:
    """JSON serializable 로 정규화."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _normalize_for_hash(v) for k, v in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize_for_hash(v) for v in value]
    # Enum / Pydantic model 등은 .value / model_dump() 로 — 호출자 책임
    if hasattr(value, "value"):
        return _normalize_for_hash(value.value)
    if hasattr(value, "model_dump"):
        return _normalize_for_hash(value.model_dump())
    return str(value)


def compute_fingerprint(
    *,
    blocker_code: str,
    target_path: str | None,
    template_id: str | None,
    template_variables: dict[str, Any] | None = None,
    contract_hash: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """결정론적 SHA256 hash 반환.

    동일한 인자 → 항상 같은 hex digest (64 chars).
    workspace 절대 경로 같은 머신 종속 값은 ``target_path`` 에 project-relative
    값만 넣어야 한다.
    """
    payload = {
        "blocker_code": blocker_code,
        "target_path": target_path,
        "template_id": template_id,
        "template_variables": template_variables or {},
        "contract_hash": contract_hash,
        "extra": extra or {},
    }
    normalized = _normalize_for_hash(payload)
    blob = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def proposal_id_from_fingerprint(fingerprint: str) -> str:
    """fingerprint → proposal_id (rem_<8글자>)."""
    return f"rem_{fingerprint[:8]}"
