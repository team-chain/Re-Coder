"""ECS execution role setup: no real credentials, AWS calls, or deployments."""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ecs_execution_role as setup
from api.routes import aws

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
ROLE = "ecsTaskExecutionRole"
ARN = f"arn:aws:iam::{ACCOUNT}:role/{ROLE}"
TOKEN = "test-session-token"


class AwsError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__("Do not expose raw AWS exception details")


class FakeSession:
    def __init__(self):
        self.identity = {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/test"}
        self.role = None
        self.calls = []
        self.fail = {}

    def client(self, service, **kwargs):
        assert service in {"iam", "sts"}
        return self

    def _record(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action in self.fail:
            raise AwsError(self.fail[action])

    def get_caller_identity(self):
        self._record("identity")
        return self.identity

    def get_role(self, **kwargs):
        self._record("get", **kwargs)
        if self.role is None:
            raise AwsError("NoSuchEntity")
        return {"Role": self.role}

    def create_role(self, **kwargs):
        self._record("create", **kwargs)
        self.role = {"Arn": f"arn:aws:iam::{self.identity['Account']}:role{kwargs['Path']}{kwargs['RoleName']}"}
        return {"Role": self.role}

    def attach_role_policy(self, **kwargs):
        self._record("attach", **kwargs)
        return {}

    @property
    def writes(self):
        return [(action, kwargs) for action, kwargs in self.calls if action in {"create", "attach"}]


@pytest.fixture
def client(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(aws, "_execution_role_plans", {})
    monkeypatch.setattr(aws, "_execution_role_session", lambda region: session)
    monkeypatch.setattr(aws.aws_policy, "configured_execution_role", lambda: ROLE)
    app = FastAPI()
    app.state.session_token = TOKEN
    app.include_router(aws.router)
    with TestClient(app) as http:
        yield http, session


def preview(client):
    http, _ = client
    return http.post("/api/aws/ecs-execution-role/preview", json={"region": REGION}, headers={"X-Session-Token": TOKEN})


def apply(client, proposal_id, approved=True, token=TOKEN):
    http, _ = client
    return http.post("/api/aws/ecs-execution-role/apply", json={"proposal_id": proposal_id, "approved": approved}, headers={"X-Session-Token": token})


def test_preview_is_read_only_and_creation_matches_review(client):
    http, session = client
    result = preview(client)
    assert result.status_code == 200, result.text
    plan = result.json()
    assert plan["status"] == "missing" and plan["role_arn"] == ARN
    assert plan["approval_level"] == 4
    assert session.writes == []
    result = apply(client, plan["proposal_id"])
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "created"
    assert [a for a, _ in session.writes] == ["create", "attach"]
    created, attached = [kw for _, kw in session.writes]
    assert created["RoleName"] == ROLE and created["Path"] == "/"
    assert json.loads(created["AssumeRolePolicyDocument"]) == plan["trust_policy"]
    assert attached == {"RoleName": ROLE, "PolicyArn": plan["policy_arn"]}
    trust = plan["trust_policy"]["Statement"][0]
    assert trust["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
    assert trust["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}
    assert apply(client, plan["proposal_id"]).status_code == 409
    assert len(session.writes) == 2


def test_existing_role_is_never_modified(client):
    _, session = client
    session.role = {"Arn": ARN}
    result = preview(client).json()
    assert result["status"] == "exists" and "proposal_id" not in result
    assert session.writes == []


def test_role_created_by_someone_else_after_preview_is_not_modified(client):
    _, session = client
    proposal = preview(client).json()["proposal_id"]
    session.role = {"Arn": ARN}
    assert apply(client, proposal).json()["status"] == "exists"
    assert session.writes == []


@pytest.mark.parametrize("token", ["", "wrong"])
def test_preview_and_apply_require_token_even_for_localhost(client, token):
    http, session = client
    assert http.post("/api/aws/ecs-execution-role/preview", json={"region": REGION}, headers={"X-Session-Token": token}).status_code == 401
    assert apply(client, "untrusted", token=token).status_code == 401
    assert session.calls == []


def test_cancellation_never_calls_aws_again(client):
    _, session = client
    proposal = preview(client).json()["proposal_id"]
    before = len(session.calls)
    assert apply(client, proposal, approved=False).json()["status"] == "cancelled"
    assert len(session.calls) == before
    assert apply(client, proposal).status_code == 409


def test_missing_or_non_boolean_approval_is_rejected(client):
    http, session = client
    for value in [None, "true", 1]:
        result = http.post("/api/aws/ecs-execution-role/apply", json={"proposal_id": "p", "approved": value}, headers={"X-Session-Token": TOKEN})
        assert result.status_code == 422
    assert not session.calls


def test_expired_or_lost_proposal_cannot_mutate(client, monkeypatch):
    _, session = client
    proposal = preview(client).json()["proposal_id"]
    created, plan = aws._execution_role_plans[proposal]
    monkeypatch.setattr(aws._execution_time, "monotonic", lambda: created + 601)
    assert apply(client, proposal).status_code == 409
    assert apply(client, "lost-after-core-restart").status_code == 409
    assert session.writes == []


@pytest.mark.parametrize("changed", ["account", "caller", "role"])
def test_approval_is_bound_to_reviewed_identity_and_configuration(client, monkeypatch, changed):
    _, session = client
    proposal = preview(client).json()["proposal_id"]
    if changed == "account":
        session.identity = {"Account": "999999999999", "Arn": "arn:aws:iam::999999999999:user/test"}
    elif changed == "caller":
        session.identity["Arn"] = f"arn:aws:iam::{ACCOUNT}:user/other"
    else:
        monkeypatch.setattr(aws.aws_policy, "configured_execution_role", lambda: "another-role")
    assert apply(client, proposal).status_code == 409
    assert session.writes == []


def test_get_role_denial_is_not_treated_as_missing(client):
    _, session = client
    session.fail["get"] = "AccessDenied"
    result = preview(client)
    assert result.status_code == 403 and "iam:GetRole" in result.text
    assert not aws._execution_role_plans and not session.writes
    assert "Do not expose" not in result.text


def test_create_denial_preserves_existing_permissions(client):
    _, session = client
    proposal = preview(client).json()["proposal_id"]
    session.fail["create"] = "AccessDenied"
    result = apply(client, proposal)
    assert result.status_code == 403 and "iam:CreateRole" in result.text
    assert [a for a, _ in session.writes] == ["create"]
    assert session.role is None


def test_partial_creation_is_reported_with_specific_recovery(client):
    _, session = client
    proposal = preview(client).json()["proposal_id"]
    session.fail["attach"] = "AccessDenied"
    result = apply(client, proposal)
    assert result.status_code == 403
    assert "생성은 완료" in result.text and "aws iam attach-role-policy" in result.text
    assert ARN in result.text and session.role is not None
    assert "AdministratorAccess" not in result.text


def test_concurrent_role_creation_does_not_attach_to_unreviewed_role(client):
    _, session = client
    proposal = preview(client).json()["proposal_id"]
    session.fail["create"] = "EntityAlreadyExists"
    assert apply(client, proposal).status_code == 409
    assert [a for a, _ in session.writes] == ["create"]


def test_custom_path_and_policy_partition_are_preserved():
    session = FakeSession()
    session.identity["Arn"] = f"arn:aws-cn:iam::{ACCOUNT}:user/test"
    plan = setup.inspect_role(session, "cn-north-1", "team/Execution")
    assert plan["role_arn"] == f"arn:aws-cn:iam::{ACCOUNT}:role/team/Execution"
    assert plan["policy_arn"].startswith("arn:aws-cn:")
    setup.create_reviewed_role(session, plan, "team/Execution")
    assert session.writes[0][1]["Path"] == "/team/"
    assert session.writes[0][1]["RoleName"] == "Execution"


def test_path_collision_does_not_offer_creation():
    session = FakeSession()
    session.role = {"Arn": f"arn:aws:iam::{ACCOUNT}:role/wrong/Execution"}
    with pytest.raises(setup.ExecutionRoleError, match="다른 경로"):
        setup.inspect_role(session, REGION, "team/Execution")
    assert session.writes == []


def test_missing_academy_role_is_not_created():
    session = FakeSession()
    with pytest.raises(setup.ExecutionRoleError, match="LabRole"):
        setup.inspect_role(session, REGION, "LabRole")
    assert session.writes == []


@pytest.mark.parametrize("role", ["../bad", "bad;command", "role/*", "x" * 65])
def test_invalid_role_names_cannot_reach_iam(role):
    session = FakeSession()
    with pytest.raises(setup.ExecutionRoleError):
        setup.inspect_role(session, REGION, role)
    assert not any(a != "identity" for a, _ in session.calls)


def test_setup_permissions_are_not_added_to_normal_deployment_policy(tmp_path):
    import aws_calls
    import aws_policy

    actions = set(aws_policy.used_actions(aws_policy.build_policy()))
    assert not actions.intersection({"iam:CreateRole", "iam:AttachRolePolicy"})
    # Exercise audit scope without traversing an installed developer environment.
    source = Path(__file__).resolve().parents[1] / "ecs_execution_role.py"
    (tmp_path / source.name).write_text(source.read_text())
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "dependency.py").write_text("import boto3\nboto3.client('iam').delete_user(UserName='not-our-code')\n")
    scan = aws_calls.scan_source(tmp_path)
    assert any("ecs_execution_role.py" in item and "승인" in item for item in scan.skipped)
    assert any(".venv/dependency.py" in item for item in scan.skipped)
    assert not scan.calls
