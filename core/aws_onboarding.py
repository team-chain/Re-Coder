"""AWS 온보딩 원클릭 IAM 셋업 — quick-create 링크와 CloudFormation 템플릿.

보드 카드 「AWS 온보딩 마찰 제거 — 원클릭 IAM 셋업 링크」.

무엇을 해결하나
    지금 온보딩은 "IAM 콘솔에서 정책 만들기 → JSON 붙여넣기 → 사용자 만들기
    → 정책 연결 → 키 발급 → 복사" 여섯 단계다. 배포 경험이 적은 사용자가
    가장 많이 이탈하는 지점이다. CloudFormation quick-create 링크는 이걸
    "링크 클릭 → 스택 생성 → 키 복사" 세 단계로 줄인다.

설계
    - 템플릿은 **정적 하나**다. 계정 ID·리전·파티션은 CloudFormation 내장
      변수(${AWS::AccountId} 등)가 스택 생성 시점에 채우므로, 사용자별로
      템플릿을 만들 필요가 없다. aws_policy.build_policy() 가 남기는
      자리표시자를 Fn::Sub 변수로 바꿔 넣는 것이 이 모듈의 핵심이다.
    - 정책 내용의 **원본은 aws_policy 하나**다. 여기서 정책을 다시 적지
      않는다 — 두 벌이 되는 순간 어긋난다. 드리프트는 테스트가 잡는다.
    - quick-create 는 템플릿이 S3 에 호스팅돼 있어야 한다(templateURL 은
      S3 URL 만 허용). 호스팅 전에는 콘솔 업로드 플로우로 폴백한다.
      호스팅 방법은 infra/README.md 에 있다.

보안 주의
    AccessKey 의 SecretAccessKey 를 스택 Outputs 로 내보낸다. Outputs 는
    스택을 볼 수 있는 사람에게 보인다 — 개인 계정 온보딩용으로는 표준적인
    절충이지만, 조직 계정이라면 키 발급을 콘솔에서 따로 하라고 안내문에
    적는다(Outputs 의 GuidanceKo 참고).
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote

import aws_policy

#: quick-create 로 만드는 스택·자원 이름. 바꾸면 안내문·테스트도 같이.
STACK_NAME = "recoder-iam-setup"
USER_NAME = "recoder-deploy"
POLICY_NAME = "recoder-deploy-policy"

#: 팀이 템플릿을 S3 에 올린 뒤 코어 .env 에 넣는 변수.
ENV_TEMPLATE_URL = "RECODER_IAM_TEMPLATE_URL"

#: aws_policy 자리표시자 → CloudFormation 내장 변수.
_SUB_MAP = {
    aws_policy.ACCOUNT_PLACEHOLDER: "${AWS::AccountId}",
    aws_policy.REGION_PLACEHOLDER: "${AWS::Region}",
    aws_policy.PARTITION_PLACEHOLDER: "${AWS::Partition}",
}


def _to_cfn_value(value: Any) -> Any:
    """정책 문서 안의 문자열 자리표시자를 Fn::Sub 로 바꾼다.

    ${AWS::...} 가 들어간 문자열을 그냥 두면 CloudFormation 이 리터럴로
    취급한다 — 반드시 Fn::Sub 로 감싸야 스택 생성 시점에 치환된다.
    """
    if isinstance(value, str):
        replaced = value
        for placeholder, sub in _SUB_MAP.items():
            replaced = replaced.replace(placeholder, sub)
        if "${AWS::" in replaced:
            return {"Fn::Sub": replaced}
        return replaced
    if isinstance(value, list):
        return [_to_cfn_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_cfn_value(v) for k, v in value.items()}
    return value


def build_quickcreate_template(
    targets: list[str] | None = None,
    *,
    task_execution_role: str = "",
    task_role: str = "",
    cluster: str = "",
    service: str = "",
    ecr_repo: str = "",
) -> dict:
    """IAM 사용자 + 최소권한 정책 + 액세스 키를 만드는 CloudFormation 템플릿."""
    exec_role, resolved_task_role = aws_policy.resolve_roles(
        task_execution_role, task_role
    )
    policy = aws_policy.build_policy(
        targets,
        account_id="",   # 스택 생성 시점에 ${AWS::AccountId} 로 채워진다
        region="",
        task_execution_role=exec_role,
        task_role=resolved_task_role,
        cluster=cluster or aws_policy.DEFAULT_CLUSTER,
        service=service or aws_policy.DEFAULT_SERVICE,
        ecr_repo=ecr_repo or aws_policy.DEFAULT_ECR_REPO,
    )
    policy_document = _to_cfn_value(policy)

    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "ReCoder 배포용 최소권한 IAM 사용자를 만듭니다. "
            "스택 생성 후 Outputs 탭의 액세스 키를 ReCoder 확장의 "
            "AWS 연결 화면에 붙여넣으세요."
        ),
        "Resources": {
            "RecoderDeployPolicy": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {
                    "ManagedPolicyName": POLICY_NAME,
                    "Description": "ReCoder 가 배포에 요구하는 최소권한",
                    "PolicyDocument": policy_document,
                },
            },
            "RecoderDeployUser": {
                "Type": "AWS::IAM::User",
                "Properties": {
                    "UserName": USER_NAME,
                    "ManagedPolicyArns": [{"Ref": "RecoderDeployPolicy"}],
                },
            },
            "RecoderDeployKey": {
                "Type": "AWS::IAM::AccessKey",
                "Properties": {"UserName": {"Ref": "RecoderDeployUser"}},
            },
        },
        "Outputs": {
            "AccessKeyId": {
                "Description": "ReCoder 에 붙여넣을 Access Key ID",
                "Value": {"Ref": "RecoderDeployKey"},
            },
            "SecretAccessKey": {
                "Description": (
                    "ReCoder 에 붙여넣을 Secret Access Key. "
                    "이 값은 스택을 볼 수 있는 사람에게 보입니다 — "
                    "개인 계정 전용. 조직 계정이면 키는 콘솔에서 따로 발급하세요."
                ),
                "Value": {"Fn::GetAtt": ["RecoderDeployKey", "SecretAccessKey"]},
            },
            "GuidanceKo": {
                "Description": "다음 단계",
                "Value": (
                    "위 두 값을 복사해 VSCode ReCoder 확장 → AWS 연결 화면에 "
                    "붙여넣으면 온보딩이 끝납니다."
                ),
            },
        },
    }


def template_json(**kwargs: Any) -> str:
    """호스팅·업로드에 쓰는 직렬화본. 들여쓰기 고정 — diff 가 안정된다."""
    return json.dumps(
        build_quickcreate_template(**kwargs), ensure_ascii=False, indent=2
    ) + "\n"


def hosted_template_url() -> str:
    """팀이 호스팅해 둔 템플릿 S3 URL. 없으면 빈 문자열."""
    return (os.environ.get(ENV_TEMPLATE_URL) or "").strip()


def quick_create_url(template_url: str, region: str = "") -> str:
    """CloudFormation quick-create 콘솔 링크.

    templateURL 은 S3 HTTPS URL 만 동작한다 — 그래서 호스팅이 전제다.
    """
    if not template_url:
        return ""
    base = "https://console.aws.amazon.com/cloudformation/home"
    region_q = f"?region={quote(region, safe='')}" if region else ""
    return (
        f"{base}{region_q}#/stacks/create/review"
        f"?templateURL={quote(template_url, safe='')}"
        f"&stackName={quote(STACK_NAME, safe='')}"
    )


def console_upload_url(region: str = "") -> str:
    """호스팅 전 폴백 — 콘솔의 '템플릿 업로드' 화면.

    확장이 템플릿 본문을 클립보드에 넣어 주므로, 사용자는 파일로 저장해
    업로드하거나 디자이너에 붙여넣으면 된다.
    """
    base = "https://console.aws.amazon.com/cloudformation/home"
    region_q = f"?region={quote(region, safe='')}" if region else ""
    return f"{base}{region_q}#/stacks/create/template"
