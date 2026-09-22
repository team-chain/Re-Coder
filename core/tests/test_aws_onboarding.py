"""원클릭 IAM 셋업 — 보드 카드 「AWS 온보딩 마찰 제거」.

여기서 고정하는 것
    1. 템플릿의 정책은 aws_policy.build_policy() 와 **한 벌**이다 (드리프트 금지).
    2. 자리표시자(<ACCOUNT_ID> 등)가 템플릿에 남지 않는다 — 전부 Fn::Sub 로
       바뀌어 스택 생성 시점에 채워진다.
    3. quick-create 링크는 호스팅 URL 이 설정됐을 때만 나온다. 없으면 콘솔
       업로드 폴백 — 어느 쪽이든 사용자가 다음에 뭘 할지 steps 에 있다.
    4. infra/recoder-iam-quickcreate.json 은 생성기 출력과 일치한다.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import aws_onboarding  # noqa: E402
import aws_policy  # noqa: E402
from api.routes import aws as aws_routes  # noqa: E402


def _template():
    return aws_onboarding.build_quickcreate_template()


# ---------------------------------------------------------------------------
# 1. 정책 한 벌 — 드리프트 금지
# ---------------------------------------------------------------------------


def _strip_subs(value):
    if isinstance(value, dict):
        if set(value.keys()) == {"Fn::Sub"}:
            return value["Fn::Sub"]
        return {k: _strip_subs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_subs(v) for v in value]
    return value


def test_템플릿_정책은_aws_policy_와_한_벌이다() -> None:
    doc = _strip_subs(
        _template()["Resources"]["RecoderDeployPolicy"]["Properties"]["PolicyDocument"]
    )
    exec_role, task_role = aws_policy.resolve_roles("", "")
    expected = aws_policy.build_policy(
        None, "", "", exec_role, task_role=task_role,
    )
    #: Fn::Sub 변수를 자리표시자로 되돌리면 원본과 정확히 같아야 한다.
    text = json.dumps(doc, ensure_ascii=False)
    text = (text.replace("${AWS::AccountId}", aws_policy.ACCOUNT_PLACEHOLDER)
                .replace("${AWS::Region}", aws_policy.REGION_PLACEHOLDER)
                .replace("${AWS::Partition}", aws_policy.PARTITION_PLACEHOLDER))
    assert json.loads(text) == expected, "정책이 두 벌로 갈라졌다 — aws_policy 가 원본이다"


def test_자리표시자가_템플릿에_남지_않는다() -> None:
    body = aws_onboarding.template_json()
    for placeholder in (aws_policy.ACCOUNT_PLACEHOLDER,
                        aws_policy.REGION_PLACEHOLDER,
                        aws_policy.PARTITION_PLACEHOLDER):
        assert placeholder not in body, f"{placeholder} 가 Fn::Sub 로 안 바뀌었다"


def test_필수_자원과_출력이_있다() -> None:
    t = _template()
    assert set(t["Resources"]) == {"RecoderDeployPolicy", "RecoderDeployUser", "RecoderDeployKey"}
    assert {"AccessKeyId", "SecretAccessKey"} <= set(t["Outputs"])
    #: 키 발급은 반드시 우리가 만든 사용자에게.
    assert t["Resources"]["RecoderDeployKey"]["Properties"]["UserName"] == {"Ref": "RecoderDeployUser"}


# ---------------------------------------------------------------------------
# 2. 링크
# ---------------------------------------------------------------------------


def test_quick_create_링크는_호스팅_URL_이_있을_때만(monkeypatch) -> None:
    monkeypatch.delenv(aws_onboarding.ENV_TEMPLATE_URL, raising=False)
    r = asyncio.run(aws_routes.get_onboarding_link(region="us-east-1"))
    assert r.template_hosted is False
    assert r.quick_create_url == ""
    assert "cloudformation" in r.console_upload_url
    assert r.steps, "폴백에서도 다음 단계 안내는 있어야 한다"

    monkeypatch.setenv(
        aws_onboarding.ENV_TEMPLATE_URL,
        "https://recoder-assets.s3.amazonaws.com/recoder-iam-quickcreate.json",
    )
    r2 = asyncio.run(aws_routes.get_onboarding_link(region="us-east-1"))
    assert r2.template_hosted is True
    assert "stackName=recoder-iam-setup" in r2.quick_create_url
    #: templateURL 은 반드시 URL 인코딩 — 안 하면 콘솔이 파라미터를 자른다.
    assert "templateURL=https%3A%2F%2F" in r2.quick_create_url
    assert "region=us-east-1" in r2.quick_create_url


def test_이상한_리전은_400(monkeypatch) -> None:
    monkeypatch.delenv(aws_onboarding.ENV_TEMPLATE_URL, raising=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(aws_routes.get_onboarding_link(region="seoul!!"))
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# 3. 호스팅 산출물 드리프트
# ---------------------------------------------------------------------------


def test_infra_템플릿_파일이_생성기_출력과_일치한다() -> None:
    out = _CORE.parent / "infra" / "recoder-iam-quickcreate.json"
    assert out.exists(), "infra/recoder-iam-quickcreate.json 이 없다 — scripts/generate_iam_template.py 실행"
    assert json.loads(out.read_text(encoding="utf-8")) == _template(), (
        "산출물이 낡았다 — python3 scripts/generate_iam_template.py 로 재생성해 커밋할 것"
    )
