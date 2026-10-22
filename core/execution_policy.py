"""Execution checks for grounded repair and the MCP analyzer, with opt-in strict OPA."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

try:
    from context_gate import mask_secrets
    from opa_gate import OPADecision, OPAGate
except ImportError:  # package entry point: python -m core.mcp_server
    from core.context_gate import mask_secrets
    from core.opa_gate import OPADecision, OPAGate

POLICY_VERSION = "execution-v1"
POLICY_PATH = "recoder/execution/allow"
ACTIONS = {
    "repair.inspect",
    "repair.generate",
    "repair.verify",
    "repair.edit",
    "repair.rule_fix",
    "repair.apply",
    "mcp.call",
}


class ExecutionDeniedError(ValueError):
    def __init__(self, record: dict):
        self.record = record
        super().__init__("실행 정책으로 차단됐습니다: " + record["reason"])


class ExecutionPolicy:
    """OPA can restrict local permissions, but cannot bypass local safety checks."""

    def __init__(self, mode: str | None = None, gate=None):
        self.mode = (
            (mode if mode is not None else os.getenv("RECODER_EXECUTION_POLICY", "local"))
            .strip()
            .lower()
        )
        if self.mode not in {"local", "opa"}:
            raise ValueError("RECODER_EXECUTION_POLICY must be local or opa")
        self.gate = gate or (OPAGate() if self.mode == "opa" else None)
        endpoint = getattr(self.gate, "_url", os.getenv("OPA_URL", "http://localhost:8181"))
        self.profile = {
            "mode": self.mode,
            "version": POLICY_VERSION,
            "endpoint_fingerprint": (
                hashlib.sha256(endpoint.encode()).hexdigest() if self.mode == "opa" else ""
            ),
        }

    def authorize(
        self,
        action: str,
        workspace: str | Path,
        *,
        paths=(),
        snapshot=False,
        verified=False,
        approved=False,
        tool="",
        stage="build",
        rule_code="",
    ) -> dict:
        """Call only with Core-owned facts; never unpack a model proposal into these flags."""
        record = {
            "action": action,
            "allowed": False,
            "decision": "deny",
            "reason": "",
            "policy_version": POLICY_VERSION,
            "mode": self.mode,
        }
        reason = ""
        try:
            if not isinstance(workspace, (str, Path)) or not str(workspace).strip():
                raise ValueError("작업 폴더가 필요합니다.")
            root = Path(workspace).expanduser().resolve()
            if not root.is_dir():
                raise ValueError("작업 폴더를 찾을 수 없습니다.")
            if action not in ACTIONS:
                raise ValueError("허용되지 않은 실행 작업입니다.")
            if stage not in {"build", "run", "ecs", "iam", "s3"}:
                raise ValueError("알 수 없는 검증 단계입니다.")
            if (
                action
                in {
                    "repair.inspect",
                    "repair.generate",
                    "repair.verify",
                    "repair.edit",
                    "repair.rule_fix",
                }
                and snapshot is not True
            ):
                raise ValueError("수정 준비와 검증은 복사본에서만 실행할 수 있습니다.")
            if action == "repair.apply" and not (
                verified is True and approved is True and stage == "build"
            ):
                raise ValueError("검증된 빌드 수정안과 명시적 승인이 필요합니다.")
            if action == "mcp.call" and tool != "recoder_analyze":
                raise ValueError("허용되지 않은 MCP 도구입니다.")
            if action == "repair.rule_fix" and not rule_code:
                raise ValueError("확인된 수정 규칙이 필요합니다.")
            if action in {"repair.edit", "repair.apply"}:
                from grounded_repair.workspace import safe_path
                from llm.task_tier import is_test_file

                if not paths or len(paths) > 4000 or len(set(paths)) != len(paths):
                    raise ValueError("수정할 파일 목록이 올바르지 않습니다.")
                for name in paths:
                    if not isinstance(name, str) or is_test_file(name):
                        raise ValueError("테스트 파일은 수정 근거로만 사용할 수 있습니다.")
                    if not safe_path(root, name).is_file():
                        raise ValueError("기존 작업 파일만 수정할 수 있습니다.")
        except (ValueError, OSError, TypeError) as exc:
            reason = mask_secrets(str(exc) or "실행 요청을 확인할 수 없습니다.")[:1000]
        if reason:
            record["reason"] = reason
            raise ExecutionDeniedError(record)
        facts = {
            "schema_version": 1,
            "action": action,
            "workspace_id": hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest(),
            "local_allowed": True,
            "snapshot": snapshot is True,
            "verified": verified is True,
            "human_approved": approved is True,
            "paths": sorted(paths),
            "tool": tool,
            "stage": stage,
            "rule_code": rule_code,
        }
        if self.mode == "local":
            record.update(allowed=True, decision="allow", reason="로컬 실행 제한을 통과했습니다.")
        else:
            try:
                verdict = self.gate.evaluate(POLICY_PATH, facts, approval_level=3)
                # allow_with_approval is not executable permission. The supported approval
                # endpoint must re-query OPA and receive an explicit allow.
                valid_version = isinstance(verdict.policy_version, str) and bool(
                    verdict.policy_version.strip()
                )
                record.update(
                    allowed=verdict.decision == OPADecision.ALLOW and valid_version,
                    decision=verdict.decision.value,
                    reason=mask_secrets(
                        str(verdict.reason or "정책에서 명시적 실행 허용을 받지 못했습니다.")
                    )[:1000],
                    policy_version=verdict.policy_version if valid_version else "unknown",
                )
                if verdict.decision == OPADecision.ALLOW and not valid_version:
                    record.update(decision="deny", reason="OPA 응답에 정책 버전이 없습니다.")
            except Exception:
                record.update(
                    allowed=False,
                    decision="fail_closed",
                    reason="OPA 정책 응답을 확인할 수 없습니다. 정책 서버를 확인하세요.",
                )
        if not record["allowed"]:
            raise ExecutionDeniedError(record)
        return record
