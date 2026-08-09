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
import os
import re
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

#: 컨테이너 **안의 코드**가 AWS 를 부를 때 쓰는 역할. 실행 역할과 **다르다.**
#:
#: 실행 역할(execution role)은 ECS 가 이미지를 받아오고 로그를 쓸 때 쓰고,
#: 태스크 역할(task role)은 컨테이너 안 애플리케이션이 쓴다. `ecs_agent.py` 가
#: `ECS_TASK_ROLE_ARN`(기본 `ecsTaskRole`)으로 **둘을 따로** 넘기므로,
#: `RegisterTaskDefinition` 은 **두 역할 모두에 대해** `iam:PassRole` 을 요구한다.
#: 하나만 주면 배포 마지막 단계에서 AccessDenied 가 난다.
TASK_ROLE = "ecsTaskRole"

#: AWS Academy 러너랩이 미리 만들어 두는 역할. 학교 계정에서 개발할 때 쓴다.
#: 러너랩은 역할을 만들 수 없어서 실행 역할·태스크 역할 **둘 다** 이걸 쓴다.
ACADEMY_TASK_EXECUTION_ROLE = "LabRole"

#: ECS 클러스터·서비스 이름의 기본값. ReCoder 가 직접 만드는 자원의 이름 규칙이다.
#: 사용자가 **이미 있는** 클러스터(`default` 등)에 배포하면 이 기본값으로는
#: 권한이 안 맞으므로, 실제 이름을 인자로 받아 ARN 에 반영한다.
DEFAULT_CLUSTER = f"{RESOURCE_PREFIX}-*"
DEFAULT_SERVICE = f"{RESOURCE_PREFIX}-*"

#: IAM 역할 이름에 허용되는 문자 (AWS 명세). **와일드카드는 없다.**
#: `*` 를 통과시키면 `role/*` 이 되어 계정 내 모든 역할에 PassRole 을 주게 된다.
_IAM_NAME = re.compile(r"[\w+=,.@-]{1,64}\Z")

#: ECS 클러스터·서비스 이름. 맨 끝의 `*` 하나만 접두사 와일드카드로 허용한다.
_ECS_NAME_CORE = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")


def _check_role_name(kind: str, value: str) -> str:
    """IAM 역할 **이름**인지 확인. ARN 도, 와일드카드도 안 된다."""
    name = (value or "").strip()
    if not _IAM_NAME.match(name):
        raise ValueError(
            f"{kind} 은 IAM 역할 이름이어야 합니다: {value!r}\n"
            f"  · ARN 이 아니라 이름만 (예: {TASK_EXECUTION_ROLE!r})\n"
            f"  · 와일드카드(*)는 쓸 수 없습니다 — 계정의 모든 역할에 "
            f"PassRole 을 주게 되어 최소권한이 무너집니다"
        )
    return name


def _check_ecs_name(kind: str, value: str) -> str:
    """ECS 클러스터·서비스 이름 확인. 접두사 와일드카드 하나까지만 허용."""
    name = (value or "").strip()
    core = name[:-1] if name.endswith("*") else name
    if not core or not _ECS_NAME_CORE.match(core):
        raise ValueError(
            f"{kind} 이름이 올바르지 않습니다: {value!r}\n"
            f"  · 영문·숫자·하이픈·밑줄만, 맨 끝에 접두사 와일드카드 하나까지 "
            f"(예: {DEFAULT_CLUSTER!r} 또는 'my-cluster')\n"
            f"  · '*' 하나만 주는 것은 전체 허용이라 거부합니다"
        )
    return name


# ── 배포 경로와 권한표가 **같은 역할 이름**을 보게 하는 단일 출처 ────
#
# 권한표가 `LabRole` 을 인가해도 배포 코드가 `ecsTaskExecutionRole` 을 찾으면
# 소용이 없다. 앞뒤가 안 맞는 안내를 하게 된다 — 사용자는 시킨 대로 했는데
# 배포 전 점검에서 없는 역할을 찾다가 실패한다.
#
# 그래서 "지금 쓸 역할 이름"을 여기 한 곳에서 정하고, 권한표와 배포 경로가
# 둘 다 이걸 본다. 환경변수 이름은 배포 경로가 이미 쓰던 것을 그대로 쓴다.

ENV_EXECUTION_ROLE_ARN = "ECS_EXECUTION_ROLE_ARN"
ENV_TASK_ROLE_ARN = "ECS_TASK_ROLE_ARN"


def role_from_env(env_var: str) -> str | None:
    """환경변수에 설정된 역할 **이름**. 설정 안 됐으면 None.

    배포 경로는 ARN 전체를 넣는 관례라 뒤쪽 이름만 뽑는다.
    값이 이상하면 조용히 기본값으로 떨어지지 않고 **터진다** — 역할을 잘못
    지정한 채 배포가 진행되면 마지막 단계에서 더 알기 어려운 오류가 난다.
    """
    raw = (os.environ.get(env_var) or "").strip()
    if not raw:
        return None
    return _check_role_name(f"환경변수 {env_var}", raw.rsplit("/", 1)[-1])


def configured_execution_role() -> str:
    """배포 경로가 실제로 쓸 실행 역할 이름."""
    return role_from_env(ENV_EXECUTION_ROLE_ARN) or TASK_EXECUTION_ROLE


def configured_task_role() -> str:
    """배포 경로가 실제로 쓸 태스크 역할 이름."""
    return role_from_env(ENV_TASK_ROLE_ARN) or TASK_ROLE


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


def _ecs_statements(
    region: str,
    account: str,
    task_execution_role: str = TASK_EXECUTION_ROLE,
    task_role: str = TASK_ROLE,
    cluster: str = DEFAULT_CLUSTER,
    service: str = DEFAULT_SERVICE,
) -> list[dict]:
    """컨테이너 배포 (ECS Fargate + ECR)."""
    repo = _arn("ecr", f"repository/{RESOURCE_PREFIX}-*", region, account)
    exec_role = _arn("iam", f"role/{task_execution_role}", region, account,
                     global_service=True)
    # 실행 역할과 태스크 역할이 같을 수 있다(학교 계정은 둘 다 LabRole).
    # 같으면 ARN 을 중복해 넣지 않는다 — 정책이 지저분해지고 비교가 어려워진다.
    pass_targets = [exec_role]
    if task_role != task_execution_role:
        pass_targets.append(
            _arn("iam", f"role/{task_role}", region, account, global_service=True)
        )
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
            # **이미지를 내려받는** 권한. 올리는 것과 다르다.
            #
            # 배포 전 보안 스캔(trivy)과 SBOM 생성(syft)이 이미지가 로컬에
            # 없으면 ECR 에서 **끌어온다.** 파이썬 코드에는 안 보인다 —
            # 외부 실행 파일이 하는 일이라 docker push 와 같은 부류다.
            #
            # 이게 없으면 스캔이 이미지를 못 받아 실패하는데, 지금 코드는
            # 그 실패를 조용히 삼키고 **"취약점 0건 = 통과"로 보고**한다.
            # 권한 하나 빠진 것이 보안 게이트 무력화로 이어지는 경로다.
            "Sid": "EcrPullImageForScan",
            "Effect": "Allow",
            "Action": [
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
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
                _arn("ecs", f"cluster/{cluster}", region, account),
                _arn("ecs", f"service/{cluster}/{service}", region, account),
            ],
        },
        {
            # 배포 전 점검이 실행 역할 존재를 확인한다 (preflight_agent).
            "Sid": "ReadTaskExecutionRole",
            "Effect": "Allow",
            "Action": ["iam:GetRole"],
            "Resource": exec_role,
        },
        {
            # Task Definition 을 등록하려면 거기 붙는 역할마다 PassRole 이 필요하다.
            # **실행 역할과 태스크 역할은 서로 다른 역할이다** — ecs_agent 가
            # ECS_EXECUTION_ROLE_ARN 과 ECS_TASK_ROLE_ARN 을 따로 넘긴다.
            # 하나만 주면 RegisterTaskDefinition 이 거부된다.
            #
            # 조건을 걸어 **ECS 작업에 넘길 때만** 허용한다 — 이게 없으면
            # 이 키로 아무 서비스에나 역할을 넘길 수 있어 권한 상승이 된다.
            "Sid": "PassEcsRolesToEcsOnly",
            "Effect": "Allow",
            "Action": ["iam:PassRole"],
            "Resource": pass_targets[0] if len(pass_targets) == 1 else pass_targets,
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
    *,
    task_role: str = TASK_ROLE,
    cluster: str = DEFAULT_CLUSTER,
    service: str = DEFAULT_SERVICE,
) -> dict:
    """최소권한 IAM 정책 문서를 만든다.

    `account_id` / `region` 을 알면 ARN 에 채워 넣고, 모르면 자리표시자를
    남긴다. 자리표시자가 남아 있으면 사용자가 콘솔에서 직접 바꿔야 하므로
    `has_placeholder()` 로 확인해 안내에 반영한다.

    ## 이름들을 왜 인자로 받나

    정책의 값어치는 **범위를 좁히는 것**인데, 좁히려면 실제 이름을 알아야 한다.
    이름을 코드에 박아두면 두 가지로 어긋난다.

      - `task_execution_role` / `task_role` — 학교(AWS Academy) 계정은 역할을
        만들 수 없어 둘 다 `LabRole` 이다. 그리고 일반 계정에서도 이 둘은
        **서로 다른 역할**이라 PassRole 대상이 둘이다.
      - `cluster` / `service` — 사용자가 이미 있는 클러스터(`default` 등)에
        배포할 수 있다. 기본값 `recoder-*` 로 고정하면 그런 사용자는 정책을
        그대로 붙여도 배포 전 점검에서 막힌다.

    모르면 기본값(우리가 만드는 자원의 이름 규칙)을 쓴다.
    """
    selected = tuple(targets) if targets else DEFAULT_TARGETS
    unknown = [t for t in selected if t not in _BUILDERS]
    if unknown:
        raise ValueError(
            f"알 수 없는 배포 대상: {unknown}. "
            f"가능한 값: {sorted(_BUILDERS)}"
        )

    exec_name = _check_role_name(
        "실행 역할(task_execution_role)",
        (task_execution_role or "").strip() or TASK_EXECUTION_ROLE,
    )
    task_name = _check_role_name(
        "태스크 역할(task_role)", (task_role or "").strip() or TASK_ROLE
    )
    cluster_name = _check_ecs_name("클러스터", (cluster or "").strip() or DEFAULT_CLUSTER)
    service_name = _check_ecs_name("서비스", (service or "").strip() or DEFAULT_SERVICE)

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
            statements.extend(
                builder(reg, account, exec_name, task_name, cluster_name, service_name)
            )
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
    **names: str,
) -> str:
    """콘솔에 그대로 붙여넣을 수 있는 JSON 문자열.

    들여쓰기 2칸 · 키 순서 유지 · 한글 이스케이프 없음.
    """
    return json.dumps(
        build_policy(targets, account_id, region, task_execution_role, **names),
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
