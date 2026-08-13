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



def test_entrypoint_candidate_must_be_a_file_not_a_directory(tmp_path):
    """[회귀] 진입점 후보가 **폴더**면 인정하지 않는다.

    `exists()` 로만 보면 `src/main/java` 같은 폴더가 있다는 이유로 진입점을
    찾았다고 판정한다. 그러면 실행 가능한 main 이 하나도 없는 라이브러리도
    통과해, **뜨지 않는 이미지를 배포 준비 완료로 보고**한다.

    지금은 폴더 후보를 목록에서 뺐지만, 나중에 누가 다시 넣어도 여기서
    막히도록 판정 자체를 고정한다.
    """
    try:
        from preflight.checks.code_checks import check_app_entrypoint
        from preflight.contract_loader import build_default_contract
        from schemas import ContractStack
    except ImportError:  # pragma: no cover
        from core.preflight.checks.code_checks import check_app_entrypoint  # type: ignore
        from core.preflight.contract_loader import build_default_contract  # type: ignore
        from core.schemas import ContractStack  # type: ignore

    # `main.py` 라는 이름의 **폴더**만 있다 — 실행할 수 있는 것이 없다.
    (tmp_path / "main.py").mkdir()
    contract = build_default_contract(ContractStack.CUSTOM)
    assert not check_app_entrypoint(tmp_path, contract).passed

    # 같은 이름의 **파일**이면 통과한다.
    other = tmp_path / "real"
    other.mkdir()
    (other / "main.py").write_text("print(1)\n", encoding="utf-8")
    assert check_app_entrypoint(other, contract).passed


def test_root_main_go_requires_package_main_and_function(tmp_path):
    """A root main.go filename must not bypass Go executable validation."""
    try:
        from preflight.checks.code_checks import check_app_entrypoint
        from preflight.contract_loader import build_default_contract
        from schemas import ContractStack
    except ImportError:  # pragma: no cover
        from core.preflight.checks.code_checks import check_app_entrypoint  # type: ignore
        from core.preflight.contract_loader import build_default_contract  # type: ignore
        from core.schemas import ContractStack  # type: ignore

    (tmp_path / "go.mod").write_text("module example.com/lib\n", encoding="utf-8")
    main_go = tmp_path / "main.go"
    contract = build_default_contract(ContractStack.CUSTOM)

    main_go.write_text(
        "package generator\nfunc main() {}\n",
        encoding="utf-8",
    )
    assert not check_app_entrypoint(tmp_path, contract).passed

    main_go.write_text(
        "package main\nfunc main() {}\n",
        encoding="utf-8",
    )
    result = check_app_entrypoint(tmp_path, contract)
    assert result.passed
    assert result.details["found_by"] == "probe"


def test_executable_probe_requires_a_real_main_declaration(tmp_path):
    """내용 프로브는 **선언을 실제로 찾아야** 한다 — 파일 존재만으로는 부족."""
    try:
        from preflight.checks.code_checks import _find_executable_entrypoint
    except ImportError:  # pragma: no cover
        from core.preflight.checks.code_checks import _find_executable_entrypoint  # type: ignore

    lib = tmp_path / "lib"
    (lib / "src" / "main" / "java" / "com" / "x").mkdir(parents=True)
    (lib / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    (lib / "src" / "main" / "java" / "com" / "x" / "Util.java").write_text(
        "class Util { int add(int a) { return a; } }\n", encoding="utf-8")
    assert _find_executable_entrypoint(lib) is None

    app = tmp_path / "app"
    (app / "src" / "main" / "java" / "com" / "x").mkdir(parents=True)
    (app / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    (app / "src" / "main" / "java" / "com" / "x" / "App.java").write_text(
        "class App { public static void main(String[] a) {} }\n", encoding="utf-8")
    assert _find_executable_entrypoint(app) is not None


def test_probe_only_runs_for_runtimes_the_manifest_claims(tmp_path):
    """[Codex P1 회귀] 프로브는 **매니페스트가 주장하는 런타임**만 돈다.

    Maven 프로젝트에 Java main 이 없고 `tools/generator.go` 에 `func main()`
    만 있으면 — 예전 프로브는 그 Go 유틸을 앱의 진입점으로 받아들였다.
    그러면 뜨지 않는 Java 이미지가 APP_ENTRYPOINT_NOT_FOUND 를 잃고
    배포 준비 완료로 보고된다.
    """
    try:
        from preflight.checks.code_checks import _find_executable_entrypoint, check_app_entrypoint
        from preflight.contract_loader import build_default_contract
        from schemas import ContractStack
    except ImportError:  # pragma: no cover
        from core.preflight.checks.code_checks import _find_executable_entrypoint, check_app_entrypoint  # type: ignore
        from core.preflight.contract_loader import build_default_contract  # type: ignore
        from core.schemas import ContractStack  # type: ignore

    ws = tmp_path / "maven-with-go-util"
    (ws / "tools").mkdir(parents=True)
    (ws / "src" / "main" / "java" / "com" / "x").mkdir(parents=True)
    (ws / "pom.xml").write_text("<project><artifactId>demo</artifactId></project>\n", encoding="utf-8")
    (ws / "src" / "main" / "java" / "com" / "x" / "Util.java").write_text(
        "class Util { int add(int a) { return a; } }\n", encoding="utf-8")
    (ws / "tools" / "generator.go").write_text(
        "package main\nfunc main() { /* codegen */ }\n", encoding="utf-8")

    # Go 매니페스트(go.mod)가 없으므로 Go 프로브는 돌지 않는다 —
    # Java main 이 없는 이 워크스페이스는 진입점이 없는 것이 맞다.
    assert _find_executable_entrypoint(ws) is None
    contract = build_default_contract(ContractStack.CUSTOM)
    assert not check_app_entrypoint(ws, contract).passed

    # [변이 시험] go.mod 를 추가하면 — 이제 Go 런타임을 주장하는 것이므로 —
    # 같은 Go 파일이 진입점으로 인정된다. 스코프가 "언어 금지"가 아니라
    # "매니페스트 근거"로 동작한다는 증거다.
    (ws / "go.mod").write_text("module example.com/tools\n", encoding="utf-8")
    assert _find_executable_entrypoint(ws) == "tools/generator.go"


def test_probe_ignores_main_declarations_in_comments_and_strings(tmp_path):
    """[Codex P1 회귀] 주석·문자열 안의 main 예시는 **선언이 아니다.**

    Java 라이브러리의 문서 주석에 `// public static void main(String[] args)`
    같은 예시가 있으면 — 예전 프로브는 원문 정규식 매칭이라 이것을 실행
    선언으로 받아들여 APP_ENTRYPOINT_NOT_FOUND 를 억눌렀다.
    """
    try:
        from preflight.checks.code_checks import _find_executable_entrypoint
    except ImportError:  # pragma: no cover
        from core.preflight.checks.code_checks import _find_executable_entrypoint  # type: ignore

    # 1) Java — 줄 주석 · 블록 주석 · 문자열 리터럴 세 곳 모두에 미끼를 둔다.
    ws = tmp_path / "java-lib"
    (ws / "src" / "main" / "java").mkdir(parents=True)
    (ws / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    (ws / "src" / "main" / "java" / "Doc.java").write_text(
        '''// 사용 예: public static void main(String[] args)
/* 옛 예제:
   public static void main(String[] args) { run(); }
*/
class Doc {
    String usage = "public static void main(String[] args)";
}
''', encoding="utf-8")
    assert _find_executable_entrypoint(ws) is None

    # [음성 대조] 진짜 선언을 추가하면 찾아진다 — 제거 로직이 선언까지
    # 지우는 과잉 방어가 아니라는 증거.
    (ws / "src" / "main" / "java" / "App.java").write_text(
        "class App { public static void main(String[] a) {} }\n", encoding="utf-8")
    assert _find_executable_entrypoint(ws) is not None

    # 2) Go — 주석 안의 func main 은 무시된다.
    go = tmp_path / "go-lib"
    go.mkdir()
    (go / "go.mod").write_text("module x\n", encoding="utf-8")
    (go / "doc.go").write_text(
        "package lib\n// func main() { run() }\n/*\nfunc main() {}\n*/\n", encoding="utf-8")
    assert _find_executable_entrypoint(go) is None

    # func main alone is insufficient: Go only builds an executable from
    # package main.
    (go / "generator.go").write_text(
        "package generator\nfunc main() { run() }\n", encoding="utf-8")
    assert _find_executable_entrypoint(go) is None
    (go / "generator.go").write_text(
        "package main\nfunc main() { run() }\n", encoding="utf-8")
    assert _find_executable_entrypoint(go) == "generator.go"

    # 3) Kotlin — 원시 문자열(triple quote) 안의 fun main 은 무시된다.
    kt = tmp_path / "kt-lib"
    kt.mkdir()
    (kt / "build.gradle.kts").write_text("plugins {}\n", encoding="utf-8")
    (kt / "Doc.kt").write_text(
        'val doc = """\nfun main() { example() }\n"""\n', encoding="utf-8")
    assert _find_executable_entrypoint(kt) is None


def test_probe_accepts_any_legal_java_modifier_order(tmp_path):
    """[Codex P2 회귀] `static public void main` 도 유효한 진입점 선언이다.

    Java 는 수식어 순서가 자유다 — `static` 과 `void` 사이에 `public`·
    `final`·`synchronized` 가 와도 합법이고 JVM 은 정상 기동한다. 정규식이
    `static\\s+void` 붙은 순서만 받으면 그런 앱이 APP_ENTRYPOINT_NOT_FOUND
    로 막힌다.
    """
    try:
        from preflight.checks.code_checks import _find_executable_entrypoint
    except ImportError:  # pragma: no cover
        from core.preflight.checks.code_checks import _find_executable_entrypoint  # type: ignore

    cases = [
        ("static public void main(String[] a) {}", True, "static public"),
        ("public static void main(String[] a) {}", True, "관례 순서"),
        ("static final synchronized public void main(String[] a) {}", True, "수식어 여러 개"),
        ("public static void main(String... args) {}", True, "varargs"),
        ("static public void main(java.lang.String args[]) {}", True, "정규화 전 배열 표기"),
        ("private static void main(int value) {}", False, "private + 잘못된 인자"),
        ("public static void main(int value) {}", False, "String 배열 아님"),
        ("static void main(String[] args) {}", False, "public 없음"),
        ("public void main(String[] args) {}", False, "static 없음"),
        ("public static void main(String value) {}", False, "단일 String"),
        ("public static void main(String[] a, int n) {}", False, "인자 두 개"),
        # 다른 문장으로 새면 안 된다 — static 필드 뒤의 다른 메서드.
        ("static int MAX = 3; public void mainframe(String[] a) {}", False, "비진입점"),
    ]
    for i, (decl, expected, label) in enumerate(cases):
        ws = tmp_path / f"case{i}"
        (ws / "src" / "main" / "java").mkdir(parents=True)
        (ws / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        (ws / "src" / "main" / "java" / "App.java").write_text(
            "class App { %s }\n" % decl, encoding="utf-8")
        got = _find_executable_entrypoint(ws) is not None
        assert got == expected, f"{label}: expected {expected}, got {got}"


def test_probe_searches_beyond_sixty_source_files(tmp_path):
    """A valid launcher is not hidden by an arbitrary global file cutoff."""
    try:
        from preflight.checks.code_checks import _find_executable_entrypoint
    except ImportError:  # pragma: no cover
        from core.preflight.checks.code_checks import _find_executable_entrypoint  # type: ignore

    source_root = tmp_path / "src" / "main" / "java" / "example"
    source_root.mkdir(parents=True)
    (tmp_path / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    for index in range(75):
        (source_root / f"A{index:03}.java").write_text(
            f"class A{index:03} {{ int value() {{ return {index}; }} }}\n",
            encoding="utf-8",
        )
    launcher = source_root / "ZLauncher.java"
    launcher.write_text(
        "class ZLauncher { public static void main(String[] args) {} }\n",
        encoding="utf-8",
    )

    assert _find_executable_entrypoint(tmp_path) == (
        "src/main/java/example/ZLauncher.java"
    )


def test_probe_accepts_suspend_kotlin_main(tmp_path):
    """Kotlin's valid top-level suspend main form is executable."""
    try:
        from preflight.checks.code_checks import _find_executable_entrypoint
    except ImportError:  # pragma: no cover
        from core.preflight.checks.code_checks import _find_executable_entrypoint  # type: ignore

    source = tmp_path / "src" / "main" / "kotlin" / "App.kt"
    source.parent.mkdir(parents=True)
    (tmp_path / "build.gradle.kts").write_text("plugins {}\n", encoding="utf-8")
    source.write_text("suspend fun main() { runApp() }\n", encoding="utf-8")

    assert _find_executable_entrypoint(tmp_path) == "src/main/kotlin/App.kt"
