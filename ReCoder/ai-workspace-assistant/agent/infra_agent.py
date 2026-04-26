"""
Infra Agent — Dockerfile 생성 (템플릿 70% + Gemini 커스터마이징 30%).
사용자 클릭 시 1회 호출.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from schemas import InfraFileProposal, RiskLevel

# ── 스택별 기본 템플릿 ─────────────────────────────────────────────────

_TEMPLATES: dict[str, str] = {
    "python-fastapi": """\
FROM python:3.11-slim
WORKDIR /app
RUN adduser --disabled-password --gecos "" appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER appuser
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
""",
    "python-flask": """\
FROM python:3.11-slim
WORKDIR /app
RUN adduser --disabled-password --gecos "" appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER appuser
EXPOSE 5000
CMD ["python", "app.py"]
""",
    "node": """\
FROM node:20-slim
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
USER node
EXPOSE 3000
CMD ["node", "index.js"]
""",
    "generic-python": """\
FROM python:3.11-slim
WORKDIR /app
RUN adduser --disabled-password --gecos "" appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER appuser
CMD ["python", "main.py"]
""",
}


# ── 프로젝트 스캔 ─────────────────────────────────────────────────────

def _detect_stack(project_path: str) -> tuple[str, dict]:
    """프로젝트 폴더를 스캔해 스택과 메타 정보 반환."""
    p    = Path(project_path)
    meta = {"port": "8000", "entrypoint": "main.py", "has_requirements": False}

    req = p / 'requirements.txt'
    if req.exists():
        meta['has_requirements'] = True
        content = req.read_text(encoding='utf-8', errors='replace').lower()
        if 'fastapi' in content or 'uvicorn' in content:
            meta['entrypoint'] = 'main.py'
            return "python-fastapi", meta
        if 'flask' in content:
            meta['entrypoint'] = 'app.py'
            meta['port'] = '5000'
            return "python-flask", meta
        return "generic-python", meta

    if (p / 'package.json').exists():
        meta['entrypoint'] = 'index.js'
        meta['port'] = '3000'
        return "node", meta

    return "generic-python", meta


def _detect_db_driver(project_path: str) -> bool:
    """DB 드라이버 존재 시 docker-compose 필요 여부 판단."""
    p = Path(project_path)
    signals: list[str] = []
    for name in ("requirements.txt", "pyproject.toml", "package.json", ".env.example"):
        target = p / name
        if target.exists():
            signals.append(target.read_text(encoding='utf-8', errors='replace').lower())
    content = "\n".join(signals)
    db_drivers = [
        'database_url', 'psycopg2', 'pymysql', 'aiomysql', 'asyncpg',
        'sqlalchemy', 'prisma', 'typeorm', '"pg"', 'mysql2',
    ]
    return any(d in content for d in db_drivers)


def _resolve_project_path(project_path: str = ".") -> Path:
    raw = (project_path or ".").strip()
    if raw == ".":
        try:
            from code_agent import _project_root
            return _project_root()
        except Exception:
            return Path.cwd()
    return Path(raw).expanduser().resolve()


# ── Gemini 커스터마이징 ───────────────────────────────────────────────

def _customize_with_gemini(template: str, meta: dict, error_context: str = "") -> str:
    """템플릿에 프로젝트 메타 정보를 반영해 Gemini로 커스터마이징."""
    prompt = f"""다음 Dockerfile 템플릿을 프로젝트 정보에 맞게 최소한으로 수정하세요.
JSON 없이 Dockerfile 내용만 출력하세요. 주석 포함.

## 템플릿
{template}

## 프로젝트 정보
- 포트: {meta.get('port', '8000')}
- 진입점: {meta.get('entrypoint', 'main.py')}
- requirements.txt 존재: {meta.get('has_requirements', False)}
{f'- 에러 컨텍스트: {error_context[:200]}' if error_context else ''}

규칙:
- latest 태그 금지 (명시적 버전 사용)
- root user 지양 (USER nobody 또는 appuser)
- pip install --no-cache-dir 사용
- EXPOSE 명시
"""
    try:
        # code_agent 의 client/폴백 체인을 재사용 — quota/모델 회수 자동 처리
        from code_agent import _get_client, _call_with_fallback
        client = _get_client()
        response, used_model = _call_with_fallback(client, prompt)
        result = (getattr(response, 'text', None) or "").strip()
        if not result:
            print(f"[infra_agent] 빈 응답 (model={used_model}), 템플릿 그대로 사용")
            return template
        # 코드 펜스 제거
        result = re.sub(r'^```(?:dockerfile|docker)?\s*', '', result, flags=re.IGNORECASE)
        result = re.sub(r'\s*```\s*$', '', result)
        print(f"[infra_agent] Dockerfile 커스터마이징 성공 (model={used_model})")
        return result.strip()
    except Exception as e:
        # 친절한 메시지로 한 줄 출력 — code_agent._humanize_gemini_error 사용
        try:
            from code_agent import _humanize_gemini_error
            msg = _humanize_gemini_error(e, os.getenv('GEMINI_MODEL', 'fallback-chain'))
        except Exception:
            msg = str(e)[:300]
        print(f"[infra_agent] Gemini 커스터마이징 실패, 템플릿 그대로 사용: {msg}")
        return template


# ── 공개 API ──────────────────────────────────────────────────────────

def generate_dockerfile(project_path: str = ".", error_context: str = "") -> InfraFileProposal:
    """Dockerfile 생성 — 템플릿 + Gemini 커스터마이징."""
    project_root = _resolve_project_path(project_path)
    stack, meta = _detect_stack(str(project_root))
    template    = _TEMPLATES.get(stack, _TEMPLATES["generic-python"])
    content     = _customize_with_gemini(template, meta, error_context)

    return InfraFileProposal(
        proposal_id   = uuid.uuid4().hex,
        file_type     = "Dockerfile",
        target_path   = "Dockerfile",
        content       = content,
        base_template = stack,
        risk          = RiskLevel.LOW,
    )


def generate_docker_compose(project_path: str = ".") -> InfraFileProposal:
    """docker-compose.yml 생성 (DB 드라이버 감지 시 다중 서비스)."""
    project_root = _resolve_project_path(project_path)
    has_db    = _detect_db_driver(str(project_root))
    _, meta   = _detect_stack(str(project_root))
    port      = meta.get('port', '8000')

    if has_db:
        content = f"""\
version: "3.9"
services:
  app:
    build: .
    ports:
      - "{port}:{port}"
    environment:
      - DATABASE_URL=postgresql://user:password@db:5432/appdb
    depends_on:
      - db

  db:
    image: postgres:15
    environment:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: password
      POSTGRES_DB: appdb
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
"""
    else:
        content = f"""\
version: "3.9"
services:
  app:
    build: .
    ports:
      - "{port}:{port}"
"""

    return InfraFileProposal(
        proposal_id   = uuid.uuid4().hex,
        file_type     = "docker-compose",
        target_path   = "docker-compose.yml",
        content       = content,
        base_template = "db-multi" if has_db else "single",
        risk          = RiskLevel.LOW,
    )


def generate_github_actions(project_path: str = ".") -> InfraFileProposal:
    """Generate a conservative GitHub Actions CI workflow."""
    project_root = _resolve_project_path(project_path)
    stack, _meta = _detect_stack(str(project_root))

    if stack.startswith("python"):
        content = """\
name: CI

on:
  push:
    branches: [ main, master ]
  pull_request:
    branches: [ main, master ]

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
        template_name = "python-ci"
    elif stack == "node":
        content = """\
name: CI

on:
  push:
    branches: [ main, master ]
  pull_request:
    branches: [ main, master ]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
          cache: npm
      - name: Install dependencies
        run: npm ci
      - name: Run checks
        run: npm test --if-present
"""
        template_name = "node-ci"
    else:
        content = """\
name: CI

on:
  push:
    branches: [ main, master ]
  pull_request:
    branches: [ main, master ]

jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: List project
        run: find . -maxdepth 2 -type f | sort
"""
        template_name = "generic-ci"

    return InfraFileProposal(
        proposal_id   = uuid.uuid4().hex,
        file_type     = "github-actions",
        target_path   = ".github/workflows/ci.yml",
        content       = content,
        base_template = template_name,
        risk          = RiskLevel.LOW,
    )
