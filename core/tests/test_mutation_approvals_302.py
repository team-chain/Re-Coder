"""3.0.2 — AWS·GitHub·운영 서비스를 바꾸는 요청은 `approved: true` 없이 400 이다.

403 이 아니라 400 인 이유: 확장의 ApiClient 는 403 을 "토큰이 바뀌었다"로 보고
토큰을 새로 읽어 한 번 더 보낸다. 승인 거절이 재시도되면 안 된다.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import github_agent as gh
from api.routes import aws_cleanup as cleanup_routes
from api.routes import deploy_ecs, deploy_s3, ecs as ecs_routes, github, gitops


def _client(*routers) -> TestClient:
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


# ── ECS 배포 ────────────────────────────────────────────────────────────


@pytest.fixture
def ecs_client(monkeypatch, tmp_path):
    monkeypatch.setenv("RECODER_ECS_STORE", str(tmp_path / "ecs.json"))
    monkeypatch.setattr(ecs_routes, "_deploy_records", {})
    started: list = []

    async def _never(*args, **kwargs):  # pragma: no cover - 불리면 실패
        started.append(args)
        raise AssertionError("승인 없는 배포가 게이트까지 갔다")

    monkeypatch.setattr(ecs_routes, "_gate_and_start", _never)
    return _client(ecs_routes.router, deploy_ecs.router), started


@pytest.mark.parametrize("body", [{}, {"approved": False}, {"approved": "true"}, {"approved": 1}])
def test_extension_ecs_deploy_requires_explicit_approval(ecs_client, body):
    client, started = ecs_client
    resp = client.post("/api/deploy/ecs", json=body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "approval_required"
    assert "승인" in resp.json()["detail"]["message"]
    assert not started and not ecs_routes._deploy_records


@pytest.mark.parametrize("body", [{}, {"approved": False}])
def test_direct_ecs_deploy_requires_explicit_approval(ecs_client, body):
    client, started = ecs_client
    resp = client.post("/api/ecs/deploy", json={"project_id": "p", "cluster": "c", "service": "s", **body})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "approval_required"
    assert not started and not ecs_routes._deploy_records


# ── ECS 중지 ────────────────────────────────────────────────────────────


class _FakeEcs:
    def __init__(self, tags=None, fail_describe=False):
        self.tags = tags
        self.fail_describe = fail_describe
        self.updates: list[dict] = []

    def describe_services(self, cluster, services, include=None):
        if self.fail_describe:
            raise RuntimeError("AccessDenied")
        assert include == ["TAGS"]
        return {"services": [{"serviceName": services[0], "tags": self.tags or []}]}

    def update_service(self, **kwargs):
        self.updates.append(kwargs)
        return {"service": {"desiredCount": kwargs["desiredCount"]}}


@pytest.fixture
def no_records(monkeypatch, tmp_path):
    monkeypatch.setenv("RECODER_ECS_STORE", str(tmp_path / "none.json"))
    monkeypatch.setattr(ecs_routes, "_deploy_records", {})


def _patch_boto(monkeypatch, fake):
    import boto3

    class _Session:
        def __init__(self, *a, **kw):
            pass

        def client(self, name, *a, **kw):
            assert name == "ecs"
            return fake

    monkeypatch.setattr(boto3.session, "Session", _Session)


def test_deploy_ecs_stop_requires_approval(no_records, monkeypatch):
    fake = _FakeEcs(tags=[{"key": "recoder:managed", "value": "true"}])
    _patch_boto(monkeypatch, fake)
    client = _client(deploy_ecs.router)
    body = {"ecs_cluster": "c", "ecs_service": "s", "aws_region": "us-east-1"}
    resp = client.post("/api/deploy/ecs/stop", json=body)
    assert resp.status_code == 400 and resp.json()["detail"]["error"] == "approval_required"
    assert fake.updates == []


@pytest.mark.parametrize("fake", [_FakeEcs(tags=[]), _FakeEcs(tags=[{"key": "recoder:managed", "value": "false"}]),
                                  _FakeEcs(fail_describe=True)])
def test_deploy_ecs_stop_refuses_services_recoder_did_not_create(no_records, monkeypatch, fake):
    _patch_boto(monkeypatch, fake)
    client = _client(deploy_ecs.router)
    body = {"ecs_cluster": "prod", "ecs_service": "payments", "aws_region": "us-east-1", "approved": True}
    resp = client.post("/api/deploy/ecs/stop", json=body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "not_managed"
    assert fake.updates == []


def test_deploy_ecs_stop_scales_a_tagged_service(no_records, monkeypatch):
    fake = _FakeEcs(tags=[{"key": "recoder:managed", "value": "true"}])
    _patch_boto(monkeypatch, fake)
    client = _client(deploy_ecs.router)
    body = {"ecs_cluster": "c", "ecs_service": "s", "aws_region": "us-east-1", "approved": True}
    resp = client.post("/api/deploy/ecs/stop", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["stopped"] is True
    assert fake.updates == [{"cluster": "c", "service": "s", "desiredCount": 0}]


def test_deploy_ecs_stop_accepts_a_service_in_the_local_deploy_record(monkeypatch, tmp_path):
    import json

    store = tmp_path / "ecs.json"
    store.write_text(json.dumps([{"cluster": "legacy", "service": "shop", "region": "us-east-1"}]), encoding="utf-8")
    monkeypatch.setenv("RECODER_ECS_STORE", str(store))
    monkeypatch.setattr(ecs_routes, "_deploy_records", {})
    fake = _FakeEcs(tags=[])
    _patch_boto(monkeypatch, fake)
    client = _client(deploy_ecs.router)
    body = {"ecs_cluster": "legacy", "ecs_service": "shop", "aws_region": "us-east-1", "approved": True}
    assert client.post("/api/deploy/ecs/stop", json=body).status_code == 200
    assert fake.updates and fake.updates[0]["desiredCount"] == 0


def test_cleanup_stop_requires_approval_and_a_managed_service(no_records, monkeypatch):
    fake = _FakeEcs(tags=[])
    monkeypatch.setattr(cleanup_routes, "clients_for", lambda region: {"ecs": fake})
    client = _client(cleanup_routes.router)
    body = {"region": "us-east-1", "cluster": "prod", "service": "payments"}
    resp = client.post("/api/aws/cleanup/stop", json=body)
    assert resp.status_code == 400 and "확인" in resp.json()["detail"]
    resp = client.post("/api/aws/cleanup/stop", json={**body, "approved": True})
    assert resp.status_code == 400 and "ReCoder 가 배포한 서비스로 확인되지 않아" in resp.json()["detail"]
    assert fake.updates == []
    fake.tags = [{"key": "recoder:managed", "value": "true"}]
    resp = client.post("/api/aws/cleanup/stop", json={**body, "approved": True})
    assert resp.status_code == 200 and resp.json()["ok"]
    assert fake.updates == [{"cluster": "prod", "service": "payments", "desiredCount": 0}]


# ── IAM 역할 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("approved", [None, False])
def test_role_setup_requires_approval_before_touching_aws(monkeypatch, approved):
    from api.routes import aws

    monkeypatch.setattr(aws, "_base_session", lambda *a, **kw: pytest.fail("승인 없이 AWS 세션을 만들었다"))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(aws.setup_aws_role(aws.AwsRoleSetupRequest(approved=approved)))
    assert caught.value.status_code == 400
    assert "확인" in caught.value.detail


# ── GitHub ──────────────────────────────────────────────────────────────


@pytest.fixture
def gh_agent(monkeypatch):
    monkeypatch.setattr(gh.GitHubAgent, "_load_token", lambda self: None)
    agent = gh.GitHubAgent()
    agent._token = "fixture-token-not-a-real-key"
    agent._user_cache = {"login": "tester"}
    monkeypatch.setattr(gh, "get_github_agent", lambda: agent)
    return agent


def test_github_mutations_require_approval(gh_agent, monkeypatch):
    monkeypatch.setattr(gh_agent, "push", lambda *a, **kw: pytest.fail("승인 없이 push 했다"))
    monkeypatch.setattr(gh_agent, "repo_create_and_push", lambda *a, **kw: pytest.fail("승인 없이 저장소를 만들었다"))
    monkeypatch.setattr(gh_agent, "set_secret", lambda *a, **kw: pytest.fail("승인 없이 Secret 을 썼다"))
    client = _client(github.router)
    for path, body in (
        ("/api/git/push", {"workspace_path": "/w", "branch": "main"}),
        ("/api/github/repo", {"workspace_path": "/w", "name": "shop"}),
        ("/api/github/secret", {"repo": "tester/shop", "name": "AWS_ROLE", "value": "x"}),
    ):
        for extra in ({}, {"approved": False}, {"approved": "yes"}):
            resp = client.post(path, json={**body, **extra})
            assert resp.status_code == 400, (path, extra, resp.text)
            assert "확인" in resp.json()["detail"]


def test_approved_github_mutations_reach_the_agent(gh_agent, monkeypatch):
    seen: list = []
    monkeypatch.setattr(gh_agent, "push", lambda *a, **kw: seen.append(("push", a, kw)) or {"status": "ok"})
    monkeypatch.setattr(gh_agent, "repo_create_and_push", lambda *a, **kw: seen.append(("repo", a)) or {"status": "ok"})
    monkeypatch.setattr(gh_agent, "set_secret", lambda *a, **kw: seen.append(("secret", a)) or {"status": "ok"})
    client = _client(github.router)
    assert client.post("/api/git/push", json={"workspace_path": "/w", "branch": "main", "approved": True,
                                              "force": "yes", "auto_commit": False}).status_code == 200
    assert client.post("/api/github/repo", json={"workspace_path": "/w", "name": "shop", "approved": True}).status_code == 200
    assert client.post("/api/github/secret", json={"repo": "t/s", "name": "N", "value": "v", "approved": True}).status_code == 200
    push = seen[0]
    #: force 는 정확히 true 일 때만 — "yes" 같은 참 같은 값으로는 강제 push 하지 않는다.
    assert push[1] == ("/w", "main", False) and push[2] == {"auto_commit": False}
    assert [s[0] for s in seen] == ["push", "repo", "secret"]


BAD_BRANCHES = ["+main", "-x", "--force", "a:b", "main:refs/heads/x", "a..b", "a/.x", "x.lock", "@", "HEAD",
                "a@{b", "a b", "a//b", "a/", "/a", "a.", ".a", "a\\b", "a~1", "a^", "a?", "a*", "a[b", "a\tb", "a\x7fb"]
GOOD_BRANCHES = ["main", "feat/x", "release-1.2", "user/feature_a", "fix+typo", "한글브랜치"]


@pytest.mark.parametrize("branch", BAD_BRANCHES)
def test_push_rejects_unsafe_branch_names(gh_agent, monkeypatch, branch):
    monkeypatch.setattr(gh_agent, "push", lambda *a, **kw: pytest.fail("잘못된 브랜치로 push 했다"))
    client = _client(github.router)
    resp = client.post("/api/git/push", json={"workspace_path": "/w", "branch": branch, "approved": True})
    assert resp.status_code == 400, resp.text
    assert gh.validate_branch_name(branch)


@pytest.mark.parametrize("branch", GOOD_BRANCHES)
def test_valid_branch_names_pass(branch):
    assert gh.validate_branch_name(branch) == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="git 이 없다")
@pytest.mark.parametrize("branch", BAD_BRANCHES + GOOD_BRANCHES)
def test_validator_is_never_looser_than_git(branch):
    """git check-ref-format --branch 가 거절하는 이름은 우리도 거절한다(우리는 + - @ 를 더 막는다)."""
    git_ok = subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True).returncode == 0
    if not git_ok:
        assert gh.validate_branch_name(branch), branch


@pytest.mark.parametrize("force", [False, True])
def test_push_puts_double_dash_before_remote_and_refspec(gh_agent, monkeypatch, tmp_path, force):
    (tmp_path / ".git").mkdir()
    pushes: list = []

    def git(cwd, args, **kwargs):
        if args[:3] == ["remote", "get-url", "origin"]:
            return 0, "https://github.com/tester/repo.git", ""
        if "push" in args:
            pushes.append(args)
            return 0, "", ""
        pytest.fail(f"Unexpected git command: {args}")

    monkeypatch.setattr(gh_agent, "_git", git)
    assert gh_agent.push(str(tmp_path), "main", force, auto_commit=False)["status"] == "ok"
    args = pushes[0][pushes[0].index("push"):]
    assert args[-3:] == ["--", "origin", "main"]
    assert ("--force-with-lease" in args) is force
    assert "--force" not in args and "-f" not in args


def test_agent_push_rejects_a_bad_current_branch(gh_agent, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()

    def git(cwd, args, **kwargs):
        if args == ["branch", "--show-current"]:
            return 0, "-oops\n", ""
        pytest.fail(f"Unexpected git command: {args}")

    monkeypatch.setattr(gh_agent, "_git", git)
    assert gh_agent.push(str(tmp_path), "", auto_commit=False)["status"] == "error"


# ── S3 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/api/deploy/s3", "/api/deploy/s3/stream"])
def test_s3_deploy_requires_approval(monkeypatch, path):
    monkeypatch.setattr(deploy_s3, "_session_and_region", lambda *a, **kw: pytest.fail("승인 없이 AWS 세션을 만들었다"))
    client = _client(deploy_s3.router)
    body = {"project": "site", "files": [{"path": "index.html", "content": "x"}]}
    for extra in ({}, {"approved": False}):
        resp = client.post(path, json={**body, **extra})
        assert resp.status_code == 400, resp.text
        assert "공개" in resp.json()["detail"]


# ── GitOps ─────────────────────────────────────────────────────────────


@pytest.fixture
def gitops_client(monkeypatch):
    ran: list = []

    async def _fake(sync_id, request):
        ran.append(request)

    monkeypatch.setattr(gitops, "_run_argocd_sync", _fake)
    return _client(gitops.router), ran


BASE_SYNC = {"project_id": "p", "app_name": "a", "argocd_server": "argo.test", "argocd_token": "t"}


@pytest.mark.parametrize("flag", ["prune", "force"])
def test_destructive_gitops_sync_requires_approval(gitops_client, flag):
    client, ran = gitops_client
    resp = client.post("/gitops/sync", json={**BASE_SYNC, flag: True})
    assert resp.status_code == 400 and "승인" in resp.json()["detail"]
    assert not ran
    resp = client.post("/gitops/sync", json={**BASE_SYNC, flag: True, "approved": True})
    assert resp.status_code == 202 and len(ran) == 1


def test_plain_gitops_sync_is_unchanged(gitops_client):
    client, ran = gitops_client
    assert client.post("/gitops/sync", json=BASE_SYNC).status_code == 202
    assert len(ran) == 1
