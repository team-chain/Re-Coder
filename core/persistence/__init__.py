"""
ReCoder 3-Layer Persistence (§33).

3개 Pydantic 모델을 SQLite 에 영속화:
    Layer 1: PreflightRun         (검사 결과)
    Layer 2: RemediationRun       (제안 적용 이력)
    Layer 3: DeploymentLedger     (배포 감사 추적, append-only)

설계 결정:
  - 단일 SQLite 파일 (``<workspace>/.recoder/recoder.db``)
  - WAL 모드 + foreign keys ON + journal_mode=WAL
  - Pydantic model 을 JSON 으로 ``payload`` TEXT 컬럼에 직렬화 (forward-compat)
  - 동시에 자주 쿼리되는 필드는 인덱싱된 컬럼으로 추출
  - datetime → ISO-8601 UTC 문자열 (SQLite native datetime 미지원)
  - DeploymentLedger 는 INSERT/UPDATE 만 허용. DELETE 는 별도 ``purge_all()`` 만.

Public API
----------
- ``RecoderDB(db_path)``                        — connection manager
- ``preflight_store.save / load / list``        — Layer 1
- ``remediation_store.save / load / list``      — Layer 2
- ``ledger_store.save / update_status / list``  — Layer 3
"""

from __future__ import annotations

from .db import RecoderDB, get_default_db_path
from .ledger_store import (
    list_deployments,
    load_deployment,
    save_deployment,
    update_deployment_status,
)
from .preflight_store import (
    list_preflight_runs,
    load_preflight_run,
    save_preflight_run,
)
from .remediation_store import (
    list_remediation_runs,
    load_remediation_run,
    save_remediation_run,
)

__all__ = [
    "RecoderDB",
    "get_default_db_path",
    # preflight (Layer 1)
    "save_preflight_run",
    "load_preflight_run",
    "list_preflight_runs",
    # remediation (Layer 2)
    "save_remediation_run",
    "load_remediation_run",
    "list_remediation_runs",
    # ledger (Layer 3)
    "save_deployment",
    "load_deployment",
    "list_deployments",
    "update_deployment_status",
]
