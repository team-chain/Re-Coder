"""
ReCoder Core — Deployment Routes

Handles Dockerfile/infra file generation, security scans, deployment
planning, execution, records, and rollback.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from schemas import (
    ApprovalLevel,
    DeployMethod,
    DeploymentPlan,
    DeploymentRecord,
    DeployStatus,
    FileType,
    InfraFileProposal,
    RiskLevel,
    StackType,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["deploy"])

# ---------------------------------------------------------------------------
# In-process stores (per server lifetime)
# ---------------------------------------------------------------------------

_infra_proposals: dict[str, InfraFileProposal] = {}
_deployment_plans: dict[str, DeploymentPlan] = {}
_deployment_records: dict[str, DeploymentRecord] = {}


_ROLLBACK_HEALTH_TIMEOUT_SECONDS = 15.0
_ROLLBACK_HEALTH_RETRY_SECONDS = 0.5


async def _verify_rollback_candidate_health(plan: DeploymentPlan) -> bool:
    """실행 직후의 로컬 HTTP 헬스 확인으로 롤백 후보 자격을 결정한다.

    ``docker run -d`` 의 성공은 프로세스가 시작됐다는 뜻일 뿐, 앱이 요청을
    처리할 수 있다는 뜻은 아니다. 후보를 안전하게 고르기 위해 최초 2xx 응답을
    기다리되, 이 검증이 실패해도 실행 결과 자체는 그대로 돌려준다. 대신 그
    배포는 다음 배포의 자동 롤백 대상으로 선택되지 않는다.
    """
    if not plan.ports:
        return False

    try:
        host_port = int(next(iter(plan.ports.keys())))
    except (StopIteration, TypeError, ValueError):
        return False

    health_path = plan.health_check_path or "/health"
    if not health_path.startswith("/"):
        health_path = "/" + health_path

    try:
        try:
            from preflight.runtime import http_probe  # type: ignore
        except ImportError:  # pragma: no cover - package 실행 호환
            from core.preflight.runtime import http_probe  # type: ignore

        deadline = time.monotonic() + _ROLLBACK_HEALTH_TIMEOUT_SECONDS
        while True:
            healthy, _status, _body = await asyncio.to_thread(
                http_probe, "127.0.0.1", host_port, health_path, 2.0,
            )
            if healthy:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(_ROLLBACK_HEALTH_RETRY_SECONDS)
    except Exception as exc:  # noqa: BLE001 - 배포 결과를 헬스 확인 오류가 뒤집지 않는다
        logger.warning("Rollback candidate health verification failed: %s", exc)
        return False


async def _mark_rollback_candidate_unhealthy(deployment_id: str, _anomaly: dict) -> None:
    """지속 검증이 이상을 감지하면 해당 배포를 향후 롤백 후보에서 제외한다."""
    record = _deployment_records.get(deployment_id)
    if record is not None:
        record.rollback_eligible = False


async def _update_rollback_candidate_after_verification(state: object) -> None:
    """지속 검증 종료 결과로 느린 시작 앱의 롤백 후보 자격을 갱신한다."""
    deployment_id = str(getattr(state, "deployment_id", ""))
    record = _deployment_records.get(deployment_id)
    if record is None:
        return

    status = str(getattr(state, "status", ""))
    if status == "stable":
        # 최초 15초 안에 준비되지 않은 앱도 5분 감시를 통과했다면 안전한 후보다.
        record.rollback_eligible = True
    elif status in {"unstable", "error"}:
        record.rollback_eligible = False


def _refresh_rollback_target(plan: DeploymentPlan) -> tuple[Optional[str], str]:
    """현재 배포 기록을 기준으로 플랜의 롤백 대상을 새로 계산한다.

    플랜은 승인 대기 중에도 다른 배포가 실행될 수 있다. 따라서 플랜을 만들 때의
    스냅샷은 화면 안내용일 뿐이며, 실제 record 에 저장할 대상은 실행 직전에 다시
    계산해야 한다.
    """
    rollback_target, rollback_reason = _previous_image_for(
        plan.container_name or "", plan.image or "",
    )
    plan.rollback_image = rollback_target
    plan.risk_reasons = [
        reason for reason in plan.risk_reasons
        if not reason.startswith("롤백 대상 없음:")
    ]
    if rollback_target is None:
        plan.risk_reasons.append(rollback_reason)
    return rollback_target, rollback_reason


def _previous_image_for(container_name: str, next_image: str) -> tuple[Optional[str], str]:
    """같은 컨테이너의 마지막 검증 완료 배포 이미지 태그를 찾는다.

    ## 왜 필요한가

    `DeployAgent.create_plan()` 은 `rollback_image=None` 을 하드코딩하고 있었고,
    저장소 어디에서도 이 값을 채우지 않았다. 그 값은 그대로
    `DeploymentRecord.rollback_target` 이 되므로, **모든 로컬 Docker 배포가
    되돌릴 수 없는 상태**였다 — `/api/deploy/rollback` 은 항상 422 를 냈다.
    단위 테스트는 조각별로만 돌아서 이 구멍이 드러나지 않았다.

    ## 같은 태그는 롤백 대상이 아니다

    이전 배포와 이번 배포가 같은 태그(`app:latest` → `app:latest`)면, 되돌려도
    docker 는 **같은 태그가 지금 가리키는 이미지**, 즉 방금 배포한 그 이미지를
    다시 띄운다. 롤백한 것처럼 보이지만 아무것도 되돌아가지 않는다. 그런
    값을 rollback_target 에 넣으면 "롤백 가능"이라고 표시해 놓고 실제로는
    사용자를 못 구한다. 그래서 태그가 같으면 대상 없음으로 두고, **왜 없는지**
    를 사유로 돌려준다. 롤백을 쓰려면 배포마다 다른 태그를 써야 한다.

    반환: (롤백 대상 이미지 태그 또는 None, 사람이 읽을 사유)
    """
    if not container_name:
        return None, "롤백 대상 없음: 컨테이너 이름이 비어 있어 이전 배포를 찾을 수 없습니다."

    same_tag_seen = False
    for record in sorted(
        _deployment_records.values(),
        key=lambda r: r.deployed_at,
        reverse=True,
    ):
        if record.container_name != container_name:
            continue
        if record.status != DeployStatus.SUCCESS:
            continue
        if not record.rollback_eligible:
            continue
        if not record.image:
            continue
        if record.image == next_image:
            # 더 뒤로 가면 다른 태그가 있을 수 있으므로 계속 본다.
            same_tag_seen = True
            continue
        return record.image, f"롤백 대상: {record.image} (이전 성공 배포)"

    if same_tag_seen:
        return None, (
            f"롤백 대상 없음: 이전 배포와 태그가 같습니다({next_image}). "
            "같은 태그로 되돌리면 방금 올린 이미지가 다시 뜹니다 — "
            "롤백을 쓰려면 배포마다 다른 태그를 지정하세요."
        )
    return None, "롤백 대상 없음: 이 컨테이너의 검증 완료 배포가 없습니다."


def _rollback_source_for(container_name: str, next_image: str) -> Optional[DeploymentRecord]:
    """현재 선택 규칙과 같은 기준으로 실행 설정을 복원할 이전 기록을 찾는다."""
    for record in sorted(
        _deployment_records.values(),
        key=lambda r: r.deployed_at,
        reverse=True,
    ):
        if (
            record.container_name == container_name
            and record.status == DeployStatus.SUCCESS
            and record.rollback_eligible
            and record.image
            and record.image != next_image
        ):
            return record
    return None


async def _remove_existing_local_container(container_name: str) -> None:
    """동일 이름 컨테이너를 교체하기 전 stop/rm 한다. 이미 없으면 실패를 무시한다."""
    loop = asyncio.get_running_loop()
    for command in (
        ["docker", "stop", container_name],
        ["docker", "rm", container_name],
    ):
        await loop.run_in_executor(
            None,
            lambda command=command: subprocess.run(
                command, shell=False, capture_output=True, text=True, timeout=60,
            ),
        )


def _get_continuous_verifier_if_available():
    """배포 경로에서 감시기를 best-effort로 가져온다."""
    try:
        from preflight.continuous_verification import get_continuous_verifier  # type: ignore
        return get_continuous_verifier()
    except Exception:  # noqa: BLE001
        try:
            from core.preflight.continuous_verification import get_continuous_verifier  # type: ignore
            return get_continuous_verifier()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Continuous verifier unavailable while replacing container: %s", exc)
            return None


async def _stop_prior_verifications_for_container(container_name: str) -> None:
    """같은 컨테이너를 교체하기 전, 이전 배포의 감시를 중지한다.

    감시는 컨테이너 이름과 localhost 포트를 관찰한다. v1 감시를 남긴 채 v2로
    교체하면 v2의 장애를 v1에 귀속해 정상 롤백 후보를 잃을 수 있다.
    """
    verifier = _get_continuous_verifier_if_available()
    if verifier is None:
        return

    try:
        active_ids = set(verifier.list_active())
        prior_ids = [
            record.deployment_id
            for record in _deployment_records.values()
            if record.container_name == container_name and record.deployment_id in active_ids
        ]
        for deployment_id in prior_ids:
            await verifier.stop(deployment_id)
    except Exception as exc:  # noqa: BLE001 - 기존 컨테이너 교체를 막지는 않는다
        logger.warning("Could not stop prior continuous verification: %s", exc)
# Static Preflight가 만든 수정안은 사용자가 배포 화면에서 "자동 수정"을 눌렀을
# 때만 적용한다. 프로세스 메모리에만 두므로 Core 재시작 후에는 다시 검사해야 한다.
@dataclass(frozen=True)
class _StoredDeploymentRemediation:
    proposal: object
    workspace_root: Path


_deployment_remediation_proposals: dict[str, _StoredDeploymentRemediation] = {}

# S3/정적 사이트를 고르기 전에는 서버 런타임·Docker·포트 가정을 검사하지
# 않는다. 시크릿·취약점·잠재적인 .env 유출 검사는 배포 대상과 무관하므로
# 계속 먼저 확인한다.
_STATIC_TARGET_INDEPENDENT_CHECK_CODES = {
    "ENV_FILE_NOT_GITIGNORED",
    "INVALID_ENV_FORMAT",
    "UNPINNED_DEPENDENCIES",
    "CRITICAL_VULNERABILITY",
    "SECRET_LEAK_RISK",
}

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class DockerfileRequest(BaseModel):
    workspace_path: str
    stack: Optional[StackType] = None
    project_id: Optional[str] = None
    extra_context: Optional[str] = None


class ScanRequest(BaseModel):
    workspace_path: str
    scan_type: str  # "trivy" | "hadolint" | "gitleaks"
    target_path: Optional[str] = None


class DeployPlanRequest(BaseModel):
    workspace_path: str
    project_id: Optional[str] = None
    method: DeployMethod = DeployMethod.LOCAL_DOCKER
    image: Optional[str] = None
    container_name: Optional[str] = None
    host_port: Optional[int] = None
    container_port: Optional[int] = None
    extra_context: Optional[str] = None
    # Security gate (Ship Stage). Default False — security scans always run.
    # Set to True to bypass Trivy/Hadolint pre-deployment gating (e.g. CI dry runs).
    skip_security_scan: bool = False
    # 설계 §4.6 / §34 — 배포 직후 5분 헬스 체크 자동 트리거 여부.
    enable_continuous_verification: bool = True


class ExecuteRequest(BaseModel):
    plan_id: str
    approved: bool
    # Per-execute override; if None, falls back to the plan's setting.
    enable_continuous_verification: Optional[bool] = None


class RollbackRequest(BaseModel):
    deployment_id: str


class DeployPreflightRequest(BaseModel):
    """배포 대상 선택 카드에 표시할 프로젝트 감지 요청."""
    workspace_path: str


class DeploymentDecisionRequest(BaseModel):
    """사용자가 승인한 배포 대상. ADR은 확장이 워크스페이스에 기록한다."""
    workspace_path: str
    target: Literal["ecs", "s3", "local"]
    evidence: list[str] = []


class DeploymentRemediationApplyRequest(BaseModel):
    """배포 차단 카드에서 사용자가 승인한 안전한 자동 수정 요청."""
    workspace_path: str


# ---------------------------------------------------------------------------
# Agent singletons — initialised lazily on first use so that missing
# optional dependencies (boto3, google-generativeai) only raise at call
# time rather than blocking the whole server startup.
# ---------------------------------------------------------------------------

_infra_agent = None   # type: ignore
_deploy_agent = None  # type: ignore


def _get_infra_agent():
    global _infra_agent
    if _infra_agent is None:
        try:
            from llm.provider_router import LLMProviderRouter  # type: ignore
            from registry import FileTemplateRegistry  # type: ignore
            from agents.infra_agent import InfraAgent  # type: ignore
            _infra_agent = InfraAgent(
                provider_router=LLMProviderRouter(),
                file_template_registry=FileTemplateRegistry(),
            )
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("InfraAgent unavailable: %s", exc)
    return _infra_agent


def _get_deploy_agent():
    global _deploy_agent
    if _deploy_agent is None:
        try:
            from llm.provider_router import LLMProviderRouter  # type: ignore
            from agents.deploy_agent import DeployAgent  # type: ignore
            _deploy_agent = DeployAgent(
                provider_router=LLMProviderRouter(),
            )
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("DeployAgent unavailable: %s", exc)
    return _deploy_agent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_stack(workspace_path: str) -> StackType:
    """Heuristically detect the project stack from workspace files."""
    ws = Path(workspace_path)
    if (ws / "requirements.txt").exists() or (ws / "pyproject.toml").exists():
        # Limit to 20 files to avoid blocking the async event loop on large projects.
        for f in list(ws.rglob("*.py"))[:20]:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
                if "fastapi" in text.lower():
                    return StackType.PYTHON_FASTAPI
                if "flask" in text.lower():
                    return StackType.PYTHON_FLASK
                if "django" in text.lower():
                    return StackType.PYTHON_DJANGO
            except Exception:
                continue
        return StackType.PYTHON_FASTAPI  # Default for Python
    if (ws / "package.json").exists():
        try:
            import json
            pkg = json.loads((ws / "package.json").read_text(encoding="utf-8"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                return StackType.NODE_NEXT
            if "@nestjs/core" in deps:
                return StackType.NODE_NEST
            if "express" in deps:
                return StackType.NODE_EXPRESS
        except Exception:
            pass
        return StackType.NODE_EXPRESS
    if (ws / "go.mod").exists():
        return StackType.GO
    if (ws / "pom.xml").exists() or (ws / "build.gradle").exists():
        return StackType.JAVA_SPRING
    if (ws / "Gemfile").exists():
        return StackType.RUBY_RAILS
    return StackType.UNKNOWN


def _read_text_if_exists(path: Path, limit: int = 100_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# FR-05-01 앱 종류 감지
#
# 이 판정은 사용자에게 **"어디에 올릴까요?" 카드의 추천 근거**로 그대로
# 보인다(확정 D7). 그래서 맞히는 것만큼 **왜 그렇게 봤는지 말할 수 있는
# 것**이 중요하다. `evidence` 는 로그가 아니라 화면에 뜨는 문장이다.
#
# 실측으로 확인한 예전 판의 구멍(12개 형태 중 8개 오답):
#   · 최상위 `*.py` 만 봐서 `src/main.py` 의 FastAPI 를 못 봄
#   · 정적 빌더가 vite 뿐 — CRA·Astro·Angular·Vue CLI 전부 미탐
#   · `go.mod`/`pom.xml`/`Gemfile` 을 안 봄 (같은 파일의 `_detect_stack` 은 봄)
#   · Dockerfile 이라는 **가장 강한 서버 신호**를 아예 안 봄
#   · 모노레포(backend/ + frontend/)를 통째로 못 봄
#   · 부분 문자열 매칭이라 주석의 `# fastapi 는 쓰지 않는다` 를 서버로 오탐
#   · Next.js 정적 export(`output: 'export'`)를 서버로 오분류
# ---------------------------------------------------------------------------

#: 탐색에서 제외할 폴더.
#:
#: 들어가면 느려지기만 하는 게 아니라, **남의 의존성 안에 있는 파일을 이
#: 프로젝트의 증거로 삼는다.** `node_modules` 안에는 express 도 vite 도 다
#: 들어 있어서, 한 번 들어가면 모든 프로젝트가 서버형이 된다.
_SKIP_DIRS = frozenset({
    "node_modules", ".git", ".hg", ".svn", ".venv", "venv", "env", ".env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "out", ".next", ".nuxt", ".svelte-kit", ".output",
    "target", ".gradle", "vendor", "coverage", "htmlcov",
    ".idea", ".vscode", ".terraform", "site-packages", ".tox", ".cache",
})

#: 여기 하나라도 있으면 "앱 루트"로 본다.
#: 서버 런타임을 뜻하는 파이썬 의존성 (정규화된 이름으로 정확히 비교).
_PY_SERVER_DEPS = {
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django",
    "starlette": "Starlette", "litestar": "Litestar", "sanic": "Sanic",
    "tornado": "Tornado", "aiohttp": "aiohttp", "bottle": "Bottle",
    "falcon": "Falcon", "quart": "Quart", "pyramid": "Pyramid",
    "gunicorn": "Gunicorn", "uvicorn": "Uvicorn", "hypercorn": "Hypercorn",
    "waitress": "Waitress",
}

#: 서버 런타임을 뜻하는 Node 의존성.
_NODE_SERVER_DEPS = {
    "express": "Express", "@nestjs/core": "NestJS", "koa": "Koa",
    "fastify": "Fastify", "@hapi/hapi": "hapi", "@adonisjs/core": "AdonisJS",
    "socket.io": "Socket.IO", "@apollo/server": "Apollo Server",
    "apollo-server": "Apollo Server", "restify": "restify",
    "@feathersjs/feathers": "Feathers", "h3": "h3", "hono": "Hono",
}

#: 정적 산출물을 만드는 빌더.
_NODE_STATIC_DEPS = {
    "vite": "Vite", "react-scripts": "Create React App",
    "@angular/cli": "Angular CLI", "@vue/cli-service": "Vue CLI",
    "astro": "Astro", "gatsby": "Gatsby", "@11ty/eleventy": "Eleventy",
    "parcel": "Parcel", "@sveltejs/adapter-static": "SvelteKit (정적)",
    "vuepress": "VuePress", "@docusaurus/core": "Docusaurus",
}

#: **`webpack` 은 뺐다.** 라이브러리·CLI·VS Code 확장이 번들러로 흔히 쓰는
#: devDependency 라, 넣어 두면 이 저장소의 `extension/` 자체가
#: "정적 웹 앱 — webpack 빌드"로 판정된다(실측). 정적 산출물을 만든다는
#: 신호로는 너무 약하다.

#: 파이썬 소스에서 프레임워크를 찾을 때 쓰는 **실제 import 문** 패턴.
#: 부분 문자열 검색은 주석 한 줄에 속는다 — 실측으로 확인한 오탐이다.
#: **`\s` 를 쓰면 안 된다.** `\s` 는 개행을 포함하므로
#: `import os` 다음 줄의 `from fastapi import FastAPI` 까지 한 덩어리로
#: 삼켜, `os\nfrom fastapi import fastapi\napp` 같은 없는 모듈 이름이
#: 만들어지고 **그 뒤 줄은 다시 매치되지 않는다.** 즉 프레임워크 import
#: 앞에 다른 import 가 하나만 있어도 감지가 통째로 실패한다.
#: 가로 공백(스페이스·탭)만 허용한다.
_PY_IMPORT_RE = re.compile(
    r"^[ \t]*(?:from[ \t]+(?P<from>[\w.]+)|import[ \t]+(?P<import>[\w., \t]+))",
    re.MULTILINE,
)

#: `requirements.txt` 한 줄에서 패키지 이름만 떼어낸다.
#: `fastapi[all]>=0.110  # 주석` → `fastapi`
_REQ_LINE_RE = re.compile(r"^\s*(?:-e\s+)?([A-Za-z0-9._-]+)")

#: 여러 줄 문자열(삼중따옴표) 블록. import 문을 찾기 전에 지운다.
_TRIPLE_QUOTED_RE = re.compile(r'"""(?:.|\n)*?"""' + r"|'''(?:.|\n)*?'''")


def _normalize_dep(name: str) -> str:
    """PEP 503 방식으로 패키지 이름을 정규화한다 (`Flask_SQLAlchemy` → `flask-sqlalchemy`)."""
    return re.sub(r"[-_.]+", "-", (name or "").strip()).lower()


#: 서버라고 거의 확정할 수 있는 표식. 앱 루트가 상한을 넘칠 때 **이런
#: 폴더를 먼저 남긴다.**
#:
#: 이름순으로 자르면 `packages/ui-00` … `ui-11` 이 자리를 다 차지하고
#: `packages/zz-api`(Dockerfile + express)가 잘려 나간다. 그러면 백엔드가
#: 있는 모노레포를 **정적 사이트로 판정해 S3 를 권하게 된다** — 모노레포를
#: 보려고 넣은 탐색이 정확히 그 지점에서 무너지는 형태다.
#: 의존성이 **실제로 선언되는** TOML 섹션. 여기 밖은 보지 않는다.
#:
#: 파일 전체를 훑으면 `description = "django 없이 만든 정적 사이트"` 의 한
#: 단어나 `[tool.mypy]` 아래 키가 의존성으로 둔갑한다. 그러면 본문에
#: "django 없이"라고 적힌 프로젝트를 화면에 **"Django 서버"** 라고 표시하게
#: 된다 — 근거를 보여주는 기능이 근거를 지어내는 셈이다.
_DEP_SECTION_RE = re.compile(
    r"^(project\.optional-dependencies(\.[\w-]+)?"
    r"|dependency-groups"
    r"|tool\.poetry\.dependencies"
    r"|tool\.poetry\.dev-dependencies"
    r"|tool\.poetry\.group\.[\w-]+\.dependencies"
    r"|tool\.pdm\.dev-dependencies)$"
)


def _collect_group_values(value: object, out: set[str]) -> None:
    """**그룹 컨테이너**에서 의존성을 거둔다 — 키는 그룹 이름이므로 버린다.

    `[project.optional-dependencies]` 와 `[dependency-groups]` 는
    `이름 -> [의존성 목록]` 모양이다. 그 **키는 extra/그룹 이름**이지
    패키지가 아니다.

        [dependency-groups]
        django = ["pytest"]        # ← 이건 "django 를 쓴다"가 아니다

    키까지 걷으면 그룹 이름이 우연히 `django`·`fastapi` 인 프로젝트가
    서버로 판정돼 ECS 를 추천받는다.
    """
    if isinstance(value, dict):
        for item in value.values():
            _collect_dep_names(item, out)
    else:
        _collect_dep_names(value, out)


def _collect_dep_names(value: object, out: set[str]) -> None:
    """**의존성 테이블**에서 이름을 거둔다.

    문자열/리스트는 요구사항 표기(`"fastapi>=0.110"`), dict 는 poetry 형태
    (`fastapi = "^0.110"`)라 **키가 패키지 이름**이다. 그룹 컨테이너에는
    쓰면 안 된다 — `_collect_group_values` 를 쓸 것.
    """
    if isinstance(value, str):
        m = _REQ_LINE_RE.match(value)
        if m:
            out.add(_normalize_dep(m.group(1)))
    elif isinstance(value, list):
        for item in value:
            _collect_dep_names(item, out)
    elif isinstance(value, dict):
        for key, item in value.items():
            # poetry 형식은 키가 이름이다: `fastapi = "^0.110"`
            out.add(_normalize_dep(str(key)))
            if isinstance(item, list):
                _collect_dep_names(item, out)


def _python_deps(app_root: Path) -> set[str]:
    """선언된 파이썬 의존성 이름 집합.

    주석·설명문·도구 설정은 **의존성이 아니다.** 섹션을 정확히 지정해서
    읽는다.
    """
    deps: set[str] = set()

    for line in _read_text_if_exists(app_root / "requirements.txt").splitlines():
        line = line.split("#", 1)[0]
        m = _REQ_LINE_RE.match(line)
        if m:
            deps.add(_normalize_dep(m.group(1)))

    pyproject = _read_text_if_exists(app_root / "pyproject.toml")
    if pyproject:
        parsed: object = None
        try:
            import tomllib  # 3.11+ 표준 라이브러리
            parsed = tomllib.loads(pyproject)
        except Exception:  # noqa: BLE001 — 깨진 TOML 은 흔하다
            parsed = None

        if isinstance(parsed, dict):
            project = parsed.get("project")
            if isinstance(project, dict):
                _collect_dep_names(project.get("dependencies"), deps)
                # extra 이름은 패키지가 아니다 — 값만 본다.
                _collect_group_values(project.get("optional-dependencies"), deps)
            # 그룹 이름도 마찬가지.
            _collect_group_values(parsed.get("dependency-groups"), deps)
            tool = parsed.get("tool")
            poetry = tool.get("poetry") if isinstance(tool, dict) else None
            if isinstance(poetry, dict):
                _collect_dep_names(poetry.get("dependencies"), deps)
                _collect_dep_names(poetry.get("dev-dependencies"), deps)
                groups = poetry.get("group")
                if isinstance(groups, dict):
                    # poetry 의 group 은 `[tool.poetry.group.<이름>.dependencies]`
                    # 라 그 안쪽이 진짜 의존성 테이블이다(키가 패키지 이름).
                    for group in groups.values():
                        if isinstance(group, dict):
                            _collect_dep_names(group.get("dependencies"), deps)
        else:
            # TOML 을 못 읽었으면 **섹션 헤더를 따라가며** 의존성 구간만 본다.
            # 파일 전체를 훑는 방식으로는 되돌아가지 않는다.
            section = ""
            in_project_deps = False
            for raw in pyproject.splitlines():
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                header = re.match(r"^\[+([^\]]+)\]+$", line)
                if header:
                    section = header.group(1).strip()
                    in_project_deps = False
                    continue
                if section == "project" and re.match(r"^dependencies\s*=", line):
                    in_project_deps = True
                elif section == "project" and re.match(r"^[A-Za-z0-9._-]+\s*=", line):
                    in_project_deps = False
                if not (_DEP_SECTION_RE.match(section) or in_project_deps):
                    continue
                for quoted in re.findall(r'"([A-Za-z0-9._-]+)[^"]*"', line):
                    deps.add(_normalize_dep(quoted))
                key = re.match(r"^([A-Za-z0-9._-]+)\s*=", line)
                if key and not in_project_deps:
                    deps.add(_normalize_dep(key.group(1)))

    pipfile = _read_text_if_exists(app_root / "Pipfile")
    if pipfile:
        section = ""
        for raw in pipfile.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            header = re.match(r"^\[([^\]]+)\]$", line)
            if header:
                section = header.group(1).strip()
                continue
            if section not in ("packages", "dev-packages"):
                continue
            key = re.match(r'^"?([A-Za-z0-9._-]+)"?\s*=', line)
            if key:
                deps.add(_normalize_dep(key.group(1)))

    return deps


def _python_imported_modules(app_root: Path) -> set[str]:
    """실제 `import` 문에 등장하는 최상위 모듈 이름.

    의존성 선언이 없는 프로젝트를 위한 보조 근거다. **부분 문자열이 아니라
    import 문**을 보기 때문에 주석이나 문자열에 속지 않는다.
    """
    modules: set[str] = set()
    files: list[Path] = []
    # **진입점 검사와 같은 깊이만 본다.**
    #
    # 예전엔 `*/*.py`·`*/*/*.py` 까지 훑어서 `backend/main.py` 도 봤다. 그런데
    # 안전 검사(`check_app_entrypoint`)의 후보는 `main.py`·`app.py`·
    # `app/main.py`·`src/main.py` 뿐이다. 그래서 모노레포를 "서버형"으로
    # 판정해 놓고 바로 다음 단계에서 `APP_ENTRYPOINT_NOT_FOUND` 로 막는
    # **막다른 길**이 생긴다.
    #
    # 여기서 절반만 아는 것보다, 모르는 것을 모른다고 하는 편이 낫다 —
    # 모노레포는 preflight 계층 전체를 앱 루트 기준으로 재설계해야 제대로
    # 된다(회차4). 그때까지는 후보와 같은 깊이만 본다.
    for pattern in ("*.py", "src/*.py", "app/*.py"):
        for p in app_root.glob(pattern):
            # **워크스페이스 안쪽 경로만 본다.** `p.parts` 는 절대경로의 모든
            # 요소라, 조상 폴더 이름이 `build`·`out`·`env` 이거나 `.` 로
            # 시작하면(예: `~/.recoder/ws`) 이 폴백이 통째로 무력화된다.
            try:
                rel_parts = p.relative_to(app_root).parts
            except ValueError:
                rel_parts = p.parts
            if any(part in _SKIP_DIRS or part.startswith(".") for part in rel_parts):
                continue
            files.append(p)
            if len(files) >= 30:
                break
        if len(files) >= 30:
            break

    for path in files:
        text = _read_text_if_exists(path, 20_000)
        # **여러 줄 문자열 안의 import 문은 코드가 아니다.**
        # 이 프로젝트는 코드 생성기라 템플릿 문자열이 흔하다 —
        # `TEMPLATE = """\nfrom flask import Flask\n"""` 를 Flask 서버로
        # 읽으면 템플릿을 가진 모든 프로젝트가 서버가 된다.
        text = _TRIPLE_QUOTED_RE.sub("", text)
        for m in _PY_IMPORT_RE.finditer(text):
            raw = m.group("from") or m.group("import") or ""
            for piece in raw.split(","):
                # `import fastapi as fa` 의 별칭을 떼어낸다. 안 떼면 모듈
                # 이름이 `fastapi as fa` 가 되어 무엇과도 안 맞는다.
                # 공백으로 자른 첫 토큰이 실제 모듈 경로다.
                head = piece.strip().split()
                if not head:
                    continue
                top = head[0].split(".", 1)[0].strip()
                if top:
                    modules.add(_normalize_dep(top))
    return modules


def _node_deps(app_root: Path) -> tuple[set[str], dict]:
    """`package.json` 의 의존성 이름 집합과 원본 dict.

    문자열 검색이 아니라 **JSON 키로 정확히** 본다.
    """
    raw = _read_text_if_exists(app_root / "package.json")
    if not raw.strip():
        return set(), {}
    try:
        import json as _json
        pkg = _json.loads(raw)
    except Exception:  # noqa: BLE001 — 깨진 package.json 도 흔하다
        return set(), {}
    if not isinstance(pkg, dict):
        return set(), {}
    names: set[str] = set()
    # `peerDependencies` 는 보지 않는다. Next 플러그인 패키지가 그것만으로
    # "Next.js 서버"가 되어 라이브러리를 배포 대상으로 만든다.
    for section in ("dependencies", "devDependencies"):
        block = pkg.get(section)
        if isinstance(block, dict):
            names.update(str(k).lower() for k in block)
    return names, pkg


def _strip_js_comments(text: str) -> str:
    """JS 소스에서 주석을 지운다. 문자열 리터럴 안의 `//` 는 건드리지 않는다.

    **왜 필요한가.** SSR 로 쓰는 Next 프로젝트가 예전 설정을 주석으로 남겨
    두는 일은 아주 흔하다.

        module.exports = {
          // output: 'export'   ← 예전에 쓰던 것
          reactStrictMode: true,
        }

    주석을 안 지우면 이 한 줄에 속아 **서버가 필요한 앱에 S3 를 추천한다.**
    `requirements.txt` 와 `pyproject.toml` 에서 이미 같은 형태의 오탐을
    막았는데 여기만 남아 있었다 — 하나를 고칠 때 같은 성질의 다른 자리를
    함께 훑어야 한다는 게 또 확인됐다.

    URL(`https://...`)의 `//` 를 주석으로 오인하지 않도록 따옴표 상태를
    따라간다.
    """
    out: list[str] = []
    i, n = 0, len(text)
    quote = ""          # 현재 열려 있는 따옴표 (없으면 "")
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:       # 이스케이프는 통째로 넘긴다
                out.append(nxt)
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


#: Astro 를 서버로 만드는 어댑터. 이게 있으면 산출물이 정적 파일이 아니다.
_ASTRO_SERVER_ADAPTERS = (
    "@astrojs/node", "@astrojs/vercel", "@astrojs/netlify",
    "@astrojs/cloudflare", "@astrojs/deno",
)


def _astro_is_server(app_root: Path, node_deps: set[str]) -> bool:
    """Astro 가 **서버 모드**인지.

    Astro 는 기본이 정적이지만 `output: 'server'`(또는 `'hybrid'`)로 두고
    어댑터를 붙이면 서버 런타임이 필요하다. 그걸 안 보면 SSR Astro 앱에
    S3 를 권하게 된다 — 올려도 동작하지 않는 추천이다.

    Next.js 를 반대 방향으로 다루면서(정적 export 감지) 같은 종류의 설정이
    Astro 에도 있다는 걸 안 봤다.
    """
    if any(a in node_deps for a in _ASTRO_SERVER_ADAPTERS):
        return True
    for name in ("astro.config.mjs", "astro.config.js", "astro.config.ts", "astro.config.cjs"):
        text = _strip_js_comments(_read_text_if_exists(app_root / name, 20_000))
        if _js_config_string_value(text, "output") in ("server", "hybrid"):
            return True
    return False


def _js_balanced_region(text: str, start: int, open_ch: str, close_ch: str) -> Optional[str]:
    """`start` 의 여는 괄호부터 짝이 맞는 닫는 괄호까지의 텍스트.

    문자열 리터럴 안의 괄호는 세지 않는다. 짝이 안 맞으면 None.
    """
    n = len(text)
    if start >= n or text[start] != open_ch:
        return None
    depth = 0
    i = start
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == quote:
                    break
                i += 1
            i += 1
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


_JS_EXPORT_MARKERS: tuple["re.Pattern[str]", ...] = (
    re.compile(r"\bmodule\.exports\s*="),
    re.compile(r"\bexport\s+default\b"),
)


def _js_exported_config_regions(text: str) -> list[str]:
    """**export 되는 설정 표현식**의 텍스트 조각들만 골라낸다.

    왜 필요한가. `_js_config_string_value` 는 파일 전체를 훑었다. 그래서
    export 앞에 무관한 헬퍼 객체가 있으면 —

        const example = { output: 'export' };   // export 안 됨
        module.exports = { reactStrictMode: true };

    — 헬퍼의 값을 설정으로 읽어, 서버가 필요한 앱에 S3 를 권했다.
    실제 설정은 export 되는 객체뿐이다. 그래서 export 대상만 오려낸다.

    다루는 형태:
      module.exports = {...}                     → 객체 리터럴
      export default {...} satisfies NextConfig  → 객체 리터럴 (뒤는 무시)
      export default defineConfig({...})         → 호출 인자 전체
      module.exports = withPlugins(a, {...})     → 호출 인자 전체
      module.exports = (phase) => ({...})        → 화살표 함수 몸통
      const cfg = {...}; module.exports = cfg    → 식별자 한 단계 해석
      const cfg: NextConfig = {...}; export default cfg  → 타입 표기 허용

    못 찾으면 빈 리스트를 돌려준다. 호출자는 "값 없음"으로 처리하는데,
    그 방향이 안전하다 — output 을 못 읽으면 SSR(서버형)으로 남고,
    서버형 추천은 정적 앱에도 동작하지만 그 반대는 동작하지 않는다.
    """
    n = len(text)

    def _rhs(pos: int, depth: int) -> Optional[str]:
        i = pos
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            return None
        ch = text[i]
        if ch == "{":
            return _js_balanced_region(text, i, "{", "}")
        if ch == "(":
            region = _js_balanced_region(text, i, "(", ")")
            if region is None:
                return None
            # `(phase) => ...` — 매개변수였다면 화살표 뒤 몸통이 진짜 값이다.
            k = i + len(region)
            while k < n and text[k] in " \t\r\n":
                k += 1
            if text.startswith("=>", k):
                return _rhs(k + 2, depth)
            return region
        if text.startswith("async", i):                   # async (phase) => ...
            return _rhs(i + len("async"), depth)
        if ch.isalpha() or ch in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$."):
                j += 1
            ident = text[i:j]
            k = j
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k < n and text[k] == "(":                  # defineConfig({...})
                return _js_balanced_region(text, k, "(", ")")
            # 맨 식별자 — 선언을 찾아 한 단계만 해석한다.
            if depth < 2:
                base = ident.split(".")[0]
                decl = re.search(
                    r"\b(?:const|let|var)\s+" + re.escape(base) + r"\b[^=;\n]*=",
                    text)
                if decl:
                    return _rhs(decl.end(), depth + 1)
            return None
        return None

    regions: list[str] = []
    for marker in _JS_EXPORT_MARKERS:
        for m in marker.finditer(text):
            region = _rhs(m.end(), 0)
            if region:
                regions.append(region)
    return regions


def _js_config_string_value(text: str, key: str) -> Optional[str]:
    """JS/JSON 설정에서 **export 되는 설정 안의** `key: "값"` 을 꺼낸다. 없으면 None.

    정규식으로는 안 된다. `const hint = "set output: 'export' for static"`
    같은 **문서 문자열 안**에도 같은 모양이 들어 있어서, 정규식은 SSR 설정을
    정적으로 오판한다. 주석을 지워도 남는 문제다.

    그래서 두 단계로 좁힌다:
    1. `_js_exported_config_regions` 로 **export 되는 표현식만** 오려낸다.
       export 앞뒤의 헬퍼 객체·예시 코드는 설정이 아니다.
    2. 그 조각 안에서 문자열 상태를 따라가며 훑고, **문자열 밖에 있는
       식별자**이거나 **따옴표로 감싼 키**(`"output": ...`)일 때만 키로
       인정한다. 값도 바로 뒤에 오는 문자열 리터럴만 읽는다.

    완전한 JS 파서는 아니다 — export 조각의 중첩 객체 안 같은 키도 잡는다.
    다만 "export 되지 않는 글자를 설정으로 오인하는" 오판은 없앤다.
    """
    for region in _js_exported_config_regions(text):
        value = _js_scan_key_string(region, key)
        if value is not None:
            return value
    return None


def _js_scan_key_string(text: str, key: str) -> Optional[str]:
    """텍스트 조각에서 문자열 상태를 따라가며 `key: "값"` 을 찾는다."""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]

        # 문자열 리터럴 — 통째로 건너뛰되, 그 내용이 키일 수 있으므로 기억한다.
        if ch in "\"'`":
            quote = ch
            j = i + 1
            buf = []
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if text[j] == quote:
                    break
                buf.append(text[j])
                j += 1
            literal = "".join(buf)
            after = j + 1
            # `"output": "export"` — 따옴표로 감싼 키
            k = after
            while k < n and text[k] in " \t\r\n":
                k += 1
            if literal == key and k < n and text[k] == ":":
                value = _read_js_string_after(text, k + 1)
                if value is not None:
                    return value
            i = after
            continue

        # 문자열 밖의 맨 식별자 — `output: 'export'`
        if ch.isalpha() or ch == "_" or ch == "$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            word = text[i:j]
            k = j
            while k < n and text[k] in " \t\r\n":
                k += 1
            if word == key and k < n and text[k] == ":":
                value = _read_js_string_after(text, k + 1)
                if value is not None:
                    return value
            i = j
            continue

        i += 1
    return None


def _read_js_string_after(text: str, start: int) -> Optional[str]:
    """`start` 위치부터 공백을 건너뛰고 **문자열 리터럴 하나**를 읽는다."""
    i, n = start, len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    if i >= n or text[i] not in "\"'`":
        return None
    quote = text[i]
    i += 1
    buf = []
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            buf.append(text[i + 1])
            i += 2
            continue
        if text[i] == quote:
            return "".join(buf)
        buf.append(text[i])
        i += 1
    return None


def _next_is_static_export(app_root: Path, pkg: dict) -> bool:
    """Next.js 가 **정적 export** 설정인지.

    `output: 'export'` 면 산출물이 정적 파일이라 S3 로 올리는 게 맞다.
    이걸 안 보면 정적 사이트를 컨테이너로 띄우라고 권하게 된다.
    """
    for name in ("next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs"):
        text = _strip_js_comments(_read_text_if_exists(app_root / name, 20_000))
        # 따옴표 있는 키(`"output": "export"`)와 없는 키를 모두 받되,
        # **문자열 안의 같은 글자에는 속지 않는다.**
        if _js_config_string_value(text, "output") == "export":
            return True
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    return any("next export" in str(v) for v in (scripts or {}).values())


def _deployment_preflight(workspace_path: str) -> dict:
    """프로젝트 파일만으로 서버형/정적 앱을 판별해 배포 선택지를 추천한다.

    **워크스페이스 루트만 본다.** 하위 폴더까지 훑어 모노레포를 지원하는
    판을 만들었다가 되돌렸다 — 이유는 아래 「모노레포」 절에 적어 뒀다.

    반환 계약: `app_kind` · `summary` · `evidence` · `recommended_target`.
    확장의 배포 카드와 `_run_deployment_safety_preflight` 가 이 모양에
    의존한다.
    """
    root = Path(workspace_path)
    if not root.is_dir():
        raise ValueError("유효한 워크스페이스 경로가 아닙니다.")

    server_evidence: list[str] = []
    static_evidence: list[str] = []
    extra_evidence: list[str] = []

    # ── 파이썬 ──────────────────────────────────────────────────────
    py_deps = _python_deps(root)
    for dep, label in _PY_SERVER_DEPS.items():
        if dep in py_deps:
            server_evidence.append(f"{label} 서버")
            break
    else:
        # 의존성 선언이 없으면 실제 import 문을 본다.
        py_modules = _python_imported_modules(root)
        for dep, label in _PY_SERVER_DEPS.items():
            if dep in py_modules:
                server_evidence.append(f"{label} 서버")
                break

    # ── Node ────────────────────────────────────────────────────────
    node_deps, pkg = _node_deps(root)
    if "next" in node_deps:
        if _next_is_static_export(root, pkg):
            static_evidence.append("Next.js 정적 export")
        else:
            server_evidence.append("Next.js 서버")
    for dep, label in _NODE_SERVER_DEPS.items():
        if dep in node_deps:
            server_evidence.append(f"{label} 서버")
            break
    # Astro 는 정적/서버 양쪽이라 설정을 봐야 한다.
    if "astro" in node_deps and _astro_is_server(root, node_deps):
        server_evidence.append("Astro 서버(SSR)")
    else:
        for dep, label in _NODE_STATIC_DEPS.items():
            if dep in node_deps:
                static_evidence.append(f"{label} 빌드")
                break

    # ── 그 밖의 서버 런타임 ──────────────────────────────────────────
    # 같은 파일의 `_detect_stack` 은 이미 이것들을 보고 있었다. 판정이
    # 두 함수에서 갈리면 Dockerfile 은 만들어 주면서 배포 대상은
    # "잘 모르겠다"고 하는 앞뒤 안 맞는 화면이 된다.
    if (root / "go.mod").is_file():
        server_evidence.append("Go 모듈")
    if (root / "pom.xml").is_file() or (root / "build.gradle").is_file() \
            or (root / "build.gradle.kts").is_file():
        server_evidence.append("Java/Spring 빌드")
    if (root / "Gemfile").is_file():
        server_evidence.append("Ruby/Rails")
    if (root / "composer.json").is_file():
        server_evidence.append("PHP/Composer")

    # ── 컨테이너·프로세스 선언 ───────────────────────────────────────
    # **가장 강한 서버 신호인데 예전 판은 아예 보지 않았다.**
    # 컨테이너로 띄우도록 만들어 둔 앱을 정적 호스팅으로 권할 수는 없다.
    if (root / "Dockerfile").is_file():
        server_evidence.append("Dockerfile")
    elif (root / "docker-compose.yml").is_file() or (root / "docker-compose.yaml").is_file():
        server_evidence.append("docker-compose")
    if (root / "Procfile").is_file():
        server_evidence.append("Procfile")

    # ── 정적 사이트 ──────────────────────────────────────────────────
    for entry in ("index.html", "public/index.html", "src/index.html"):
        if (root / entry).is_file():
            static_evidence.append("정적 HTML 엔트리")
            break
    if (root / "_config.yml").is_file():
        static_evidence.append("Jekyll 사이트")
    # 괄호가 없으면 `hugo.toml or (config.toml and content/)` 로 읽혀 두 줄이
    # 서로 다른 규칙으로 동작한다. `config.toml` 은 Hugo 전용이 아니므로
    # `content/` 를 함께 요구하고, `hugo.toml` 은 그 자체로 확정이다.
    if (root / "hugo.toml").is_file() or (
        (root / "config.toml").is_file() and (root / "content").is_dir()
    ):
        static_evidence.append("Hugo 사이트")
    for name in ("vite.config.ts", "vite.config.js", "vite.config.mjs"):
        if (root / name).is_file():
            static_evidence.append("Vite 설정")
            break

    # ── 부가 정보 (판정에는 쓰지 않고 근거로만 보여준다) ──────────────
    if any(root.glob("*.db")) or any(root.glob("*.sqlite")) or any(root.glob("*.sqlite3")) \
            or any(d.startswith("sqlite") or d == "aiosqlite" for d in py_deps):
        extra_evidence.append("SQLite 데이터 저장")

    # 중복 제거 — 순서는 유지한다(먼저 나온 근거가 더 중요하다).
    def _dedup(items: list[str]) -> list[str]:
        seen: set[str] = set()
        return [x for x in items if not (x in seen or seen.add(x))]

    server_evidence = _dedup(server_evidence)
    static_evidence = _dedup(static_evidence)
    extra_evidence = _dedup(extra_evidence)

    if server_evidence:
        evidence = server_evidence + extra_evidence
        # 두 신호가 같이 있으면 **그 사실을 숨기지 않는다.** 서버형을 고르는
        # 이유는 "서버가 정적 파일도 서빙할 수 있어서"이지 정적 신호가
        # 없어서가 아니다. 사용자가 반대로 고를 수도 있어야 한다(D5).
        if static_evidence:
            evidence = evidence + [
                "정적 빌드 신호도 있음: " + "·".join(static_evidence[:3])
            ]
        return {
            "app_kind": "server",
            "summary": f"서버형 앱 — {'·'.join(server_evidence[:4])}",
            "evidence": evidence,
            "recommended_target": "ecs",
        }

    if static_evidence:
        return {
            "app_kind": "static",
            "summary": f"정적 웹 앱 — {'·'.join(static_evidence[:4])}",
            "evidence": static_evidence + extra_evidence,
            "recommended_target": "s3",
        }

    return {
        "app_kind": "unknown",
        "summary": "프로젝트 유형을 확신하기 어려움",
        "evidence": extra_evidence or ["명확한 서버 또는 정적 빌드 설정을 찾지 못함"],
        "recommended_target": "local",
    }


def _detect_preflight_contract_stack(root: Path):
    """recoder.yml 이 없는 프로젝트용 최소 ContractStack 감지.

    배포 대상 감지와 정적 Preflight가 서로 다른 기준을 쓰지 않도록, 이 함수는
    FastAPI/Flask/Next/Express만 구분하고 그 외에는 CUSTOM으로 보수적으로 처리한다.
    """
    try:
        from schemas import ContractStack
    except ImportError:  # pragma: no cover - package 실행 호환
        from core.schemas import ContractStack  # type: ignore

    # **배포 대상 감지와 같은 판단 근거를 쓴다.**
    #
    # 예전에는 최상위 `*.py` 를 부분 문자열로 훑었다. 그러면 진입점이
    # `src/main.py` 인 흔한 배치에서 CUSTOM 으로 떨어지고, CUSTOM 의
    # 진입점 후보에는 `src/main.py` 가 없어서 **방금 앱을 찾아 놓고
    # `APP_ENTRYPOINT_NOT_FOUND` 로 막는** 앞뒤 안 맞는 결과가 나온다.
    # (`PYTHON_FASTAPI` 후보에는 `src/main.py` 가 들어 있다.)
    #
    # 이 함수의 docstring 이 원래부터 "서로 다른 기준을 쓰지 않도록"이라고
    # 못 박고 있었는데, 감지 쪽만 고치면서 그 약속이 깨졌다.
    node_deps, _pkg = _node_deps(root)
    if "next" in node_deps:
        return ContractStack.NODE_NEXT
    # **Express 일 때만 NODE_EXPRESS 다.**
    #
    # 예전엔 `package.json` 만 있으면 전부 NODE_EXPRESS 였다. 그런데 그 스택의
    # health 검사는 `app.get(...)`·`router.get(...)` 이라는 **Express 문법만**
    # 안다. Fastify(`fastify.get`)·NestJS(`@Get()` 데코레이터)·Koa·Hono 는
    # 멀쩡히 `/health` 를 정의해 놓아도 인식되지 않아 `MISSING_HEALTH_ENDPOINT`
    # 로 **막힌다** — 사용자가 고칠 것이 없는데 막히는 형태다.
    #
    # 그래서 확신할 수 있는 경우에만 NODE_EXPRESS 로 보내고, 나머지 Node 는
    # CUSTOM 으로 둔다. CUSTOM 의 health 검사는 차단이 아니라 **경고**다
    # ("직접 확인하세요"). 모르는 것을 아는 척해서 막는 것보다 낫다.
    if "express" in node_deps:
        return ContractStack.NODE_EXPRESS
    if node_deps or (root / "package.json").is_file():
        return ContractStack.CUSTOM

    python_names = _python_deps(root) | _python_imported_modules(root)
    if "fastapi" in python_names:
        return ContractStack.PYTHON_FASTAPI
    if "flask" in python_names:
        return ContractStack.PYTHON_FLASK
    return ContractStack.CUSTOM


def _run_deployment_safety_preflight(workspace_path: str, app_kind: str = "unknown") -> dict:
    """정적 Preflight와 기존 remediation 엔진을 배포 카드용 결과로 변환한다."""
    root = Path(workspace_path)
    if not root.is_dir():
        raise ValueError("유효한 워크스페이스 경로가 아닙니다.")

    try:
        from preflight import StaticPreflightRunner
        from preflight.static import CHECK_REGISTRY
        from preflight.contract_loader import build_default_contract, load_contract
        from remediation import generate_proposals
    except ImportError:  # pragma: no cover - package 실행 호환
        from core.preflight import StaticPreflightRunner  # type: ignore
        from core.preflight.static import CHECK_REGISTRY  # type: ignore
        from core.preflight.contract_loader import build_default_contract, load_contract  # type: ignore
        from core.remediation import generate_proposals  # type: ignore

    contract = load_contract(root)
    if contract is None:
        contract = build_default_contract(_detect_preflight_contract_stack(root))
    static_check_codes = None
    if app_kind == "static":
        static_check_codes = {
            code for code, _ in CHECK_REGISTRY
            if code.value in _STATIC_TARGET_INDEPENDENT_CHECK_CODES
        }
    run = StaticPreflightRunner(str(root), contract).run_sync(static_check_codes)
    proposals = generate_proposals(run, contract, root)
    workspace_root = root.resolve()
    for proposal in proposals:
        _deployment_remediation_proposals[proposal.proposal_id] = _StoredDeploymentRemediation(
            proposal=proposal,
            workspace_root=workspace_root,
        )

    proposal_by_code = {
        proposal.source_blocker_code.value: proposal
        for proposal in proposals
    }

    def issue_payload(issue) -> dict:
        code = issue.code.value if hasattr(issue.code, "value") else str(issue.code)
        proposal = proposal_by_code.get(code)
        # 이 제안은 .env가 아닌 .env.example만 만들어 실제 required_env
        # 검사 결과를 해소하지 못한다. 카드에서 자동 수정으로 보이면 "성공" 후
        # 재검사에서도 동일하게 막히므로, 작성 안내로만 표시한다.
        env_example_guidance = bool(
            code == "MISSING_REQUIRED_ENV"
            and proposal
            and getattr(proposal, "target_path", None) == ".env.example"
        )
        return {
            "code": code,
            "message": issue.message,
            "fix": issue.fix_hint or (proposal.summary if proposal else "수정 방법을 확인한 뒤 다시 검사하세요."),
            "severity": issue.severity.value if hasattr(issue.severity, "value") else str(issue.severity),
            "remediation_available": bool(
                proposal and proposal.auto_apply_available and not env_example_guidance
            ),
            "proposal_id": proposal.proposal_id if proposal else None,
        }

    reasons = [issue_payload(blocker) for blocker in run.blockers]
    warnings = [issue_payload(warning) for warning in run.warnings]
    return {
        "blocked": bool(run.blockers),
        "status": run.status.value if hasattr(run.status, "value") else str(run.status),
        "score": run.score,
        "reasons": reasons,
        # fixes 는 API 소비자가 설명과 해결책만 간단히 표시할 때 쓰는 호환 필드다.
        "fixes": [
            {"code": reason["code"], "message": reason["fix"], "proposal_id": reason["proposal_id"],
             "auto_apply_available": reason["remediation_available"]}
            for reason in reasons
        ],
        "warnings": warnings,
    }


def _build_deployment_decision_adr(workspace_path: str, target: str, evidence: list[str]) -> dict:
    """배포 대상 선택을 기존 ADR 형식으로 만들고, 확장이 기록할 파일 정보를 반환한다."""
    try:
        from adr import build_adr_ops, normalize_decisions
    except ImportError:
        from core.adr import build_adr_ops, normalize_decisions

    options = [
        {
            "key": "ecs",
            "label": "ECS 컨테이너",
            "summary": "서버형 앱을 컨테이너로 운영",
            "pros": ["서버 런타임 지원", "확장 가능한 운영 환경"],
            "cons": ["AWS 설정이 필요"],
        },
        {
            "key": "s3",
            "label": "S3 정적 호스팅",
            "summary": "빌드된 정적 파일을 제공",
            "pros": ["운영 비용과 구성이 단순"],
            "cons": ["서버 API를 직접 실행할 수 없음"],
        },
        {
            "key": "local",
            "label": "나중에 · 로컬 먼저",
            "summary": "로컬 Docker로 먼저 검증",
            "pros": ["원격 자격증명 없이 검증 가능"],
            "cons": ["외부 사용자에게 공개되지 않음"],
        },
    ]
    decision = normalize_decisions([{
        "id": "deployment-target",
        "question": "이 앱을 어디에 배포할까요?",
        "chosen_key": target,
        "options": options,
        "impact": "감지 근거: " + (", ".join(str(item) for item in evidence[:5]) or "감지 근거 없음"),
    }])
    ops = build_adr_ops(decision, "배포 대상 선택", Path(workspace_path))
    if not ops:
        raise RuntimeError("배포 대상 ADR을 만들 수 없습니다.")
    return ops[0]


@router.post("/api/deploy/preflight")
async def deploy_preflight(request: DeployPreflightRequest) -> dict:
    """배포 버튼 직후 앱 감지와 차단 검사 결과를 함께 반환한다.

    정적 Preflight는 디스크 검사와 보안 패턴 검색을 수행하므로 이벤트 루프 밖에서
    실행한다. 응답의 ``blocked/reasons/fixes`` 는 배포 차단 수정안 카드에 사용한다.
    """
    try:
        detected = await asyncio.to_thread(_deployment_preflight, request.workspace_path)
        safety = await asyncio.to_thread(
            _run_deployment_safety_preflight,
            request.workspace_path,
            detected["app_kind"],
        )
        return {**detected, **safety}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/deploy/remediations/{proposal_id}/apply")
async def apply_deployment_remediation(
    proposal_id: str,
    request: DeploymentRemediationApplyRequest,
) -> dict:
    """사용자가 명시적으로 누른 자동 수정만 안전하게 적용한다."""
    stored = _deployment_remediation_proposals.get(proposal_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="수정안을 찾을 수 없습니다. 다시 검사해 주세요.")
    workspace = Path(request.workspace_path).resolve()
    if not workspace.is_dir():
        raise HTTPException(status_code=400, detail="유효한 워크스페이스 경로가 아닙니다.")
    if workspace != stored.workspace_root:
        raise HTTPException(
            status_code=409,
            detail="이 수정안은 원래 검사한 워크스페이스에서만 적용할 수 있습니다. 다시 검사해 주세요.",
        )
    proposal = stored.proposal

    try:
        from remediation import apply_proposal
    except ImportError:  # pragma: no cover - package 실행 호환
        from core.remediation import apply_proposal  # type: ignore

    result = await asyncio.to_thread(apply_proposal, proposal, workspace)
    payload = {
        "success": result.success,
        "proposal_id": result.proposal_id,
        "applied_files": result.applied_files,
        "backup_dir": result.backup_dir,
        "message": result.error_message or result.skipped_reason or (
            "자동 수정을 적용했습니다. 다시 검사해 주세요." if result.success else "자동 수정에 실패했습니다."
        ),
        "rerun_required": True,
    }
    if not result.success:
        raise HTTPException(status_code=409, detail=payload["message"])
    return payload


@router.post("/api/deploy/decision")
async def record_deployment_decision(request: DeploymentDecisionRequest) -> dict:
    """선택한 배포 대상을 ADR로 기록할 데이터를 반환한다.

    Core는 워크스페이스에 직접 쓰지 않는다. 호출한 VS Code 확장이 반환된
    ``adr`` 파일을 기록하므로, 사용자가 누른 선택과 실제 파일 변경이 연결된다.
    """
    if not Path(request.workspace_path).is_dir():
        raise HTTPException(status_code=400, detail="유효한 워크스페이스 경로가 아닙니다.")
    adr = _build_deployment_decision_adr(
        request.workspace_path,
        request.target,
        request.evidence,
    )
    next_view = {"ecs": "ecs", "s3": "s3", "local": "docker"}[request.target]
    return {"target": request.target, "next_view": next_view, "adr": adr}


def _log_scan_to_session(scan_type: str, target: str, result: dict) -> None:
    """Best-effort persistence of scan outcome to SQLite session_logger.

    Failures are swallowed so the API path is never broken by logger problems.
    """
    try:
        from datetime import datetime, timezone
        from session_logger import get_session_logger  # type: ignore
        from schemas import SessionEvent  # type: ignore

        crit = int(result.get("critical_count", 0))
        high = int(result.get("high_count", 0))
        summary = result.get("summary") or f"{scan_type} scan on {target}"
        status = result.get("status", "unknown")

        event = SessionEvent(
            time=datetime.now(timezone.utc).isoformat(),
            event_type=f"security_scan.{scan_type}",
            error_summary=f"crit={crit} high={high}",
            error_fingerprint=f"{scan_type}:{target}",
            related_file_names=[target] if target else [],
            ai_suggestion_summary=str(summary)[:500],
            user_action="ignored",
            result="success" if status == "ok" else ("failed" if status == "error" else "pending"),
            validation="unknown",
        )
        get_session_logger().log_event("ship-stage", event)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).debug("session_logger unavailable for scan log: %s", exc)


def _normalise_scan_result(scan_type: str, target: str, raw: dict) -> dict:
    """Map an InfraAgent scan output dict into the canonical ScanResult shape.

    ScanResult = {status, scan_type, critical_count, high_count, medium_count,
                  findings[], summary, target}
    """
    if not raw.get("success", False):
        return {
            "status": "error",
            "scan_type": scan_type,
            "target": target,
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "findings": [],
            "summary": raw.get("error") or raw.get("summary") or "Scan failed.",
            "message": raw.get("error", "Scan failed."),
        }

    findings: list[dict] = []
    critical_count = 0
    high_count = 0
    medium_count = 0

    if scan_type == "trivy":
        critical = raw.get("critical", []) or []
        high = raw.get("high", []) or []
        critical_count = len(critical)
        high_count = len(high)
        for entry in critical:
            findings.append({"severity": "CRITICAL", **entry})
        for entry in high:
            findings.append({"severity": "HIGH", **entry})
    elif scan_type == "hadolint":
        for v in raw.get("violations", []) or []:
            level = str(v.get("level", "")).lower()
            if level == "error":
                critical_count += 1
                severity = "CRITICAL"
            elif level == "warning":
                high_count += 1
                severity = "HIGH"
            else:
                medium_count += 1
                severity = "MEDIUM"
            findings.append({"severity": severity, **v})
    elif scan_type == "gitleaks":
        # Every leaked secret is treated as CRITICAL.
        for f in raw.get("findings", []) or []:
            critical_count += 1
            findings.append({"severity": "CRITICAL", **f})

    return {
        "status": "ok",
        "scan_type": scan_type,
        "target": target,
        "critical_count": critical_count,
        "high_count": high_count,
        "medium_count": medium_count,
        "findings": findings,
        "summary": raw.get("summary") or "",
    }


async def _execute_scan(scan_type: str, workspace_path: str, target_path: Optional[str]) -> dict:
    """Dispatch to InfraAgent.run_{trivy,hadolint,gitleaks}_scan and normalise output.

    Dispatch rules per scan_type:
      - trivy:    target = target_path (image name, e.g. "myapp:latest")
                  — if missing, falls back to "<basename(workspace)>:latest".
      - hadolint: target = target_path (Dockerfile path)
                  — if missing, falls back to "<workspace>/Dockerfile".
      - gitleaks: target = workspace_path (the repo root).
    """
    if scan_type not in {"trivy", "hadolint", "gitleaks"}:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scan type: '{scan_type}'. Use trivy, hadolint, or gitleaks.",
        )

    agent = _get_infra_agent()
    if agent is None:
        return {
            "status": "error",
            "scan_type": scan_type,
            "target": target_path or workspace_path,
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "findings": [],
            "summary": "InfraAgent unavailable (dependencies missing).",
            "message": "InfraAgent unavailable on this host.",
        }

    ws = Path(workspace_path) if workspace_path else None

    try:
        if scan_type == "trivy":
            image = target_path
            if not image:
                if ws is not None and ws.exists():
                    image = f"{ws.name.lower().replace(' ', '-') or 'app'}:latest"
                else:
                    image = "app:latest"
            raw = await asyncio.wait_for(agent.run_trivy_scan(image), timeout=300)
            target_for_log = image
        elif scan_type == "hadolint":
            dockerfile = target_path
            if not dockerfile and ws is not None:
                dockerfile = str(ws / "Dockerfile")
            if not dockerfile:
                raise HTTPException(
                    status_code=400,
                    detail="hadolint scan requires either target_path or workspace_path.",
                )
            raw = await asyncio.wait_for(agent.run_hadolint_scan(dockerfile), timeout=300)
            target_for_log = dockerfile
        else:  # gitleaks
            scan_root = workspace_path or target_path
            if not scan_root:
                raise HTTPException(
                    status_code=400,
                    detail="gitleaks scan requires workspace_path.",
                )
            raw = await asyncio.wait_for(agent.run_gitleaks_scan(scan_root), timeout=300)
            target_for_log = scan_root
    except asyncio.TimeoutError:
        result = {
            "status": "error",
            "scan_type": scan_type,
            "target": target_path or workspace_path,
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "findings": [],
            "summary": f"Scan '{scan_type}' exceeded 300s timeout.",
            "message": "timeout",
        }
        _log_scan_to_session(scan_type, target_path or workspace_path or "", result)
        return result
    except FileNotFoundError:
        result = {
            "status": "error",
            "scan_type": scan_type,
            "target": target_path or workspace_path,
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "findings": [],
            "summary": "Docker is not available on this host. Start Docker Desktop and retry.",
            "message": "docker_not_found",
        }
        _log_scan_to_session(scan_type, target_path or workspace_path or "", result)
        return result
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        result = {
            "status": "error",
            "scan_type": scan_type,
            "target": target_path or workspace_path,
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "findings": [],
            "summary": f"Scan failed: {exc}",
            "message": str(exc),
        }
        _log_scan_to_session(scan_type, target_path or workspace_path or "", result)
        return result

    normalised = _normalise_scan_result(scan_type, target_for_log, raw)
    _log_scan_to_session(scan_type, target_for_log, normalised)
    return normalised


def _write_proposal_to_workspace(proposal, workspace_override, proposal_id):
    """Proposal 파일을 워크스페이스 루트 기준으로 디스크에 쓴다.

    상대 target_path 면 (override -> proposal.workspace_path -> cwd) 순으로 루트 결정.
    과거 버그: 루트를 항상 Path.cwd()(=Core 실행 디렉토리 core/)로 잡아
    .github/workflows/deploy.yml 이 사용자 프로젝트가 아니라 core/ 에 써졌다.
    """
    target = Path(proposal.target_path)
    if not target.is_absolute():
        root = (
            workspace_override
            or getattr(proposal, "workspace_path", None)
            or str(Path.cwd())
        )
        target = Path(root).expanduser().resolve() / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(proposal.content, encoding="utf-8")
    file_type = getattr(proposal.file_type, "value", proposal.file_type)
    return {
        "status": "saved",
        "proposal_id": proposal_id,
        "path": str(target),
        "file_type": file_type,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


#: LLM 이 죽어도 초안은 나와야 한다 — 템플릿만으로 만드는 Dockerfile.
#:
#: 예전에는 이 경로가 `agent is None`(의존성 누락) 일 때만 쓰였다. 그런데
#: 실제로 사람을 막은 건 "에이전트가 없는 것"이 아니라 **에이전트는 있는데
#: LLM 호출이 실패하는 것**이었다(자격증명 만료·rate limit·네트워크). 그때는
#: 예외가 라우트를 그대로 뚫고 나가 Starlette 이 평문 `Internal Server Error`
#: 를 반환했고, 사용자에게는 원인도 다음 행동도 없는 빨간 배너만 남았다.
#: 그래서 두 경우 모두 이 폴백을 쓴다.
_DOCKERFILE_TEMPLATE_BY_STACK = {
    StackType.PYTHON_FASTAPI: "Dockerfile.python-fastapi",
    StackType.PYTHON_FLASK: "Dockerfile.python-flask",
    StackType.PYTHON_DJANGO: "Dockerfile.python-flask",
    StackType.NODE_EXPRESS: "Dockerfile.node-express",
    StackType.NODE_NEXT: "Dockerfile.node-next",
    StackType.NODE_NEST: "Dockerfile.node-express",
}

_UNRESOLVED_FILE_TEMPLATE_RE = re.compile(r"\{\{[^{}\r\n]+\}\}")
_SAFE_NODE_ENTRYPOINT_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_SAFE_PYTHON_TARGET_RE = re.compile(
    r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$"
)


class _UnsupportedDockerfileFallback(ValueError):
    """AI 없이 검증된 Dockerfile을 만들 수 없는 스택."""


class _DockerfileTemplateRenderError(RuntimeError):
    """지원 스택의 로컬 템플릿이 완전한 Dockerfile을 만들지 못한 경우."""


def _safe_node_entrypoint(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().strip("\"'").replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if (
        not candidate
        or not _SAFE_NODE_ENTRYPOINT_RE.fullmatch(candidate)
        or candidate.startswith("/")
        or ".." in candidate.split("/")
        or Path(candidate).suffix.lower() not in {".js", ".cjs", ".mjs"}
    ):
        return None
    return candidate


def _entrypoint_from_node_command(command: object) -> str | None:
    if not isinstance(command, str):
        return None
    match = re.search(
        r"(?:^|\s)(?:node|nodemon)\s+"
        r"(?:--[A-Za-z0-9_-]+(?:=[^\s]+)?\s+)*"
        r"[\"']?([^\"'\s;&|]+)",
        command,
    )
    return _safe_node_entrypoint(match.group(1)) if match else None


def _discover_node_entrypoint(
    workspace_path: str,
    stack: StackType,
    project: object | None,
) -> str:
    root = Path(workspace_path).expanduser().resolve()
    package: dict = {}
    try:
        loaded = json.loads((root / "package.json").read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            package = loaded
    except (OSError, ValueError):
        pass

    candidates: list[object] = []
    scripts = package.get("scripts")
    if isinstance(scripts, dict):
        candidates.extend(
            _entrypoint_from_node_command(scripts.get(name))
            for name in ("start:prod", "start")
        )
    # `scripts.start`는 실제 서버 실행 계약이고 `main`은 라이브러리 export일
    # 수도 있으므로 start 명령을 우선한다.
    candidates.append(package.get("main"))

    candidates.extend(
        path for path in (
            "index.js", "server.js", "app.js",
            "src/index.js", "src/server.js", "src/app.js",
        )
        if (root / path).is_file()
    )
    candidates.append(
        _entrypoint_from_node_command(
            getattr(project, "default_run_command", None),
        )
    )
    candidates.append(
        "dist/main.js" if stack == StackType.NODE_NEST else "index.js"
    )

    for value in candidates:
        entrypoint = _safe_node_entrypoint(value)
        if entrypoint:
            return entrypoint
    return "index.js"


def _python_module_for_path(root: Path, path: Path) -> str | None:
    try:
        parts = list(path.relative_to(root).with_suffix("").parts)
    except ValueError:
        return None
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _python_source_candidates(root: Path) -> list[Path]:
    preferred = [
        root / relative for relative in (
            "main.py", "app.py", "src/main.py", "src/app.py", "app/main.py",
        )
    ]
    seen = {path for path in preferred}
    discovered: list[Path] = []
    try:
        paths = sorted(root.rglob("*.py"))
    except OSError:
        paths = []
    for path in paths:
        try:
            relative_parts = path.relative_to(root).parts[:-1]
        except ValueError:
            continue
        if any(part in _SKIP_DIRS or part.startswith(".") for part in relative_parts):
            continue
        if path not in seen:
            discovered.append(path)
    return [path for path in preferred if path.is_file()] + discovered


def _discover_python_target(
    workspace_path: str,
    stack: StackType,
    project: object | None,
) -> str:
    root = Path(workspace_path).expanduser().resolve()
    if stack == StackType.PYTHON_DJANGO:
        for path in _python_source_candidates(root):
            if path.name != "wsgi.py":
                continue
            module = _python_module_for_path(root, path)
            if module:
                return f"{module}:application"
        default_target = "config.wsgi:application"
    else:
        factory = "FastAPI" if stack == StackType.PYTHON_FASTAPI else "Flask"
        factory_re = re.compile(
            rf"^[ \t]*(?P<name>[A-Za-z_]\w*)[ \t]*(?::[^=\n]+)?="
            rf"[ \t]*(?:[A-Za-z_]\w*\.)?{factory}[ \t]*\(",
            re.MULTILINE,
        )
        for path in _python_source_candidates(root):
            try:
                source = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            match = factory_re.search(source)
            module = _python_module_for_path(root, path)
            if match and module:
                return f"{module}:{match.group('name')}"
        default_target = "main:app" if stack == StackType.PYTHON_FASTAPI else "app:app"

    run_command = getattr(project, "default_run_command", None)
    if isinstance(run_command, str):
        match = re.search(
            r"(?:uvicorn|hypercorn|gunicorn)\s+"
            r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*)",
            run_command,
        )
        if match and _SAFE_PYTHON_TARGET_RE.fullmatch(match.group(1)):
            return match.group(1)
    return default_target


def _discover_health_path(
    workspace_path: str,
    stack: StackType,
    project: object | None = None,
) -> str:
    """Dockerfile HEALTHCHECK 가 찌를 경로.

    **판단은 여기서 하지 않는다.** `infra_agent.discover_health_path` 한 곳에
    모아 뒀다. 예전에는 compose 쪽과 여기에 각각 구현이 있었고 둘이 서로
    달라서, 같은 Next.js 프로젝트에서 docker-compose.yml 과 Dockerfile 이
    **다른 경로**를 찔렀다. 헬스체크가 틀리면 예외가 아니라 "영원히
    unhealthy" 로 나타나므로 갈라진 채로는 아무도 눈치채지 못한다.
    """
    try:
        from infra_agent import discover_health_path  # type: ignore
    except ImportError:  # pragma: no cover - 저장소 루트에서 실행할 때
        from core.infra_agent import discover_health_path  # type: ignore

    configured = getattr(project, "health_check_path", None)
    return discover_health_path(workspace_path, stack.value, configured)


def _dockerfile_template_defaults(
    workspace_path: str,
    stack: StackType,
    project: object | None = None,
) -> dict[str, str]:
    """Return safe, complete values for every registered Dockerfile marker."""
    raw_name = Path(workspace_path).expanduser().resolve().name
    app_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_name).strip("-.")[:63] or "app"
    node_stacks = {
        StackType.NODE_EXPRESS,
        StackType.NODE_NEXT,
        StackType.NODE_NEST,
    }
    python_stacks = {
        StackType.PYTHON_FASTAPI,
        StackType.PYTHON_FLASK,
        StackType.PYTHON_DJANGO,
    }
    default_ports = {
        StackType.PYTHON_FASTAPI: 8000,
        StackType.PYTHON_FLASK: 5000,
        StackType.PYTHON_DJANGO: 8000,
        StackType.NODE_EXPRESS: 3000,
        StackType.NODE_NEXT: 3000,
        StackType.NODE_NEST: 3000,
    }
    detected_port = getattr(project, "default_port", None)
    port = (
        detected_port
        if isinstance(detected_port, int) and not isinstance(detected_port, bool)
        else default_ports.get(stack, 8000)
    )
    health_path = _discover_health_path(workspace_path, stack, project)
    return {
        "APP_NAME": app_name,
        "PYTHON_VERSION": "3.11",
        "NODE_VERSION": "20",
        "PORT": str(port),
        "HEALTH_CHECK_PATH": str(health_path),
        "APP_TARGET": (
            _discover_python_target(workspace_path, stack, project)
            if stack in python_stacks else "main:app"
        ),
        "START_SCRIPT": (
            _discover_node_entrypoint(workspace_path, stack, project)
            if stack in node_stacks else "index.js"
        ),
    }


def _dockerfile_from_template(
    workspace_path: str,
    stack: StackType,
    project: object | None = None,
) -> tuple[str, str]:
    """검증된 로컬 템플릿으로 완전한 Dockerfile을 렌더한다.

    실행 명령을 모르는 스택이나 손상된 템플릿을 `sleep` 컨테이너로
    위장하지 않는다. 호출자는 명시적 오류로 사용자에게 알려야 한다.
    """
    from registry import FileTemplateRegistry  # type: ignore

    template_id = _DOCKERFILE_TEMPLATE_BY_STACK.get(stack)
    if template_id is None:
        raise _UnsupportedDockerfileFallback(
            f"{stack.value} 스택은 AI 없이 검증된 Dockerfile 폴백을 제공하지 "
            "않습니다. AI Ready를 복구하거나 프로젝트에 Dockerfile을 직접 "
            "추가한 뒤 다시 시도하세요."
        )

    try:
        content = FileTemplateRegistry().render(
            template_id,
            _dockerfile_template_defaults(workspace_path, stack, project),
        )
        unresolved = _UNRESOLVED_FILE_TEMPLATE_RE.search(content)
        if unresolved:
            raise ValueError(
                f"unresolved file-template marker: {unresolved.group(0)}"
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("Dockerfile 템플릿 렌더 실패: %s", exc)
        raise _DockerfileTemplateRenderError(
            f"{stack.value} 기본 Dockerfile 템플릿을 완전하게 렌더하지 못했습니다."
        ) from exc
    return content, template_id


#: 사용자에게 그대로 보여줄 문장. 원인 + **다음에 뭘 하면 되는지**까지 담는다.
#: "Internal Server Error" 만 보여주면 사용자는 재시도 말고 할 수 있는 게 없다.
def _public_ai_failure_reason(exc: Exception | None) -> str:
    """Return an actionable reason without exposing provider error details."""
    if exc is None:
        return "AI 에이전트를 초기화하지 못했습니다."

    message = str(exc).lower()
    if any(token in message for token in ("rate limit", "throttl", "quota")):
        return "AI 제공자의 요청 한도에 도달했습니다."
    if any(token in message for token in (
        "credential", "api key", "api_key", "unauthorized", "forbidden", "auth",
    )):
        return "AI 인증 정보 또는 자격증명을 확인하지 못했습니다."
    if any(token in message for token in ("timeout", "timed out")):
        return "AI 제공자의 응답 시간이 초과됐습니다."
    if any(token in message for token in ("connection", "network", "dns")):
        return "AI 제공자와 네트워크 연결에 실패했습니다."
    return f"AI 제공자 호출에 실패했습니다 ({exc.__class__.__name__})."


def _ai_unavailable_note(exc: Exception | None) -> str:
    reason = _public_ai_failure_reason(exc)
    return (
        f"AI 맞춤 생성을 건너뛰고 기본 템플릿으로 초안을 만들었습니다. "
        f"원인: {reason} — AI 연결(자격증명·API 키)을 확인한 뒤 다시 생성하면 "
        f"프로젝트에 맞춰 다듬어집니다. 지금 초안 그대로 사용해도 됩니다."
    )


@router.post("/api/deploy/dockerfile")
async def generate_dockerfile(request: DockerfileRequest) -> InfraFileProposal:
    """
    Generate a Dockerfile proposal for the workspace.

    Auto-detects the stack if not specified, then delegates to the
    InfraAgent. **AI 호출이 실패해도 500 을 내지 않고** 템플릿 초안으로
    폴백하며, 왜 AI 를 못 썼는지를 risk_reasons 에 담아 사용자에게 알린다.
    """
    stack = request.stack or _detect_stack(request.workspace_path)

    proposal = None
    project = None
    ai_note = ""
    agent = _get_infra_agent()
    if agent is not None:
        # InfraAgent.generate_dockerfile(workspace_path, project: ProjectProfile)
        # 시그니처에 맞춰 ProjectProfile 을 구성. 사용자가 /api/project/scan 을 안 했어도
        # 최소한의 정보로 호출 가능하도록 inline 구성.
        try:
            from project_scanner import get_project_scanner  # type: ignore
            project = get_project_scanner().scan(request.workspace_path)
            # 호출자가 스택을 명시했으면 파일 휴리스틱보다 우선한다. 서로
            # 다른 스택에서 계산된 포트/실행 명령까지 가져오면 FastAPI에
            # Express의 3000/index.js를 적용하는 식의 교차 오염이 생긴다.
            if request.stack is not None and project.stack != stack:
                project = project.model_copy(update={
                    "stack": stack,
                    "default_port": None,
                    "default_run_command": None,
                })
        except Exception:
            # 폴백 — 빈 ProjectProfile (필수 필드만)
            from schemas import ProjectProfile  # type: ignore
            project = ProjectProfile(
                workspace_path=request.workspace_path,
                stack=stack,
            )
        generate = agent.generate_dockerfile
        try:
            # 호출 뒤 발생한 TypeError를 "구버전 시그니처"로 오인해 유료 LLM
            # 호출을 두 번 실행하지 않는다. 호출 전에 시그니처를 판정한다.
            try:
                signature = inspect.signature(generate)
            except (TypeError, ValueError):
                # 서명을 읽을 수 없는 callable은 현재(2인자) 계약을 따른다.
                args = (request.workspace_path, project)
            else:
                try:
                    signature.bind(request.workspace_path, project)
                    args = (request.workspace_path, project)
                except TypeError:
                    args = (request.workspace_path,)
            proposal = await generate(*args)
        except Exception as exc:  # noqa: BLE001
            # **여기가 데모에서 터진 지점.** LLM 제공자가 RuntimeError 를 던지면
            # 그대로 빠져나가지 않고 템플릿으로 폴백한다.
            logger.warning("Dockerfile AI 생성 실패, 템플릿 폴백: %s", exc)
            proposal, ai_note = None, _ai_unavailable_note(exc)
    else:
        ai_note = _ai_unavailable_note(None)

    # LLM이 일부 값만 반환하면 Registry가 나머지 {{TOKEN}}을 그대로 둔다.
    # AI 경로도 승인 가능한 완성본만 통과시키고, 불완전하면 검증된 폴백으로
    # 내린다.
    if proposal is not None:
        unresolved = _UNRESOLVED_FILE_TEMPLATE_RE.search(proposal.content)
        if unresolved:
            logger.warning(
                "Dockerfile AI 결과에 미치환 토큰이 있어 템플릿 폴백: %s",
                unresolved.group(0),
            )
            proposal = None
            ai_note = (
                "AI 맞춤 생성 결과에 채워지지 않은 템플릿 값이 있어 기본 "
                "템플릿으로 다시 만들었습니다. 내용을 검토한 뒤 저장해 주세요."
            )

    if proposal is None:
        try:
            content, template_id = _dockerfile_from_template(
                request.workspace_path, stack, project,
            )
        except _UnsupportedDockerfileFallback as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except _DockerfileTemplateRenderError as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "검증된 기본 Dockerfile 템플릿을 생성하지 못했습니다. "
                    "ReCoder 템플릿 설치 상태를 확인한 뒤 다시 시도하세요."
                ),
            ) from exc
        proposal = InfraFileProposal(
            file_type=FileType.DOCKERFILE,
            target_path="Dockerfile",
            content=content,
            base_template=template_id,
            risk_level=RiskLevel.LOW,
            approval_level=ApprovalLevel.CONFIRM,
            risk_reasons=[ai_note] if ai_note else [],
        )

    if not getattr(proposal, "workspace_path", None):
        proposal = proposal.model_copy(update={
            "workspace_path": str(Path(request.workspace_path).expanduser().resolve()),
        })
    _infra_proposals[proposal.proposal_id] = proposal
    return proposal


class ComposeRequest(BaseModel):
    workspace_path: str
    project_id: Optional[str] = None


@router.post("/api/deploy/compose")
async def generate_compose_route(request: ComposeRequest) -> InfraFileProposal:
    """
    docker-compose.yml 초안 생성.

    이 라우트는 **없었다.** 확장의 「인프라 파일 생성」에는 Dockerfile ·
    Compose · GitHub Actions 세 탭이 있는데 Compose 만 대응 엔드포인트가
    없어서 탭이 동작할 수 없었다(호출하면 404). infra_agent 에 생성기는
    이미 있었으므로 라우트만 붙인다.
    """
    ws_path = (request.workspace_path or "").strip()
    if not ws_path:
        raise HTTPException(status_code=400, detail="workspace_path 가 비어있습니다.")
    if not Path(ws_path).expanduser().resolve().exists():
        raise HTTPException(status_code=404, detail=f"워크스페이스 경로가 없습니다: {ws_path}")

    try:
        from infra_agent import generate_docker_compose  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"infra_agent.generate_docker_compose import 실패: {exc}",
        ) from exc

    # 스캔은 실패해도 진행한다 — project=None 이면 생성기가 자체 폴백을 쓴다.
    try:
        from project_scanner import get_project_scanner  # type: ignore
        project = await asyncio.to_thread(get_project_scanner().scan, ws_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("프로젝트 스캔 실패, compose 폴백: %s", exc)
        project = None

    try:
        proposal = await asyncio.to_thread(generate_docker_compose, project, ws_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=(
                f"docker-compose.yml 생성 실패: {exc} — 워크스페이스에 인식 가능한 "
                f"프로젝트 파일(package.json·requirements.txt 등)이 있는지 확인하세요."
            ),
        ) from exc

    abs_ws = str(Path(ws_path).expanduser().resolve())
    update = {"workspace_path": abs_ws}
    if proposal.file_type != FileType.DOCKER_COMPOSE:
        update["file_type"] = FileType.DOCKER_COMPOSE
    proposal = proposal.model_copy(update=update)

    _infra_proposals[proposal.proposal_id] = proposal
    return proposal


@router.post("/api/deploy/dockerfile/approve")
async def approve_dockerfile(proposal_id: str, approved: bool, workspace_path: str = "") -> dict:
    """Approve or reject a Dockerfile / infra file proposal.

    워크스페이스 루트 기준으로 쓴다 (cwd 폴백 버그 동일 수정).
    """
    proposal = _infra_proposals.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Proposal '{proposal_id}' not found.")

    if not approved:
        file_type = getattr(proposal.file_type, "value", proposal.file_type)
        del _infra_proposals[proposal_id]
        return {
            "status": "rejected",
            "proposal_id": proposal_id,
            "file_type": file_type,
        }

    result = _write_proposal_to_workspace(proposal, workspace_path, proposal_id)
    del _infra_proposals[proposal_id]
    return result


# ---------------------------------------------------------------------------
# GitHub Actions Workflow generation (설계 §4.1.2 Ship Stage 확장)
# ---------------------------------------------------------------------------


class GithubActionsRequest(BaseModel):
    workspace_path: str
    project_id: Optional[str] = None
    extra_context: Optional[str] = None


@router.post("/api/deploy/github-actions")
async def generate_github_actions_route(request: GithubActionsRequest) -> InfraFileProposal:
    """
    Generate a GitHub Actions workflow YAML for the workspace.

    Returns an InfraFileProposal (file_type=GITHUB_ACTIONS) targeting
    `.github/workflows/deploy.yml`. The proposal is held in-memory until
    the user approves via `/api/deploy/github-actions/approve`.

    설계 A.4 InfraFileProposal — file_type "github-actions" 케이스.
    """
    # Lazy import: project_scanner / infra_agent 둘 다 server.py 전역에 의존하지 않도록
    try:
        from project_scanner import get_project_scanner  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"project_scanner import 실패: {e}") from e

    try:
        from infra_agent import generate_github_actions  # type: ignore
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"infra_agent.generate_github_actions import 실패: {e}",
        ) from e

    # 1. 워크스페이스 스캔 → ProjectProfile
    ws_path = request.workspace_path
    if not ws_path:
        raise HTTPException(status_code=400, detail="workspace_path 가 비어있습니다.")
    if not Path(ws_path).expanduser().resolve().exists():
        raise HTTPException(status_code=404, detail=f"워크스페이스 경로가 없습니다: {ws_path}")

    try:
        scanner = get_project_scanner()
        project = await asyncio.to_thread(scanner.scan, ws_path)
    except Exception as e:
        # 스캔 실패(스택 미감지/검증 오류 등)해도 워크플로 생성은 계속한다.
        # generate_github_actions 가 project=None 이면 자체 폴백(generic CI)으로 생성.
        import logging as _logging
        _logging.getLogger(__name__).warning("프로젝트 스캔 실패, generic 폴백: %s", e)
        project = None

    # 2. 워크플로우 생성 (FileTemplate Registry 기반)
    try:
        proposal = await asyncio.to_thread(generate_github_actions, project, ws_path)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"GitHub Actions 워크플로우 생성 실패: {e}",
        ) from e

    # 3. file_type 보정 + workspace_path 절대경로 박기.
    #    approve 단계가 이 경로 기준으로 .github/workflows/deploy.yml 을 쓰지 않으면
    #    Core 의 cwd(=core/)에 써지는 버그가 있었다.
    abs_ws = str(Path(ws_path).expanduser().resolve())
    update = {"workspace_path": abs_ws}
    if proposal.file_type != FileType.GITHUB_ACTIONS:
        update["file_type"] = FileType.GITHUB_ACTIONS
    proposal = proposal.model_copy(update=update)

    _infra_proposals[proposal.proposal_id] = proposal
    return proposal


@router.post("/api/deploy/github-actions/approve")
async def approve_github_actions(proposal_id: str, approved: bool, workspace_path: str = "") -> dict:
    """
    Approve or reject a GitHub Actions workflow proposal.

    On approve, writes the YAML to `<workspace>/.github/workflows/deploy.yml`.
    파일 위치 우선순위(상대 target_path): 쿼리 workspace_path -> proposal.workspace_path
    -> (폴백) cwd. 1·2 가 항상 채워지므로 더는 core/ 에 잘못 써지지 않는다.
    """
    proposal = _infra_proposals.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Proposal '{proposal_id}' not found.")

    if not approved:
        del _infra_proposals[proposal_id]
        return {"status": "rejected", "proposal_id": proposal_id}

    result = _write_proposal_to_workspace(proposal, workspace_path, proposal_id)
    del _infra_proposals[proposal_id]
    return result


@router.post("/api/deploy/scan")
async def run_scan(request: ScanRequest) -> dict:
    """
    Run a one-shot security scan via the InfraAgent.

    Supported scan_type values: ``trivy``, ``hadolint``, ``gitleaks``.

    Returns a normalised ScanResult dict:
      {
        status: "ok" | "error",
        scan_type: str,
        target: str,
        critical_count: int,
        high_count: int,
        medium_count: int,
        findings: list[dict],
        summary: str,
      }

    Docker not running, missing dependencies, and timeouts return
    ``{status: "error", message: "..."}``. Only invalid ``scan_type`` raises
    HTTP 400.
    """
    return await _execute_scan(
        request.scan_type, request.workspace_path, request.target_path
    )


async def _run_pre_deploy_security_gate(request: DeployPlanRequest) -> dict:
    """Run Trivy (filesystem/image) + Hadolint (Dockerfile) before planning.

    Returns a dict with:
      - blockers:    list[str]  — human-readable critical findings
      - risk_reasons:list[str]  — additional non-critical reasons
      - elevated:    bool       — True if any critical was found
      - reports:     dict       — raw normalised scan reports per scanner
    """
    blockers: list[str] = []
    risk_reasons: list[str] = []
    reports: dict = {}

    workspace = request.workspace_path
    ws_path = Path(workspace) if workspace else None
    dockerfile_path = None
    if ws_path is not None and (ws_path / "Dockerfile").exists():
        dockerfile_path = str(ws_path / "Dockerfile")

    # Hadolint — only meaningful if a Dockerfile exists.
    if dockerfile_path is not None:
        hadolint_report = await _execute_scan("hadolint", workspace, dockerfile_path)
        reports["hadolint"] = hadolint_report
        if hadolint_report.get("status") == "ok":
            crit = int(hadolint_report.get("critical_count", 0))
            if crit > 0:
                blockers.append(f"Hadolint: {crit} Dockerfile error(s)")
            high = int(hadolint_report.get("high_count", 0))
            if high > 0:
                risk_reasons.append(f"Hadolint: {high} warning(s)")

    # Trivy — only against an explicitly-provided image name.
    if request.image:
        trivy_report = await _execute_scan("trivy", workspace, request.image)
        reports["trivy"] = trivy_report
        if trivy_report.get("status") == "ok":
            crit = int(trivy_report.get("critical_count", 0))
            if crit > 0:
                blockers.append(f"Trivy: {crit} CRITICAL CVE(s) in {request.image}")
            high = int(trivy_report.get("high_count", 0))
            if high > 0:
                risk_reasons.append(f"Trivy: {high} HIGH CVE(s) in {request.image}")

    return {
        "blockers": blockers,
        "risk_reasons": risk_reasons,
        "elevated": bool(blockers),
        "reports": reports,
    }


@router.post("/api/deploy/plan")
async def create_deployment_plan(request: DeployPlanRequest) -> DeploymentPlan:
    """Generate an executable DeploymentPlan via the DeployAgent.

    Ship Stage security gate: unless ``skip_security_scan=true``, Trivy and
    Hadolint are run before the plan is built. If any CRITICAL findings are
    detected, the resulting plan is annotated with ``risk_level=HIGH``,
    ``approval_level=DOUBLE_CONFIRM``, and the blockers are embedded into
    ``risk_reasons`` (prefixed with ``BLOCKER:``).
    """
    gate: dict = {"blockers": [], "risk_reasons": [], "elevated": False, "reports": {}}
    if not request.skip_security_scan:
        try:
            gate = await _run_pre_deploy_security_gate(request)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("Pre-deploy security gate failed: %s", exc)
            gate = {
                "blockers": [],
                "risk_reasons": [f"Security gate did not run: {exc}"],
                "elevated": False,
                "reports": {},
            }

    deploy_agent = _get_deploy_agent()
    if deploy_agent is not None:
        plan = await deploy_agent.create_plan(request)
    else:
        # Placeholder plan
        plan = DeploymentPlan(
            method=request.method,
            action=__import__("schemas", fromlist=["ActionType"]).ActionType.DOCKER_RUN,
            image=request.image or "app:latest",
            container_name=request.container_name or "app",
            ports={str(request.host_port or 8080): str(request.container_port or 8080)},
            health_check_path="/health",
            risk_level=RiskLevel.MEDIUM,
            approval_level=ApprovalLevel.CONFIRM,
        )

    # 화면에 보여 줄 현재 롤백 후보를 계산한다. 이 값은 승인 대기 중 오래될 수
    # 있으므로 execute_deployment 에서 반드시 한 번 더 새로 계산한다.
    _refresh_rollback_target(plan)

    # Apply the security-gate verdict onto the plan.
    extra_reasons: list[str] = []
    extra_reasons.extend(f"BLOCKER: {b}" for b in gate["blockers"])
    extra_reasons.extend(gate["risk_reasons"])
    if extra_reasons:
        plan.risk_reasons = list(plan.risk_reasons) + extra_reasons
    if gate["elevated"]:
        plan.risk_level = RiskLevel.HIGH
        plan.approval_level = ApprovalLevel.DOUBLE_CONFIRM

    _deployment_plans[plan.plan_id] = plan
    return plan


@router.post("/api/deploy/execute")
async def execute_deployment(request: ExecuteRequest) -> dict:
    """
    Execute an approved DeploymentPlan.

    Uses the CommandTemplateRegistry to build shell commands and runs them.
    """
    plan = _deployment_plans.get(request.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan '{request.plan_id}' not found.")

    if not request.approved:
        del _deployment_plans[request.plan_id]
        return {"status": "cancelled", "plan_id": request.plan_id}

    # 플랜 생성 뒤 다른 배포가 실행될 수 있으므로, record 에 저장할 롤백 대상은
    # 반드시 실행 시점의 마지막 *검증 완료* 배포로 다시 잡는다.
    rollback_target, rollback_reason = _refresh_rollback_target(plan)
    rollback_source = _rollback_source_for(plan.container_name or "", plan.image or "")

    # ── 보안: image / container_name 화이트리스트 검증 (shell injection 차단) ──
    # docker 이미지 이름 문법: [registry/][namespace/]name[:tag][@digest]
    # 컨테이너 이름: [a-zA-Z0-9][a-zA-Z0-9_.-]+
    import re as _re
    _IMG_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/\-@]{0,254}$")
    _NAME_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")
    if plan.image and not _IMG_RE.match(plan.image):
        raise HTTPException(status_code=400, detail="Invalid image name (forbidden characters).")
    if plan.container_name and not _NAME_RE.match(plan.container_name):
        raise HTTPException(status_code=400, detail="Invalid container name (forbidden characters).")
    for _hp, _cp in plan.ports.items():
        if not str(_hp).isdigit() or not str(_cp).isdigit():
            raise HTTPException(status_code=400, detail="Port must be numeric.")

    if plan.command_template_id:
        # Template-based path: registry 가 list-form 을 반환하도록 요구하고,
        # str 반환 시 shlex.split 으로 안전하게 토큰화한다 (shell=False 보장).
        from registry import CommandTemplateRegistry  # type: ignore
        import shlex as _shlex
        reg = CommandTemplateRegistry()
        try:
            first_hp, first_cp = next(iter(plan.ports.items()), ("8080", "8080"))
            built = reg.build_command(plan.command_template_id, {
                "image_name": plan.image or "",
                "container_name": plan.container_name or "",
                "host_port": int(first_hp),
                "container_port": int(first_cp),
            })
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Command build failed: {exc}") from exc
        if isinstance(built, list):
            cmd_args = [str(x) for x in built]
        else:
            # str 반환은 deprecated — 시연 호환을 위해 shlex 로 토큰화 (shell 호출 없음)
            cmd_args = _shlex.split(str(built))
    else:
        # 안전한 args list 직접 조립 (shell=False 강제)
        cmd_args: list[str] = ["docker", "run", "-d", "--name", str(plan.container_name)]
        for _hp, _cp in plan.ports.items():
            cmd_args.extend(["-p", f"{int(_hp)}:{int(_cp)}"])
        cmd_args.extend(["--restart", "unless-stopped", str(plan.image)])

    try:
        # 같은 이름으로 docker run 하면 기존 컨테이너가 남아 있는 정상 재배포는
        # 항상 실패한다. 실제 배포 경로도 롤백과 동일하게 기존 컨테이너를 교체한다.
        if plan.method == DeployMethod.LOCAL_DOCKER:
            await _stop_prior_verifications_for_container(plan.container_name or "")
            await _remove_existing_local_container(plan.container_name or "")
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                cmd_args, shell=False, capture_output=True, text=True, timeout=300
            ),
        )
        success = result.returncode == 0
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Deployment execution failed: {exc}") from exc

    # docker run 성공만으로는 앱이 준비됐다고 볼 수 없다. HTTP 헬스 확인을 통과한
    # 기록만 이후 배포의 롤백 후보가 된다. 이 확인이 실패해도 이번 실행 자체의
    # 결과는 실패로 바꾸지 않는다. 사용자는 장애 버전에 대해 여전히 롤백을 요청할
    # 수 있어야 하기 때문이다.
    rollback_eligible = success and await _verify_rollback_candidate_health(plan)

    # Record the deployment
    from schemas import ActionType
    record = DeploymentRecord(
        project_id=getattr(plan, "project_id", "unknown"),
        method=plan.method,
        image=plan.image or "",
        container_name=plan.container_name or "",
        health_check_path=plan.health_check_path,
        # 롤백이 같은 모양으로 다시 띄울 수 있도록 실행 조건을 함께 남긴다.
        ports={str(k): str(v) for k, v in (plan.ports or {}).items()},
        env={str(k): str(v) for k, v in (getattr(plan, "env", None) or {}).items()},
        rollback_target=rollback_target,
        rollback_source_deployment_id=(
            rollback_source.deployment_id if rollback_source is not None else None
        ),
        rollback_ports=(dict(rollback_source.ports) if rollback_source is not None else {}),
        rollback_env=(dict(rollback_source.env) if rollback_source is not None else {}),
        rollback_health_check_path=(
            rollback_source.health_check_path if rollback_source is not None else None
        ),
        rollback_eligible=rollback_eligible,
        status=DeployStatus.SUCCESS if success else DeployStatus.FAILED,
    )
    _deployment_records[record.deployment_id] = record
    del _deployment_plans[request.plan_id]

    # 설계 §4.6 / §34 — 배포 성공 직후 Continuous Verification 자동 트리거.
    # 실패 시에도 verification 자체의 예외가 배포 응답을 흔들지 않도록 모두 catch.
    cv_started = False
    cv_enabled = request.enable_continuous_verification
    if cv_enabled is None:
        cv_enabled = bool(getattr(plan, "enable_continuous_verification", True))
    if success and cv_enabled:
        get_continuous_verifier = None  # type: ignore
        try:
            from preflight.continuous_verification import get_continuous_verifier  # type: ignore
        except Exception:  # noqa: BLE001
            try:
                from core.preflight.continuous_verification import get_continuous_verifier  # type: ignore
            except Exception as _exc:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(
                    "ContinuousVerifier unavailable: %s", _exc,
                )

        if get_continuous_verifier is not None:
            try:
                first_hp = next(iter(plan.ports.items()), ("8080", "8080"))[0]
                health_path = plan.health_check_path or "/health"
                if not health_path.startswith("/"):
                    health_path = "/" + health_path
                health_url = f"http://localhost:{first_hp}{health_path}"
                verifier = get_continuous_verifier()
                await verifier.start(
                    deployment_id=record.deployment_id,
                    container_name=record.container_name,
                    health_check_url=health_url,
                    duration_minutes=5,
                    project_id=getattr(plan, "project_id", None),
                    on_threshold_exceeded=_mark_rollback_candidate_unhealthy,
                    on_complete=_update_rollback_candidate_after_verification,
                )
                cv_started = True
            except Exception as _exc:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(
                    "Continuous verification start failed (deployment still success): %s",
                    _exc,
                )

    return {
        "status": "success" if success else "failed",
        "deployment_id": record.deployment_id,
        "rollback_target": record.rollback_target,
        "rollback_eligible": record.rollback_eligible,
        "rollback_reason": rollback_reason,
        "stdout": result.stdout[:2000],
        "stderr": result.stderr[:2000],
        "continuous_verification": {
            "enabled": bool(cv_enabled),
            "started": bool(cv_started),
        },
    }


@router.post("/api/deploy/local")
async def execute_deployment_local(request: ExecuteRequest) -> dict:
    """
    Alias for /api/deploy/execute — matches the path name specified in
    the v6.4 design document (§20.5 DeploymentPlan, method=local_docker).
    """
    return await execute_deployment(request)


@router.get("/api/deploy/records")
async def list_deployment_records() -> list[DeploymentRecord]:
    """Return all deployment records for the current session."""
    return list(_deployment_records.values())


@router.post("/api/deploy/rollback")
async def rollback(request: RollbackRequest) -> dict:
    """이전 이미지로 되돌린다. **배포 당시의 실행 조건을 그대로 재현한다.**

    예전에는 이미지 태그만 바꿔 띄워서 포트 매핑이 사라졌다. 그 실패는 어디에도
    빨간불이 뜨지 않는다 — 컨테이너 내부 헬스체크는 `127.0.0.1` 을 보므로 docker
    는 healthy 로 표시하고, 지속 검증도 정상으로 보고하고, 이 API 도 200 을
    돌려준다. 모든 지표가 "복구됨"인데 사용자만 접속하지 못한다. 장애 대응 중에
    이걸 만나면 원인을 찾는 데 시간을 다 쓴다.
    """
    record = _deployment_records.get(request.deployment_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Deployment '{request.deployment_id}' not found.")

    if not record.rollback_target:
        raise HTTPException(
            status_code=422,
            detail=f"Deployment '{request.deployment_id}' has no rollback target.",
        )

    # 보안: 이름·이미지·포트·환경변수를 화이트리스트로 검증한 뒤 args list 로 호출한다.
    # shell=True 가 아니므로 && 체이닝 불가 → stop / rm / run 을 3단계 순차 실행.
    #
    # **검증은 try 밖에서 한다.** 안에서 HTTPException 을 던지면 아래
    # `except Exception` 이 그것마저 500 으로 감싸서, 400 이어야 할 입력 오류가
    # 서버 오류로 둔갑한다(사용자는 자기가 고칠 수 있는 문제인 줄 모른다).
    import re as _re
    _IMG_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/\-@]{0,254}$")
    _NAME_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")
    _ENV_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    if not _NAME_RE.match(record.container_name or ""):
        raise HTTPException(status_code=400, detail="Invalid container_name on record.")
    if not _IMG_RE.match(record.rollback_target):
        raise HTTPException(status_code=400, detail="Invalid rollback_target on record.")

    rollback_ports = (
        record.rollback_ports
        if record.rollback_source_deployment_id is not None
        else record.ports
    )
    rollback_env = (
        record.rollback_env
        if record.rollback_source_deployment_id is not None
        else record.env
    )

    run_args = ["docker", "run", "-d", "--name", record.container_name]
    for _hp, _cp in (rollback_ports or {}).items():
        try:
            _host, _cont = int(_hp), int(_cp)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"기록된 포트 매핑이 올바르지 않습니다: {_hp}:{_cp}",
            ) from None
        run_args.extend(["-p", f"{_host}:{_cont}"])
    for _k, _v in (rollback_env or {}).items():
        if not _ENV_RE.match(str(_k)):
            raise HTTPException(
                status_code=400,
                detail=f"기록된 환경변수 이름이 올바르지 않습니다: {_k!r}",
            )
        run_args.extend(["-e", f"{_k}={_v}"])
    run_args.extend(["--restart", "unless-stopped", record.rollback_target])

    try:
        loop = asyncio.get_running_loop()
        # 1) docker stop (실패 무시 — 이미 중지됐을 수 있음)
        await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["docker", "stop", record.container_name],
                shell=False, capture_output=True, text=True, timeout=60,
            ),
        )
        # 2) docker rm (실패 무시)
        await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["docker", "rm", record.container_name],
                shell=False, capture_output=True, text=True, timeout=60,
            ),
        )
        # 3) docker run — 이게 실패하면 rollback 실패
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                run_args, shell=False, capture_output=True, text=True, timeout=120,
            ),
        )
        success = result.returncode == 0
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Rollback failed: {exc}") from exc

    record.status = DeployStatus.ROLLED_BACK if success else DeployStatus.FAILED

    return {
        "status": "ok" if success else "failed",
        "deployment_id": request.deployment_id,
        "rolled_back_to": record.rollback_target,
        "ports": dict(rollback_ports or {}),
        # 포트 기록이 없는 배포(이 필드가 생기기 전의 기록)는 롤백해도 밖에서
        # 접속할 수 없다. 조용히 성공으로 보이지 않게 알린다.
        "warning": (
            None if (rollback_ports or {})
            else "이 배포에는 포트 기록이 없어 롤백된 컨테이너에 외부 접속이 불가능할 수 있습니다."
        ),
        "stdout": result.stdout[:2000],
        "stderr": result.stderr[:2000],
    }


def _get_verifier():
    """ContinuousVerifier singleton."""
    try:
        from preflight.continuous_verification import get_continuous_verifier
        return get_continuous_verifier()
    except Exception:
        try:
            from core.preflight.continuous_verification import get_continuous_verifier
            return get_continuous_verifier()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"ContinuousVerifier unavailable: {exc}",
            ) from exc


@router.get("/api/deploy/verification/{deployment_id}/status")
async def get_verification_status(deployment_id: str) -> dict:
    verifier = _get_verifier()
    snapshot = verifier.get_status(deployment_id)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"No continuous verification found for deployment '{deployment_id}'.",
        )
    return snapshot


@router.post("/api/deploy/verification/{deployment_id}/stop")
async def stop_verification(deployment_id: str) -> dict:
    verifier = _get_verifier()
    return await verifier.stop(deployment_id)
