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


def test_detects_python_server_below_the_top_level(tmp_path):
    """[미탐] `src/main.py` 의 FastAPI — 예전엔 최상위 *.py 만 봤다."""
    _write(tmp_path, {
        "src/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "src/requirements.txt": "fastapi\nuvicorn\n",
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "server"
    assert result["recommended_target"] == "ecs"
    # 근거는 화면에 뜨는 문장이다 — 어디서 찾았는지 말해야 한다.
    assert any("src/" in e for e in result["evidence"]), result["evidence"]


def test_detects_create_react_app_as_static(tmp_path):
    """[미탐] 정적 빌더가 vite 뿐이라 CRA 를 통째로 놓쳤다."""
    _write(tmp_path, {
        "package.json": '{"dependencies":{"react":"^18","react-scripts":"5.0.1"}}',
        "public/index.html": "<html></html>",
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "static"
    assert result["recommended_target"] == "s3"


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


def test_detects_app_inside_a_monorepo(tmp_path):
    """[미탐] backend/ + frontend/ 구조를 통째로 못 봤다."""
    _write(tmp_path, {
        "backend/requirements.txt": "fastapi\n",
        "backend/main.py": "from fastapi import FastAPI",
        "frontend/package.json": '{"dependencies":{"react":"^18","vite":"^5"}}',
    })
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "server"
    # 정적 신호가 같이 있으면 숨기지 않는다 — 사용자가 반대로 고를 수 있어야 한다(D5).
    assert any("정적" in e for e in result["evidence"]), result["evidence"]


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


def test_backend_is_found_even_when_it_sorts_last_in_a_big_monorepo(tmp_path):
    """[P1-2] 앱 루트 상한을 이름순으로 자르면 백엔드가 잘려 나간다.

    `packages/ui-00` … `ui-13` 이 자리를 다 차지하고 `packages/zz-api`
    (Dockerfile + express)가 밀려나면, 백엔드가 있는 모노레포를 정적
    사이트로 판정해 **S3 를 권하게 된다.** 모노레포를 보려고 넣은 탐색이
    정확히 그 지점에서 무너지는 형태다.
    """
    files = {f"packages/ui-{i:02d}/package.json": '{"dependencies":{"react":"^18"}}'
             for i in range(14)}
    files["packages/zz-api/package.json"] = '{"dependencies":{"express":"^4"}}'
    files["packages/zz-api/Dockerfile"] = "FROM node:20"
    _write(tmp_path, files)

    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "server", result
    assert "zz-api" in "·".join(result["evidence"]), result["evidence"]


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


def test_symlinked_duplicate_of_the_same_app_is_not_counted_twice(tmp_path):
    """[P2-3] 심볼릭 링크로 같은 폴더가 여러 번 잡히면 근거가 중복된다."""
    import os as _os

    _write(tmp_path, {
        "api/requirements.txt": "fastapi\n",
        "api/main.py": "from fastapi import FastAPI",
    })
    try:
        _os.symlink(tmp_path, tmp_path / "mirror", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없다")

    result = deploy._deployment_preflight(str(tmp_path))
    assert not [e for e in result["evidence"] if "mirror" in e], result["evidence"]
    assert len(result["evidence"]) == len(set(result["evidence"]))


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


def test_detection_stays_fast_on_a_large_tree(tmp_path):
    """[P2-6] 데모 중 감지가 몇 초씩 걸리면 안 된다.

    파일이 많은 asset 폴더가 탐색 예산을 다 먹어 정작 백엔드에 도달하지
    못하는 일도 없어야 한다.
    """
    import time

    assets = tmp_path / "assets"
    assets.mkdir()
    for i in range(4000):
        (assets / f"f{i}.bin").write_text("", encoding="utf-8")
    _write(tmp_path, {
        "backend/requirements.txt": "fastapi\n",
        "index.html": "<html></html>",
    })

    started = time.perf_counter()
    result = deploy._deployment_preflight(str(tmp_path))
    elapsed = time.perf_counter() - started

    assert result["app_kind"] == "server", result
    assert any("backend" in e for e in result["evidence"]), result["evidence"]
    assert elapsed < 3.0, f"감지에 {elapsed:.2f}s 걸렸다"


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


def test_a_file_heavy_folder_does_not_exhaust_the_scan_budget(tmp_path):
    """[P2-6] 파일이 많은 폴더 하나가 탐색 예산을 다 먹으면 안 된다.

    파일을 상한에 세면, 먼저 만난 asset 폴더에서 예산이 바닥나 **그 뒤에
    있는 backend/ 에 도달하지 못한 채** 탐색이 끝난다. 그러면 백엔드가 있는
    프로젝트를 정적 사이트로 판정한다.
    """
    assets = tmp_path / "a-assets"
    assets.mkdir()
    for i in range(5000):
        (assets / f"f{i}.bin").write_text("", encoding="utf-8")

    _write(tmp_path, {
        "svc/backend/requirements.txt": "fastapi\n",
        "index.html": "<html></html>",
    })

    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "server", result
    assert any("backend" in e for e in result["evidence"]), result["evidence"]


def test_symlinked_directories_are_never_traversed(tmp_path):
    """심볼릭 링크는 **애초에 들어가지 않는다.**

    순환 링크에서 무한 루프가 나지 않는 근본 이유가 깊이 상한이 아니라
    이것이다. 링크를 따라가기 시작하면 같은 앱이 경로만 다르게 여러 번
    잡혀 근거 문장이 중복된다.
    """
    import os as _os

    _write(tmp_path, {"api/requirements.txt": "fastapi\n"})
    try:
        _os.symlink(tmp_path, tmp_path / "mirror", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없다")

    roots = deploy._iter_app_roots(tmp_path)
    assert not [p for p in roots if "mirror" in str(p)], roots
