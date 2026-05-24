"""
recoder.yml 로드 / 저장 / 해시.

ReleaseContract 의 wire 표현은 Pydantic 이지만, 실제 디스크 파일은 YAML.
Wizard 가 생성한 recoder.yml 을 Preflight 가 읽고 검사 기준으로 사용한다.

설계서 §29 / §29.4 (contract_hash 추적).

YAML 라이브러리 의존성:
    pyyaml — 이미 requirements.txt 에 있음 (uvicorn[standard] 의존성)
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

try:
    import yaml  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "pyyaml is required for contract_loader. "
        "Run: pip install pyyaml"
    ) from exc

try:
    from schemas import ContractStack, ReleaseContract
except ImportError:  # pragma: no cover
    from core.schemas import ContractStack, ReleaseContract  # type: ignore


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: 워크스페이스 루트에 두는 contract 파일 이름.
CONTRACT_FILENAME: str = "recoder.yml"

#: 추가로 인식하는 별칭 (사용자가 .yaml 로 저장한 경우).
CONTRACT_ALIASES: tuple[str, ...] = ("recoder.yml", "recoder.yaml", ".recoder.yml")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ContractError(ValueError):
    """recoder.yml 처리 중 발생한 에러 (Pydantic ValidationError 와 구분)."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def find_contract_path(workspace: Path) -> Optional[Path]:
    """워크스페이스에서 contract 파일을 찾는다.

    우선순위: recoder.yml > recoder.yaml > .recoder.yml.

    Returns:
        존재하면 절대 경로, 없으면 None.
    """
    for name in CONTRACT_ALIASES:
        candidate = workspace / name
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def load_contract_from_path(path: Path) -> ReleaseContract:
    """recoder.yml 파일을 읽어 ReleaseContract 로 변환.

    Raises:
        ContractError: 파일 없음 / YAML 파싱 실패 / Pydantic validation 실패.
    """
    if not path.exists():
        raise ContractError(f"Contract file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"Failed to read contract file: {exc}") from exc

    try:
        data = yaml.safe_load(raw)  # safe_load — 임의 Python 객체 deserialize 금지
    except yaml.YAMLError as exc:
        raise ContractError(f"Invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ContractError(
            f"recoder.yml 의 최상위는 dict 여야 합니다. 받은 타입: {type(data).__name__}"
        )

    # Pydantic validation
    try:
        contract = ReleaseContract.model_validate(data)
    except Exception as exc:  # ValidationError를 포함한 광범위 catch
        raise ContractError(f"Contract schema validation failed: {exc}") from exc

    # contract_hash 자동 계산 — 사용자가 직접 채우지 않아도 자동 부여
    if not contract.contract_hash:
        contract.contract_hash = compute_contract_hash(contract)

    return contract


def load_contract(workspace: Path) -> Optional[ReleaseContract]:
    """워크스페이스에서 contract 자동 탐색 + 로드. 없으면 None."""
    path = find_contract_path(workspace)
    if path is None:
        return None
    return load_contract_from_path(path)


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def save_contract(contract: ReleaseContract, workspace: Path) -> Path:
    """ReleaseContract 를 workspace/recoder.yml 로 저장.

    저장 직전에 contract_hash 를 재계산 + updated_at 갱신.

    Returns:
        저장된 파일 절대 경로.
    """
    # hash 재계산 (다른 필드 변경 반영)
    # to_dict 는 BaseModel patch 로 model_dump(mode="json") 동작.
    contract.contract_hash = None  # 해시 계산 전 비우기 (자기 자신 영향 제거)
    payload = contract.to_dict()
    contract.contract_hash = compute_contract_hash_from_dict(payload)
    payload["contract_hash"] = contract.contract_hash

    # updated_at 갱신
    from datetime import datetime, timezone
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    out_path = workspace / CONTRACT_FILENAME
    yaml_text = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    out_path.write_text(yaml_text, encoding="utf-8")
    log.info("ReleaseContract saved: %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Hashing — contract_hash (§29.4)
# ---------------------------------------------------------------------------


def compute_contract_hash(contract: ReleaseContract) -> str:
    """ReleaseContract 의 SHA256 hex digest.

    DeploymentLedger.contract_hash 와 매칭되어 "이 배포가 어떤 contract 로
    검증됐는지" 추적된다.

    Hash 계산 시 contract_hash / created_at / updated_at 은 제외한다 (자기 참조 +
    시간 의존성 제거).
    """
    data = contract.to_dict()
    return compute_contract_hash_from_dict(data)


def compute_contract_hash_from_dict(data: dict[str, Any]) -> str:
    """dict 직렬화 결과의 SHA256.

    내부적으로 ``compute_contract_hash`` 가 사용. 메타 필드 (해시 자체 / 시간)는
    제외한다.
    """
    purified = {
        k: v
        for k, v in data.items()
        if k not in {"contract_hash", "created_at", "updated_at"}
    }
    # 키 정렬 → 동일 내용이면 항상 같은 해시
    canonical = json.dumps(purified, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Convenience — 빈 contract 템플릿 생성 (Wizard 자동 추정 결과)
# ---------------------------------------------------------------------------


def build_default_contract(stack: ContractStack) -> ReleaseContract:
    """스택만 정해진 상태에서 합리적 기본값으로 ReleaseContract 생성.

    First Run Wizard 가 프로젝트 스캔 후 첫 번째로 호출. 이후 5개 질문을
    통해 사용자가 일부 필드를 덮어쓴다.
    """
    from schemas import ContractProjectMeta

    default_ports: dict[ContractStack, int] = {
        ContractStack.PYTHON_FASTAPI: 8000,
        ContractStack.PYTHON_FLASK:   5000,
        ContractStack.NODE_EXPRESS:   3000,
        ContractStack.NODE_NEXT:      3000,
        ContractStack.CUSTOM:         8080,
    }
    port = default_ports.get(stack, 8080)

    contract = ReleaseContract(
        project=ContractProjectMeta(stack=stack, name=None),
    )
    # runtime 기본값 덮어쓰기 (스택별)
    contract.runtime.app_port = port
    contract.runtime.host_port = port

    return contract
