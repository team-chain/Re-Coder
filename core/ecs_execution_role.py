"""Review and create the configured ECS execution role, without changing existing roles.

Uses the current connection, never the deployment role's more privileged base profile.
No deployment, credential switching, or trust-policy update is performed here.
"""
from __future__ import annotations

import json
import re
from typing import Any


class ExecutionRoleError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.status_code = status_code
        super().__init__(message)


def error_code(exc: Exception) -> str:
    response = getattr(exc, "response", {})
    return str(response.get("Error", {}).get("Code", "AWSRequestFailed")) if isinstance(response, dict) else "AWSRequestFailed"


def _failure(action: str, exc: Exception, *, partial: str = "") -> ExecutionRoleError:
    code = error_code(exc)
    denied = code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
    advice = (
        "현재 AWS 연결에 해당 권한이 없습니다. 관리자에게 이 역할의 설정을 요청하거나 권한이 있는 연결로 다시 확인하세요. "
        "학교 계정은 제공된 LabRole 설정을 확인하세요."
        if denied else "AWS 연결과 IAM 상태를 확인한 뒤 실행 역할을 다시 확인하세요."
    )
    return ExecutionRoleError(f"{partial}{action} 실패 ({code}). {advice}", 403 if denied else 502)


def inspect_role(session: Any, region: str, role_path: str) -> dict:
    """Only STS GetCallerIdentity and IAM GetRole; missing != access denied."""
    try:
        identity = session.client("sts", region_name=region).get_caller_identity()
    except Exception as exc:
        raise _failure("sts:GetCallerIdentity", exc) from exc
    account = str(identity.get("Account", ""))
    caller = str(identity.get("Arn", ""))
    match = re.fullmatch(r"arn:(aws|aws-cn|aws-us-gov):(iam|sts)::(\d{12}):.+", caller)
    if not re.fullmatch(r"\d{12}", account) or not match or match[3] != account:
        raise ExecutionRoleError("AWS 계정과 호출자 정보를 확인하지 못했습니다.")
    partition = match[1]
    parts = role_path.split("/")
    if (not parts or len(parts[-1]) > 64 or len(role_path) > 512
            or any(part in {".", ".."} or not re.fullmatch(r"[\w+=,.@-]+", part, flags=re.ASCII) for part in parts)):
        raise ExecutionRoleError("설정된 ECS 실행 역할 이름/경로가 올바르지 않습니다.")
    name = parts[-1]
    arn = f"arn:{partition}:iam::{account}:role/{role_path}"
    policy_arn = f"arn:{partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": account}},
        }],
    }
    result = {
        "account_id": account, "caller_arn": caller, "region": region,
        "role_name": name, "role_path": role_path, "role_arn": arn,
        "policy_arn": policy_arn, "trust_policy": trust, "approval_level": 4,
        "required_actions": ["iam:CreateRole", "iam:AttachRolePolicy"],
    }
    try:
        existing = session.client("iam", region_name=region).get_role(RoleName=name)["Role"]
    except Exception as exc:
        if error_code(exc) != "NoSuchEntity":
            raise _failure("iam:GetRole", exc) from exc
        if name == "LabRole":
            raise ExecutionRoleError("학교 계정의 LabRole이 없습니다. 랩 세션과 ECS_EXECUTION_ROLE_ARN 설정을 확인하세요. LabRole은 앱에서 만들지 않습니다.")
        return {**result, "status": "missing", "message": "ECS 실행 역할이 없습니다. 아래 내용을 검토하고 승인하면 생성합니다."}
    if existing.get("Arn") != arn:
        raise ExecutionRoleError("같은 이름의 역할이 다른 경로에 있습니다. ECS_EXECUTION_ROLE_ARN 설정을 확인하세요. 기존 역할은 변경하지 않았습니다.", 409)
    return {**result, "status": "exists", "message": "실행 역할이 이미 있습니다. 기존 역할은 변경하지 않았습니다. 이 결과는 존재 여부만 확인한 것이며, 신뢰 정책이나 배포 권한의 검증 결과는 아닙니다."}


def create_reviewed_role(session: Any, reviewed: dict, configured_role: str) -> dict:
    """Recheck the identity/configuration and absence immediately before mutation."""
    if configured_role != reviewed["role_path"]:
        raise ExecutionRoleError("실행 역할 설정이 변경됐습니다. 다시 확인하고 승인하세요.", 409)
    current = inspect_role(session, reviewed["region"], configured_role)
    for key in ("account_id", "caller_arn", "role_arn", "policy_arn", "trust_policy"):
        if current[key] != reviewed[key]:
            raise ExecutionRoleError("AWS 연결 또는 생성 내용이 변경됐습니다. 다시 확인하고 승인하세요.", 409)
    if current["status"] == "exists":
        return current
    iam = session.client("iam", region_name=reviewed["region"])
    path = "/" + configured_role.rsplit("/", 1)[0] + "/" if "/" in configured_role else "/"
    try:
        iam.create_role(
            RoleName=reviewed["role_name"], Path=path,
            AssumeRolePolicyDocument=json.dumps(reviewed["trust_policy"]),
            Description="ECS execution role created with user approval in ReCoder.",
        )
    except Exception as exc:
        if error_code(exc) == "EntityAlreadyExists":
            raise ExecutionRoleError("확인하는 동안 역할이 생성됐습니다. 기존 역할은 변경하지 않았습니다. 다시 확인하세요.", 409) from exc
        raise _failure("iam:CreateRole", exc) from exc
    try:
        iam.attach_role_policy(RoleName=reviewed["role_name"], PolicyArn=reviewed["policy_arn"])
    except Exception as exc:
        command = f"aws iam attach-role-policy --role-name {reviewed['role_name']} --policy-arn {reviewed['policy_arn']}"
        raise _failure("iam:AttachRolePolicy", exc, partial=(
            f"역할 {reviewed['role_arn']} 생성은 완료됐지만 정책 연결은 완료하지 못했습니다. "
            f"생성된 역할은 유지했습니다. 관리자에게 다음 명령으로 정책 연결을 요청하세요: {command}\n"
        )) from exc
    return {**current, "status": "created", "message": "ECS 실행 역할 생성과 표준 정책 연결을 완료했습니다. IAM 반영에 잠시 시간이 걸릴 수 있습니다. 배포 권한을 다시 확인한 뒤 배포를 실행하세요."}
