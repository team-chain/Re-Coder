"""AWS 온보딩 — 프로그램 안에서 끝내는 최소권한 역할 (역할 생성 + AssumeRole).

보드 카드 「AWS 온보딩 마찰 제거」의 두 번째 절반.

무엇을 해결하나
    첫 절반(aws_onboarding — quick-create 링크)은 사용자를 AWS 콘솔로 보낸다.
    콘솔에서 스택을 만들고 키 두 개를 복사해 붙여넣어야 한다. 보안은 좋지만
    "버튼 하나"가 아니다. 사용자 컴퓨터에 이미 자격증명(~/.aws 프로필)이
    있다면 그걸로 **역할 하나를 만들고 그 역할만 빌려 쓰면** 콘솔에 갈
    일이 없다.

흐름
    1. 사용자 프로필(또는 지금 연결된 자격증명)로 STS GetCallerIdentity.
    2. 역할 ``recoder-deploy-role`` 이 없으면 만든다. 신뢰 정책은 **호출한
       주체만** assume 할 수 있게 좁힌다. 이미 있으면 주체가 신뢰 정책에
       없을 때만 추가한다.
    3. 인라인 정책으로 aws_policy.build_policy() 결과를 붙인다 — 정책 원본은
       여전히 aws_policy 하나다. 여기서 정책을 다시 적지 않는다.
    4. sts:AssumeRole 로 임시 자격증명(기본 1시간)을 받는다. 코어는 이후
       모든 AWS 호출을 이 자격증명으로 한다.

왜 사용자가 아니라 역할인가
    quick-create 는 IAM 사용자 + 장기 액세스 키를 만든다. 키는 어딘가에
    저장돼야 한다. 역할은 장기 비밀이 없다 — 저장할 것이 역할 ARN 하나뿐이고,
    임시 자격증명은 만료된다. 관리자 자격증명은 역할을 만드는 순간에만 쓰고
    저장하지 않는다.

권한이 없으면
    프로필에 iam:CreateRole / iam:PutRolePolicy 가 없으면(학교 계정,
    권한이 좁은 IAM 사용자) RoleSetupDenied 를 낸다. 호출자는 이걸 받아
    콘솔 quick-create 폴백으로 안내한다. 즉 콘솔 경로는 없어지는 게 아니라
    "권한이 모자랄 때의 길"로 남는다.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import aws_policy

logger = logging.getLogger(__name__)

#: 만드는 역할·정책 이름. quick-create 의 사용자 이름(recoder-deploy)과 구분되게.
ROLE_NAME = "recoder-deploy-role"
ROLE_POLICY_NAME = "recoder-deploy-policy"
#: AssumeRole 세션 이름 — CloudTrail 에 이 이름으로 남는다.
SESSION_NAME = "recoder"
#: 기본 세션 길이. 역할의 MaxSessionDuration 기본값(1시간)과 같다.
#: 기반 자격증명이 역할(SSO 등)이면 역할 연쇄 제한으로 1시간을 넘길 수 없다.
DEFAULT_DURATION_SECONDS = 3600
#: 만료 이 시간 전이면 갱신한다.
REFRESH_MARGIN = timedelta(minutes=10)

#: CreateRole 직후 AssumeRole 은 IAM 전파 지연으로 몇 초간 거부될 수 있다.
_ASSUME_RETRIES = 6
_ASSUME_RETRY_DELAY = 2.0


class RoleSetupDenied(Exception):
    """기반 자격증명에 역할을 만들/빌릴 권한이 없다 — 콘솔 폴백으로."""

    def __init__(self, action: str, detail: str = "") -> None:
        self.action = action
        self.detail = detail
        super().__init__(f"{action} 권한이 거부되었습니다. {detail}".strip())


@dataclass
class TemporaryCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: datetime

    def expires_soon(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.expiration - now <= REFRESH_MARGIN

    def as_env(self) -> dict[str, str]:
        return {
            "AWS_ACCESS_KEY_ID": self.access_key_id,
            "AWS_SECRET_ACCESS_KEY": self.secret_access_key,
            "AWS_SESSION_TOKEN": self.session_token,
        }


@dataclass
class RoleSetupResult:
    role_arn: str
    role_name: str
    created: bool
    trust_updated: bool
    policy_statements: int
    principal_arn: str
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 주체 · 신뢰 정책
# ---------------------------------------------------------------------------


def principal_for_trust(identity_arn: str, account: str, partition: str = "aws") -> str:
    """STS 가 돌려준 ARN 을 신뢰 정책에 넣을 IAM 주체 ARN 으로 바꾼다.

    - IAM 사용자 / 역할 ARN 은 그대로.
    - assumed-role(SSO, 이미 역할을 쓰는 사용자)은 밑의 IAM 역할 ARN 으로.
      assumed-role ARN 자체는 세션마다 달라 신뢰 정책에 못 넣는다.
    - root 는 계정 root. (root 로 온보딩하는 걸 권하진 않지만 막지도 않는다 —
      막으면 개인 계정 사용자가 첫날에 벽을 만난다.)
    """
    arn = (identity_arn or "").strip()
    if ":iam:" in arn and (":user/" in arn or ":role/" in arn or arn.endswith(":root")):
        return arn
    marker = ":assumed-role/"
    if ":sts:" in arn and marker in arn:
        prefix, role_and_session = arn.split(marker, 1)
        role_name = role_and_session.rsplit("/", 1)[0]
        parts = prefix.split(":")
        return f"arn:{parts[1]}:iam::{parts[4]}:role/{role_name}"
    if account:
        return f"arn:{partition}:iam::{account}:root"
    raise ValueError(f"신뢰 정책에 넣을 주체를 알 수 없습니다: {identity_arn!r}")


def build_trust_policy(principals: list[str]) -> dict:
    """호출 주체만 이 역할을 assume 할 수 있는 신뢰 정책."""
    uniq: list[str] = []
    for p in principals:
        if p and p not in uniq:
            uniq.append(p)
    if not uniq:
        raise ValueError("신뢰 정책에 주체가 하나도 없습니다.")
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "RecoderCanAssume",
            "Effect": "Allow",
            "Principal": {"AWS": uniq[0] if len(uniq) == 1 else uniq},
            "Action": "sts:AssumeRole",
        }],
    }


def principals_in(trust_policy: dict) -> list[str]:
    """기존 신뢰 정책에서 AWS 주체 목록을 꺼낸다 (합칠 때 쓴다)."""
    out: list[str] = []
    statements = trust_policy.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for st in statements:
        if not isinstance(st, dict) or st.get("Effect") != "Allow":
            continue
        principal = st.get("Principal", {})
        aws = principal.get("AWS") if isinstance(principal, dict) else None
        if isinstance(aws, str):
            out.append(aws)
        elif isinstance(aws, list):
            out.extend(str(a) for a in aws)
    return out


# ---------------------------------------------------------------------------
# 권한 정책
# ---------------------------------------------------------------------------


def _self_read_statement(role_arn: str) -> dict:
    """역할이 **자기 자신의** 정책을 읽을 수 있게 한다.

    코어의 권한 점검(_inspect_deploy_permissions)은 assumed-role ARN 에서
    IAM 역할 ARN 을 복원하기 위해 GetRole 을 부르고, 붙은 정책이 과도한지
    보려고 인라인 정책을 읽는다. 자기 역할 하나로만 좁힌다 — role/* 를 열면
    최소권한이 깨진다. aws_policy 에 넣지 않는 이유: quick-create 사용자
    경로에는 역할이 없어 의미가 없다.
    """
    return {
        "Sid": "ReadOwnRolePolicies",
        "Effect": "Allow",
        "Action": [
            "iam:GetRole",
            "iam:ListRolePolicies",
            "iam:GetRolePolicy",
            "iam:ListAttachedRolePolicies",
        ],
        "Resource": role_arn,
    }


def build_role_policy(
    account: str,
    region: str,
    role_arn: str,
    targets: list[str] | None = None,
    *,
    task_execution_role: str = "",
    task_role: str = "",
    cluster: str = "",
    service: str = "",
    ecr_repo: str = "",
) -> dict:
    """역할에 붙일 인라인 정책 — aws_policy.build_policy() + 자기 읽기 한 줄."""
    exec_role, resolved_task_role = aws_policy.resolve_roles(task_execution_role, task_role)
    policy = aws_policy.build_policy(
        targets,
        account_id=account,
        region=region,
        task_execution_role=exec_role,
        task_role=resolved_task_role,
        cluster=cluster or aws_policy.DEFAULT_CLUSTER,
        service=service or aws_policy.DEFAULT_SERVICE,
        ecr_repo=ecr_repo or aws_policy.DEFAULT_ECR_REPO,
    )
    if aws_policy.has_placeholder(policy):
        raise ValueError("계정·리전을 알아야 역할 정책을 만들 수 있습니다.")
    policy["Statement"].append(_self_read_statement(role_arn))
    return policy


#: IAM 인라인 정책(역할 전체 합산) 한도. 넘으면 PutRolePolicy 가 LimitExceeded.
INLINE_POLICY_LIMIT = 10240


def policy_fits_inline(policy: dict) -> bool:
    return len(json.dumps(policy, separators=(",", ":"))) <= INLINE_POLICY_LIMIT


# ---------------------------------------------------------------------------
# 역할 만들기 · 빌리기
# ---------------------------------------------------------------------------


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def _is_denied(exc: Exception) -> bool:
    code = _error_code(exc)
    if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "NotAuthorized"}:
        return True
    msg = str(exc)
    return "AccessDenied" in msg or "not authorized" in msg.lower()


def ensure_deploy_role(
    iam,
    *,
    account: str,
    region: str,
    principal_arn: str,
    partition: str = "aws",
    role_name: str = ROLE_NAME,
    targets: list[str] | None = None,
    task_execution_role: str = "",
    task_role: str = "",
    cluster: str = "",
    service: str = "",
    ecr_repo: str = "",
) -> RoleSetupResult:
    """역할이 있으면 맞추고, 없으면 만든다. 멱등이다 — 여러 번 눌러도 같다.

    iam 은 boto3 IAM 클라이언트(기반 자격증명으로 만든 것).
    """
    role_arn = f"arn:{partition}:iam::{account}:role/{role_name}"
    created = False
    trust_updated = False
    warnings: list[str] = []

    # 1) 역할 존재 확인
    existing_trust: Optional[dict] = None
    try:
        got = iam.get_role(RoleName=role_name)
        existing_trust = got["Role"].get("AssumeRolePolicyDocument") or {}
        if isinstance(existing_trust, str):
            existing_trust = json.loads(existing_trust)
        role_arn = str(got["Role"].get("Arn") or role_arn)
    except Exception as exc:  # noqa: BLE001
        code = _error_code(exc)
        if code in {"NoSuchEntity", "NoSuchEntityException"}:
            existing_trust = None
        elif _is_denied(exc):
            raise RoleSetupDenied("iam:GetRole", str(exc)) from exc
        else:
            raise

    # 2) 없으면 만들고, 있으면 신뢰 정책에 내가 있는지 본다
    if existing_trust is None:
        trust = build_trust_policy([principal_arn])
        try:
            made = iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust),
                Description="ReCoder 가 배포에 쓰는 최소권한 역할. ReCoder 확장이 만들었습니다.",
                MaxSessionDuration=DEFAULT_DURATION_SECONDS,
                Tags=[{"Key": "ManagedBy", "Value": "recoder"}],
            )
            role_arn = str(made["Role"].get("Arn") or role_arn)
            created = True
        except Exception as exc:  # noqa: BLE001
            if _is_denied(exc):
                raise RoleSetupDenied("iam:CreateRole", str(exc)) from exc
            raise
    else:
        known = principals_in(existing_trust)
        if principal_arn not in known:
            trust = build_trust_policy(known + [principal_arn])
            try:
                iam.update_assume_role_policy(
                    RoleName=role_name, PolicyDocument=json.dumps(trust),
                )
                trust_updated = True
            except Exception as exc:  # noqa: BLE001
                if _is_denied(exc):
                    raise RoleSetupDenied("iam:UpdateAssumeRolePolicy", str(exc)) from exc
                raise

    # 3) 권한 정책 — 항상 다시 쓴다. 정책 원본(aws_policy)이 바뀌면 다음
    #    셋업에서 따라온다. 덮어쓰기라 멱등.
    policy = build_role_policy(
        account, region, role_arn, targets,
        task_execution_role=task_execution_role, task_role=task_role,
        cluster=cluster, service=service, ecr_repo=ecr_repo,
    )
    if not policy_fits_inline(policy):
        raise ValueError(
            f"역할 정책이 인라인 한도({INLINE_POLICY_LIMIT}자)를 넘습니다. "
            "aws_policy 의 문장을 나누거나 관리형 정책으로 바꿔야 합니다."
        )
    try:
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName=ROLE_POLICY_NAME,
            PolicyDocument=json.dumps(policy),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_denied(exc):
            raise RoleSetupDenied("iam:PutRolePolicy", str(exc)) from exc
        raise

    return RoleSetupResult(
        role_arn=role_arn,
        role_name=role_name,
        created=created,
        trust_updated=trust_updated,
        policy_statements=len(policy["Statement"]),
        principal_arn=principal_arn,
        warnings=warnings,
    )


def assume_deploy_role(
    sts,
    role_arn: str,
    *,
    duration_seconds: int = DEFAULT_DURATION_SECONDS,
    session_name: str = SESSION_NAME,
    retries: int = _ASSUME_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> TemporaryCredentials:
    """역할을 빌려 임시 자격증명을 받는다.

    CreateRole 직후에는 IAM 전파 지연으로 AccessDenied 가 몇 초 나올 수
    있어 재시도한다. 마지막까지 거부면 RoleSetupDenied(sts:AssumeRole).
    """
    last: Optional[Exception] = None
    for attempt in range(max(1, retries)):
        try:
            resp = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName=session_name,
                DurationSeconds=duration_seconds,
            )
            creds = resp["Credentials"]
            expiration = creds["Expiration"]
            if isinstance(expiration, str):
                expiration = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
            if expiration.tzinfo is None:
                expiration = expiration.replace(tzinfo=timezone.utc)
            return TemporaryCredentials(
                access_key_id=str(creds["AccessKeyId"]),
                secret_access_key=str(creds["SecretAccessKey"]),
                session_token=str(creds["SessionToken"]),
                expiration=expiration,
            )
        except Exception as exc:  # noqa: BLE001
            last = exc
            if _is_denied(exc) and attempt + 1 < retries:
                sleep(_ASSUME_RETRY_DELAY)
                continue
            if _is_denied(exc):
                raise RoleSetupDenied("sts:AssumeRole", str(exc)) from exc
            raise
    raise RoleSetupDenied("sts:AssumeRole", str(last) if last else "")


def role_summary(result: RoleSetupResult, creds: TemporaryCredentials) -> dict[str, Any]:
    """화면에 보여 줄 요약 — 비밀 값은 넣지 않는다."""
    return {
        "role_arn": result.role_arn,
        "role_name": result.role_name,
        "created": result.created,
        "trust_updated": result.trust_updated,
        "policy_statements": result.policy_statements,
        "principal_arn": result.principal_arn,
        "expires_at": creds.expiration.isoformat(),
        "warnings": list(result.warnings),
    }
