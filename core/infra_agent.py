"""
Infra Agent — Dockerfile 및 docker-compose 생성 (Stage 2).

설계서 §13.2 기준:
- 기존 _TEMPLATES 하드코딩 제거 → FileTemplate Registry 사용
- generate_dockerfile(): FileRegistry.detect_stack_template() → FileRegistry.render() → LLM 커스터마이징 제안
- generate_docker_compose(): FileRegistry render → InfraFileProposal 반환
- approval_level=1 (로컬 파일 생성)

LLM은 커스터마이징할 섹션만 제안. 실제 파일 조립은 Registry가 한다.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Optional

from schemas import (
    AnalyzeRequest,
    InfraFileProposal,
    ProjectProfile,
    ProjectStack,
    RiskLevel,
)
from registries.file_registry import get_file_registry
from llm.router import get_router
from llm.base import LLMRequest


class StackDetectionError(RuntimeError):
    """스택 감지 실패 시 발생."""


def _detect_stack_in_dir(p: Path) -> tuple[str, dict] | None:
    """
    단일 디렉토리에서 스택 감지. 감지 실패 시 None 반환.
    """
    meta: dict = {"port": "8000", "entrypoint": "main.py", "has_requirements": False}

    # Python 프로젝트 감지 (requirements.txt 우선)
    req = p / "requirements.txt"
    if req.exists():
        meta["has_requirements"] = True
        content = req.read_text(encoding="utf-8", errors="replace").lower()
        if "fastapi" in content or "uvicorn" in content:
            meta["entrypoint"] = "main.py"
            return "python-fastapi", meta
        if "flask" in content:
            meta["entrypoint"] = "app.py"
            meta["port"] = "5000"
            return "python-flask", meta
        # requirements.txt 있지만 알 수 없는 프레임워크 → None 반환해 상위에서 처리
        return None

    # Node.js 프로젝트 감지
    if (p / "package.json").exists():
        meta["entrypoint"] = "index.js"
        meta["port"] = "3000"
        try:
            import json as _json
            package = _json.loads(
                (p / "package.json").read_text(encoding="utf-8", errors="replace")
            )
        except Exception:
            package = {}

        deps: dict[str, str] = {}
        if isinstance(package, dict):
            for key in ("dependencies", "devDependencies"):
                values = package.get(key)
                if isinstance(values, dict):
                    deps.update({str(k).lower(): str(v) for k, v in values.items()})

        if "next" in deps:
            meta["entrypoint"] = "npm start"
            return "node-next", meta
        if "express" in deps:
            return "node-express", meta

    return None


def _detect_stack(project_path: str) -> tuple[str, dict]:
    """
    프로젝트 폴더를 스캔해 스택과 메타 정보 반환.

    1차: 주어진 경로에서 바로 감지
    2차: 1단계 하위 디렉토리 탐색 (모노레포 / 부모 폴더 오픈 대응)

    Returns:
        tuple[str, dict]: (stack_name, metadata)
        - stack_name: "python-fastapi", "python-flask", "node-express", "node-next"
        - metadata: {"port", "entrypoint", "has_requirements", ...}
    """
    p = Path(project_path)

    # 1차: 루트에서 바로 감지
    result = _detect_stack_in_dir(p)
    if result is not None:
        return result

    # 2차: 1단계 하위 디렉토리 탐색
    # Python requirements.txt 우선 탐색 → Node 순
    subdirs = sorted(
        [d for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")],
        key=lambda d: d.name,
    )

    # Python 먼저
    for sub in subdirs:
        sub_result = _detect_stack_in_dir(sub)
        if sub_result and sub_result[0].startswith("python"):
            return sub_result

    # Node
    for sub in subdirs:
        sub_result = _detect_stack_in_dir(sub)
        if sub_result and sub_result[0].startswith("node"):
            return sub_result

    # 루트에 requirements.txt 있지만 프레임워크 불명확
    req = p / "requirements.txt"
    if req.exists():
        raise StackDetectionError(
            "스택을 자동 감지하지 못했습니다. requirements.txt에 fastapi/flask 중 하나를 포함해주세요."
        )

    # 루트에 package.json 있지만 express/next 없음
    if (p / "package.json").exists():
        raise StackDetectionError(
            "스택을 자동 감지하지 못했습니다. package.json에 express 또는 next를 포함해주세요."
        )

    raise StackDetectionError(
        "스택을 자동 감지하지 못했습니다. requirements.txt 또는 package.json이 필요합니다."
    )


def _detect_db_driver(project_path: str) -> bool:
    """
    DB 드라이버 존재 여부를 확인해 docker-compose 필요 여부 판단.

    Returns:
        bool: DB 드라이버가 감지되면 True (docker-compose 필요)
    """
    p = Path(project_path)
    signals: list[str] = []
    for name in ("requirements.txt", "pyproject.toml", "package.json", ".env.example"):
        target = p / name
        if target.exists():
            try:
                signals.append(
                    target.read_text(encoding="utf-8", errors="replace").lower()
                )
            except Exception:
                pass

    content = "\n".join(signals)
    db_drivers = [
        "database_url",
        "psycopg2",
        "pymysql",
        "aiomysql",
        "asyncpg",
        "sqlalchemy",
        "prisma",
        "typeorm",
        '"pg"',
        "mysql2",
    ]
    return any(d in content for d in db_drivers)


def _resolve_project_path(project_path: str | Path = ".") -> Path:
    """프로젝트 경로 정규화."""
    raw = str(project_path or ".").strip()
    if raw == ".":
        return Path.cwd()
    return Path(raw).expanduser().resolve()


def _customize_with_llm(
    template: str, stack: str, meta: dict, error_context: str = ""
) -> str:
    """
    템플릿에 프로젝트 메타 정보를 반영해 LLM으로 커스터마이징.

    설계서 §13.2:
    - LLM은 커스터마이징 제안만 (섹션 이름, 제안 내용)
    - 실제 파일 조립은 Registry가 한다
    - RECODER_INFRA_AI_CUSTOMIZE=0으로 비활성화 가능

    Args:
        template: FileRegistry.render()로 생성한 기본 템플릿
        stack: 스택 이름 (python-fastapi, node-express 등)
        meta: 프로젝트 메타정보 (port, entrypoint 등)
        error_context: 에러 컨텍스트 (선택사항)

    Returns:
        str: LLM이 제안한 커스터마이징된 Dockerfile
    """
    use_ai = os.getenv("RECODER_INFRA_AI_CUSTOMIZE", "1").strip().lower()
    if use_ai in {"0", "false", "no", "off"}:
        return template

    prompt = f"""다음 Dockerfile 템플릿을 프로젝트 정보에 맞게 최소한으로 수정하세요.
JSON 없이 Dockerfile 내용만 출력하세요. 주석 포함.

## 스택: {stack}

## 템플릿
{template}

## 프로젝트 정보
- 포트: {meta.get('port', '8000')}
- 진입점: {meta.get('entrypoint', 'main.py')}
- 패키지 파일 존재: {meta.get('has_requirements', False)}
{f'- 에러 컨텍스트: {error_context[:200]}' if error_context else ''}

규칙:
- latest 태그 금지 (명시적 버전 사용)
- root user 지양 (USER nobody 또는 appuser)
- pip install --no-cache-dir 사용 (Python 프로젝트)
- EXPOSE 명시
- 멀티스테이지 빌드 권장 (크기 최소화)
"""

    try:
        llm_resp = get_router().call(
            LLMRequest(prompt=prompt, max_tokens=2048, temperature=0.0),
            agent="infra_agent",
            operation="customize_dockerfile",
        )
        result = llm_resp.text.strip()
        if not result:
            return template

        # 코드 펜스 제거
        result = re.sub(
            r"^```(?:dockerfile|docker)?\s*", "", result, flags=re.IGNORECASE
        )
        result = re.sub(r"\s*```\s*$", "", result)
        return result.strip()
    except Exception:
        return template


def generate_dockerfile(
    request: Optional[AnalyzeRequest] = None,
    project_profile: Optional[ProjectProfile] = None,
    workspace_path: str = ".",
) -> InfraFileProposal:
    """
    Dockerfile 생성 (Stage 2).

    설계서 §13.2:
    1. FileRegistry.detect_stack_template()으로 스택 자동 감지
    2. FileRegistry.render()로 기본 파일 생성
    3. LLM에게 커스터마이징할 섹션 제안 요청
    4. InfraFileProposal 반환 (approval_level=1)

    Args:
        request: AnalyzeRequest (선택, 에러 컨텍스트용)
        project_profile: ProjectProfile (선택, 우선 사용)
        workspace_path: 프로젝트 경로 (기본값: ".")

    Returns:
        InfraFileProposal: Dockerfile 제안
    """
    if project_profile:
        project_root = Path(project_profile.workspace_path)
        stack = project_profile.stack.value
    else:
        project_root = _resolve_project_path(workspace_path)
        stack, _ = _detect_stack(str(project_root))

    # FileRegistry에서 스택별 기본 템플릿 가져오기
    registry = get_file_registry()
    # stack name (e.g. "python-fastapi") → template_id ("dockerfile-python-fastapi")
    template_id = f"dockerfile-{stack}" if not stack.startswith("dockerfile-") else stack
    if registry.get(template_id) is None:
        # 폴백: detect_stack_template 로 직접 감지 (경로 없으면 기본값 사용)
        try:
            template_id = registry.detect_stack_template(str(project_root))
        except Exception:
            template_id = "dockerfile-python-fastapi"
    template = registry.render(template_id, {})  # Dockerfile 템플릿은 placeholder 없음

    # 메타정보 추출 (원격 모드에서 경로 없으면 빈 dict 사용)
    try:
        _, meta = _detect_stack(str(project_root))
    except Exception:
        meta = {}

    # LLM 커스터마이징
    error_context = ""
    if request and request.terminal_output:
        error_context = request.terminal_output[:300]

    customized_content = _customize_with_llm(template, stack, meta, error_context)

    return InfraFileProposal(
        proposal_id=uuid.uuid4().hex,
        file_type="Dockerfile",
        target_path="Dockerfile",
        content=customized_content,
        base_template=stack,
        risk_level=RiskLevel.LOW,
        approval_level=1,  # 로컬 파일 생성
    )


def _env_file_block(project_root: Path) -> str:
    """`.env.example` 가 있으면 compose 의 'env_file: [\".env\"]' 블록 반환, 없으면 빈 문자열.

    UX: 사용자에게 "DB URL 등 환경 변수는 .env 에 두세요" 라는 신호를 자동으로 반영.
    """
    if (project_root / ".env.example").exists() or (project_root / ".env").exists():
        return "    env_file:\n      - .env\n"
    return ""


def generate_docker_compose(
    project_profile: Optional[ProjectProfile] = None,
    workspace_path: str = ".",
) -> InfraFileProposal:
    """
    docker-compose.yml 생성 (Stage 2).

    개선:
    - DB 드라이버 감지 시 `docker-compose-db` 템플릿(app + postgres + healthcheck) 사용
    - `.env.example` 존재 시 `env_file: [\".env\"]` 자동 추가
    - healthcheck 가 wget→curl 폴백 형태라 alpine/slim 모두 동작
    """
    if project_profile:
        project_root = Path(project_profile.workspace_path)
        stack = project_profile.stack.value
        default_port = str(project_profile.default_port)
    else:
        project_root = _resolve_project_path(workspace_path)
        try:
            stack, meta = _detect_stack(str(project_root))
        except Exception:
            stack, meta = "python-fastapi", {}
        default_port = meta.get("port", "8000")

    try:
        has_db = _detect_db_driver(str(project_root))
    except Exception:
        has_db = False
    template_id = "docker-compose-db" if has_db else "docker-compose"

    registry = get_file_registry()
    content = registry.render(
        template_id,
        {
            "image": "recoder-app:latest",
            "container_name": "recoder-app",
            "host_port": default_port,
            "container_port": default_port,
            "health_check_path": "/health",
            "env_file_block": _env_file_block(project_root),
        },
    )

    return InfraFileProposal(
        proposal_id=uuid.uuid4().hex,
        file_type="docker-compose",
        target_path="docker-compose.yml",
        content=content,
        base_template="db-multi" if has_db else "single",
        risk_level=RiskLevel.LOW,
        approval_level=1,  # 로컬 파일 생성
    )


# ── .dockerignore 생성 ────────────────────────────────────────────────

_DOCKERIGNORE_BASE = """\
# ReCoder default .dockerignore — 이미지 크기/빌드 시간 단축용
.git
.gitignore
.github/
.vscode/
.idea/
README.md
*.md
.DS_Store
Thumbs.db

# OS / IDE
__pycache__/
*.py[cod]
*$py.class

# Local secrets — 절대 이미지에 포함 금지
.env
.env.*
!.env.example

# 빌드 산출물
dist/
build/
out/
.next/
.cache/
coverage/
*.log
"""

_DOCKERIGNORE_PYTHON = """\
.venv/
venv/
env/
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
"""

_DOCKERIGNORE_NODE = """\
node_modules/
npm-debug.log*
yarn-debug.log*
yarn-error.log*
.pnpm-store/
"""


def generate_dockerignore(
    project_profile: Optional[ProjectProfile] = None,
    workspace_path: str = ".",
) -> InfraFileProposal:
    """
    .dockerignore 자동 생성 — 이미지에서 시크릿/빌드 산출물/캐시 제외.
    스택별로 추가 패턴을 덧붙여 첫 빌드부터 가벼움.
    """
    if project_profile:
        project_root = Path(project_profile.workspace_path)
        stack = project_profile.stack.value
    else:
        project_root = _resolve_project_path(workspace_path)
        try:
            stack, _ = _detect_stack(str(project_root))
        except StackDetectionError:
            stack = "generic"

    extra = ""
    if stack.startswith("python"):
        extra = "\n# Python\n" + _DOCKERIGNORE_PYTHON
    elif stack.startswith("node"):
        extra = "\n# Node\n" + _DOCKERIGNORE_NODE

    content = _DOCKERIGNORE_BASE + extra

    return InfraFileProposal(
        proposal_id=uuid.uuid4().hex,
        file_type="dockerignore",
        target_path=".dockerignore",
        content=content,
        base_template=f"dockerignore-{stack}",
        risk_level=RiskLevel.LOW,
        approval_level=1,
    )


def generate_github_actions(
    project_profile: Optional[ProjectProfile] = None,
    workspace_path: str = ".",
) -> InfraFileProposal:
    """
    GitHub Actions CI 워크플로우 생성.

    스택별 최소한의 CI/CD 파이프라인을 자동 생성.
    """
    if project_profile:
        project_root = Path(project_profile.workspace_path)
        stack = project_profile.stack.value
    else:
        project_root = _resolve_project_path(workspace_path)
        stack, _ = _detect_stack(str(project_root))

    if stack.startswith("python"):
        content = _GHA_PYTHON
        template_name = "python-ci"
    elif stack in {"node-express", "node-next"}:
        content = _GHA_NODE
        template_name = "node-ci"
    else:
        content = _GHA_GENERIC
        template_name = "generic-ci"

    required_secrets = [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "ECR_REGISTRY",
        "EC2_HOST",
        "EC2_SSH_KEY",
    ]

    secrets_comment = (
        "# ─────────────────────────────────────────────────────────────────────\n"
        "# 설계서 v6.4 §13 — Required GitHub Secrets\n"
        "# ─────────────────────────────────────────────────────────────────────\n"
        "# AWS/ECR/EC2 배포 확장을 사용한다면 다음 GitHub Secrets 를 등록하세요\n"
        "# (Repository → Settings → Secrets and variables → Actions):\n"
        "#\n"
        "#   - AWS_ACCESS_KEY_ID\n"
        "#   - AWS_SECRET_ACCESS_KEY\n"
        "#   - ECR_REGISTRY     (예: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com)\n"
        "#   - EC2_HOST         (배포 대상 EC2 공인 호스트 또는 도메인)\n"
        "#   - EC2_SSH_KEY      (EC2 접속용 개인 키 전체 본문, BEGIN/END 포함)\n"
        "#\n"
        "# IAM 최소 권한 — ecr:* / ec2:DescribeInstances 만 허용한 Inline policy 권장.\n"
        "# ─────────────────────────────────────────────────────────────────────\n"
        "\n"
    )
    content = secrets_comment + content

    return InfraFileProposal(
        proposal_id=uuid.uuid4().hex,
        file_type="github-actions",
        target_path=".github/workflows/deploy.yml",
        content=content,
        base_template=template_name,
        risk_level=RiskLevel.LOW,
        approval_level=1,
        required_secrets=required_secrets,
    )


_GHA_PYTHON = """\
name: CI

on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
      - name: Syntax check
        run: python -m compileall .
"""

_GHA_NODE = """\
name: CI

on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - name: Install dependencies
        run: npm install
      - name: Run checks
        run: npm test --if-present
"""

_GHA_GENERIC = """\
name: CI

on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]

jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: List project
        run: find . -maxdepth 2 -type f | sort
"""


# ── 싱글턴 접근 (향후 확장용) ─────────────────────────────────────────

_instance = None


def get_infra_agent():
    """Infra Agent 싱글턴 반환."""
    return None


__all__ = [
    "generate_dockerfile",
    "generate_docker_compose",
    "generate_dockerignore",
    "generate_github_actions",
    "StackDetectionError",
]
