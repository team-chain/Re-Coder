"""스캔 실패 사유 분류 — raw 에러 대신 원인·다음 행동.

보드 카드 「스캔 실패 표시가 raw 에러 — 원인·다음 행동을 담은 미검증 문구로 교체」.

여기서 고정하는 것
    1. 실기기에서 실제로 났던 두 문구 — Windows npipe 연결 실패, No such image —
       가 각각 docker_not_running / image_not_found 로 갈린다.
    2. 모든 실패 결과에 reason_code · cause · next_action 이 있고, summary 는
       "확인하지 못했습니다 — …" 로 시작한다(통과와 절대 혼동되지 않게).
    3. 못 맞춘 원문은 unknown 이지만 raw 첫 줄이 cause 에 남는다 — 숨기지 않는다.
    4. _execute_scan 의 모든 실패 분기(에이전트 없음·데몬 없음·타임아웃·
       docker 없음·예외·스캐너 rc≠0)가 같은 필드를 낸다.
    5. 배포 게이트의 미검증 사유에도 원인 → 다음 행동이 실린다.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import scan_failure  # noqa: E402
from api.routes import deploy  # noqa: E402

NPIPE = (
    "error during connect: Get \"http://%2F%2F.%2Fpipe%2Fdocker_engine/v1.24/images/json\": "
    "open //./pipe/docker_engine: The system cannot find the file specified. "
    "In the default daemon configuration on Windows, the docker client must be run elevated to connect."
)
NO_SUCH_IMAGE = (
    "2026-09-20T15:02:11Z\tFATAL\timage scan error: scan error: unable to initialize a scanner: "
    "unable to initialize an image scanner: 4 errors occurred: * docker error: unable to inspect "
    "the image (app:latest): Error response from daemon: No such image: app:latest"
)
SOCK = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"


# ---------------------------------------------------------------------------
# 1·2·3. 분류
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text, code", [
    (NPIPE, scan_failure.DOCKER_NOT_RUNNING),
    (SOCK, scan_failure.DOCKER_NOT_RUNNING),
    (NO_SUCH_IMAGE, scan_failure.IMAGE_NOT_FOUND),
    ("Unable to find image 'app:latest' locally", scan_failure.IMAGE_NOT_FOUND),
    ("Error: pull access denied for app, repository does not exist", scan_failure.IMAGE_NOT_FOUND),
    ("failed to download vulnerability DB: dial tcp 1.2.3.4:443: i/o timeout", scan_failure.SCANNER_PULL_FAILED),
    ("[Errno 2] No such file or directory: 'gitleaks'", scan_failure.SCANNER_MISSING),
    ("hadolint: command not found", scan_failure.SCANNER_MISSING),
    ("Dockerfile not found: /x/Dockerfile", scan_failure.DOCKERFILE_MISSING),
])
def test_실기기_문구가_원인으로_갈린다(text: str, code: str) -> None:
    f = scan_failure.classify(text, scan_type="trivy", target="app:latest")
    assert f.reason_code == code
    assert f.cause and f.next_action
    assert f.as_fields()["summary"].startswith("확인하지 못했습니다 — ")


def test_이미지_없음은_이미지_이름을_원인에_넣고_빌드를_안내한다() -> None:
    f = scan_failure.classify(NO_SUCH_IMAGE, target="app:latest")
    assert "app:latest" in f.cause
    assert "빌드" in f.next_action


def test_docker_미실행은_Docker_Desktop_시작을_안내한다() -> None:
    f = scan_failure.classify(NPIPE)
    assert "Docker Desktop" in f.next_action
    assert f.raw.startswith("error during connect")


def test_모르는_원문은_unknown_이지만_첫_줄을_남긴다() -> None:
    f = scan_failure.classify("\n\nsomething weird happened\nsecond line")
    assert f.reason_code == scan_failure.UNKNOWN
    assert "something weird happened" in f.cause
    assert "second line" not in f.cause
    assert f.as_fields()["message"] == "something weird happened"


def test_code_를_주면_분류를_건너뛴다() -> None:
    f = scan_failure.classify(NO_SUCH_IMAGE, code=scan_failure.TIMEOUT)
    assert f.reason_code == scan_failure.TIMEOUT
    assert "300초" in f.cause


# ---------------------------------------------------------------------------
# 4. _execute_scan 의 실패 분기
# ---------------------------------------------------------------------------


_FIELDS = {"reason_code", "cause", "next_action", "summary", "message"}


def _run(coro):
    return asyncio.run(coro)


def _quiet_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy, "_log_scan_to_session", lambda *a, **k: None)


def test_에이전트가_없으면_dependencies_missing(monkeypatch) -> None:
    _quiet_log(monkeypatch)
    monkeypatch.setattr(deploy, "_get_infra_agent", lambda: None)
    r = _run(deploy._execute_scan("trivy", "", "app:latest"))
    assert r["status"] == "error" and r["reason_code"] == scan_failure.DEPENDENCIES_MISSING
    assert _FIELDS <= set(r)


def test_docker_자동시작이_실패하면_not_run_docker_not_running(monkeypatch) -> None:
    _quiet_log(monkeypatch)
    monkeypatch.setattr(deploy, "_get_infra_agent", lambda: SimpleNamespace())
    import docker_autostart
    monkeypatch.setattr(
        docker_autostart, "ensure_docker",
        lambda wait_seconds=None: SimpleNamespace(ready=False, message="Docker Desktop 시작을 시도했지만 75초 안에 준비되지 않았습니다."),
    )
    r = _run(deploy._execute_scan("trivy", "", "app:latest"))
    assert r["status"] == "not_run"
    assert r["reason_code"] == scan_failure.DOCKER_NOT_RUNNING
    assert r["target"] == "app:latest"
    assert "Docker Desktop" in r["next_action"]


def test_스캐너가_rc_0_이_아니면_stderr_를_분류한다(monkeypatch) -> None:
    _quiet_log(monkeypatch)

    async def run_trivy_scan(image):
        return {"success": False, "error": NO_SUCH_IMAGE, "critical": [], "high": [], "summary": "Scan failed."}

    monkeypatch.setattr(deploy, "_get_infra_agent", lambda: SimpleNamespace(run_trivy_scan=run_trivy_scan))
    import docker_autostart
    monkeypatch.setattr(docker_autostart, "ensure_docker", lambda wait_seconds=None: SimpleNamespace(ready=True, message="ok"))
    r = _run(deploy._execute_scan("trivy", "", "app:latest"))
    assert r["status"] == "error"
    assert r["reason_code"] == scan_failure.IMAGE_NOT_FOUND
    assert "app:latest" in r["cause"]
    assert r["message"].startswith("2026-09-20")     # 원문은 남는다


def test_타임아웃과_docker_부재_예외도_분류된다(monkeypatch) -> None:
    _quiet_log(monkeypatch)
    import docker_autostart
    monkeypatch.setattr(docker_autostart, "ensure_docker", lambda wait_seconds=None: SimpleNamespace(ready=True, message="ok"))

    async def slow(image):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(deploy, "_get_infra_agent", lambda: SimpleNamespace(run_trivy_scan=slow))
    r = _run(deploy._execute_scan("trivy", "", "app:latest"))
    assert r["reason_code"] == scan_failure.TIMEOUT

    async def missing(image):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(deploy, "_get_infra_agent", lambda: SimpleNamespace(run_trivy_scan=missing))
    r = _run(deploy._execute_scan("trivy", "", "app:latest"))
    assert r["reason_code"] == scan_failure.DOCKER_MISSING

    async def boom(image):
        raise RuntimeError(SOCK)

    monkeypatch.setattr(deploy, "_get_infra_agent", lambda: SimpleNamespace(run_trivy_scan=boom))
    r = _run(deploy._execute_scan("trivy", "", "app:latest"))
    assert r["reason_code"] == scan_failure.DOCKER_NOT_RUNNING


def test_gitleaks_바이너리_없음은_scanner_missing(monkeypatch) -> None:
    _quiet_log(monkeypatch)

    async def run_gitleaks_scan(root):
        return {"success": False, "error": "[Errno 2] No such file or directory: 'gitleaks'"}

    monkeypatch.setattr(deploy, "_get_infra_agent", lambda: SimpleNamespace(run_gitleaks_scan=run_gitleaks_scan))
    r = _run(deploy._execute_scan("gitleaks", "/tmp/ws", None))
    assert r["reason_code"] == scan_failure.SCANNER_MISSING
    assert "gitleaks" in r["cause"]


# ---------------------------------------------------------------------------
# 5. 배포 게이트 사유
# ---------------------------------------------------------------------------


def test_게이트_미검증_사유에_원인과_다음_행동이_실린다(monkeypatch) -> None:
    monkeypatch.setattr(deploy, "_local_image_exists", lambda image: True)

    async def broken(scan_type, workspace, target):
        return {
            "status": "error", "scan_type": scan_type, "critical_count": 0, "high_count": 0,
            "findings": [], **scan_failure.failure_fields(NPIPE, scan_type="trivy", target="app:latest"),
        }

    monkeypatch.setattr(deploy, "_execute_scan", broken)
    gate = _run(deploy._run_pre_deploy_security_gate(
        deploy.DeployPlanRequest(workspace_path="/tmp/ws", image="app:latest"),
    ))
    assert gate["unverified"] is True
    reason = next(r for r in gate["risk_reasons"] if r.startswith("Trivy: 스캔 실패"))
    assert "Docker Desktop" in reason and "→" in reason
