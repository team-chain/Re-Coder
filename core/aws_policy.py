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
from dataclasses import dataclass
from typing import Literal

Target = Literal["ecs", "s3", "bedrock"]

#: 계정 ID·리전을 모를 때 정책에 남기는 자리표시자.
#: 사용자가 콘솔에서 직접 바꿔 넣을 수 있도록 눈에 띄는 형태로 둔다.
ACCOUNT_PLACEHOLDER = "<ACCOUNT_ID>"
REGION_PLACEHOLDER = "<REGION>"
#: ARN 파티션. 대부분 `aws` 지만 GovCloud 는 `aws-us-gov`, 중국은 `aws-cn` 이다.
#: **리전에서 파생되는 값**이라, 리전을 모르면 이것도 모른다.
PARTITION_PLACEHOLDER = "<PARTITION>"

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
#: ECR 리포지토리 이름 기본값. 클러스터·서비스와 **같은 성질**이다 —
#: 사용자가 이미 있는 리포지토리(`my-api` 등)에 밀어 넣으면 기본값으로는
#: 권한이 안 맞는다. 그런데 클러스터·서비스만 인자로 빼고 여기를 빠뜨렸었다.
DEFAULT_ECR_REPO = f"{RESOURCE_PREFIX}-*"

#: IAM 역할 이름에 허용되는 문자 (AWS 명세). **와일드카드는 없다.**
#: `*` 를 통과시키면 `role/*` 이 되어 계정 내 모든 역할에 PassRole 을 주게 된다.
_IAM_NAME = re.compile(r"[\w+=,.@-]{1,64}\Z")

#: ECS 클러스터·서비스 이름. 맨 끝의 `*` 하나만 접두사 와일드카드로 허용한다.
_ECS_NAME_CORE = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")


def _check_role_name(kind: str, value: str) -> str:
    """IAM 역할 지정자 확인. **경로(path)를 허용하고 와일드카드는 막는다.**

    IAM 역할에는 경로가 있을 수 있다 — `arn:aws:iam::123:role/team/EcsExec`
    처럼. 이때 정책의 Resource 는 `role/team/EcsExec` 여야 하고, 마지막
    조각(`EcsExec`)만 쓰면 **다른 ARN 을 가리켜 PassRole 이 거부된다.**
    그래서 경로째로 받고, `role_short_name()` 이 필요할 때만 끝 이름을 뗀다.

    ARN 전체(`arn:...`)는 여전히 거부한다 — 콜론이 들어간 값은 경로가 아니다.
    """
    name = (value or "").strip().strip("/")
    if not name or ":" in name or len(name) > 512:
        raise ValueError(
            f"{kind} 은 IAM 역할 이름이어야 합니다: {value!r}\n"
            f"  · ARN 이 아니라 이름만 (예: {TASK_EXECUTION_ROLE!r})\n"
            f"  · 경로가 있으면 경로째로 주세요 (예: 'team/EcsExec')"
        )
    for segment in name.split("/"):
        if not _IAM_NAME.match(segment):
            raise ValueError(
                f"{kind} 이름이 올바르지 않습니다: {value!r}\n"
                f"  · 각 조각은 IAM 이름 규칙을 따라야 합니다\n"
                f"  · 와일드카드(*)는 쓸 수 없습니다 — 계정의 모든 역할에 "
                f"PassRole 을 주게 되어 최소권한이 무너집니다"
            )
    return name


def role_short_name(role: str) -> str:
    """경로를 뗀 역할 이름. `RoleName` 인자에 넣을 값.

    `iam:GetRole(RoleName=...)` 는 **경로 없는 이름**을 받는다. 반면 정책의
    Resource ARN 은 경로를 포함해야 한다. 둘을 섞으면 한쪽이 틀린다.
    """
    return (role or "").rstrip("/").rsplit("/", 1)[-1]


#: AWS 리전 이름. `us-east-1` `ap-northeast-2` `us-gov-west-1` `cn-north-1` 형태.
#: **와일드카드가 들어가면 안 된다** — `arn:aws:ecs:*:...` 는 전 리전을 연다.
_REGION_NAME = re.compile(r"[a-z]{2}(-[a-z]+){1,2}-\d{1,2}\Z")


def _check_region(value: str) -> str:
    """리전 이름 확인. 자리표시자는 그대로 통과시킨다.

    역할·클러스터 이름은 검증하면서 리전만 빼놓았었다. `region=*` 를 주면
    `arn:aws:ecs:*:...` 가 되어 **정책이 전 리전으로 넓어지는데**,
    자리표시자가 아니라서 "채울 것 없음"으로 표시된다. 조용히 넓어진다.
    """
    name = (value or "").strip()
    if name == REGION_PLACEHOLDER or _REGION_NAME.match(name):
        return name
    raise ValueError(
        f"리전 이름이 올바르지 않습니다: {value!r}\n"
        f"  · `us-east-1` `ap-northeast-2` 같은 형태여야 합니다\n"
        f"  · 와일드카드(*)는 쓸 수 없습니다 — 정책이 전 리전으로 넓어집니다\n"
        f"  · 모르면 비워 두세요. 자리표시자({REGION_PLACEHOLDER})가 남습니다"
    )


#: AWS 계정 ID — 숫자 12자리. 다른 형태는 ARN 을 망가뜨리거나 넓힌다.
_ACCOUNT_ID = re.compile(r"\d{12}\Z")


def _check_account(value: str) -> str:
    """계정 ID 확인. 자리표시자는 통과.

    형제 입력(역할·리전·클러스터)은 다 검증하면서 **계정만 빠져 있었다.**
    `account_id="*"` 를 주면 `arn:aws:iam::*:role/...` 이 되어 **어느 계정의**
    역할이든 ECS 에 넘길 수 있게 되는데, 자리표시자가 아니라서 "채울 것 없음"
    으로 나간다. 지금은 STS 에서만 값이 오지만, 막아두지 않으면 다음에 입력
    경로가 하나 생기는 순간 뚫린다.
    """
    name = (value or "").strip()
    if name == ACCOUNT_PLACEHOLDER or _ACCOUNT_ID.match(name):
        return name
    raise ValueError(
        f"계정 ID 가 올바르지 않습니다: {value!r}\n"
        f"  · 숫자 12자리여야 합니다 (예: 123456789012)\n"
        f"  · 모르면 비워 두세요. 자리표시자({ACCOUNT_PLACEHOLDER})가 남습니다"
    )


#: ECR 리포지토리 이름 규칙 (AWS 명세). ECS 이름과 **다르다.**
#:   · 소문자만 (ECS 는 대문자 허용)
#:   · 네임스페이스 `/` 와 마침표 `.` 허용 (ECS 는 둘 다 불가)
_ECR_SEGMENT = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
_ECR_REPO_NAME = re.compile(rf"(?:{_ECR_SEGMENT}/)*{_ECR_SEGMENT}\Z")


def _check_ecr_repo(value: str) -> str:
    """ECR 리포지토리 이름 확인. 접두사 와일드카드 하나까지 허용.

    ECS 클러스터·서비스 검증기를 그대로 돌려 쓰고 있었는데, **두 문법이
    다르다.** 그래서 두 방향으로 틀렸다.

      - `team/my.api` 처럼 **정상적인** ECR 이름을 거부했다 (네임스페이스·마침표)
      - `MyRepo` 처럼 **ECR 이 거부하는** 대문자 이름을 통과시켰다

    검증기를 재사용할 때 문법이 같은지 확인하지 않은 것이 원인이다.
    """
    name = (value or "").strip()
    core = name[:-1] if name.endswith("*") else name
    # 와일드카드 앞의 구분자는 허용한다 (`recoder-*` 가 기본값이다).
    if core.endswith((".", "_", "-", "/")) and name.endswith("*"):
        core = core[:-1]
    if not core or len(name) > 256 or not _ECR_REPO_NAME.match(core):
        raise ValueError(
            f"ECR 리포지토리 이름이 올바르지 않습니다: {value!r}\n"
            f"  · 소문자·숫자와 `. _ - /` 만 쓸 수 있습니다 (대문자 불가)\n"
            f"  · 네임스페이스를 쓸 수 있습니다 (예: 'team/my.api')\n"
            f"  · 맨 끝에 접두사 와일드카드 하나까지 (예: {DEFAULT_ECR_REPO!r})"
        )
    return name


def validate_region(value: str) -> str:
    """리전 형식 확인 (공개). 호출자가 저하 처리를 하고 싶을 때 쓴다."""
    return _check_region(value)


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

    배포 경로는 ARN 전체를 넣는 관례라 `:role/` 뒤를 잘라낸다.
    **경로는 보존한다** — `role/team/EcsExec` 에서 `EcsExec` 만 남기면
    정책이 다른 ARN 을 가리켜 PassRole 이 거부된다.

    값이 이상하면 조용히 기본값으로 떨어지지 않고 **터진다** — 역할을 잘못
    지정한 채 배포가 진행되면 마지막 단계에서 더 알기 어려운 오류가 난다.
    """
    raw = (os.environ.get(env_var) or "").strip()
    if not raw:
        return None
    if ":role/" in raw:
        raw = raw.split(":role/", 1)[1]
    return _check_role_name(f"환경변수 {env_var}", raw)


def configured_execution_role() -> str:
    """배포 경로가 실제로 쓸 실행 역할 이름."""
    return role_from_env(ENV_EXECUTION_ROLE_ARN) or TASK_EXECUTION_ROLE


def configured_task_role() -> str:
    """배포 경로가 실제로 쓸 태스크 역할 이름."""
    return role_from_env(ENV_TASK_ROLE_ARN) or TASK_ROLE


def resolve_roles(execution: str = "", task: str = "") -> tuple[str, str]:
    """(실행 역할, 태스크 역할) — **정책과 배포가 함께 쓰는 단 하나의 결정 함수.**

    ## 왜 하나여야 하나

    예전에는 결정 함수가 둘이었다. 라우트에 학교 계정을 감지해 `LabRole` 로
    바꾸는 갈래가 있었고, 배포 경로는 `configured_*()` 만 봤다. 결과:

      - 정책은 `LabRole` 을 인가하고 화면도 "LabRole 을 씁니다"라고 안내
      - 배포 전 점검은 `ecsTaskExecutionRole` 을 찾다가 실패

    **이 함수가 막으려던 바로 그 불일치를, 갈래를 하나 더 만들어 스스로
    재현했다.** 그래서 기본값을 정하는 곳을 여기 하나로 못 박는다.

    ## 학교 계정은 어떻게 하나

    여기서 자동으로 `LabRole` 로 바꾸지 **않는다.** 배포 경로는 환경변수를
    보고 움직이므로, 환경변수를 안 건드린 채 정책만 바꾸면 또 갈라진다.
    학교 계정 감지는 **"환경변수를 이렇게 설정하세요"라고 안내**하는 데만
    쓴다. 그래야 정책과 배포가 같은 값을 본다.

    우선순위: **직접 지정 → 환경변수 → 기본값**
    """
    exec_name = (execution or "").strip()
    task_name = (task or "").strip()
    return (
        _check_role_name("실행 역할(task_execution_role)", exec_name)
        if exec_name else configured_execution_role(),
        _check_role_name("태스크 역할(task_role)", task_name)
        if task_name else configured_task_role(),
    )


@dataclass(frozen=True)
class ArnContext:
    """ARN 을 만드는 데 필요한 세 조각. **함께 정해지고 함께 미상이 된다.**

    ## 왜 묶어야 하나

    이 셋을 따로 두고 각각 "알면 채우고 모르면 자리표시자" 로 처리했더니,
    **말이 안 되는 조합**이 만들어졌다.

      - 리전은 `<REGION>` 인데 파티션은 `aws` → GovCloud 사용자가 `<REGION>`
        을 `us-gov-west-1` 로 바꿔도 앞의 `arn:aws:` 는 그대로다. 정책은
        완성돼 보이지만 **어떤 리소스와도 매칭되지 않는다.**

    파티션은 리전에서 **파생**되는 값이다. 독립적으로 알 수 있는 게 아니다.
    그래서 따로 두지 않고 한 자료구조로 묶어, 생성 지점을 하나로 만든다.
    `_arn()` 은 이 객체만 받는다 — 세 값을 따로 넘길 방법이 없어야 어긋나지
    않는다.
    """

    account: str
    region: str
    partition: str

    @classmethod
    def of(cls, account_id: str = "", region: str = "") -> "ArnContext":
        """알려진 값으로 채우고, 모르는 것과 **거기서 파생되는 것**을 표시한다."""
        account = _check_account((account_id or "").strip() or ACCOUNT_PLACEHOLDER)
        raw = (region or "").strip()
        if not raw or raw == REGION_PLACEHOLDER:
            # 리전을 모르면 파티션도 모른다. 여기가 핵심.
            return cls(account, REGION_PLACEHOLDER, PARTITION_PLACEHOLDER)
        checked = _check_region(raw)
        return cls(account, checked, partition_for(checked))

    @property
    def unknowns(self) -> list[str]:
        """사용자가 직접 채워야 하는 자리표시자 목록."""
        return [
            value for value in (self.account, self.region, self.partition)
            if value.startswith("<") and value.endswith(">")
        ]


def partition_for(region: str) -> str:
    """리전이 속한 ARN 파티션.

    **모든 AWS 가 `arn:aws:` 가 아니다.** 미국 정부용(GovCloud)은
    `arn:aws-us-gov:`, 중국은 `arn:aws-cn:` 을 쓴다. 파티션이 틀리면 ARN 이
    **어떤 리소스와도 매칭되지 않아** 정책을 붙여도 전부 거부된다.

    지난 라운드에 리전 검증을 넣으면서 `us-gov-west-1` `cn-north-1` 을
    허용 목록에 넣었는데, 정작 파티션은 `aws` 로 고정돼 있었다. 허용해 놓고
    동작은 안 되는 조합을 내가 만든 셈이다.
    """
    reg = (region or "").strip()
    if reg.startswith("us-gov-"):
        return "aws-us-gov"
    if reg.startswith("cn-"):
        return "aws-cn"
    # 격리(air-gapped) 리전. 리전 정규식이 이 이름들을 받아주므로 여기서도
    # 처리해야 한다 — 정규식은 통과시키는데 파티션 표에 없으면 GovCloud 때와
    # 똑같이 "완성돼 보이는데 전부 거부되는" 정책이 나온다.
    if reg.startswith("us-isob-"):
        return "aws-iso-b"
    if reg.startswith("us-iso-"):
        return "aws-iso"
    return "aws"


def _arn(service: str, resource: str, ctx: ArnContext,
         *, global_service: bool = False) -> str:
    """ARN 조립. 전역 서비스(iam 등)는 리전 칸을 비운다.

    세 조각을 따로 받지 않고 `ArnContext` 만 받는다 — 따로 넘길 수 있으면
    언젠가 어긋난 조합이 들어온다. 전역 서비스라도 **파티션은 따라간다**
    (GovCloud 계정의 IAM 역할은 `arn:aws-us-gov:iam::...` 이다).
    """
    region = "" if global_service else ctx.region
    return f"arn:{ctx.partition}:{service}:{region}:{ctx.account}:{resource}"


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
    ctx: ArnContext,
    task_execution_role: str = TASK_EXECUTION_ROLE,
    task_role: str = TASK_ROLE,
    cluster: str = DEFAULT_CLUSTER,
    service: str = DEFAULT_SERVICE,
    ecr_repo: str = DEFAULT_ECR_REPO,
) -> list[dict]:
    """컨테이너 배포 (ECS Fargate + ECR)."""
    repo = _arn("ecr", f"repository/{ecr_repo}", ctx)
    exec_role = _arn("iam", f"role/{task_execution_role}", ctx,
                     global_service=True)
    # 실행 역할과 태스크 역할이 같을 수 있다(학교 계정은 둘 다 LabRole).
    # 같으면 ARN 을 중복해 넣지 않는다 — 정책이 지저분해지고 비교가 어려워진다.
    pass_targets = [exec_role]
    if task_role != task_execution_role:
        pass_targets.append(
            _arn("iam", f"role/{task_role}", ctx, global_service=True)
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
                _arn("ecs", f"cluster/{cluster}", ctx),
                _arn("ecs", f"service/{cluster}/{service}", ctx),
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
            # AWS 연결 시점에 현재 키가 실제 배포 권한을 갖는지 미리 점검한다.
            # 이 API는 리소스 단위 제한을 지원하지 않아 *가 불가피하지만 읽기
            # 전용 시뮬레이션이며, 배포 대상 액션만 평가하도록 코드가 제한한다.
            "Sid": "SimulateDeploymentKeyPermissions",
            "Effect": "Allow",
            "Action": ["iam:SimulatePrincipalPolicy"],
            "Resource": "*",
        },
        {
            # 현재 IAM 사용자의 연결 정책과 인라인 정책을 읽는다.
            # 연결하는 사용자 이름은 정책 생성 시점에 알 수 없다. 이 IAM 읽기
            # 액션은 자원 변경 권한이 전혀 없으므로, 사용자 ARN 와일드카드 대신
            # 명시적인 읽기 전용 예외로 둔다.
            "Sid": "ReadDeploymentUserPolicies",
            "Effect": "Allow",
            "Action": [
                "iam:ListAttachedUserPolicies",
                "iam:ListUserPolicies",
                "iam:GetUserPolicy",
            ],
            "Resource": "*",
        },
        {
            # ECS에 넘길 실행·태스크 역할의 연결·인라인 정책만 읽는다.
            # role/* 를 쓰면 계정의 모든 역할 정보를 열어 최소권한이 깨진다.
            "Sid": "ReadDeploymentRolePolicies",
            "Effect": "Allow",
            "Action": [
                "iam:ListAttachedRolePolicies",
                "iam:ListRolePolicies",
                "iam:GetRolePolicy",
            ],
            "Resource": pass_targets[0] if len(pass_targets) == 1 else pass_targets,
        },
        {
            # 연결된 관리형 정책의 기본 버전을 읽어 전체 권한인지 판별한다.
            # 연결된 정책 ARN도 등록 전에는 알 수 없으며, 모두 읽기 전용이다.
            "Sid": "ReadAttachedManagedPolicyDocuments",
            "Effect": "Allow",
            "Action": [
                "iam:GetPolicy",
                "iam:GetPolicyVersion",
            ],
            "Resource": "*",
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
        # ── FR-05-04: 없는 인프라를 직접 만들어 기동시키는 데 필요한 것들 ──
        #
        # 여기부터는 `aws_infra.py` 가 부르는 액션이다. 그 모듈은 boto3
        # 클라이언트를 **인자로 받도록** 만들어져 있어서(테스트 가능성을 위해)
        # `boto3.client(...)` 대입을 찾는 정적 스캐너에는 잡히지 않는다.
        # 그래서 이 목록은 `tests/test_aws_policy.py` 의 **런타임 기록 대조**
        # (moto 로 실제 호출을 흘려보내고 Recorder 로 잡아 비교)로 지킨다.
        {
            # 클러스터를 만든다. 빈 클러스터는 요금이 없다.
            "Sid": "EcsCreateCluster",
            "Effect": "Allow",
            "Action": ["ecs:CreateCluster"],
            "Resource": _arn("ecs", f"cluster/{cluster}", ctx),
        },
        {
            # 서비스를 만들고, 태스크 수를 조절한다(0 으로 내리면 과금 정지).
            "Sid": "EcsCreateService",
            "Effect": "Allow",
            "Action": ["ecs:CreateService"],
            "Resource": _arn("ecs", f"service/{cluster}/{service}", ctx),
        },
        {
            # 기동된 태스크의 공인 IP 를 찾아 접속 URL 을 만든다 (DoD "URL 로 접속됨").
            # ListTasks 는 리소스 단위 제한을 지원하지 않아 "*" 가 강제된다.
            "Sid": "EcsFindRunningTasks",
            "Effect": "Allow",
            "Action": ["ecs:ListTasks"],
            "Resource": "*",
            "Condition": {
                "ArnEquals": {"ecs:cluster": _arn("ecs", f"cluster/{cluster}", ctx)}
            },
        },
        {
            "Sid": "EcsDescribeTasks",
            "Effect": "Allow",
            "Action": ["ecs:DescribeTasks"],
            "Resource": _arn("ecs", f"task/{cluster}/*", ctx),
        },
        {
            # 첫 CreateService 는 계정에 ECS 서비스 연결 역할이 있어야 한다.
            # 없으면 AWS 가 만들어 주는데, 그러려면 이 권한이 필요하다.
            # 조건으로 **ECS 용으로만** 만들 수 있게 좁힌다.
            "Sid": "CreateEcsServiceLinkedRole",
            "Effect": "Allow",
            "Action": ["iam:CreateServiceLinkedRole"],
            "Resource": "*",
            "Condition": {
                "StringEquals": {"iam:AWSServiceName": "ecs.amazonaws.com"}
            },
        },
        {
            # 컨테이너 로그가 갈 곳. 보관 기간을 걸어 비용 누적을 막는다.
            "Sid": "EcsLogGroup",
            "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:PutRetentionPolicy"],
            "Resource": _arn("logs", "log-group:/ecs/*", ctx),
        },
        {
            # 옛 이미지를 자동 정리한다. GB 당 월 $0.10 이라 방치하면 쌓인다.
            "Sid": "EcrLifecycle",
            "Effect": "Allow",
            "Action": ["ecr:PutLifecyclePolicy"],
            "Resource": repo,
        },
        {
            # 기본 VPC 와 인터넷으로 나가는 서브넷을 찾는다.
            # EC2 의 Describe* 는 **리소스 단위 제한을 지원하지 않는다** —
            # "*" 가 AWS 쪽 강제이지 우리가 게을러서가 아니다.
            "Sid": "DiscoverDefaultNetwork",
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeVpcs",
                "ec2:DescribeSubnets",
                "ec2:DescribeRouteTables",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeNetworkInterfaces",
            ],
            "Resource": "*",
        },
        {
            # 앱 포트를 여는 보안 그룹을 만든다.
            # CreateSecurityGroup 은 만들 그룹과 넣을 VPC 를 둘 다 요구한다.
            "Sid": "CreateAppSecurityGroup",
            "Effect": "Allow",
            "Action": [
                "ec2:CreateSecurityGroup",
                "ec2:AuthorizeSecurityGroupIngress",
            ],
            "Resource": [
                _arn("ec2", "security-group/*", ctx),
                _arn("ec2", "vpc/*", ctx),
            ],
        },
    ]


def _s3_statements(ctx: ArnContext) -> list[dict]:
    """정적 사이트 배포 (S3).

    현재 코어에는 BYO S3 업로드 경로가 아직 없다(게이트웨이가 팀 버킷에
    올린다). FR-05-03「S3 배포 BYO 전환」이 끝나면 이 권한이 쓰인다.
    미리 넣어두는 이유는, 정책을 두 번 발급받게 하면 사용자가 중간에
    막히기 때문이다.
    """
    # S3 버킷 ARN 은 리전·계정 칸이 비지만 **파티션은 따라간다.**
    bucket = f"arn:{ctx.partition}:s3:::{RESOURCE_PREFIX}-*"
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


def _bedrock_statements(ctx: ArnContext) -> list[dict]:
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
                f"arn:{ctx.partition}:bedrock:*::foundation-model/*",
                _arn("bedrock", "inference-profile/*", ctx),
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
    ecr_repo: str = DEFAULT_ECR_REPO,
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
    repo_name = _check_ecr_repo((ecr_repo or "").strip() or DEFAULT_ECR_REPO)

    ctx = ArnContext.of(account_id, region)

    statements = _sts_statements()
    # 요청 순서가 아니라 **정해진 순서**로 배치한다 — 같은 대상 조합이면
    # 항상 같은 JSON 이 나와야 사용자가 이전 것과 비교할 수 있다.
    for target in DEFAULT_TARGETS:
        if target not in selected:
            continue
        builder = _BUILDERS[target]
        if target == "ecs":
            statements.extend(
                builder(ctx, exec_name, task_name, cluster_name, service_name, repo_name)
            )
        else:
            statements.extend(builder(ctx))

    return {"Version": "2012-10-17", "Statement": statements}


#: 사용자가 직접 채워야 하는 자리표시자 전체. 새로 추가하면 여기에도 넣어야
#: `has_placeholder()` 가 알아본다 — 빠뜨리면 "채울 것 없음" 으로 잘못 안내된다.
ALL_PLACEHOLDERS = (ACCOUNT_PLACEHOLDER, REGION_PLACEHOLDER, PARTITION_PLACEHOLDER)


def has_placeholder(policy: dict) -> bool:
    """정책에 아직 사용자가 채워야 할 자리표시자가 남아 있는가."""
    text = json.dumps(policy)
    return any(ph in text for ph in ALL_PLACEHOLDERS)


def placeholders_in(policy: dict) -> list[str]:
    """남아 있는 자리표시자 목록. 안내에 그대로 쓴다."""
    text = json.dumps(policy)
    return [ph for ph in ALL_PLACEHOLDERS if ph in text]


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
