"""AI-DLC 배포 대상 결정: 감지 추천과 ADR 산출을 검증한다."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

try:
    from api.routes import deploy
except ImportError:  # pragma: no cover - package 실행 호환
    from core.api.routes import deploy


def test_deploy_preflight_recommends_ecs_for_fastapi_with_sqlite(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    (tmp_path / "app.sqlite3").write_text("", encoding="utf-8")

    result = deploy._deployment_preflight(str(tmp_path))

    assert result["app_kind"] == "server"
    assert result["recommended_target"] == "ecs"
    assert "FastAPI 서버" in result["evidence"]
    assert "SQLite 데이터 저장" in result["evidence"]


def test_deploy_preflight_recommends_s3_for_static_site(tmp_path):
    (tmp_path / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"devDependencies":{"vite":"5.0.0"}}', encoding="utf-8")

    result = deploy._deployment_preflight(str(tmp_path))

    assert result["app_kind"] == "static"
    assert result["recommended_target"] == "s3"
    assert "정적 HTML 엔트리" in result["evidence"]


def test_static_site_preflight_defers_container_only_blockers(tmp_path):
    """S3 추천 단계에서 Docker·서버·PORT 전제 때문에 막히면 안 된다."""
    (tmp_path / "index.html").write_text("<main>static</main>", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"devDependencies":{"vite":"5.0.0"}}', encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")

    result = asyncio.run(deploy.deploy_preflight(
        deploy.DeployPreflightRequest(workspace_path=str(tmp_path))
    ))

    assert result["recommended_target"] == "s3"
    assert result["blocked"] is False
    codes = {reason["code"] for reason in result["reasons"]}
    assert not {"MISSING_DOCKERFILE", "MISSING_REQUIRED_ENV", "MISSING_HEALTH_ENDPOINT", "APP_ENTRYPOINT_NOT_FOUND"} & codes


def test_deployment_choice_builds_adr_with_selected_target_and_evidence(tmp_path):
    adr = deploy._build_deployment_decision_adr(
        str(tmp_path),
        "ecs",
        ["FastAPI 서버", "SQLite 데이터 저장"],
    )

    assert adr["file"] == "docs/adr/ADR-001-deployment-target.md"
    assert "ECS 컨테이너" in adr["content"]
    assert "FastAPI 서버, SQLite 데이터 저장" in adr["content"]
    assert "S3 정적 호스팅" in adr["content"]


def test_deploy_decision_routes_are_registered():
    paths = {route.path for route in deploy.router.routes}
    assert "/api/deploy/preflight" in paths
    assert "/api/deploy/decision" in paths
    assert "/api/deploy/remediations/{proposal_id}/apply" in paths


def test_deploy_preflight_includes_block_reasons_and_fixes(tmp_path):
    """배포 화면은 감지 정보뿐 아니라 정적 검사 차단 사유도 받아야 한다."""
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    result = asyncio.run(deploy.deploy_preflight(
        deploy.DeployPreflightRequest(workspace_path=str(tmp_path))
    ))

    assert "blocked" in result
    assert "reasons" in result
    assert "fixes" in result
    assert result["blocked"] is True
    assert result["reasons"]
    assert {"code", "message", "fix", "remediation_available"} <= result["reasons"][0].keys()

    dockerfile_issue = next(reason for reason in result["reasons"] if reason["code"] == "MISSING_DOCKERFILE")
    assert dockerfile_issue["remediation_available"] is True
    assert dockerfile_issue["proposal_id"]

    applied = asyncio.run(deploy.apply_deployment_remediation(
        dockerfile_issue["proposal_id"],
        deploy.DeploymentRemediationApplyRequest(workspace_path=str(tmp_path)),
    ))
    assert applied["success"] is True
    assert (tmp_path / "Dockerfile").is_file()


def test_remediation_cannot_be_applied_to_a_different_workspace(tmp_path):
    """결정론적 proposal ID여도 원래 검사한 폴더 밖에는 쓰지 않는다."""
    source = tmp_path / "source"
    other = tmp_path / "other"
    source.mkdir()
    other.mkdir()
    (source / "main.py").write_text("print('hello')\n", encoding="utf-8")

    result = asyncio.run(deploy.deploy_preflight(
        deploy.DeployPreflightRequest(workspace_path=str(source))
    ))
    dockerfile_issue = next(reason for reason in result["reasons"] if reason["code"] == "MISSING_DOCKERFILE")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(deploy.apply_deployment_remediation(
            dockerfile_issue["proposal_id"],
            deploy.DeploymentRemediationApplyRequest(workspace_path=str(other)),
        ))

    assert exc.value.status_code == 409
    assert not (other / "Dockerfile").exists()


def test_env_example_is_guidance_not_an_automatic_unblocker(tmp_path):
    """.env.example 생성은 실제 .env required_env 검사를 통과시키지 않는다."""
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    result = asyncio.run(deploy.deploy_preflight(
        deploy.DeployPreflightRequest(workspace_path=str(tmp_path))
    ))
    env_issue = next(reason for reason in result["reasons"] if reason["code"] == "MISSING_REQUIRED_ENV")

    assert env_issue["proposal_id"]
    assert env_issue["remediation_available"] is False


# ── [회귀] FR-05-01 배포 판정 규칙 보강 ────────────────────────────────
#
# 카드: 「FR-05-01 배포 판정 규칙 보강 (deploy.py)」
#
# 이 판정 결과는 "어디에 올릴까요?" 카드의 **추천 근거로 화면에 그대로**
# 보인다(확정 D7). 아래 8건은 예전 판을 12가지 프로젝트 형태에 실제로
# 돌려보고 확인한 오답이다 — 미탐 6, 오탐 2.


def _write(root, files):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def test_detects_python_server_in_a_src_layout(tmp_path):
    """[미탐] `src/main.py` 의 FastAPI — 예전엔 최상위 *.py 만 봤다.

    `src/` 배치는 진입점 검사(`check_app_entrypoint`)의 후보에도 들어 있어서
    감지와 검사가 어긋나지 않는다. 더 깊은 하위 폴더(모노레포)는 일부러
    보지 않는다 — 아래 `test_monorepo_is_not_claimed_as_supported` 참고.
    """
    _write(tmp_path, {
        "requirements.txt": "fastapi\nuvicorn\n",
        "src/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "server"
    assert result["recommended_target"] == "ecs"


def test_detects_python_server_from_src_imports_without_declared_deps(tmp_path):
    """의존성 선언이 없어도 `src/main.py` 의 import 로 찾는다."""
    _write(tmp_path, {"src/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    assert deploy._deployment_preflight(str(tmp_path))["app_kind"] == "server"


def test_detects_create_react_app_as_static(tmp_path):
    """[미탐] 정적 빌더가 vite 뿐이라 CRA 를 통째로 놓쳤다.

    `public/index.html` 만으로도 정적 판정은 나오므로, **빌더를 실제로
    알아봤는지**까지 단언한다. 판정만 보면 빌더 목록에서 CRA 를 빼도
    테스트가 통과한다(변이 시험에서 실제로 살아남았다).
    """
    _write(tmp_path, {
        "package.json": '{"dependencies":{"react":"^18","react-scripts":"5.0.1"}}',
        "public/index.html": "<html></html>",
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "static"
    assert result["recommended_target"] == "s3"
    assert "Create React App" in "·".join(result["evidence"]), result["evidence"]


@pytest.mark.parametrize("dep,label", [
    ("react-scripts", "Create React App"),
    ("@angular/cli", "Angular CLI"),
    ("@vue/cli-service", "Vue CLI"),
    ("gatsby", "Gatsby"),
    ("vite", "Vite"),
])
def test_static_builders_are_named_in_the_evidence(tmp_path, dep, label):
    """근거는 화면에 그대로 뜬다 — 어떤 빌더를 알아봤는지 말해야 한다(D7)."""
    _write(tmp_path, {"package.json": '{"devDependencies":{"%s":"1.0.0"}}' % dep})
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "static", result
    assert label in "·".join(result["evidence"]), result["evidence"]


def test_detects_astro_as_static(tmp_path):
    """[미탐] Astro·Gatsby·Angular 류 빌더."""
    _write(tmp_path, {"package.json": '{"dependencies":{"astro":"^4"}}'})
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "static"


@pytest.mark.parametrize("files", [
    {"go.mod": "module example.com/api\n\ngo 1.22\n"},
    {"pom.xml": "<project><artifactId>demo</artifactId></project>"},
    {"Gemfile": 'source "https://rubygems.org"\ngem "rails"\n'},
])
def test_detects_non_python_non_node_servers(tmp_path, files):
    """[미탐] 같은 파일의 `_detect_stack` 은 보고 있던 것들.

    판정이 두 함수에서 갈리면 Dockerfile 은 만들어 주면서 배포 대상은
    "잘 모르겠다"고 하는 앞뒤 안 맞는 화면이 된다.
    """
    _write(tmp_path, files)
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "server"
    assert result["recommended_target"] == "ecs"


def test_dockerfile_alone_is_a_server_signal(tmp_path):
    """[미탐] 가장 강한 서버 신호인데 예전 판은 아예 보지 않았다."""
    _write(tmp_path, {
        "Dockerfile": 'FROM node:20\nCMD ["node","server.js"]\n',
        "server.js": "require('http').createServer().listen(3000)",
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "server"
    assert "Dockerfile" in "·".join(result["evidence"])


def test_a_comment_mentioning_a_framework_is_not_a_server(tmp_path):
    """[오탐] `# fastapi 는 쓰지 않는다` 한 줄에 속아 정적 사이트를 ECS 로 보냈다."""
    _write(tmp_path, {
        "requirements.txt": "# fastapi 는 쓰지 않는다\nrequests\n",
        "index.html": "<html>정적 사이트</html>",
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "static", result
    assert result["recommended_target"] == "s3"


def test_a_string_mentioning_a_framework_is_not_an_import(tmp_path):
    """[오탐] 소스 안의 문자열도 마찬가지다 — import 문만 근거로 삼는다."""
    _write(tmp_path, {
        "app.py": 'DOCS = "이 프로젝트는 flask 로 옮길 예정"\nprint(DOCS)\n',
        "index.html": "<html></html>",
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "static", result


def test_next_static_export_goes_to_s3_not_ecs(tmp_path):
    """[오분류] `output: 'export'` 는 산출물이 정적 파일이다."""
    _write(tmp_path, {
        "package.json": '{"dependencies":{"next":"14"}}',
        "next.config.js": "module.exports = { output: 'export' }",
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "static"
    assert result["recommended_target"] == "s3"


def test_next_without_export_is_still_a_server(tmp_path):
    """반대 방향도 확인한다 — 너무 넓게 고치면 SSR 앱을 S3 로 보낸다."""
    _write(tmp_path, {"package.json": '{"dependencies":{"next":"14"}}'})
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "server"
    assert result["recommended_target"] == "ecs"


def test_node_modules_is_never_used_as_evidence(tmp_path):
    """[중요] 남의 의존성 안의 파일을 이 프로젝트의 증거로 삼으면 안 된다.

    `node_modules` 에는 express 도 vite 도 다 들어 있다. 한 번 들어가면
    **모든 프로젝트가 서버형이 된다.**
    """
    _write(tmp_path, {
        "index.html": "<html></html>",
        "node_modules/express/package.json": '{"name":"express","dependencies":{"express":"4"}}',
        "node_modules/express/Dockerfile": "FROM node:20",
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "static", result


def test_empty_project_stays_unknown(tmp_path):
    """근거가 없으면 확신하지 않는다 — 사람에게 묻는 쪽이 맞다(D5)."""
    _write(tmp_path, {"README.md": "# 아직 아무것도 없음"})
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "unknown"
    assert result["recommended_target"] == "local"


def test_evidence_is_never_empty(tmp_path):
    """근거 없는 추천은 D7 위반이다 — 카드에 보여줄 문장이 항상 있어야 한다."""
    for idx, files in enumerate((
        {"go.mod": "module x\n"},
        {"index.html": "<html></html>"},
        {"README.md": "#"},
    )):
        # 폴더 이름을 `hash()` 로 만들면 PYTHONHASHSEED 에 따라 매 실행
        # 달라진다 — 테스트에 이유 없는 비결정성을 넣지 않는다.
        root = tmp_path / f"case{idx}"
        root.mkdir()
        _write(root, files)
        result = deploy._deployment_preflight(str(root))
        assert result["evidence"], result
        assert all(isinstance(e, str) and e.strip() for e in result["evidence"])


# ── [회귀] 적대적 자체검수에서 나온 결함 ──────────────────────────────
#
# 위 판정기를 검수에 넘겼더니 P1 2건을 포함해 여러 건이 나왔다. 특히
# P1-1 은 "주석에 속지 않는다"고 고쳐 놓고 `pyproject.toml` 에서는 그대로
# 뚫려 있던 것 — 같은 성질의 다른 자리를 안 훑은 형태다.


@pytest.mark.parametrize("pyproject,label", [
    ('[project]\nname = "docs"\ndescription = "django 없이 만든 정적 문서 사이트"\ndependencies = ["requests"]\n',
     "설명문 속 단어"),
    ('[project]\nname = "x"\ndependencies = ["requests"]\n\n[tool.mypy.overrides]\ndjango = true\n',
     "도구 설정 키"),
    ('# 참고: "fastapi" 는 쓰지 않는다\n[project]\nname = "x"\ndependencies = ["requests"]\n',
     "주석"),
    ('[project]\nname = "x"\ndependencies = ["requests"]\nurls = { home = "https://flask.example.com" }\n',
     "URL 조각"),
])
def test_pyproject_non_dependency_text_is_not_a_framework(tmp_path, pyproject, label):
    """[P1-1] 의존성 섹션 밖의 글자는 의존성이 아니다.

    본문에 "django 없이"라고 적힌 프로젝트를 화면에 **"Django 서버"** 라고
    표시하면, 근거를 보여주는 기능이 근거를 지어내는 셈이 된다.
    """
    _write(tmp_path, {"pyproject.toml": pyproject, "index.html": "<html></html>"})
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "static", f"{label}: {result}"


@pytest.mark.parametrize("pyproject", [
    '[project]\nname = "api"\ndependencies = ["fastapi>=0.110", "uvicorn"]\n',
    '[tool.poetry.dependencies]\npython = "^3.12"\nflask = "^3.0"\n',
    '[project]\nname = "api"\ndependencies = []\n\n[project.optional-dependencies]\nweb = ["django>=5"]\n',
])
def test_real_pyproject_dependencies_are_still_detected(tmp_path, pyproject):
    """반대 방향 — 좁게 고치다가 진짜 의존성을 놓치면 안 된다."""
    _write(tmp_path, {"pyproject.toml": pyproject})
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "server", result


def test_ancestor_directory_named_build_does_not_disable_import_fallback(tmp_path):
    """[P2-1] 제외 검사가 절대경로 전체를 보면, 상위 폴더 이름 하나로 폴백이 죽는다."""
    ws = tmp_path / "build" / "ws"
    _write(ws, {"main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    result = deploy._deployment_preflight(str(ws))
    assert result["app_kind"] == "server", result


def test_webpack_alone_is_not_a_static_site(tmp_path):
    """[P2-2] 번들러 devDependency 하나로 라이브러리를 S3 로 보내면 안 된다.

    이 저장소의 `extension/`(VS Code 확장) 자체가 그렇게 판정됐다.
    """
    _write(tmp_path, {"package.json": '{"name":"lib","devDependencies":{"webpack":"^5"}}'})
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] != "static", result


def test_import_inside_a_triple_quoted_template_is_not_code(tmp_path):
    """[P2-4] 이 프로젝트는 코드 생성기라 템플릿 문자열이 흔하다.

    템플릿 안의 `from flask import Flask` 를 코드로 읽으면, 템플릿을 가진
    모든 프로젝트가 서버가 된다.
    """
    _write(tmp_path, {
        "gen.py": 'TEMPLATE = """\nfrom flask import Flask\napp = Flask(__name__)\n"""\nprint(TEMPLATE)\n',
        "index.html": "<html></html>",
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "static", result


def test_hugo_detection_precedence(tmp_path):
    """[P2-5] `hugo.toml or (config.toml and content/)` 로 읽혀야 한다."""
    solo = tmp_path / "solo"
    _write(solo, {"hugo.toml": 'title = "blog"'})
    assert deploy._deployment_preflight(str(solo))["app_kind"] == "static"

    # `config.toml` 은 Hugo 전용이 아니다 — `content/` 없이 정적으로 보면 안 된다.
    ambiguous = tmp_path / "ambiguous"
    _write(ambiguous, {"config.toml": "x = 1"})
    assert deploy._deployment_preflight(str(ambiguous))["app_kind"] == "unknown"

    proper = tmp_path / "proper"
    _write(proper, {"config.toml": 'title = "b"', "content/post.md": "# hi"})
    assert deploy._deployment_preflight(str(proper))["app_kind"] == "static"


def test_next_as_a_peer_dependency_is_not_a_next_server(tmp_path):
    """플러그인 패키지가 `peerDependencies` 하나로 서버가 되면 안 된다."""
    _write(tmp_path, {
        "package.json": '{"name":"next-plugin","peerDependencies":{"next":"14"}}',
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] != "server", result


def test_pyproject_fallback_parser_also_limits_sections(tmp_path):
    """[P1-1] `tomllib` 이 못 읽는 TOML 에서도 섹션 제한이 살아 있어야 한다.

    깨진 `pyproject.toml` 은 흔하다(편집 중, 손으로 쓴 것). 그때 쓰는
    폴백 파서가 파일 전체를 훑으면, 정상 경로만 고치고 폴백은 뚫린 채로
    남는다 — 같은 성질의 다른 자리를 안 훑는 형태다.
    """
    import tomllib

    broken = (
        '[project\n'                       # 닫는 대괄호가 없다 → tomllib 실패
        'name = "x"\n'
        'description = "django 없이 만든 정적 사이트"\n'
        '[tool.x]\n'
        'fastapi = 1\n'
    )
    with pytest.raises(Exception):
        tomllib.loads(broken)              # 폴백 경로를 실제로 타는지 보장

    _write(tmp_path, {"pyproject.toml": broken, "index.html": "<html></html>"})
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "static", result


# ── [회귀] Codex PR 리뷰 P2 (2026-08-12) ──────────────────────────────


@pytest.mark.parametrize("src,label", [
    ("import os\nfrom fastapi import FastAPI\napp = FastAPI()\n", "import 하나 먼저"),
    ("import os, sys\nimport json\nfrom flask import Flask\n", "복수 import 먼저"),
    ("import os\nimport sys\nimport json\nfrom django.conf import settings\n", "여러 줄"),
])
def test_framework_import_after_other_imports_is_still_found(tmp_path, src, label):
    """[Codex P2] import 캡처가 개행을 넘어가 다음 줄을 삼키던 것.

    `[\\w.,\\s]+` 의 `\\s` 가 개행을 포함해서, `import os` 가 다음 줄의
    `from fastapi import FastAPI` 까지 한 덩어리로 먹었다. 그러면
    `os\\nfrom fastapi import fastapi\\napp` 같은 없는 모듈 이름이 만들어지고
    **그 줄은 다시 매치되지 않는다.** 프레임워크 import 앞에 다른 import 가
    하나만 있어도 감지가 통째로 실패한다 — 거의 모든 실제 파일이 그렇다.
    """
    _write(tmp_path, {"main.py": src})
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "server", f"{label}: {result}"


def test_import_capture_does_not_span_lines(tmp_path):
    """같은 것을 모듈 목록 수준에서 직접 확인한다."""
    _write(tmp_path, {"main.py": "import os\nfrom fastapi import FastAPI\n"})
    mods = deploy._python_imported_modules(tmp_path)
    assert "fastapi" in mods and "os" in mods, mods
    assert not any("\n" in m for m in mods), f"개행이 섞인 모듈 이름: {mods}"


@pytest.mark.parametrize("config,expected,label", [
    ("module.exports = {\n  // output: 'export'\n  reactStrictMode: true,\n}\n",
     "server", "줄 주석"),
    ("module.exports = {\n  /* output: 'export' */\n  reactStrictMode: true,\n}\n",
     "server", "블록 주석"),
    ("module.exports = { output: 'export' }\n", "static", "진짜 설정"),
    # **같은 줄**에 둔다. 다음 줄에 두면 URL 뒤를 통째로 주석 처리해도
    # 설정이 살아남아, 따옴표 추적이 빠져도 테스트가 통과한다(변이 시험에서
    # 실제로 살아남았다).
    ("module.exports = { assetPrefix: 'https://cdn.example.com', output: 'export' }\n",
     "static", "URL 의 // 는 주석이 아니다 (같은 줄)"),
])
def test_commented_out_next_export_is_not_a_static_signal(tmp_path, config, expected, label):
    """[Codex P2] 주석으로 남긴 `output: 'export'` 에 속아 SSR 앱에 S3 를 권했다.

    예전 설정을 주석으로 남기는 건 아주 흔하다. `requirements.txt` 와
    `pyproject.toml` 에서 같은 형태의 오탐을 이미 막았는데 여기만 남아
    있었다.
    """
    _write(tmp_path, {
        "package.json": '{"dependencies":{"next":"14"}}',
        "next.config.js": config,
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == expected, f"{label}: {result}"


# ── [회귀] Codex PR 리뷰 2차 — P1 포함 ────────────────────────────────


APP_FILES = {
    "requirements.txt": "fastapi\nuvicorn\n",
    "src/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    "Dockerfile": "FROM python:3.12\n",
}
REPO_FILES = {".gitignore": ".env\n", ".git/config": ""}


def test_contract_stack_detection_matches_the_app_detector(tmp_path):
    """[Codex P1 원인] 두 감지기가 서로 다른 기준을 쓰면 안 된다.

    `_detect_preflight_contract_stack` 의 docstring 이 원래부터 "배포 대상
    감지와 정적 Preflight 가 서로 다른 기준을 쓰지 않도록"이라고 못 박고
    있었는데, 감지 쪽만 고치면서 그 약속이 깨졌다. 진입점이 `src/main.py` 면
    CUSTOM 으로 떨어지고, CUSTOM 후보에는 `src/main.py` 가 없다.
    """
    try:
        from schemas import ContractStack
    except ImportError:  # pragma: no cover
        from core.schemas import ContractStack  # type: ignore

    _write(tmp_path, APP_FILES)
    assert deploy._detect_preflight_contract_stack(tmp_path) == ContractStack.PYTHON_FASTAPI

    flask_root = tmp_path / "flask_app"
    _write(flask_root, {"requirements.txt": "flask\n", "src/app.py": "from flask import Flask\n"})
    assert deploy._detect_preflight_contract_stack(flask_root) == ContractStack.PYTHON_FLASK


@pytest.mark.parametrize("src,label", [
    ("import fastapi as fa\napp = fa.FastAPI()\n", "import X as Y"),
    ("import flask as flask_app\n", "flask as flask_app"),
    ("import os, fastapi as fa\n", "복수 + 별칭"),
])
def test_import_aliases_are_stripped(tmp_path, src, label):
    """[Codex P2] `import fastapi as fa` 의 별칭을 안 떼면 모듈 이름이
    `fastapi as fa` 가 되어 무엇과도 안 맞는다."""
    _write(tmp_path, {"main.py": src})
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "server", f"{label}: {result}"


@pytest.mark.parametrize("config,expected,label", [
    ('module.exports = { "output": "export" }\n', "static", "JSON 스타일 키"),
    ("module.exports = { 'output': 'export' }\n", "static", "작은따옴표 키"),
    ('module.exports = { "outputFileTracing": true }\n', "server", "비슷한 다른 키"),
])
def test_quoted_next_output_key_is_accepted(tmp_path, config, expected, label):
    """[Codex P2] `{ "output": "export" }` 도 유효한 표기다."""
    _write(tmp_path, {
        "package.json": '{"dependencies":{"next":"14"}}',
        "next.config.js": config,
    })
    assert deploy._deployment_preflight(str(tmp_path))["app_kind"] == expected, label


# ── [회귀] Codex 3차 P1/P2 ────────────────────────────────────────────

_CONTRACT_YML = (
    "project:\n  name: demo\n  stack: python-fastapi\n"
    "runtime:\n  env_file: .env\n  port: 9999\n"
    "preflight:\n  required_env: [SUPER_SECRET_KEY]\n"
)



# ── 모노레포는 **의도적으로 지원하지 않는다** ──────────────────────────
#
# 하위 폴더까지 훑어 모노레포를 지원하는 판을 만들었다가 되돌렸다.
# Codex 리뷰 4라운드에서 P1 5건이 전부 그 한 곳에서 나왔기 때문이다.
#
# 원인은 기능이 나빠서가 아니라 **끼워 넣는 방식이 이 코드베이스의 전제와
# 안 맞아서**다. preflight 계층 전체가 "검사 대상 = 워크스페이스 루트"를
# 전제로 짜여 있다 — 계약 로딩, 계약 상대 경로, `.gitignore` 탐색, 진입점
# 후보, 수정안 적용 경로가 전부 그렇다. 거기에 "앱 루트는 따로 있을 수
# 있다"를 한 겹씩 끼워 넣으니 전제가 깨진 자리가 계속 드러났다.
#
# 제대로 하려면 preflight 계층을 앱 루트 기준으로 한 번에 재설계해야 한다
# (회차4). 그때까지는 **모르는 것을 모른다고 말한다.**


def test_monorepo_is_not_claimed_as_supported(tmp_path):
    """모노레포는 `unknown` 으로 두고 사용자에게 묻는다.

    절반만 아는 것보다 낫다. 하위 폴더의 앱을 "서버형"이라고 판정해 놓으면,
    안전 검사는 루트만 보므로 `APP_ENTRYPOINT_NOT_FOUND` 로 막혀 **사용자가
    아무것도 할 수 없는 막다른 길**이 된다.
    """
    _write(tmp_path, {
        "backend/requirements.txt": "fastapi\n",
        "backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "frontend/package.json": '{"dependencies":{"react":"^18","vite":"^5"}}',
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "unknown", result
    assert result["recommended_target"] == "local", result


def test_detection_result_has_no_app_root_field(tmp_path):
    """`app_root` 전파를 되돌렸다 — 되살아나면 여기서 잡힌다.

    이 필드가 다시 생기면 그것을 소비하는 계층(계약·gitignore·진입점·수정안
    적용)이 함께 따라와야 한다. 한 곳만 넓히면 4라운드 동안 반복된 그
    형태가 되풀이된다.
    """
    _write(tmp_path, {"requirements.txt": "fastapi\n", "main.py": "from fastapi import FastAPI\n"})
    assert "app_root" not in deploy._deployment_preflight(str(tmp_path))


def test_safety_preflight_takes_no_app_root_argument():
    """안전 검사는 워크스페이스 루트만 받는다."""
    import inspect

    params = list(inspect.signature(deploy._run_deployment_safety_preflight).parameters)
    assert params == ["workspace_path", "app_kind"], params


#: **감지기가 "서버형"이라고 말할 수 있는 모든 경로**의 최소 픽스처.
#:
#: 이 표는 `_deployment_preflight` 가 서버 근거를 붙이는 자리와 1:1 로
#: 대응해야 한다. 새 런타임 신호를 추가하면 **여기에도 추가**해야 아래
#: 불변식이 그 런타임을 실제로 덮는다.
#:
#: 왜 이렇게까지 하냐 — 예전 판에서는 이 불변식을 FastAPI·Go·Express·
#: Dockerfile 네 개로만 돌렸고, 그중 Go 는 `main.go` 가, Dockerfile 픽스처는
#: `server.js` 가 우연히 진입점 후보에 있어서 **운으로 통과**했다. 정작
#: Spring·Rails·PHP·Procfile·docker-compose·NestJS 6가지가 막다른 길이었는데
#: 테스트는 초록이었다.
_SERVER_FIXTURES = [
    ({"requirements.txt": "fastapi\n", "main.py": "from fastapi import FastAPI\napp=FastAPI()\n"}, "FastAPI"),
    ({"requirements.txt": "flask\n", "app.py": "from flask import Flask\n"}, "Flask"),
    ({"requirements.txt": "django\n", "manage.py": "import django\n"}, "Django"),
    ({"requirements.txt": "fastapi\n", "src/main.py": "from fastapi import FastAPI\n"}, "FastAPI (src 배치)"),
    ({"src/main.py": "from fastapi import FastAPI\n"}, "의존성 선언 없이 import 만"),
    ({"package.json": '{"dependencies":{"express":"^4"}}', "index.js": "x"}, "Express"),
    ({"package.json": '{"dependencies":{"@nestjs/core":"^10"}}', "src/main.ts": "x"}, "NestJS"),
    ({"package.json": '{"dependencies":{"fastify":"^4"}}', "src/index.ts": "x"}, "Fastify"),
    ({"package.json": '{"dependencies":{"koa":"^2"}}', "app.js": "x"}, "Koa"),
    ({"package.json": '{"dependencies":{"next":"14"}}', "next.config.js": "module.exports={}"}, "Next SSR"),
    ({"package.json": '{"dependencies":{"astro":"^4","@astrojs/node":"^8"}}',
      "astro.config.mjs": "export default { output: 'server' }\n", "src/index.ts": "x"}, "Astro SSR"),
    ({"go.mod": "module x\n", "main.go": "package main\n"}, "Go"),
    ({"go.mod": "module x\n", "cmd/api/main.go": "package main\nfunc main() {}\n"}, "Go (cmd 배치)"),
    ({"pom.xml": "<project/>",
      "src/main/java/App.java": "class App { public static void main(String[] a) {} }"}, "Spring (maven)"),
    ({"build.gradle": "plugins{}",
      "src/main/java/App.java": "class App { public static void main(String[] a) {} }"}, "Spring (gradle)"),
    ({"build.gradle.kts": "plugins{}", "src/main/kotlin/App.kt": "fun main(){}"}, "Kotlin"),
    ({"Gemfile": 'gem "rails"\n', "config.ru": "run App\n"}, "Rails"),
    ({"composer.json": '{"require":{"laravel/framework":"^11"}}', "public/index.php": "<?php"}, "PHP"),
    ({"Dockerfile": "FROM node:20\nCMD [\"node\",\"x\"]\n"}, "Dockerfile 만"),
    ({"docker-compose.yml": "services:\n  api:\n    image: x\n"}, "docker-compose 만"),
    ({"Procfile": "web: ./run\n"}, "Procfile 만"),
]


@pytest.mark.parametrize("files,label", _SERVER_FIXTURES)
def test_every_server_runtime_is_actually_deployable(tmp_path, files, label):
    """**불변식** — "서버형"이라고 판정했으면 진입점을 못 찾아 막히면 안 된다.

    확장은 `blocked` 가 참이면 배포 대상 선택을 통째로 비활성화한다. 즉
    이 불변식이 깨지면 **사용자가 아무것도 할 수 없는 막다른 길**이 된다.

    Codex 리뷰 5라운드 중 P1 이 전부 이 형태였다. 개별 사례를 하나씩 막는
    대신 감지기가 서버로 인정하는 **모든 경로**에 대해 검사한다.
    """
    _write(tmp_path, {**files, ".gitignore": ".env\n", ".git/config": ""})
    detected = deploy._deployment_preflight(str(tmp_path))
    assert detected["app_kind"] == "server", f"{label}: 서버형으로 판정되지 않았다 — {detected}"

    safety = deploy._run_deployment_safety_preflight(str(tmp_path), detected["app_kind"])
    assert "APP_ENTRYPOINT_NOT_FOUND" not in [r["code"] for r in safety["reasons"]], (
        f"{label}: 서버형이라고 해놓고 진입점을 못 찾아 막았다 — "
        f"진입점 후보에 이 런타임의 관례 경로가 없다"
    )


@pytest.mark.parametrize("config,deps,expected,label", [
    ("export default { output: 'server' }\n", '"@astrojs/node":"^8"', "server", "output:server + 어댑터"),
    ("export default { output: 'hybrid' }\n", "", "server", "hybrid"),
    ("", '"@astrojs/vercel":"^7"', "server", "어댑터만 있어도 서버"),
    ("", "", "static", "기본은 정적"),
    ("export default { output: 'static' }\n", "", "static", "명시적 static"),
    ("// output: 'server'  ← 예전 설정\nexport default {}\n", "", "static", "주석 처리된 server"),
])
def test_astro_output_mode_decides_the_target(tmp_path, config, deps, expected, label):
    """[Codex P2] Astro 는 정적/서버 양쪽이다.

    `output: 'server'` 나 어댑터가 있으면 서버 런타임이 필요한데, 정적으로
    보면 **올려도 동작하지 않는 S3 를 권하게 된다.** Next.js 를 반대 방향으로
    다루면서(정적 export 감지) 같은 종류의 설정이 Astro 에도 있다는 걸
    안 봤다.
    """
    files = {"package.json": '{"dependencies":{"astro":"^4"%s}}' % (("," + deps) if deps else "")}
    if config:
        files["astro.config.mjs"] = config
    _write(tmp_path, files)
    assert deploy._deployment_preflight(str(tmp_path))["app_kind"] == expected, label


# ── [회귀] Codex 6차 — P1 2건 + P2 2건 ───────────────────────────────
#
# 이번 4건은 전부 **감지기를 넓힌 만큼 뒤쪽 검사가 못 따라온** 형태다.
# preflight 계층이 지원하는 스택은 4개(FastAPI/Flask/Express/Next)+CUSTOM 뿐인데
# 감지기만 런타임을 늘리면, 진입점 후보·health 검사 문법·계약 스택이 어긋난다.


@pytest.mark.parametrize("dep,entry,label", [
    ("fastify", "src/index.ts", "Fastify"),
    ("koa", "app.js", "Koa"),
    ("hono", "src/index.ts", "Hono"),
    ("@nestjs/core", "src/main.ts", "NestJS"),
])
def test_non_express_node_runtimes_are_not_forced_into_express_checks(tmp_path, dep, entry, label):
    """[Codex P1] Express 가 아닌 Node 런타임을 NODE_EXPRESS 로 보내면 막힌다.

    그 스택의 health 검사는 `app.get(...)`·`router.get(...)` 이라는 Express
    문법만 안다. Fastify(`fastify.get`)·NestJS(`@Get()` 데코레이터)는 멀쩡히
    `/health` 를 정의해도 인식되지 않아 `MISSING_HEALTH_ENDPOINT` 로 막힌다 —
    **사용자가 고칠 것이 없는데 막히는** 형태다.

    CUSTOM 의 health 검사는 차단이 아니라 경고("직접 확인하세요")다.
    모르는 것을 아는 척해서 막는 것보다 낫다.
    """
    try:
        from schemas import ContractStack
    except ImportError:  # pragma: no cover
        from core.schemas import ContractStack  # type: ignore

    _write(tmp_path, {
        "package.json": '{"dependencies":{"%s":"^1"}}' % dep,
        entry: "x",
        ".gitignore": ".env\n", ".git/config": "", ".env": "X=1\n",
    })
    assert deploy._detect_preflight_contract_stack(tmp_path) == ContractStack.CUSTOM, label

    detected = deploy._deployment_preflight(str(tmp_path))
    assert detected["app_kind"] == "server", label
    safety = deploy._run_deployment_safety_preflight(str(tmp_path), detected["app_kind"])
    codes = [r["code"] for r in safety["reasons"]]
    assert "MISSING_HEALTH_ENDPOINT" not in codes, f"{label}: {codes}"
    assert "APP_ENTRYPOINT_NOT_FOUND" not in codes, f"{label}: {codes}"


def test_express_still_maps_to_the_express_stack(tmp_path):
    """반대 방향 — Express 는 여전히 NODE_EXPRESS 여야 검사가 의미 있다."""
    try:
        from schemas import ContractStack
    except ImportError:  # pragma: no cover
        from core.schemas import ContractStack  # type: ignore

    _write(tmp_path, {"package.json": '{"dependencies":{"express":"^4"}}', "index.js": "x"})
    assert deploy._detect_preflight_contract_stack(tmp_path) == ContractStack.NODE_EXPRESS


def test_a_java_library_without_main_is_not_treated_as_deployable(tmp_path):
    """[Codex P1] 폴더 존재를 진입점으로 인정하면 **검사가 약해진다.**

    `src/main/java` 가 있다는 이유로 통과시키면, 실행 가능한 main 이 하나도
    없는 라이브러리도 "배포 준비 완료"가 된다 — 뜨지 않는 이미지를 승인하는
    셈이다.
    """
    _write(tmp_path, {
        "pom.xml": "<project/>",
        "src/main/java/com/x/Util.java": "class Util { int add(int a){ return a; } }",
        ".gitignore": ".env\n", ".git/config": "", ".env": "X=1\n",
    })
    detected = deploy._deployment_preflight(str(tmp_path))
    safety = deploy._run_deployment_safety_preflight(str(tmp_path), detected["app_kind"])
    assert "APP_ENTRYPOINT_NOT_FOUND" in [r["code"] for r in safety["reasons"]], safety["reasons"]


def test_a_java_app_with_a_main_method_is_deployable(tmp_path):
    """반대 방향 — 진짜 main 이 있으면 통과해야 한다."""
    _write(tmp_path, {
        "pom.xml": "<project/>",
        "src/main/java/com/x/App.java": "class App { public static void main(String[] a) {} }",
        ".gitignore": ".env\n", ".git/config": "", ".env": "X=1\n",
    })
    detected = deploy._deployment_preflight(str(tmp_path))
    safety = deploy._run_deployment_safety_preflight(str(tmp_path), detected["app_kind"])
    assert "APP_ENTRYPOINT_NOT_FOUND" not in [r["code"] for r in safety["reasons"]]


@pytest.mark.parametrize("toml,expected,label", [
    ('[project]\nname="lib"\ndependencies=["requests"]\n\n[dependency-groups]\ndjango = ["pytest"]\n',
     "static", "그룹 이름이 django"),
    ('[project]\nname="lib"\ndependencies=["requests"]\n\n[project.optional-dependencies]\nfastapi = ["httpx"]\n',
     "static", "extra 이름이 fastapi"),
    ('[project]\nname="api"\ndependencies=["fastapi"]\n', "server", "진짜 의존성"),
    ('[project]\nname="api"\ndependencies=[]\n\n[project.optional-dependencies]\nweb = ["django>=5"]\n',
     "server", "extra 값에 django"),
    ('[tool.poetry.dependencies]\npython="^3.12"\nflask="^3"\n', "server", "poetry 키는 패키지 이름"),
    ('[tool.poetry.group.dev.dependencies]\nfastapi="^0.1"\n', "server", "poetry group 안쪽은 테이블"),
])
def test_dependency_group_names_are_not_dependencies(tmp_path, toml, expected, label):
    """[Codex P2] `[dependency-groups] django = [...]` 의 키는 **그룹 이름**이다.

    키까지 걷으면 그룹/extra 이름이 우연히 `django`·`fastapi` 인 프로젝트가
    서버로 판정돼 ECS 를 추천받는다.
    """
    _write(tmp_path, {"pyproject.toml": toml, "index.html": "<html></html>"})
    assert deploy._deployment_preflight(str(tmp_path))["app_kind"] == expected, label


@pytest.mark.parametrize("text,expected,label", [
    ("module.exports = { output: 'export' }", "export", "맨 키"),
    ('module.exports = { "output": "export" }', "export", "따옴표 키"),
    ('const hint = "set output: \'export\' for static deployments"\nmodule.exports = {}',
     None, "문서 문자열 안 ← P2"),
    ("// output: 'export'\nmodule.exports = {}", None, "주석 안"),
    ("module.exports = { outputFileTracing: true }", None, "비슷한 다른 키"),
    ("module.exports = { assetPrefix: 'https://x.com', output: 'export' }", "export", "URL 뒤 같은 줄"),
])
def test_config_key_scanner_ignores_string_literals(text, expected, label):
    """[Codex P2] 정규식은 **문자열 안의 같은 글자**에 속는다.

    `const hint = "set output: 'export' for static"` 같은 설명문이 있으면 SSR
    설정을 정적으로 오판해 S3 를 권한다. 주석을 지워도 남는 문제라, 키 위치를
    실제로 판별하는 스캐너로 바꿨다.
    """
    stripped = deploy._strip_js_comments(text)
    assert deploy._js_config_string_value(stripped, "output") == expected, label


def test_next_ssr_with_a_documentation_string_is_still_a_server(tmp_path):
    """통합 확인 — 설명문에 속아 SSR 앱에 S3 를 권하면 안 된다."""
    _write(tmp_path, {
        "package.json": '{"dependencies":{"next":"14"}}',
        "next.config.js": 'const hint = "set output: \'export\' for static";\nmodule.exports = { reactStrictMode: true };\n',
    })
    assert deploy._deployment_preflight(str(tmp_path))["app_kind"] == "server"


# ---------------------------------------------------------------------------
# [Codex P2] output 감지는 **export 되는 설정**만 봐야 한다
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected,label", [
    # ── 오판을 막아야 하는 형태 ──────────────────────────────────────────
    ("const example = { output: 'export' };\nmodule.exports = { reactStrictMode: true };",
     None, "export 앞의 무관한 헬퍼 객체 ← P2 본판"),
    ("module.exports = { reactStrictMode: true };\nconst late = { output: 'export' };",
     None, "export 뒤의 무관한 객체"),
    ("const example = { output: 'export' };", None, "export 자체가 없음"),
    # ── 정상 인식을 유지해야 하는 형태 ──────────────────────────────────
    ("module.exports = { output: 'export' }", "export", "직접 객체"),
    ("export default { output: 'export' }", "export", "export default 객체"),
    ("const cfg = { output: 'export' };\nmodule.exports = cfg;", "export", "식별자 해석 (module.exports)"),
    ("const cfg = { output: 'export' };\nexport default cfg;", "export", "식별자 해석 (export default)"),
    ("const cfg: NextConfig = { output: 'export' };\nexport default cfg;", "export", "TS 타입 표기"),
    ("export default defineConfig({ output: 'server' })", "server", "defineConfig 래퍼 (Astro 형)"),
    ("module.exports = withPlugins([], { output: 'export' })", "export", "플러그인 래퍼"),
    ("module.exports = (phase) => ({ output: 'export' })", "export", "화살표 함수 설정"),
    ("export default { output: 'export' } satisfies NextConfig", "export", "satisfies 후치"),
    # ── 헬퍼가 있어도 export 쪽 값이 이긴다 ─────────────────────────────
    ("const example = { output: 'server' };\nmodule.exports = { output: 'export' };",
     "export", "헬퍼와 export 가 다른 값이면 export 쪽"),
])
def test_config_scanner_reads_only_the_exported_configuration(text, expected, label):
    """[Codex P2 회귀] export 앞의 헬퍼 객체에 속아 SSR 앱에 S3 를 권했다.

    `const example = { output: 'export' }; module.exports = { ... }` 처럼
    **export 되지 않는 객체**의 값을 설정으로 읽으면, 서버가 필요한 앱의
    산출물을 정적이라고 오판한다. 설정은 export 되는 표현식뿐이다.
    """
    stripped = deploy._strip_js_comments(text)
    assert deploy._js_config_string_value(stripped, "output") == expected, label


def test_next_ssr_with_helper_object_before_export_is_still_a_server(tmp_path):
    """통합 확인 — Codex P2 시나리오 그대로. 헬퍼에 속아 S3 를 권하면 안 된다."""
    _write(tmp_path, {
        "package.json": '{"dependencies":{"next":"14"}}',
        "next.config.js": "const example = { output: 'export' };\nmodule.exports = { reactStrictMode: true };\n",
    })
    assert deploy._deployment_preflight(str(tmp_path))["app_kind"] == "server"


def test_next_static_export_via_identifier_is_still_static(tmp_path):
    """[음성 대조] 관례 형태(`const nextConfig = {...}; module.exports = nextConfig`)는
    export 스코프를 좁혀도 **계속 정적으로 인식**되어야 한다."""
    _write(tmp_path, {
        "package.json": '{"dependencies":{"next":"14"}}',
        "next.config.js": "const nextConfig = { output: 'export' };\nmodule.exports = nextConfig;\n",
    })
    assert deploy._deployment_preflight(str(tmp_path))["app_kind"] == "static"
