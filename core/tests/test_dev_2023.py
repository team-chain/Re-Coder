"""2.0.23 — 릴리스 태그 검사 · 리소스 태그 · 마을 보안 표시."""
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import aws_infra

ROOT = Path(__file__).resolve().parents[2]


# ── 릴리스 태그 = 제품 버전 ───────────────────────────────────────────────

@pytest.fixture
def tagcheck():
    return runpy.run_path(str(ROOT / "scripts" / "check_release_tag.py"))


def _fake_repo(tmp_path: Path, ext="2.0.23", lock="2.0.23", core="2.0.23") -> Path:
    (tmp_path / "extension").mkdir()
    (tmp_path / "core").mkdir()
    (tmp_path / "extension" / "package.json").write_text(json.dumps({"version": ext}), encoding="utf-8")
    (tmp_path / "extension" / "package-lock.json").write_text(json.dumps({"version": lock}), encoding="utf-8")
    (tmp_path / "core" / "version.py").write_text(f'VERSION = "{core}"\n', encoding="utf-8")
    return tmp_path


def test_matching_tag_passes_and_rc_is_prerelease(tagcheck, tmp_path):
    root = _fake_repo(tmp_path)
    assert tagcheck["check"]("v2.0.23", root)[0:3:2] == (True, False)
    ok, msg, rc = tagcheck["check"]("v2.0.23-rc1", root)
    assert ok and rc and "시험" in msg


def test_mismatched_or_malformed_tag_fails(tagcheck, tmp_path):
    root = _fake_repo(tmp_path, core="2.0.22")
    ok, msg, _ = tagcheck["check"]("v2.0.23", root)
    assert not ok and "core/version.py=2.0.22" in msg
    assert not tagcheck["check"]("2.0.23", root)[0]
    assert not tagcheck["check"]("v2.0", root)[0]


def test_repository_versions_are_consistent(tagcheck):
    found = tagcheck["versions"]()
    assert len(set(found.values())) == 1, found


def test_release_workflow_builds_on_tags_and_publishes_with_provenance():
    import yaml
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    on = wf.get("on") or wf.get(True)
    assert on["push"]["tags"] == ["v*"]
    jobs = wf["jobs"]
    for job in ("windows-vsix", "linux-vsix", "macos-arm64-vsix"):
        assert jobs[job]["needs"] == "version-check"
    publish = jobs["publish"]
    assert publish["if"] == "github.ref_type == 'tag'"
    assert publish["permissions"] == {"contents": "write", "id-token": "write", "attestations": "write"}
    assert wf["permissions"] == {"contents": "read"}  # 쓰기 권한은 publish job 에만
    uses = [s.get("uses", "") for s in publish["steps"]]
    assert any(u.startswith("actions/attest-build-provenance@") for u in uses)
    assert any("gh release create" in (s.get("run") or "") for s in publish["steps"])


# ── ReCoder 리소스 태그 ───────────────────────────────────────────────────

def _moto():
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws
    return boto3, mock_aws


def test_only_newly_created_resources_get_managed_tag():
    boto3, mock_aws = _moto()
    with mock_aws():
        ecs = boto3.client("ecs", region_name="us-east-1")
        ecr = boto3.client("ecr", region_name="us-east-1")
        logs = boto3.client("logs", region_name="us-east-1")
        ec2 = boto3.client("ec2", region_name="us-east-1")
        tags = aws_infra.recoder_tags("shop", "shop-svc")
        # 사용자가 미리 만든 클러스터 — ReCoder 가 재사용해도 태그를 붙이면 안 된다(정리 때 지워짐)
        ecs.create_cluster(clusterName="user-owned")
        aws_infra.ensure_cluster(ecs, "user-owned", tags=tags)
        arn = aws_infra.ensure_cluster(ecs, "recoder-new", tags=tags)
        got = {t["key"]: t["value"] for t in ecs.list_tags_for_resource(resourceArn=arn)["tags"]}
        assert got == {"recoder:managed": "true", "recoder:project": "shop", "recoder:service": "shop-svc"}
        user_arn = ecs.describe_clusters(clusters=["user-owned"])["clusters"][0]["clusterArn"]
        assert ecs.list_tags_for_resource(resourceArn=user_arn).get("tags", []) == []

        uri = aws_infra.ensure_ecr_repository(ecr, "recoder-new", tags=tags)
        repo_arn = ecr.describe_repositories(repositoryNames=["recoder-new"])["repositories"][0]["repositoryArn"]
        assert {"Key": "recoder:managed", "Value": "true"} in ecr.list_tags_for_resource(resourceArn=repo_arn)["tags"]
        assert uri

        aws_infra.ensure_log_group(logs, "/ecs/recoder-new", tags=tags)
        lg = logs.describe_log_groups(logGroupNamePrefix="/ecs/recoder-new")["logGroups"][0]
        assert logs.list_tags_for_resource(resourceArn=lg["arn"].removesuffix(":*"))["tags"]["recoder:managed"] == "true"

        vpc = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
        sg = aws_infra.ensure_security_group(ec2, vpc_id=vpc, name="recoder-new-sg", port=3000, tags=tags)
        sg_tags = ec2.describe_security_groups(GroupIds=[sg])["SecurityGroups"][0].get("Tags", [])
        assert {"Key": "recoder:managed", "Value": "true"} in sg_tags


def test_tag_failure_never_breaks_creation():
    ecs = MagicMock()
    ecs.describe_clusters.return_value = {"clusters": []}
    ecs.create_cluster.return_value = {"cluster": {"clusterArn": "arn:aws:ecs:us-east-1:1:cluster/x"}}
    ecs.tag_resource.side_effect = RuntimeError("AccessDenied: ecs:TagResource")
    assert aws_infra.ensure_cluster(ecs, "x", tags={"recoder:managed": "true"}) == "arn:aws:ecs:us-east-1:1:cluster/x"
    assert aws_infra.tag_created(ecs, "ecs", "arn", None) is False


def test_tag_values_are_sanitised():
    tags = aws_infra.recoder_tags("C:\\Users\\동규\\shop<1>", "svc")
    assert tags["recoder:project"].startswith("C:-Users-") and "<" not in tags["recoder:project"]


def test_new_s3_bucket_is_tagged_but_tag_errors_are_ignored():
    from api.routes import deploy_s3
    client = MagicMock()
    deploy_s3._tag_bucket(client, "recoder-x")
    client.put_bucket_tagging.assert_called_once_with(
        Bucket="recoder-x", Tagging={"TagSet": [{"Key": "recoder:managed", "Value": "true"}]})
    client.put_bucket_tagging.side_effect = RuntimeError("denied")
    deploy_s3._tag_bucket(client, "recoder-x")  # 예외가 새지 않는다


# ── 마을: 보안 검사가 막은 패키지 ────────────────────────────────────────

def test_fix_round_emits_security_block_for_untrusted_package(monkeypatch):
    import code_agent as ca
    import gen_engine as ge
    events = []

    class R:
        def call(self, req, agent=None, operation=None):
            return SimpleNamespace(text=json.dumps({"edits": []}), model_used="m", provider="p", metadata={})

    monkeypatch.setattr(ca, "get_router", lambda: R())
    ops = [{"action": "create", "file": "package.json", "content": '{"dependencies": {"tiny-pay-sdk-kr": "1.0.0"}}'}]
    issue = {"severity": "error", "code": "NODE_PACKAGE_UNTRUSTED", "file": "package.json",
             "message": "package.json 에 설치하면 위험한 패키지가 있습니다: `tiny-pay-sdk-kr` — 처음 올라온 지 3일",
             "fix": "이 패키지를 쓰지 마세요."}
    ge.edit_fix_round("p", ops, [issue], emit=events.append)
    block = [e for e in events if e.get("step") == "security_block"]
    assert block == [{"step": "security_block", "agent": "review", "file": "package.json",
                      "message": "수상한 패키지 차단 — tiny-pay-sdk-kr"}]
