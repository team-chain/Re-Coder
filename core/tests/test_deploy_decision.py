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


def test_detected_app_root_is_reported(tmp_path):
    """감지 결과에 **어느 폴더에서 찾았는지**가 실려야 한다."""
    _write(tmp_path, {f"backend/{k}": v for k, v in APP_FILES.items()})
    result = deploy._deployment_preflight(str(tmp_path))
    assert result["app_kind"] == "server"
    assert result["app_root"] == "backend", result

    flat = tmp_path / "flat"
    _write(flat, APP_FILES)
    assert deploy._deployment_preflight(str(flat))["app_root"] == "", "루트면 빈 문자열"


def test_safety_preflight_checks_the_detected_app_root(tmp_path):
    """[Codex P1] 감지는 `backend/` 를 찾았는데 검사는 루트만 뒤지던 것.

    그러면 방금 앱을 찾아 놓고 `APP_ENTRYPOINT_NOT_FOUND` 로 막는다. 확장은
    `blocked` 가 참이면 배포 대상 선택을 통째로 비활성화하므로 **사용자가
    아무것도 할 수 없는 막다른 길**이 된다. 하위 폴더 탐색을 넣으면서 만든
    구멍이다.
    """
    _write(tmp_path, {**{f"backend/{k}": v for k, v in APP_FILES.items()}, **REPO_FILES})
    detected = deploy._deployment_preflight(str(tmp_path))
    safety = deploy._run_deployment_safety_preflight(
        str(tmp_path), detected["app_kind"], detected["app_root"]
    )
    codes = [r["code"] for r in safety["reasons"]]
    assert "APP_ENTRYPOINT_NOT_FOUND" not in codes, codes
    assert safety["checked_path"] == "backend", safety


def test_nested_and_root_layouts_produce_the_same_verdict(tmp_path):
    """[Codex P1] **같은 앱이면 어디에 있든 같은 결과**여야 한다.

    이게 이 수정의 진짜 기준이다. 개별 검사 하나를 통과시키는 게 아니라,
    폴더 배치 때문에 판단이 갈리지 않게 하는 것이다.
    """
    flat = tmp_path / "flat"
    _write(flat, {**APP_FILES, **REPO_FILES})
    nested = tmp_path / "nested"
    _write(nested, {**{f"backend/{k}": v for k, v in APP_FILES.items()}, **REPO_FILES,
                    "frontend/package.json": '{"dependencies":{"react":"^18"}}'})

    a = deploy._deployment_preflight(str(flat))
    b = deploy._deployment_preflight(str(nested))
    sa = deploy._run_deployment_safety_preflight(str(flat), a["app_kind"], a["app_root"])
    sb = deploy._run_deployment_safety_preflight(str(nested), b["app_kind"], b["app_root"])

    assert [r["code"] for r in sa["reasons"]] == [r["code"] for r in sb["reasons"]], (
        f"루트={[r['code'] for r in sa['reasons']]} / "
        f"하위={[r['code'] for r in sb['reasons']]}"
    )


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


def test_safety_preflight_rejects_an_app_root_outside_the_workspace(tmp_path):
    """`app_root` 는 응답에 실려 확장까지 갔다 돌아온다 — 경로 탈출을 막는다.

    `checked_path` 만 보면 부족하다. 탈출에 성공해도 상대경로 계산이 실패해
    빈 문자열이 나오기 때문이다. **실제로 어느 폴더를 검사했는지**가
    결과로 드러나게 만들어 대조한다.
    """
    ws = tmp_path / "ws"
    _write(ws, {**REPO_FILES, "requirements.txt": "fastapi\n", "src/main.py": "from fastapi import FastAPI\n"})
    # 바깥 폴더에는 Dockerfile 이 있다 — 여기를 검사하면 MISSING_DOCKERFILE 이 사라진다.
    outside = tmp_path / "outside"
    _write(outside, {"requirements.txt": "fastapi\n", "src/main.py": "from fastapi import FastAPI\n",
                     "Dockerfile": "FROM python:3.12\n"})

    escaped = deploy._run_deployment_safety_preflight(str(ws), "server", "../outside")
    inside = deploy._run_deployment_safety_preflight(str(ws), "server", "")

    assert escaped["checked_path"] == "", escaped
    assert [r["code"] for r in escaped["reasons"]] == [r["code"] for r in inside["reasons"]], (
        "워크스페이스 밖을 검사했다"
    )


def test_preflight_route_passes_the_detected_app_root(tmp_path):
    """[Codex P1] 라우트가 감지 결과의 `app_root` 를 검사에 실제로 넘기는가.

    함수 단위로만 검증하면 **배선이 빠져도 통과한다** — 변이 시험에서 실제로
    살아남았다. 사용자가 지나는 경로 그대로 확인한다.
    """
    import asyncio as _asyncio

    _write(tmp_path, {**{f"backend/{k}": v for k, v in APP_FILES.items()}, **REPO_FILES})
    payload = _asyncio.run(
        deploy.deploy_preflight(deploy.DeployPreflightRequest(workspace_path=str(tmp_path)))
    )
    assert payload["app_root"] == "backend", payload
    assert payload["checked_path"] == "backend", payload
    assert "APP_ENTRYPOINT_NOT_FOUND" not in [r["code"] for r in payload["reasons"]]


def test_applied_remediation_lands_in_the_app_root(tmp_path):
    """[Codex P1] 수정안이 **검사한 폴더**에 실제로 쓰이는가.

    저장 구조만 확인하면 적용 경로가 워크스페이스 루트로 되돌아가도
    통과한다 — 이것도 변이 시험에서 살아남았다. 파일이 어디 생기는지를 본다.
    """
    import asyncio as _asyncio

    # Dockerfile 을 빼서 MISSING_DOCKERFILE 제안이 나오게 한다.
    app = {k: v for k, v in APP_FILES.items() if k != "Dockerfile"}
    _write(tmp_path, {**{f"backend/{k}": v for k, v in app.items()}, **REPO_FILES})

    detected = deploy._deployment_preflight(str(tmp_path))
    deploy._deployment_remediation_proposals.clear()
    safety = deploy._run_deployment_safety_preflight(
        str(tmp_path), detected["app_kind"], detected["app_root"]
    )
    target = next(
        (r for r in safety["reasons"]
         if r["code"] == "MISSING_DOCKERFILE" and r["remediation_available"]),
        None,
    )
    if target is None:
        pytest.skip("이 조합에서 자동 적용 가능한 Dockerfile 제안이 나오지 않는다")

    _asyncio.run(deploy.apply_deployment_remediation(
        target["proposal_id"],
        deploy.DeploymentRemediationApplyRequest(workspace_path=str(tmp_path)),
    ))

    assert (tmp_path / "backend" / "Dockerfile").is_file(), "앱 루트에 안 생겼다"
    assert not (tmp_path / "Dockerfile").exists(), "워크스페이스 루트에 생겼다"


def test_remediation_is_applied_to_the_checked_folder(tmp_path):
    """수정안은 **검사한 폴더**에 적용돼야 한다.

    `backend/` 를 검사해 만든 제안을 워크스페이스 루트에 적용하면 파일이
    엉뚱한 곳에 생긴다.
    """
    _write(tmp_path, {**{f"backend/{k}": v for k, v in APP_FILES.items()}, **REPO_FILES})
    detected = deploy._deployment_preflight(str(tmp_path))
    # 제안 저장소는 모듈 전역이라 다른 테스트의 잔여물이 섞인다.
    # 이 테스트가 만든 것만 보도록 먼저 비운다.
    deploy._deployment_remediation_proposals.clear()
    safety = deploy._run_deployment_safety_preflight(
        str(tmp_path), detected["app_kind"], detected["app_root"]
    )
    ids = {r["proposal_id"] for r in safety["reasons"] if r["proposal_id"]}
    stored = [deploy._deployment_remediation_proposals[i] for i in ids]
    assert stored, "제안이 하나도 만들어지지 않았다"
    for item in stored:
        # 소유권은 워크스페이스 기준, 적용은 앱 루트 기준.
        assert item.workspace_root == tmp_path.resolve()
        assert item.app_root == (tmp_path / "backend").resolve()


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


def test_framework_evidence_beats_root_container_metadata(tmp_path):
    """[Codex P1] 저장소 루트의 `Dockerfile` 이 앱 루트를 선점하던 것.

    루트를 먼저 방문하므로 Dockerfile 이 `server_app_root` 를 잡으면, 뒤에
    나온 `backend/` 의 FastAPI 근거가 그걸 못 이겼다. 그러면 검사가 다시
    루트로 가서 `APP_ENTRYPOINT_NOT_FOUND` 막다른 길이 그대로 재발한다.

    컨테이너 선언은 "여기서 뭔가를 띄운다"는 일반적 메타데이터고,
    프레임워크와 진입점을 가진 폴더가 있으면 그쪽이 앱 루트다.
    """
    _write(tmp_path, {
        "Dockerfile": "FROM python:3.12\n",
        "backend/requirements.txt": "fastapi\nuvicorn\n",
        "backend/src/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        **REPO_FILES,
    })
    detected = deploy._deployment_preflight(str(tmp_path))
    assert detected["app_root"] == "backend", detected

    safety = deploy._run_deployment_safety_preflight(
        str(tmp_path), detected["app_kind"], detected["app_root"]
    )
    assert "APP_ENTRYPOINT_NOT_FOUND" not in [r["code"] for r in safety["reasons"]]


def test_container_metadata_still_used_when_nothing_stronger_exists(tmp_path):
    """반대 방향 — 프레임워크 근거가 없으면 컨테이너 선언이라도 쓴다."""
    _write(tmp_path, {"svc/Dockerfile": "FROM node:20\n", **REPO_FILES})
    detected = deploy._deployment_preflight(str(tmp_path))
    assert detected["app_kind"] == "server"
    assert detected["app_root"] == "svc", detected


def test_workspace_release_contract_is_not_discarded_for_nested_apps(tmp_path):
    """[Codex P1] 루트의 `recoder.yml` 을 조용히 버리면 정책이 사라진다.

    검사 위치만 앱 루트로 옮기면서 계약도 거기서만 찾으면, 사용자가 정한
    `required_env`·포트·정책이 더는 강제되지 않는다 — **사용자 계약이라면
    막았을 배포가 통과**한다.
    """
    _write(tmp_path, {
        "recoder.yml": _CONTRACT_YML,
        "backend/requirements.txt": "fastapi\n",
        "backend/src/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        **REPO_FILES,
    })
    detected = deploy._deployment_preflight(str(tmp_path))
    safety = deploy._run_deployment_safety_preflight(
        str(tmp_path), detected["app_kind"], detected["app_root"]
    )
    env_blockers = [r for r in safety["reasons"] if r["code"] == "MISSING_REQUIRED_ENV"]
    assert env_blockers, safety["reasons"]
    assert "SUPER_SECRET_KEY" in env_blockers[0]["fix"], env_blockers


def test_app_root_contract_wins_over_the_workspace_contract(tmp_path):
    """앱 루트에 자체 계약이 있으면 그쪽이 더 구체적이므로 우선한다."""
    _write(tmp_path, {
        "recoder.yml": _CONTRACT_YML,
        "backend/recoder.yml": _CONTRACT_YML.replace("SUPER_SECRET_KEY", "BACKEND_ONLY_KEY"),
        "backend/requirements.txt": "fastapi\n",
        "backend/src/main.py": "from fastapi import FastAPI\n",
        **REPO_FILES,
    })
    detected = deploy._deployment_preflight(str(tmp_path))
    safety = deploy._run_deployment_safety_preflight(
        str(tmp_path), detected["app_kind"], detected["app_root"]
    )
    env_blockers = [r for r in safety["reasons"] if r["code"] == "MISSING_REQUIRED_ENV"]
    assert env_blockers and "BACKEND_ONLY_KEY" in env_blockers[0]["fix"], env_blockers


def test_app_root_is_revalidated_right_before_applying(tmp_path):
    """[Codex P2] 검사와 적용 사이에 폴더가 링크로 바뀌면 승인 범위 밖에 쓴다.

    앞선 소유권 확인은 **워크스페이스만** 보므로 이 경로를 막지 못한다.
    """
    import asyncio as _asyncio
    import os as _os
    import shutil as _shutil

    app = {k: v for k, v in APP_FILES.items() if k != "Dockerfile"}
    _write(tmp_path, {**{f"backend/{k}": v for k, v in app.items()}, **REPO_FILES})
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir(exist_ok=True)

    detected = deploy._deployment_preflight(str(tmp_path))
    deploy._deployment_remediation_proposals.clear()
    safety = deploy._run_deployment_safety_preflight(
        str(tmp_path), detected["app_kind"], detected["app_root"]
    )
    proposal_id = next((r["proposal_id"] for r in safety["reasons"] if r["proposal_id"]), None)
    if proposal_id is None:
        pytest.skip("자동 적용 가능한 제안이 없다")

    _shutil.rmtree(tmp_path / "backend")
    try:
        _os.symlink(outside, tmp_path / "backend", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없다")

    with pytest.raises(HTTPException) as caught:
        _asyncio.run(deploy.apply_deployment_remediation(
            proposal_id,
            deploy.DeploymentRemediationApplyRequest(workspace_path=str(tmp_path)),
        ))
    assert caught.value.status_code == 409
    assert not list(outside.iterdir()), "워크스페이스 밖에 파일이 생겼다"
