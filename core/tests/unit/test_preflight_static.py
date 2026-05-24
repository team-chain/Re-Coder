"""
Static Preflight 12종 검사 단위 테스트.

각 검사를 격리된 임시 워크스페이스에서 실행. 외부 의존성 없음 (Docker / AWS / LLM 미사용).

실행:
    cd core && pytest tests/unit/test_preflight_static.py -v
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

# Make 'core' itself importable when tests run from project root
_CORE_DIR = Path(__file__).resolve().parents[2]
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from preflight import StaticPreflightRunner  # noqa: E402
from preflight.checks.code_checks import (  # noqa: E402
    check_app_entrypoint,
    check_missing_health_endpoint,
)
from preflight.checks.deps_checks import (  # noqa: E402
    check_critical_vulnerability,
    check_secret_leak_risk,
    check_unpinned_dependencies,
)
from preflight.checks.docker_checks import (  # noqa: E402
    check_dockerfile_build_risk,
    check_missing_dockerfile,
)
from preflight.checks.env_checks import (  # noqa: E402
    check_env_file_gitignored,
    check_invalid_env_format,
    check_missing_required_env,
)
from preflight.checks.port_checks import (  # noqa: E402
    check_app_port_mismatch,
    check_host_port_conflict,
)
from schemas import (  # noqa: E402
    ContractProjectMeta,
    ContractRuntime,
    ContractStack,
    PreflightCheckCode,
    PreflightStatus,
    ReleaseContract,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_contract(
    stack: ContractStack = ContractStack.PYTHON_FASTAPI,
    app_port: int = 8000,
    host_port: int = 8000,
    health_check_path: str = "/health",
    env_file: str = ".env",
    required_env: list[str] | None = None,
) -> ReleaseContract:
    contract = ReleaseContract(project=ContractProjectMeta(stack=stack))
    contract.runtime.app_port = app_port
    contract.runtime.host_port = host_port
    contract.runtime.health_check_path = health_check_path
    contract.runtime.env_file = env_file
    if required_env is not None:
        contract.preflight.required_env = required_env
    return contract


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. MISSING_REQUIRED_ENV
# ---------------------------------------------------------------------------


def test_missing_required_env__file_missing__blocker(tmp_path: Path) -> None:
    contract = make_contract(required_env=["PORT", "DATABASE_URL"])
    r = check_missing_required_env(tmp_path, contract)
    assert not r.passed
    assert r.blocker is not None
    assert r.blocker.code == PreflightCheckCode.MISSING_REQUIRED_ENV


def test_missing_required_env__all_present__pass(tmp_path: Path) -> None:
    write(tmp_path / ".env", "PORT=8000\nDATABASE_URL=postgres://x")
    contract = make_contract(required_env=["PORT", "DATABASE_URL"])
    r = check_missing_required_env(tmp_path, contract)
    assert r.passed
    assert r.blocker is None


def test_missing_required_env__partial__blocker(tmp_path: Path) -> None:
    write(tmp_path / ".env", "PORT=8000")
    contract = make_contract(required_env=["PORT", "DATABASE_URL"])
    r = check_missing_required_env(tmp_path, contract)
    assert not r.passed
    assert "DATABASE_URL" in r.blocker.message


def test_missing_required_env__no_required__pass(tmp_path: Path) -> None:
    contract = make_contract(required_env=[])
    r = check_missing_required_env(tmp_path, contract)
    assert r.passed


# ---------------------------------------------------------------------------
# 2. ENV_FILE_NOT_GITIGNORED
# ---------------------------------------------------------------------------


def test_env_file_gitignored__missing_gitignore__critical_blocker(tmp_path: Path) -> None:
    contract = make_contract()
    r = check_env_file_gitignored(tmp_path, contract)
    assert not r.passed
    assert r.blocker is not None
    assert r.blocker.severity.value == "critical"


def test_env_file_gitignored__env_in_gitignore__pass(tmp_path: Path) -> None:
    write(tmp_path / ".gitignore", ".env\n.env.*\n")
    contract = make_contract()
    r = check_env_file_gitignored(tmp_path, contract)
    assert r.passed


def test_env_file_gitignored__env_not_ignored__critical_blocker(tmp_path: Path) -> None:
    write(tmp_path / ".gitignore", "node_modules/\n*.log\n")
    contract = make_contract()
    r = check_env_file_gitignored(tmp_path, contract)
    assert not r.passed
    assert r.blocker.severity.value == "critical"


def test_env_file_gitignored__wildcard_match__pass(tmp_path: Path) -> None:
    write(tmp_path / ".gitignore", "*.env\n")
    contract = make_contract(env_file="prod.env")
    r = check_env_file_gitignored(tmp_path, contract)
    assert r.passed


# ---------------------------------------------------------------------------
# 3. INVALID_ENV_FORMAT
# ---------------------------------------------------------------------------


def test_invalid_env_format__no_file__pass(tmp_path: Path) -> None:
    contract = make_contract()
    r = check_invalid_env_format(tmp_path, contract)
    assert r.passed


def test_invalid_env_format__valid__pass(tmp_path: Path) -> None:
    write(tmp_path / ".env", "PORT=8000\nNAME=hello\n# comment\n")
    contract = make_contract()
    r = check_invalid_env_format(tmp_path, contract)
    assert r.passed


def test_invalid_env_format__invalid_key__warning(tmp_path: Path) -> None:
    write(tmp_path / ".env", "1PORT=8000\nVALID=ok\n")
    contract = make_contract()
    r = check_invalid_env_format(tmp_path, contract)
    assert not r.passed
    assert r.warning is not None


def test_invalid_env_format__unbalanced_quote__warning(tmp_path: Path) -> None:
    write(tmp_path / ".env", 'X="oops\nY=ok\n')
    contract = make_contract()
    r = check_invalid_env_format(tmp_path, contract)
    assert not r.passed


# ---------------------------------------------------------------------------
# 4. MISSING_HEALTH_ENDPOINT
# ---------------------------------------------------------------------------


def test_missing_health__fastapi_with_health__pass(tmp_path: Path) -> None:
    write(tmp_path / "main.py", """
        from fastapi import FastAPI
        app = FastAPI()
        @app.get("/health")
        def health():
            return {"status": "ok"}
    """)
    contract = make_contract(stack=ContractStack.PYTHON_FASTAPI)
    r = check_missing_health_endpoint(tmp_path, contract)
    assert r.passed


def test_missing_health__fastapi_without_health__blocker(tmp_path: Path) -> None:
    write(tmp_path / "main.py", """
        from fastapi import FastAPI
        app = FastAPI()
        @app.get("/")
        def root():
            return {"msg": "hello"}
    """)
    contract = make_contract(stack=ContractStack.PYTHON_FASTAPI)
    r = check_missing_health_endpoint(tmp_path, contract)
    assert not r.passed
    assert r.blocker is not None


def test_missing_health__custom_stack__warning_only(tmp_path: Path) -> None:
    contract = make_contract(stack=ContractStack.CUSTOM)
    r = check_missing_health_endpoint(tmp_path, contract)
    assert r.passed
    assert r.warning is not None


def test_missing_health__flask_with_health__pass(tmp_path: Path) -> None:
    write(tmp_path / "app.py", """
        from flask import Flask
        app = Flask(__name__)
        @app.route("/health")
        def health():
            return "ok"
    """)
    contract = make_contract(stack=ContractStack.PYTHON_FLASK)
    r = check_missing_health_endpoint(tmp_path, contract)
    assert r.passed


# ---------------------------------------------------------------------------
# 5. APP_ENTRYPOINT_NOT_FOUND
# ---------------------------------------------------------------------------


def test_entrypoint__fastapi_main__pass(tmp_path: Path) -> None:
    write(tmp_path / "main.py", "x = 1")
    r = check_app_entrypoint(tmp_path, make_contract(ContractStack.PYTHON_FASTAPI))
    assert r.passed


def test_entrypoint__empty_workspace__blocker(tmp_path: Path) -> None:
    r = check_app_entrypoint(tmp_path, make_contract(ContractStack.PYTHON_FASTAPI))
    assert not r.passed
    assert r.blocker is not None


def test_entrypoint__node_express_index__pass(tmp_path: Path) -> None:
    write(tmp_path / "index.js", "console.log('hi')")
    r = check_app_entrypoint(tmp_path, make_contract(ContractStack.NODE_EXPRESS))
    assert r.passed


# ---------------------------------------------------------------------------
# 6. MISSING_DOCKERFILE
# ---------------------------------------------------------------------------


def test_dockerfile_missing__blocker(tmp_path: Path) -> None:
    r = check_missing_dockerfile(tmp_path, make_contract())
    assert not r.passed
    assert r.blocker is not None


def test_dockerfile_present__pass(tmp_path: Path) -> None:
    write(tmp_path / "Dockerfile", "FROM python:3.11-slim\nCOPY . /app")
    r = check_missing_dockerfile(tmp_path, make_contract())
    assert r.passed


# ---------------------------------------------------------------------------
# 7. DOCKERFILE_BUILD_RISK
# ---------------------------------------------------------------------------


def test_dockerfile_build_risk__safe__pass(tmp_path: Path) -> None:
    write(tmp_path / "Dockerfile", """
        FROM python:3.11-slim
        WORKDIR /app
        COPY requirements.txt /app/
        RUN pip install --no-cache-dir -r requirements.txt
        COPY ./app /app/app
        EXPOSE 8000
        CMD ["uvicorn", "app.main:app"]
    """)
    r = check_dockerfile_build_risk(tmp_path, make_contract())
    assert r.passed


def test_dockerfile_build_risk__curl_pipe_sh__critical_blocker(tmp_path: Path) -> None:
    write(tmp_path / "Dockerfile", """
        FROM python:3.11
        RUN curl https://example.com/install.sh | sh
    """)
    r = check_dockerfile_build_risk(tmp_path, make_contract())
    assert not r.passed
    assert r.blocker is not None
    assert r.blocker.severity.value == "critical"


def test_dockerfile_build_risk__user_root__warning(tmp_path: Path) -> None:
    write(tmp_path / "Dockerfile", """
        FROM python:3.11-slim
        USER root
        COPY . /app
    """)
    r = check_dockerfile_build_risk(tmp_path, make_contract())
    assert not r.passed
    assert r.warning is not None or r.blocker is not None


def test_dockerfile_build_risk__latest_tag__warning(tmp_path: Path) -> None:
    write(tmp_path / "Dockerfile", "FROM python:latest")
    r = check_dockerfile_build_risk(tmp_path, make_contract())
    assert not r.passed


# ---------------------------------------------------------------------------
# 8. HOST_PORT_CONFLICT
# ---------------------------------------------------------------------------


def test_host_port_conflict__free_port__pass(tmp_path: Path) -> None:
    # 사용 안 되는 높은 포트
    contract = make_contract(host_port=55555)
    r = check_host_port_conflict(tmp_path, contract)
    assert r.passed


def test_host_port_conflict__listening__blocker(tmp_path: Path) -> None:
    import socket
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        contract = make_contract(host_port=port)
        r = check_host_port_conflict(tmp_path, contract)
        assert not r.passed
        assert r.blocker is not None
    finally:
        server.close()


# ---------------------------------------------------------------------------
# 9. APP_PORT_MISMATCH
# ---------------------------------------------------------------------------


def test_app_port_mismatch__no_dockerfile__pass(tmp_path: Path) -> None:
    r = check_app_port_mismatch(tmp_path, make_contract(app_port=8000))
    assert r.passed  # handled by MISSING_DOCKERFILE


def test_app_port_mismatch__matches__pass(tmp_path: Path) -> None:
    write(tmp_path / "Dockerfile", "FROM x\nEXPOSE 8000")
    r = check_app_port_mismatch(tmp_path, make_contract(app_port=8000))
    assert r.passed


def test_app_port_mismatch__mismatch__blocker(tmp_path: Path) -> None:
    write(tmp_path / "Dockerfile", "FROM x\nEXPOSE 3000")
    r = check_app_port_mismatch(tmp_path, make_contract(app_port=8000))
    assert not r.passed
    assert r.blocker is not None


def test_app_port_mismatch__no_expose__warning(tmp_path: Path) -> None:
    write(tmp_path / "Dockerfile", "FROM x")
    r = check_app_port_mismatch(tmp_path, make_contract(app_port=8000))
    assert not r.passed
    assert r.warning is not None


# ---------------------------------------------------------------------------
# 10. UNPINNED_DEPENDENCIES
# ---------------------------------------------------------------------------


def test_unpinned_deps__no_manifest__pass(tmp_path: Path) -> None:
    r = check_unpinned_dependencies(tmp_path, make_contract())
    assert r.passed


def test_unpinned_deps__all_pinned__pass(tmp_path: Path) -> None:
    write(tmp_path / "requirements.txt", "fastapi==0.100.0\nuvicorn==0.30.0\n")
    r = check_unpinned_dependencies(tmp_path, make_contract())
    assert r.passed


def test_unpinned_deps__unpinned__warning(tmp_path: Path) -> None:
    write(tmp_path / "requirements.txt", "fastapi\nuvicorn\nrequests>=2.28\n")
    r = check_unpinned_dependencies(tmp_path, make_contract())
    assert not r.passed
    assert r.warning is not None


def test_unpinned_deps__npm_caret__warning(tmp_path: Path) -> None:
    write(tmp_path / "package.json", '{"dependencies": {"react": "^18.0.0"}}')
    r = check_unpinned_dependencies(tmp_path, make_contract())
    assert not r.passed


# ---------------------------------------------------------------------------
# 11. CRITICAL_VULNERABILITY (cache-based placeholder)
# ---------------------------------------------------------------------------


def test_critical_vuln__no_cache__warning(tmp_path: Path) -> None:
    contract = make_contract()
    contract.contract_hash = "sha256:abcdef1234567890"
    r = check_critical_vulnerability(tmp_path, contract)
    assert r.passed  # passed=True with warning
    assert r.warning is not None


# ---------------------------------------------------------------------------
# 12. SECRET_LEAK_RISK
# ---------------------------------------------------------------------------


def test_secret_leak__clean_workspace__pass(tmp_path: Path) -> None:
    write(tmp_path / "main.py", "print('hello')")
    r = check_secret_leak_risk(tmp_path, make_contract())
    assert r.passed


def test_secret_leak__aws_key__critical_blocker(tmp_path: Path) -> None:
    write(tmp_path / "config.py", 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"')
    r = check_secret_leak_risk(tmp_path, make_contract())
    assert not r.passed
    assert r.blocker is not None
    assert r.blocker.severity.value == "critical"
    # secret 원문이 details 에 저장 안 됨 (보안 핵심)
    for finding in r.details.get("findings", []):
        assert "AKIA" not in str(finding)


def test_secret_leak__private_key_block__critical(tmp_path: Path) -> None:
    write(tmp_path / "key.pem", "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----")
    # .pem 은 텍스트 확장자에 없어서 스킵될 수도. 다른 파일에 넣음
    write(tmp_path / "keys.md", "-----BEGIN PRIVATE KEY-----\nABCDE\n-----END PRIVATE KEY-----")
    r = check_secret_leak_risk(tmp_path, make_contract())
    assert not r.passed


# ---------------------------------------------------------------------------
# Integration — Runner
# ---------------------------------------------------------------------------


def test_runner__empty_workspace__blocked_with_multiple_blockers(tmp_path: Path) -> None:
    contract = make_contract()
    runner = StaticPreflightRunner(str(tmp_path), contract, project_id="test")
    result = runner.run_sync()
    assert result.status == PreflightStatus.BLOCKED
    assert len(result.blockers) > 0
    assert 0 <= result.score <= 100
    assert len(result.static_checks.results) == 12  # 12종 모두 실행


def test_runner__healthy_workspace__passed_or_warn(tmp_path: Path) -> None:
    # 완벽한 워크스페이스 구성
    write(tmp_path / ".gitignore", ".env\n.env.*\n")
    write(tmp_path / ".env", "PORT=8000")
    write(tmp_path / "Dockerfile", """
        FROM python:3.11-slim
        WORKDIR /app
        COPY requirements.txt /app/
        RUN pip install --no-cache-dir -r requirements.txt
        COPY . /app
        EXPOSE 8000
        CMD ["python", "main.py"]
    """)
    write(tmp_path / "requirements.txt", "fastapi==0.100.0\n")
    write(tmp_path / "main.py", """
        from fastapi import FastAPI
        app = FastAPI()
        @app.get("/health")
        def health():
            return {"status": "ok"}
    """)
    contract = make_contract(host_port=55555)  # free port
    runner = StaticPreflightRunner(str(tmp_path), contract, project_id="ok")
    result = runner.run_sync()
    # 12종 모두 PASS 또는 WARN — BLOCKED 아님
    assert result.status in {PreflightStatus.PASSED, PreflightStatus.WARN}
    # CRITICAL_VULNERABILITY 는 캐시 없어서 warning — score 가 100 미만이지만 너무 낮으면 안 됨
    assert result.score >= 60


def test_runner__results_dict_has_12_codes(tmp_path: Path) -> None:
    runner = StaticPreflightRunner(str(tmp_path), make_contract(), project_id="t")
    result = runner.run_sync()
    codes = set(result.static_checks.results.keys())
    expected = {c.value for c, _ in __import__("preflight.static",
                                               fromlist=["CHECK_REGISTRY"]).CHECK_REGISTRY}
    assert codes == expected


def test_runner__safe_workspace_path_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        StaticPreflightRunner("", make_contract(), project_id="invalid")
    with pytest.raises(ValueError):
        StaticPreflightRunner("/this/path/should/not/exist/12345", make_contract())
