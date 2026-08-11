"""AWS connect는 키를 파일에 저장하지 않고 현재 Core 진단을 갱신하는지 확인한다."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from api.routes import aws  # noqa: E402


def test_permission_check_route_is_registered() -> None:
    paths = {route.path for route in aws.router.routes}
    assert "/api/aws/permissions/check" in paths


def test_permission_check_covers_every_ecs_preflight_and_deploy_action() -> None:
    """초록 권한 점검 뒤 ECS preflight가 권한 부족으로 실패하면 안 된다."""
    expected = {
        "iam:GetRole",
        "ecr:DescribeRepositories",
        "logs:DescribeLogGroups",
        "ecs:DescribeTaskDefinition",
    }
    assert expected <= set(aws.REQUIRED_DEPLOY_ACTIONS)


def test_permission_context_uses_the_ecs_request_repo_not_unused_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ECR_REPOSITORY는 ECSDeployRequest가 읽지 않으므로 검사에도 쓰면 안 된다."""
    monkeypatch.setenv("ECR_REPOSITORY", "different-repository")

    context, error = aws._resolved_permission_context(None)

    assert error is None
    assert context is not None
    assert context.ecr_repo == "recoder-app"


def test_permission_context_uses_ecs_adapter_defaults_and_service_repo() -> None:
    """빈 확장 폼도 실제 ECS 어댑터와 같은 대상 ARN을 검사한다."""
    context, error = aws._resolved_permission_context(
        aws.AwsDeploymentPermissionContext(ecs_service="team-service")
    )

    assert error is None
    assert context is not None
    assert context.ecs_cluster == "recoder-cluster"
    assert context.ecs_service == "team-service"
    assert context.ecr_repo == "team-service"
    assert context.task_family == "recoder-task"


def test_connect_validates_and_keeps_credentials_in_current_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ORIGINAL_ACCESS_KEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ORIGINAL_SECRET")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    received: dict[str, str] = {}
    diagnostics_refreshed = False

    def fake_sts(*, profile: str | None, region: str) -> dict[str, str]:
        received["access_key"] = os.environ["AWS_ACCESS_KEY_ID"]
        received["secret_key"] = os.environ["AWS_SECRET_ACCESS_KEY"]
        received["region"] = region
        assert profile is None
        return {"account": "123456789012", "arn": "arn:aws:iam::123456789012:user/test", "user_id": "AIDA"}

    monkeypatch.setattr(aws, "_call_sts_get_caller_identity", fake_sts)
    monkeypatch.setattr(aws, "_inspect_deploy_permissions", lambda *_: aws.AwsPermissionCheck(inspected=True))
    def fake_refresh_diagnostics() -> None:
        nonlocal diagnostics_refreshed
        diagnostics_refreshed = True
    monkeypatch.setattr(aws, "_refresh_diagnostics_cache", fake_refresh_diagnostics)

    result = asyncio.run(aws.connect_aws(aws.AwsConnectRequest(
        access_key_id="AKIA12345678901234",
        secret_access_key="secret-key-for-test",
        region="ap-northeast-2",
    )))

    assert result.ready is True
    assert result.identity and result.identity.account == "123456789012"
    assert result.storage == "secret_storage"
    assert diagnostics_refreshed is True
    assert received == {
        "access_key": "AKIA12345678901234",
        "secret_key": "secret-key-for-test",
        "region": "ap-northeast-2",
    }
    # 키는 파일이 아니라 현재 Core 프로세스 메모리에만 적용된다. Extension은
    # 별도로 SecretStorage에 보관해 다음 Core 시작 시에만 다시 주입한다.
    assert os.environ["AWS_ACCESS_KEY_ID"] == "AKIA12345678901234"
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "secret-key-for-test"
    assert os.environ["AWS_REGION"] == "ap-northeast-2"


def test_connect_does_not_call_legacy_file_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aws, "_call_sts_get_caller_identity", lambda **_: {"account": "1", "arn": "arn", "user_id": "id"})
    monkeypatch.setattr(aws, "_inspect_deploy_permissions", lambda *_: aws.AwsPermissionCheck())
    monkeypatch.setattr(aws, "_save_recoder_credentials", lambda *args: pytest.fail("connect must not write ~/.recoder"))
    monkeypatch.setattr(aws, "_save_aws_credentials_file", lambda *args: pytest.fail("connect must not write ~/.aws"))
    monkeypatch.setattr(aws, "_refresh_diagnostics_cache", lambda: None)

    result = asyncio.run(aws.connect_aws(aws.AwsConnectRequest(
        access_key_id="AKIA12345678901234",
        secret_access_key="secret-key-for-test",
    )))

    assert result.ready is True


def test_permission_check_warns_for_missing_actions_and_administrator_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeIam:
        def simulate_principal_policy(self, **_: object) -> dict:
            return {"EvaluationResults": [
                {"EvalActionName": "ecr:GetAuthorizationToken", "EvalDecision": "allowed"},
                {"EvalActionName": "ecs:UpdateService", "EvalDecision": "implicitDeny"},
            ]}

        def list_attached_user_policies(self, **_: object) -> dict:
            return {"AttachedPolicies": [
                {"PolicyName": "AdministratorAccess", "PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess"},
            ]}

        def list_user_policies(self, **_: object) -> dict:
            return {"PolicyNames": ["DeployAdmin"]}

        def get_user_policy(self, **_: object) -> dict:
            return {"PolicyDocument": {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}}

    class FakeSession:
        def client(self, *_: object, **__: object) -> FakeIam:
            return FakeIam()

    monkeypatch.setattr(aws, "_build_boto3_session", lambda **_: FakeSession())
    monkeypatch.setenv("ECS_CLUSTER", "recoder-cluster")
    monkeypatch.setenv("ECS_SERVICE", "recoder-service")

    report = aws._inspect_deploy_permissions(
        {"arn": "arn:aws:iam::123456789012:user/recoder-deployer"},
        "ap-northeast-2",
    )

    assert report.inspected is True
    assert "ecs:UpdateService" in report.missing_actions
    assert "AdministratorAccess" in report.excessive_policies
    assert "인라인 정책: DeployAdmin" in report.excessive_policies
    assert any("너무 강력" in warning or "전체 권한" in warning for warning in report.warnings)


def test_permission_simulation_uses_deployment_resources_and_passrole_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """최소권한 정책과 같은 ARN·조건으로만 IAM Simulator를 호출한다."""
    calls: list[dict[str, object]] = []

    class FakeIam:
        def simulate_principal_policy(self, **kwargs: object) -> dict:
            calls.append(kwargs)
            return {"EvaluationResults": [
                {"EvalActionName": action, "EvalDecision": "allowed"}
                for action in kwargs["ActionNames"]  # type: ignore[index]
            ]}

        def list_attached_user_policies(self, **_: object) -> dict:
            return {"AttachedPolicies": []}

        def list_user_policies(self, **_: object) -> dict:
            return {"PolicyNames": []}

    class FakeSession:
        def client(self, *_: object, **__: object) -> FakeIam:
            return FakeIam()

    monkeypatch.setattr(aws, "_build_boto3_session", lambda **_: FakeSession())
    context = aws.AwsDeploymentPermissionContext(
        ecr_repo="team-api",
        ecs_cluster="team-cluster",
        ecs_service="team-service",
        aws_region="us-west-2",
        task_execution_role="team/EcsExecution",
        task_role="team/EcsTask",
    )
    report = aws._inspect_deploy_permissions(
        {"account": "123456789012", "arn": "arn:aws:iam::123456789012:user/recoder-deployer"},
        "ap-northeast-2",
        context,
    )

    assert report.inspected is True
    assert report.missing_actions == []
    ecr_call = next(call for call in calls if "ecr:PutImage" in call["ActionNames"])
    assert ecr_call["ResourceArns"] == [
        "arn:aws:ecr:us-west-2:123456789012:repository/team-api",
    ]
    passrole_call = next(call for call in calls if call["ActionNames"] == ["iam:PassRole"])
    assert passrole_call["ResourceArns"] == [
        "arn:aws:iam::123456789012:role/team/EcsExecution",
        "arn:aws:iam::123456789012:role/team/EcsTask",
    ]
    assert passrole_call["ContextEntries"] == [{
        "ContextKeyName": "iam:PassedToService",
        "ContextKeyValues": ["ecs-tasks.amazonaws.com"],
        "ContextKeyType": "string",
    }]


def test_permission_simulation_skips_empty_task_role_and_uses_deploy_account_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """기본 task role은 PassRole 대상이 아니며 ECR은 실제 registry 계정을 쓴다."""
    calls: list[dict[str, object]] = []

    class FakeIam:
        def simulate_principal_policy(self, **kwargs: object) -> dict:
            calls.append(kwargs)
            return {"EvaluationResults": [
                {"EvalActionName": action, "EvalDecision": "allowed"}
                for action in kwargs["ActionNames"]  # type: ignore[index]
            ]}

        def list_attached_user_policies(self, **_: object) -> dict:
            return {"AttachedPolicies": []}

        def list_user_policies(self, **_: object) -> dict:
            return {"PolicyNames": []}

    class FakeSession:
        def client(self, *_: object, **__: object) -> FakeIam:
            return FakeIam()

    monkeypatch.setattr(aws, "_build_boto3_session", lambda **_: FakeSession())
    context = aws.AwsDeploymentPermissionContext(
        ecr_repo="recoder-app",
        ecs_cluster="cluster",
        ecs_service="service",
        task_execution_role="recoder/EcsExecution",
        task_role="",
    )

    report = aws._inspect_deploy_permissions(
        {"account": "123456789012", "arn": "arn:aws:iam::123456789012:user/recoder-deployer"},
        "ap-northeast-2",
        context,
    )

    assert report.inspected is True
    ecr_call = next(call for call in calls if "ecr:PutImage" in call["ActionNames"])
    assert ecr_call["ResourceArns"] == [
        "arn:aws:ecr:ap-northeast-2:123456789012:repository/recoder-app",
    ]
    passrole_call = next(call for call in calls if call["ActionNames"] == ["iam:PassRole"])
    assert passrole_call["ResourceArns"] == [
        "arn:aws:iam::123456789012:role/recoder/EcsExecution",
    ]
    provision_call = next(call for call in calls if "ecs:CreateService" in call["ActionNames"])
    assert provision_call["ResourceArns"] == [
        "arn:aws:ecs:ap-northeast-2:123456789012:service/cluster/service",
    ]
    logs_call = next(call for call in calls if "logs:CreateLogGroup" in call["ActionNames"])
    assert logs_call["ResourceArns"] == [
        "arn:aws:logs:ap-northeast-2:123456789012:log-group:/ecs/recoder-task",
    ]


def test_permission_simulation_uses_selected_task_family_log_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeIam:
        def simulate_principal_policy(self, **kwargs: object) -> dict:
            calls.append(kwargs)
            return {"EvaluationResults": [
                {"EvalActionName": action, "EvalDecision": "allowed"}
                for action in kwargs["ActionNames"]  # type: ignore[index]
            ]}

        def list_attached_user_policies(self, **_: object) -> dict:
            return {"AttachedPolicies": []}

        def list_user_policies(self, **_: object) -> dict:
            return {"PolicyNames": []}

    class FakeSession:
        def client(self, *_: object, **__: object) -> FakeIam:
            return FakeIam()

    monkeypatch.setattr(aws, "_build_boto3_session", lambda **_: FakeSession())
    report = aws._inspect_deploy_permissions(
        {"account": "123456789012", "arn": "arn:aws:iam::123456789012:user/recoder-deployer"},
        "ap-northeast-2",
        aws.AwsDeploymentPermissionContext(task_family="team-task"),
    )

    assert report.inspected is True
    logs_call = next(call for call in calls if "logs:CreateLogGroup" in call["ActionNames"])
    assert logs_call["ResourceArns"] == [
        "arn:aws:logs:ap-northeast-2:123456789012:log-group:/ecs/team-task",
    ]


def test_invalid_configured_role_keeps_sts_connection_and_marks_check_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """잘못된 역할 환경변수는 재파싱하지 않고 경고로만 반환한다."""
    monkeypatch.setenv("ECS_EXECUTION_ROLE_ARN", "invalid*role")

    report = aws._inspect_deploy_permissions(
        {"account": "123456789012", "arn": "arn:aws:iam::123456789012:user/recoder-deployer"},
        "ap-northeast-2",
    )

    assert report.inspected is False
    assert any("ECS 역할 설정" in warning for warning in report.warnings)


def test_incomplete_permission_simulation_is_not_marked_as_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IAM Simulator 그룹 하나라도 실패하면 초록 완료로 보이면 안 된다."""
    class FakeIam:
        def simulate_principal_policy(self, **kwargs: object) -> dict:
            if "ecs:CreateService" in kwargs["ActionNames"]:  # type: ignore[operator]
                raise RuntimeError("simulator unavailable")
            return {"EvaluationResults": [
                {"EvalActionName": action, "EvalDecision": "allowed"}
                for action in kwargs["ActionNames"]  # type: ignore[index]
            ]}

        def list_attached_user_policies(self, **_: object) -> dict:
            return {"AttachedPolicies": []}

        def list_user_policies(self, **_: object) -> dict:
            return {"PolicyNames": []}

    class FakeSession:
        def client(self, *_: object, **__: object) -> FakeIam:
            return FakeIam()

    monkeypatch.setattr(aws, "_build_boto3_session", lambda **_: FakeSession())
    report = aws._inspect_deploy_permissions(
        {"account": "123456789012", "arn": "arn:aws:iam::123456789012:user/recoder-deployer"},
        "ap-northeast-2",
    )

    assert report.missing_actions == []
    assert report.inspected is False
    assert any("일부 배포 권한을 자동 확인하지 못했습니다" in warning for warning in report.warnings)
