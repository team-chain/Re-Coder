"""
Unit tests for Runtime Preflight (§31).

Docker 미설치 환경에서도 안전하게 돌도록 설계:
  - detect_docker() 호출만 하는 path 는 항상 동작
  - 실제 docker run 이 필요한 path 는 docker 없으면 자동 skip + WARN 처리
  - HTTP probe / log masking / port wait 같은 순수 함수 단위 검증
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterator

import pytest

_CORE = Path(__file__).resolve().parents[2]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from preflight.runtime import (  # noqa: E402
    DockerCapabilities,
    RuntimePreflightResult,
    RuntimePreflightRunner,
    detect_docker,
    detect_log_pattern_issues,
    fetch_container_logs,
    http_probe,
    mask_log,
    merge_runtime_into_preflight_run,
    parse_duration,
    run_smoke_tests,
    wait_for_health,
    wait_for_port_listen,
)
from schemas import (  # noqa: E402
    ContractProjectMeta,
    ContractRuntime,
    ContractSmokeTest,
    ContractStack,
    PreflightRun,
    PreflightRuntimeChecks,
    PreflightStatus,
    ReleaseContract,
)


# ---------------------------------------------------------------------------
# Helpers — synthetic HTTP server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """테스트용 미니 HTTP 서버 — /health 200 / /fail 500 / /echo 200 응답."""

    def log_message(self, format: str, *args: object) -> None:  # silence
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        elif self.path == "/fail":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"fail")
        elif self.path.startswith("/echo"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"echoed")
        else:
            self.send_response(404)
            self.end_headers()


def _free_port() -> int:
    """OS 가 할당한 free port. immediately released — 잠시 race window 있으나 충분."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mini_http_server() -> Iterator[tuple[str, int]]:
    """테스트 동안 떠 있는 미니 HTTP 서버 (127.0.0.1:<random>)."""
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # 약간의 ramp-up
        time.sleep(0.05)
        yield ("127.0.0.1", port)
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_parse_duration__seconds() -> None:
    assert parse_duration("30s") == 30
    assert parse_duration("30") == 30


def test_parse_duration__minutes() -> None:
    assert parse_duration("5m") == 300


def test_parse_duration__hours() -> None:
    assert parse_duration("1h") == 3600


def test_parse_duration__default_on_bad_input() -> None:
    assert parse_duration("", default=42) == 42
    assert parse_duration(None, default=42) == 42
    assert parse_duration("bogus", default=42) == 42


def test_mask_log__aws_key_redacted() -> None:
    out = mask_log("Found AKIAIOSFODNN7EXAMPLE in config")
    assert "AKIA" not in out
    assert "<REDACTED>" in out


def test_mask_log__github_pat_redacted() -> None:
    out = mask_log("token=ghp_" + "A" * 36 + " was logged")
    assert "ghp_" not in out


def test_mask_log__openai_key_redacted() -> None:
    out = mask_log("OPENAI_API_KEY=sk-" + "A" * 48)
    assert "sk-" + "A" * 48 not in out


def test_mask_log__private_key_block_redacted() -> None:
    out = mask_log("-----BEGIN RSA PRIVATE KEY-----\nasdfgh\n-----END...")
    assert "BEGIN RSA PRIVATE KEY" not in out


def test_mask_log__bearer_token_redacted() -> None:
    out = mask_log("Authorization: Bearer abcdefghijklmnop1234567890XYZ")
    assert "Bearer abcdefghijklmnop1234567890XYZ" not in out


def test_mask_log__keyvalue_pattern_redacted() -> None:
    out = mask_log('API_KEY="super-long-secret-value-here"')
    assert "super-long-secret-value-here" not in out


def test_mask_log__empty_returns_empty() -> None:
    assert mask_log("") == ""


def test_detect_log_pattern__clean_logs_ok() -> None:
    ok, bad = detect_log_pattern_issues(
        "Application startup complete\nServing on 0.0.0.0:8000\n"
    )
    assert ok is True
    assert bad == []


def test_detect_log_pattern__fatal_detected() -> None:
    ok, bad = detect_log_pattern_issues(
        "INFO starting\nFATAL: cannot bind to port 8000\nbye"
    )
    assert ok is False
    assert any("fatal" in line.lower() for line in bad)


def test_detect_log_pattern__module_not_found() -> None:
    ok, bad = detect_log_pattern_issues(
        "Traceback...\nModuleNotFoundError: No module named 'fastapi'"
    )
    assert ok is False
    assert bad  # at least one


def test_detect_log_pattern__caps_at_5() -> None:
    text = "\n".join(["FATAL: x"] * 20)
    ok, bad = detect_log_pattern_issues(text)
    assert ok is False
    assert len(bad) <= 5


# ---------------------------------------------------------------------------
# HTTP probe + wait_for_* — uses real local HTTP server
# ---------------------------------------------------------------------------


def test_http_probe__200_health(mini_http_server: tuple[str, int]) -> None:
    host, port = mini_http_server
    ok, status, body = http_probe(host, port, path="/health", timeout=2.0)
    assert ok is True
    assert status == 200
    assert "ok" in body


def test_http_probe__500_failure(mini_http_server: tuple[str, int]) -> None:
    host, port = mini_http_server
    ok, status, _ = http_probe(host, port, path="/fail")
    assert ok is False
    assert status == 500


def test_http_probe__expected_status_accepts_500(mini_http_server: tuple[str, int]) -> None:
    host, port = mini_http_server
    ok, status, _ = http_probe(
        host, port, path="/fail", expected_status=[500]
    )
    assert ok is True


def test_http_probe__connection_refused() -> None:
    """LISTEN 안 하는 포트로 probe → ok=False, status=0."""
    ok, status, _ = http_probe("127.0.0.1", 1, path="/", timeout=0.3)
    assert ok is False
    assert status == 0


def test_wait_for_port_listen__success_quick(mini_http_server: tuple[str, int]) -> None:
    host, port = mini_http_server
    t0 = time.monotonic()
    ok = wait_for_port_listen(host, port, timeout_seconds=2)
    assert ok is True
    assert time.monotonic() - t0 < 2.0


def test_wait_for_port_listen__timeout_when_no_listener() -> None:
    """아무도 LISTEN 안 하면 timeout."""
    t0 = time.monotonic()
    ok = wait_for_port_listen("127.0.0.1", 1, timeout_seconds=1)
    elapsed = time.monotonic() - t0
    assert ok is False
    # Windows scheduler slop 고려 — lower bound 만 강제, upper bound 는 넉넉히
    assert 0.8 <= elapsed <= 3.0


def test_wait_for_health__success(mini_http_server: tuple[str, int]) -> None:
    host, port = mini_http_server
    ok, attempts = wait_for_health(host, port, "/health", timeout_seconds=2)
    assert ok is True
    assert attempts >= 1


def test_wait_for_health__500_not_ok(mini_http_server: tuple[str, int]) -> None:
    host, port = mini_http_server
    ok, attempts = wait_for_health(host, port, "/fail", timeout_seconds=1)
    assert ok is False
    assert attempts >= 1


def test_run_smoke_tests__all_pass(mini_http_server: tuple[str, int]) -> None:
    host, port = mini_http_server
    smokes = [
        ContractSmokeTest(path="/health"),
        ContractSmokeTest(path="/echo"),
    ]
    ok, results = run_smoke_tests(host, port, smokes)
    assert ok is True
    assert len(results) == 2
    assert all(r["passed"] for r in results)


def test_run_smoke_tests__one_fail(mini_http_server: tuple[str, int]) -> None:
    host, port = mini_http_server
    smokes = [
        ContractSmokeTest(path="/health"),
        ContractSmokeTest(path="/fail"),
    ]
    ok, results = run_smoke_tests(host, port, smokes)
    assert ok is False
    failed = [r for r in results if not r["passed"]]
    assert len(failed) == 1
    assert failed[0]["path"] == "/fail"


# ---------------------------------------------------------------------------
# Docker detection — works regardless of Docker presence
# ---------------------------------------------------------------------------


def test_detect_docker__returns_capabilities() -> None:
    caps = detect_docker()
    assert isinstance(caps, DockerCapabilities)
    # cli_available / sdk_available are bool — we just verify shape
    assert isinstance(caps.cli_available, bool)
    assert isinstance(caps.sdk_available, bool)


# ---------------------------------------------------------------------------
# Runner — skip path when docker missing
# ---------------------------------------------------------------------------


def _mk_contract() -> ReleaseContract:
    return ReleaseContract(
        project=ContractProjectMeta(name="test", stack=ContractStack.PYTHON_FASTAPI),
        runtime=ContractRuntime(host_port=18080, app_port=18000),
    )


def test_runner__skips_when_docker_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """detect_docker 가 False 면 RuntimePreflightRunner 는 WARN 으로 즉시 반환."""

    def fake_detect_docker() -> DockerCapabilities:
        return DockerCapabilities(cli_available=False, sdk_available=False)

    monkeypatch.setattr("preflight.runtime.detect_docker", fake_detect_docker)
    runner = RuntimePreflightRunner(tmp_path, _mk_contract())
    result = runner.run()
    assert isinstance(result, RuntimePreflightResult)
    assert result.status == PreflightStatus.WARN
    assert any("docker CLI" in i for i in result.issues)


def test_runner__no_image_returns_warn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """docker 는 있는데 image_ref 미지정 + RECODER_IMAGE 환경변수도 없으면 WARN."""

    def fake_detect_docker() -> DockerCapabilities:
        return DockerCapabilities(cli_available=True, sdk_available=False, version_text="24.0")

    monkeypatch.setattr("preflight.runtime.detect_docker", fake_detect_docker)
    monkeypatch.delenv("RECODER_IMAGE", raising=False)
    runner = RuntimePreflightRunner(tmp_path, _mk_contract())
    result = runner.run()
    assert result.status == PreflightStatus.WARN
    assert any("image_ref" in i for i in result.issues)


def test_runner__env_from_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """._build_env() 가 .env 파일을 읽어 dict 로 반환."""
    monkeypatch.delenv("RECODER_IMAGE", raising=False)
    (tmp_path / ".env").write_text(
        'PORT=8000\nDATABASE_URL="postgres://x:y@host/db"\n# comment\nKEY=val\n',
        encoding="utf-8",
    )
    runner = RuntimePreflightRunner(tmp_path, _mk_contract())
    env = runner._build_env()
    assert env["PORT"] == "18000"  # contract.runtime.app_port — overrides .env PORT
    assert env["DATABASE_URL"] == "postgres://x:y@host/db"
    assert env["KEY"] == "val"


# ---------------------------------------------------------------------------
# merge_runtime_into_preflight_run — pure
# ---------------------------------------------------------------------------


def test_merge__success_keeps_passed() -> None:
    pre = PreflightRun(status=PreflightStatus.PASSED, score=100)
    rc = PreflightRuntimeChecks(
        container_alive=True, health_passed=True,
        smoke_passed=True, log_pattern_ok=True,
    )
    rt = RuntimePreflightResult(
        runtime_checks=rc, status=PreflightStatus.PASSED, issues=[],
    )
    merged = merge_runtime_into_preflight_run(pre, rt)
    assert merged.status == PreflightStatus.PASSED
    assert merged.score == 100
    assert merged.runtime_checks.container_alive is True


def test_merge__runtime_blocker_demotes_status() -> None:
    pre = PreflightRun(status=PreflightStatus.PASSED, score=100)
    rc = PreflightRuntimeChecks(container_alive=False)
    rt = RuntimePreflightResult(
        runtime_checks=rc, status=PreflightStatus.BLOCKED, issues=["container died"],
    )
    merged = merge_runtime_into_preflight_run(pre, rt)
    assert merged.status == PreflightStatus.BLOCKED
    assert merged.score < 100


def test_merge__static_warn_runtime_warn_stays_warn() -> None:
    pre = PreflightRun(status=PreflightStatus.WARN, score=85)
    rt = RuntimePreflightResult(
        runtime_checks=PreflightRuntimeChecks(
            container_alive=True, health_passed=True, smoke_passed=True,
            log_pattern_ok=False,
        ),
        status=PreflightStatus.WARN,
        issues=["log noise"],
    )
    merged = merge_runtime_into_preflight_run(pre, rt)
    assert merged.status == PreflightStatus.WARN
    assert merged.score < 85  # log_pattern_ok=False → -10


def test_merge__health_fail_subtracts_20() -> None:
    pre = PreflightRun(status=PreflightStatus.PASSED, score=100)
    rt = RuntimePreflightResult(
        runtime_checks=PreflightRuntimeChecks(
            container_alive=True, health_passed=False,
        ),
        status=PreflightStatus.BLOCKED,
    )
    merged = merge_runtime_into_preflight_run(pre, rt)
    assert merged.score == 80  # 100 - 20


def test_merge__static_blocked_stays_blocked_even_if_runtime_passed() -> None:
    pre = PreflightRun(status=PreflightStatus.BLOCKED, score=50)
    rt = RuntimePreflightResult(
        runtime_checks=PreflightRuntimeChecks(
            container_alive=True, health_passed=True,
            smoke_passed=True, log_pattern_ok=True,
        ),
        status=PreflightStatus.PASSED,
    )
    merged = merge_runtime_into_preflight_run(pre, rt)
    assert merged.status == PreflightStatus.BLOCKED  # static 이 더 나쁨
