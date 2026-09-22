"""롤백 대상 이미지의 **생존 보장** — 고정 태그와 파괴 전 존재 확인.

## 실기기에서 본 실패 (검증 D3)

v1 → v2 로컬 배포 뒤 이상이 감지돼 롤백을 승인했더니
`docker: Error response from daemon: No such image: sha256:1cca…` 로 실패했다.
그리고 **v2 컨테이너도 이미 stop/rm 된 뒤**라 서비스가 통째로 사라졌다.

원인은 둘이다.

1. Docker Desktop 기본인 containerd 이미지 스토어는 태그가 다음 빌드로 옮겨 가고
   그 이미지를 쓰던 컨테이너까지 지워지면 옛 이미지를 **바로 GC** 한다. 이미지
   ID 는 불변이지만 영원하지는 않다. 그래서 배포 직후 `<repo>:recoder-rb-<배포ID>`
   고정 태그를 붙여 참조를 붙잡아 둔다.
2. 롤백 라우트가 되돌릴 이미지가 있는지 보지 않고 먼저 컨테이너를 지웠다.
   이제 `docker image inspect` 로 먼저 확인하고, 없으면 **아무것도 건드리지 않고**
   실패한다.
"""
from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

import api.routes.deploy as deploy_route
from schemas import DeploymentRecord, DeployMethod, DeployStatus


@pytest.fixture(autouse=True)
def clean_records():
    deploy_route._deployment_records.clear()
    yield
    deploy_route._deployment_records.clear()


def _record(
    container: str,
    image: str,
    *,
    image_id: str | None = None,
    pinned_image: str | None = None,
    rollback_target: str | None = None,
    status: DeployStatus = DeployStatus.SUCCESS,
) -> DeploymentRecord:
    rec = DeploymentRecord(
        project_id="p",
        method=DeployMethod.LOCAL_DOCKER,
        image=image,
        image_id=image_id,
        pinned_image=pinned_image,
        container_name=container,
        ports={"3456": "3456"},
        status=status,
        rollback_eligible=True,
        rollback_target=rollback_target,
    )
    deploy_route._deployment_records[rec.deployment_id] = rec
    return rec


# ── 고정 태그 이름 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ref, repo",
    [
        ("recoder-sample:latest", "recoder-sample"),
        ("recoder-sample", "recoder-sample"),
        ("ghcr.io/team/app:v1", "ghcr.io/team/app"),
        ("localhost:5000/app:v1", "localhost:5000/app"),
        ("localhost:5000/app", "localhost:5000/app"),
        ("app@sha256:abcd", "app"),
    ],
)
def test_저장소_이름은_태그와_다이제스트를_뗀다(ref, repo):
    assert deploy_route._image_repo(ref) == repo


def test_고정_태그는_저장소와_배포ID_로_만든다():
    tag = deploy_route._pin_tag_for("recoder-sample:latest", "0123456789abcdefXYZ")

    assert tag == "recoder-sample:recoder-rb-0123456789ab"


def test_이미지_이름이_비면_고정_태그를_만들지_않는다():
    assert deploy_route._pin_tag_for("", "abc") is None
    assert deploy_route._pin_tag_for("app:latest", "") is None


# ── 롤백 대상 선택: 고정 태그 > 이미지 ID > 태그 ─────────────────────────────


def test_같은_태그_재배포는_고정_태그를_이미지_ID_보다_먼저_쓴다():
    _record(
        "api", "api:latest",
        image_id="sha256:1cca5b585ba949d10013f96a3145ad3cf3c13832ae9f77ded35f5bf916e9c9b8",
        pinned_image="api:recoder-rb-aaaaaaaaaaaa",
    )

    target, reason = deploy_route._previous_image_for("api", "api:latest")

    assert target == "api:recoder-rb-aaaaaaaaaaaa"
    assert "고정 태그" in reason


def test_다른_태그_배포도_고정_태그가_있으면_그걸_쓴다():
    _record("api", "api:v1", image_id="sha256:v1", pinned_image="api:recoder-rb-bbbbbbbbbbbb")

    target, _ = deploy_route._previous_image_for("api", "api:v2")

    assert target == "api:recoder-rb-bbbbbbbbbbbb"


def test_고정_태그가_없으면_이미지_ID_로_폴백한다():
    _record("api", "api:latest", image_id="sha256:only-id")

    target, _ = deploy_route._previous_image_for("api", "api:latest")

    assert target == "sha256:only-id"


def test_같은_태그_기록도_불변_참조가_있으면_실행_조건_원본이_된다():
    """대상은 찾는데 원본 기록(포트·env·감시 재개)이 비는 불일치를 막는다."""
    rec = _record("api", "api:latest", pinned_image="api:recoder-rb-cccccccccccc")

    source = deploy_route._rollback_source_for("api", "api:latest")

    assert source is rec


def test_같은_태그_기록에_불변_참조가_없으면_원본도_없다():
    _record("api", "api:latest")

    assert deploy_route._rollback_source_for("api", "api:latest") is None


# ── 롤백 라우트: 파괴 전 존재 확인 ───────────────────────────────────────────


def test_되돌릴_이미지가_없으면_컨테이너를_건드리지_않고_실패한다(monkeypatch):
    rec = _record("api", "api:latest", rollback_target="sha256:gone")

    async def _missing(_ref):
        return False

    calls: list[list[str]] = []

    def _spy_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(deploy_route, "_rollback_image_available", _missing)
    monkeypatch.setattr(deploy_route.subprocess, "run", _spy_run)

    result = asyncio.run(
        deploy_route.rollback(deploy_route.RollbackRequest(deployment_id=rec.deployment_id))
    )

    assert result["status"] == "failed"
    assert result["container_untouched"] is True
    assert "없습니다" in result["error"] and "건드리지 않았습니다" in result["error"]
    assert not any(c[:2] in (["docker", "stop"], ["docker", "rm"], ["docker", "run"]) for c in calls), (
        f"이미지가 없는데 docker 를 건드렸다: {calls}"
    )
    assert rec.status == DeployStatus.SUCCESS, "실패한 롤백이 기록 상태를 바꿨다"


def test_이미지_존재_확인은_docker_image_inspect_를_쓴다(monkeypatch):
    seen: list[list[str]] = []

    def _fake_run(args, **_kwargs):
        seen.append(list(args))
        return SimpleNamespace(returncode=0, stdout="sha256:abc\n", stderr="")

    monkeypatch.setattr(deploy_route.subprocess, "run", _fake_run)

    assert asyncio.run(deploy_route._rollback_image_available("api:recoder-rb-x")) is True
    assert seen[0][:3] == ["docker", "image", "inspect"]
    assert seen[0][-1] == "api:recoder-rb-x"


def test_이미지_존재_확인_실패는_없음으로_본다(monkeypatch):
    def _fail(args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="No such image")

    monkeypatch.setattr(deploy_route.subprocess, "run", _fail)

    assert asyncio.run(deploy_route._rollback_image_available("sha256:gone")) is False
    assert asyncio.run(deploy_route._rollback_image_available("")) is False


# ── 고정 태그 붙이기·정리 ────────────────────────────────────────────────────


def test_배포_직후_이미지에_고정_태그를_붙인다(monkeypatch):
    seen: list[list[str]] = []

    def _fake_run(args, **_kwargs):
        seen.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(deploy_route.subprocess, "run", _fake_run)

    pin = asyncio.run(
        deploy_route._pin_rollback_image("recoder-sample:latest", "sha256:1cca", "deadbeefcafe0001")
    )

    assert pin == "recoder-sample:recoder-rb-deadbeefcafe"
    assert seen == [["docker", "tag", "sha256:1cca", "recoder-sample:recoder-rb-deadbeefcafe"]]


def test_고정_태그_실패는_None_이고_배포를_막지_않는다(monkeypatch):
    def _fail(args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(deploy_route.subprocess, "run", _fail)

    assert asyncio.run(deploy_route._pin_rollback_image("app:latest", None, "x" * 12)) is None


def test_오래된_고정_태그는_최근_N개와_보호_대상만_남기고_지운다(monkeypatch):
    rows = [
        "app:recoder-rb-000000000005\t2026-09-22 05:00:00 +0900 KST",
        "app:recoder-rb-000000000001\t2026-09-22 01:00:00 +0900 KST",
        "app:recoder-rb-000000000003\t2026-09-22 03:00:00 +0900 KST",
        "app:recoder-rb-000000000002\t2026-09-22 02:00:00 +0900 KST",
        "app:recoder-rb-000000000004\t2026-09-22 04:00:00 +0900 KST",
    ]
    removed: list[str] = []

    def _fake_run(args, **_kwargs):
        if args[:2] == ["docker", "images"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(rows) + "\n", stderr="")
        if args[:2] == ["docker", "rmi"]:
            removed.append(args[2])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker call: {args}")

    monkeypatch.setattr(deploy_route.subprocess, "run", _fake_run)

    pruned = asyncio.run(
        deploy_route._prune_old_rollback_pins("app:latest", keep={"app:recoder-rb-000000000001"})
    )

    # 최신 3개(5,4,3)는 남고, 1은 보호 대상이라 남고, 2만 지운다.
    assert pruned == ["app:recoder-rb-000000000002"]
    assert removed == ["app:recoder-rb-000000000002"]


def test_고정_태그가_적으면_아무것도_지우지_않는다(monkeypatch):
    def _fake_run(args, **_kwargs):
        assert args[:2] == ["docker", "images"], f"목록만 봐야 한다: {args}"
        return SimpleNamespace(
            returncode=0,
            stdout="app:recoder-rb-000000000001\t2026-09-22 01:00:00 +0900 KST\n",
            stderr="",
        )

    monkeypatch.setattr(deploy_route.subprocess, "run", _fake_run)

    assert asyncio.run(deploy_route._prune_old_rollback_pins("app:latest", keep=set())) == []


def test_복구도_고정_태그를_이미지_ID_보다_먼저_쓴다(monkeypatch):
    """교체 배포가 실패했을 때 이전 컨테이너를 되살리는 경로도 같은 규칙."""
    rec = DeploymentRecord(
        project_id="p", method=DeployMethod.LOCAL_DOCKER, image="app:latest",
        image_id="sha256:gone", pinned_image="app:recoder-rb-keepme",
        container_name="app", ports={"3456": "3456"}, status=DeployStatus.SUCCESS,
    )
    runs: list[list[str]] = []

    def _fake_run(args, **_kwargs):
        runs.append(list(args))
        return SimpleNamespace(returncode=0, stdout="cid\n", stderr="")

    async def _healthy(_ports, _path):
        return True

    monkeypatch.setattr(deploy_route.subprocess, "run", _fake_run)
    monkeypatch.setattr(deploy_route, "_probe_local_http_health", _healthy)

    ok, _out, _err = asyncio.run(deploy_route._restore_prior_local_container(rec))

    assert ok is True
    run_cmd = next(c for c in runs if c[:2] == ["docker", "run"])
    assert run_cmd[-1] == "app:recoder-rb-keepme"
