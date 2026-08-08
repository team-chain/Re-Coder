"""
aws_policy.py — ReCoder 가 요구하는 **최소권한 IAM 정책** 생성 (FR-04-02 · ADR-D10/D12).

사용자는 자기 AWS 계정에 키를 만들어 ReCoder 에 연결한다(BYO). 이때
"어떤 권한을 줘야 하는가"를 알려주지 않으면 둘 중 하나가 된다.

  - 몰라서 `AdministratorAccess` 를 붙인다 → 유출 시 계정 전체가 털린다
  - 적당히 줬다가 배포 도중 권한 부족으로 실패한다 → 원인 파악도 어렵다

그래서 **코드가 실제로 호출하는 API 만** 담은 정책을 만들어 그대로 복사해
쓰게 한다. 이 파일이 그 정답표의 단일 출처다.

## 이 목록의 근거

각 Statement 의 `Sid` 와 주석에 **어느 코드가 그 권한을 쓰는지** 적어 둔다.
권한을 늘리거나 줄일 때 근거 없이 손대지 않기 위해서다. 코드에서 호출이
사라지면 여기서도 빠져야 하고, 새 호출이 생기면 여기 먼저 추가해야 한다.

## 주의 — 코드 grep 만으로는 안 보이는 권한이 있다

  - **docker push**: 파이썬이 아니라 docker CLI 가 ECR 에 올린다. 레이어
    업로드 액션(`InitiateLayerUpload` 등)은 코드에 안 나오지만 반드시 필요하다.
  - **iam:PassRole**: `register_task_definition` 에 실행 역할 ARN 을 넘기므로
    필요하다. 없으면 배포 마지막 단계에서야 터진다.
"""

from __future__ import annotations

import json
from typing import Literal

Target = Literal["ecs", "s3", "bedrock"]

#: 계정 ID·리전을 모를 때 정책에 남기는 자리표시자.
#: 사용자가 콘솔에서 직접 바꿔 넣을 수 있도록 눈에 띄는 형태로 둔다.
ACCOUNT_PLACEHOLDER = "<ACCOUNT_ID>"
REGION_PLACEHOLDER = "<REGION>"

#: ECR 리포지토리·S3 버킷 이름 접두사. 권한을 이 접두사로 좁혀
#: 사용자의 다른 리포지토리·버킷에는 손대지 못하게 한다.
RESOURCE_PREFIX = "recoder"

#: ECS 작업이 이미지를 받아올 때 쓰는 실행 역할 이름 (AWS 표준).
#:
#: **계정 종류에 따라 다르다.** 일반 계정은 `ecsTaskExecutionRole` 을 직접
#: 만들지만, AWS Academy 러너랩은 IAM 역할 생성이 막혀 있고 대신 미리 만들어진
#: `LabRole` 을 준다. 그래서 이름을 고정하지 않고 인자로 받는다.
#: (러너랩 `LabRole` 의 신뢰 정책에 `ecs-tasks.amazonaws.com` 이 들어 있는 것을
#: 실제 계정에서 확인했다 — 그대로 실행 역할로 쓸 수 있다.)
TASK_EXECUTION_ROLE = "ecsTaskExecutionRole"

#: AWS Academy 러너랩이 미리 만들어 두는 역할. 학교 계정에서 개발할 때 쓴다.
ACADEMY_TASK_EXECUTION_ROLE = "LabRole"


def _arn(service: str, resource: str, region: str, account: str,
         *, global_service: bool = False) -> str:
    """ARN 조립. 전역 서비스(iam 등)는 리전 칸을 비운다."""
    return f"arn:aws:{service}:{'' if global_service else region}:{account}:{resource}"


def _sts_statements() -> list[dict]:
    """자격증명 확인 — `sts.get_caller_identity()`

    `api/routes/aws.py`, `ecs_deploy_agent.py`, `first_run.py` 가 연결 확인에 쓴다.
    계정 단위 조회라 리소스를 좁힐 수 없다.
    """
    return [{
        "Sid": "WhoAmI",
        "Effect": "Allow",
        "Action": ["sts:GetCallerIdentity"],
        "Resource": "*",
    }]


def _ecs_statements(region: str, account: str,
                    task_execution_role: str = TASK_EXECUTION_ROLE) -> list[dict]:
    """컨테이너 배포 (ECS Fargate + ECR)."""
    repo = _arn("ecr", f"repository/{RESOURCE_PREFIX}-*", region, account)
    role = _arn("iam", f"role/{task_execution_role}", region, account,
                global_service=True)
    return [
        {
            # ECR 로그인 토큰은 계정 단위 발급이라 리소스를 좁힐 수 없다.
            # `aws ecr get-login-password` (ecs_deploy_agent.build_and_push)
            "Sid": "EcrLogin",
            "Effect": "Allow",
            "Action": ["ecr:GetAuthorizationToken"],
            "Resource": "*",
        },
        {
            # 리포지토리 생성·조회 + docker push 가 쓰는 레이어 업로드 액션.
            # 레이어 액션은 파이썬 코드에 안 보인다 — docker CLI 가 호출한다.
            "Sid": "EcrPushImage",
            "Effect": "Allow",
            "Action": [
                "ecr:CreateRepository",
                "ecr:DescribeRepositories",
                "ecr:BatchCheckLayerAvailability",
                "ecr:InitiateLayerUpload",
                "ecr:UploadLayerPart",
                "ecr:CompleteLayerUpload",
                "ecr:PutImage",
            ],
            "Resource": repo,
        },
        {
            # RegisterTaskDefinition·DescribeTaskDefinition 은 AWS 가
            # 리소스 단위 제한을 지원하지 않는다 — "*" 가 강제된다.
            "Sid": "EcsTaskDefinition",
            "Effect": "Allow",
            "Action": [
                "ecs:RegisterTaskDefinition",
                "ecs:DescribeTaskDefinition",
            ],
            "Resource": "*",
        },
        {
            # 클러스터·서비스 조회 및 롤링 업데이트.
            # (ecs_deploy_agent / agents.ecs_agent / preflight_agent)
            "Sid": "EcsDeployService",
            "Effect": "Allow",
            "Action": [
                "ecs:DescribeClusters",
                "ecs:DescribeServices",
                "ecs:UpdateService",
            ],
            "Resource": [
                _arn("ecs", f"cluster/{RESOURCE_PREFIX}-*", region, account),
                _arn("ecs", f"service/{RESOURCE_PREFIX}-*/*", region, account),
            ],
        },
        {
            # 배포 전 점검이 실행 역할 존재를 확인한다 (preflight_agent).
            "Sid": "ReadTaskExecutionRole",
            "Effect": "Allow",
            "Action": ["iam:GetRole"],
            "Resource": role,
        },
        {
            # Task Definition 에 실행 역할을 붙이려면 PassRole 이 필요하다.
            # 조건을 걸어 **ECS 작업에 넘길 때만** 허용한다 — 이게 없으면
            # 이 키로 아무 서비스에나 역할을 넘길 수 있어 권한 상승이 된다.
            "Sid": "PassTaskExecutionRoleToEcsOnly",
            "Effect": "Allow",
            "Action": ["iam:PassRole"],
            "Resource": role,
            "Condition": {
                "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
            },
        },
        {
            # 배포 전 점검이 로그 그룹 존재를 확인한다 (preflight_agent).
            # DescribeLogGroups 는 리소스 단위 제한을 지원하지 않는다.
            "Sid": "ReadLogGroups",
            "Effect": "Allow",
            "Action": ["logs:DescribeLogGroups"],
            "Resource": "*",
        },
    ]


def _s3_statements(region: str, account: str) -> list[dict]:
    """정적 사이트 배포 (S3).

    현재 코어에는 BYO S3 업로드 경로가 아직 없다(게이트웨이가 팀 버킷에
    올린다). FR-05-03「S3 배포 BYO 전환」이 끝나면 이 권한이 쓰인다.
    미리 넣어두는 이유는, 정책을 두 번 발급받게 하면 사용자가 중간에
    막히기 때문이다.
    """
    bucket = f"arn:aws:s3:::{RESOURCE_PREFIX}-*"
    return [
        {
            "Sid": "S3StaticSiteBucket",
            "Effect": "Allow",
            "Action": [
                "s3:CreateBucket",
                "s3:ListBucket",
                "s3:GetBucketLocation",
                "s3:PutBucketWebsite",
                "s3:PutBucketPolicy",
            ],
            "Resource": bucket,
        },
        {
            "Sid": "S3StaticSiteObjects",
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
            "Resource": f"{bucket}/*",
        },
    ]


def _bedrock_statements(region: str, account: str) -> list[dict]:
    """AI 호출 (Bedrock).

    `llm/bedrock_provider.py` 가 `converse(...)` 를 쓴다.

    **이름에 속으면 안 된다.** Converse 는 `bedrock:Converse` 가 아니라
    **`bedrock:InvokeModel`** 로 인가된다. AWS 문서 원문: *"This operation
    requires permission for the bedrock:InvokeModel action."*
    https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html

    그래서 `InvokeModel` 하나만 준다. 스트리밍(`ConverseStream`)은 코드에
    호출이 없어 빼놨다 — 쓰기 시작하면 대조 테스트가 알려준다.

    리소스를 두 개 주는 이유: 교차 리전 추론 프로파일을 쓰면 프로파일 ARN 과
    그 뒤의 파운데이션 모델 ARN **둘 다** 인가가 필요하다.

    `first_run.py` 의 사용 가능 모델 조회에 `ListFoundationModels` 가 필요하다.
    """
    return [
        {
            "Sid": "InvokeFoundationModels",
            "Effect": "Allow",
            "Action": ["bedrock:InvokeModel"],
            "Resource": [
                "arn:aws:bedrock:*::foundation-model/*",
                _arn("bedrock", "inference-profile/*", region, account),
            ],
        },
        {
            # 사용 가능한 모델 목록 조회 — 계정 단위라 리소스를 좁힐 수 없다.
            "Sid": "ListModels",
            "Effect": "Allow",
            "Action": ["bedrock:ListFoundationModels"],
            "Resource": "*",
        },
    ]


_BUILDERS = {
    "ecs": _ecs_statements,
    "s3": _s3_statements,
    "bedrock": _bedrock_statements,
}

#: 대상을 지정하지 않았을 때 발급하는 기본 조합.
DEFAULT_TARGETS: tuple[Target, ...] = ("ecs", "s3", "bedrock")


def build_policy(
    targets: list[str] | tuple[str, ...] | None = None,
    account_id: str = "",
    region: str = "",
    task_execution_role: str = TASK_EXECUTION_ROLE,
) -> dict:
    """최소권한 IAM 정책 문서를 만든다.

    `account_id` / `region` 을 알면 ARN 에 채워 넣고, 모르면 자리표시자를
    남긴다. 자리표시자가 남아 있으면 사용자가 콘솔에서 직접 바꿔야 하므로
    `has_placeholder()` 로 확인해 안내에 반영한다.

    `task_execution_role` 은 ECS 작업이 이미지를 받아올 때 쓰는 역할 이름이다.
    학교(AWS Academy) 계정에서는 역할을 만들 수 없으므로
    `ACADEMY_TASK_EXECUTION_ROLE`("LabRole")을 넘긴다.
    """
    selected = tuple(targets) if targets else DEFAULT_TARGETS
    unknown = [t for t in selected if t not in _BUILDERS]
    if unknown:
        raise ValueError(
            f"알 수 없는 배포 대상: {unknown}. "
            f"가능한 값: {sorted(_BUILDERS)}"
        )

    role = (task_execution_role or "").strip() or TASK_EXECUTION_ROLE
    if "/" in role or ":" in role:
        # ARN 전체를 넘기는 실수를 막는다 — 여기 필요한 건 **이름**이다.
        raise ValueError(
            f"실행 역할은 ARN 이 아니라 이름이어야 합니다: {task_execution_role!r} "
            f"(예: {TASK_EXECUTION_ROLE!r} 또는 {ACADEMY_TASK_EXECUTION_ROLE!r})"
        )

    account = (account_id or "").strip() or ACCOUNT_PLACEHOLDER
    reg = (region or "").strip() or REGION_PLACEHOLDER

    statements = _sts_statements()
    # 요청 순서가 아니라 **정해진 순서**로 배치한다 — 같은 대상 조합이면
    # 항상 같은 JSON 이 나와야 사용자가 이전 것과 비교할 수 있다.
    for target in DEFAULT_TARGETS:
        if target not in selected:
            continue
        builder = _BUILDERS[target]
        if target == "ecs":
            statements.extend(builder(reg, account, role))
        else:
            statements.extend(builder(reg, account))

    return {"Version": "2012-10-17", "Statement": statements}


def has_placeholder(policy: dict) -> bool:
    """정책에 아직 사용자가 채워야 할 자리표시자가 남아 있는가."""
    text = json.dumps(policy)
    return ACCOUNT_PLACEHOLDER in text or REGION_PLACEHOLDER in text


def policy_json(
    targets: list[str] | tuple[str, ...] | None = None,
    account_id: str = "",
    region: str = "",
    task_execution_role: str = TASK_EXECUTION_ROLE,
) -> str:
    """콘솔에 그대로 붙여넣을 수 있는 JSON 문자열.

    들여쓰기 2칸 · 키 순서 유지 · 한글 이스케이프 없음.
    """
    return json.dumps(
        build_policy(targets, account_id, region, task_execution_role),
        indent=2,
        ensure_ascii=False,
    )


def used_actions(policy: dict) -> list[str]:
    """정책이 허용하는 액션 전체 (중복 제거·정렬). 검증·문서화용."""
    actions: set[str] = set()
    for stmt in policy.get("Statement", []):
        value = stmt.get("Action", [])
        actions.update([value] if isinstance(value, str) else value)
    return sorted(actions)
