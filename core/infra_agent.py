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
    FileType,
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


def _detect_stack(project_path: str) -> tuple[str, dict]:
    """
    프로젝트 폴더를 스캔해 스택과 메타 정보 반환.

    Returns:
        tuple[str, dict]: (stack_name, metadata)
        - stack_name: "python-fastapi", "python-flask", "node-express", "node-next"
        - metadata: {"port", "entrypoint", "has_requirements", ...}
    """
    p = Path(project_path)
    meta = {"port": "8000", "entrypoint": "main.py", "has_requirements": False}

    # Python 프로젝트 감지
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
        raise StackDetectionError(
            "스택을 자동 감지하지 못했습니다. requirements.txt에 fastapi/flask 중 하나를 포함해주세요."
        )

    # Node.js 프로젝트 감지
    if (p / "package.json").exists():
        meta["entrypoint"] = "index.js"
        meta["port"] = "3000"
        try:
            import json
            package = json.loads(
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

        raise StackDetectionError(
            "스택을 자동 감지하지 못했습니다. package.json에 express 또는 next를 포함해주세요."
        )

    raise StackDetectionError(
        "스택을 자동 감지하지 못했습니다. requirements.txt 또는 package.json이 필요합니다."
    )


def _detect_db_driver(project_path: str) -> Optional[str]:
    """
    설정과 의존성에서 DB 엔진을 확인한다.

    ORM 자체(sqlalchemy/prisma/typeorm)나 빈 DATABASE_URL 표시는 엔진을
    결정하지 못하므로 다중 서비스의 근거로 쓰지 않는다. 엔진을 모르는
    상태에서 PostgreSQL을 임의로 붙이면 MySQL 프로젝트를 깨뜨리기 때문이다.

    Returns:
        "postgresql", "mysql", 또는 엔진을 확정할 수 없으면 None
    """
    p = Path(project_path)
    signals: list[str] = []
    for name in (
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        ".env",
        ".env.example",
        "prisma/schema.prisma",
    ):
        target = p / name
        if target.exists():
            try:
                signals.append(
                    target.read_text(encoding="utf-8", errors="replace").lower()
                )
            except Exception:
                pass

    content = "\n".join(signals)

    # 실제 URL 또는 Prisma provider는 설치만 된 드라이버보다 강한 신호다.
    configured_postgres = bool(re.search(
        r"(?:postgres(?:ql)?(?:\+[a-z0-9_-]+)?://|"
        r"provider\s*=\s*['\"]postgresql['\"])",
        content,
    ))
    configured_mysql = bool(re.search(
        r"(?:mysql(?:\+[a-z0-9_-]+)?://|"
        r"provider\s*=\s*['\"]mysql['\"])",
        content,
    ))
    if configured_postgres != configured_mysql:
        return "postgresql" if configured_postgres else "mysql"

    postgres_driver = bool(re.search(
        r"(?:\bpsycopg(?:2(?:-binary)?|3)?\b|\basyncpg\b|['\"]pg['\"]\s*:)",
        content,
    ))
    mysql_driver = bool(re.search(
        r"(?:\bpymysql\b|\baiomysql\b|\bmysqlclient\b|"
        r"\bmysql-connector(?:-python)?\b|['\"]mysql2['\"]\s*:)",
        content,
    ))
    if postgres_driver != mysql_driver:
        return "postgresql" if postgres_driver else "mysql"
    return None


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
        # 폴백: detect_stack_template 로 직접 감지
        try:
            template_id = registry.detect_stack_template(str(project_root))
        except Exception:
            template_id = "dockerfile-python-fastapi"
    template = registry.render(template_id, {})  # Dockerfile 템플릿은 placeholder 없음

    # 메타정보 추출
    _, meta = _detect_stack(str(project_root))

    # LLM 커스터마이징
    error_context = ""
    if request and request.terminal_output:
        error_context = request.terminal_output[:300]

    customized_content = _customize_with_llm(template, stack, meta, error_context)

    return InfraFileProposal(
        proposal_id=uuid.uuid4().hex,
        file_type=FileType.DOCKERFILE,
        target_path="Dockerfile",
        content=customized_content,
        base_template=stack,
        risk_level=RiskLevel.LOW,
        approval_level=1,  # 로컬 파일 생성
    )


def compose_health_check_block(
    stack: str,
    container_port: str,
    health_check_path: str = "/health",
    start_period: str = "10s",
) -> str:
    """compose 의 app healthcheck 블록. 못 만들면 **빈 문자열**.

    왜 wget/curl 을 안 쓰는가
        예전 템플릿은 `wget --spider || curl -fs` 를 박아 뒀는데, 정작
        우리가 만들어 주는 런타임 이미지에 그 둘이 **모두 없다.**
        python:slim(Debian slim) 도 node:20-slim 도 담지 않는다.
        그래서 앱이 멀쩡히 떠 있어도 compose 는 영구히 unhealthy 로 보고하고,
        `depends_on: condition: service_healthy` 가 걸린 쪽은 아예 못 뜬다.
        Dockerfile 템플릿 쪽은 같은 이유로 이미 python urllib 로 고쳤는데
        compose 만 남아 있었다.

    왜 exec 형식(CMD)인가
        CMD-SHELL 로 두면 YAML 큰따옴표 안에 명령의 따옴표가 또 들어가
        이스케이프가 깨지기 쉽다. 리스트 형식은 셸을 안 거치므로
        인용 문제가 아예 없다.

    모르는 스택이면 빈 문자열
        **항상 실패하는 헬스체크는 없는 것보다 나쁘다.** 정상 컨테이너를
        unhealthy 로 표시해 재시작 루프를 만든다.
    """
    url = f"http://127.0.0.1:{container_port}{health_check_path}"

    if stack.startswith("python"):
        #: python 은 이 이미지에 반드시 있다. 작은따옴표만 써서 YAML
        #: 큰따옴표 문자열 안에 그대로 들어가게 한다.
        probe = (
            '["CMD", "python", "-c", '
            f"\"import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('{url}', "
            'timeout=5).status == 200 else 1)"]'
        )
    elif stack.startswith("node"):
        probe = (
            '["CMD", "node", "-e", '
            f"\"require('http').get('{url}', r => process.exit(r.statusCode === 200 ? 0 : 1))"
            ".on('error', () => process.exit(1))\"]"
        )
    else:
        return ""

    return (
        "    healthcheck:\n"
        f"      test: {probe}\n"
        "      interval: 30s\n"
        "      timeout: 10s\n"
        "      retries: 3\n"
        f"      start_period: {start_period}\n"
    )


def generate_docker_compose(
    project_profile: Optional[ProjectProfile] = None,
    workspace_path: str = ".",
) -> InfraFileProposal:
    """
    docker-compose.yml 생성 (Stage 2).

    설계서 §13.2:
    - FileRegistry.render()로 생성
    - approval_level=1 (로컬 파일 생성)
    - DB 드라이버 감지 시 다중 서비스 (app + db)

    Args:
        project_profile: ProjectProfile (선택)
        workspace_path: 프로젝트 경로 (기본값: ".")

    Returns:
        InfraFileProposal: docker-compose.yml 제안
    """
    if project_profile:
        project_root = Path(project_profile.workspace_path)
        stack = project_profile.stack.value
        default_port = str(project_profile.default_port)
    else:
        project_root = _resolve_project_path(workspace_path)
        stack, meta = _detect_stack(str(project_root))
        default_port = meta.get("port", "8000")

    db_engine = _detect_db_driver(str(project_root))

    # 템플릿의 {env_file_block} 자리에 넣을 값. **실제로 `.env` 가 있을 때만**
    # env_file 블록을 넣는다. 없으면 빈 문자열이되 **반드시 넘긴다** — render()
    # 는 없는 플레이스홀더를 필수 누락으로 보고 ValueError 를 내서
    # docker-compose 생성이 통째로 실패하던 원인이었다.
    #
    # `.env.example` 만 있고 `.env` 는 없는 경우(갓 클론한 저장소의 흔한 상태 —
    # `.env` 는 보통 gitignore)에는 넣지 않는다. docker compose 는 `env_file:
    # - .env` 를 **필수**로 취급해서, 없는 `.env` 를 가리키면 생성된 compose 가
    # `docker compose up` 에서 아예 안 뜬다. 사용자가 `.env.example` 를 복사해
    # `.env` 를 만들면 그때 자연히 잡힌다.
    env_file_block = ""
    if (project_root / ".env").exists():
        env_file_block = "    env_file:\n      - .env\n"

    # FileRegistry에서 docker-compose 템플릿 가져오기
    registry = get_file_registry()
    compose_template = {
        "postgresql": "docker-compose-db",
        "mysql": "docker-compose-mysql",
    }.get(db_engine, "docker-compose")

    health_path = _compose_health_path(project_profile, stack)
    content = registry.render(
        compose_template,
        {
            "image": "recoder-app:latest",
            "container_name": "recoder-app",
            "host_port": default_port,
            "container_port": default_port,
            "env_file_block": env_file_block,
            "health_check_block": compose_health_check_block(
                stack,
                default_port,
                health_path,
                start_period="15s" if db_engine else "10s",
            ),
        },
    )

    # ── build 가 가리키는 Dockerfile 이 실제로 있는가 ────────────────────
    #
    # 템플릿은 `build: context: . / dockerfile: Dockerfile` 을 쓴다. 그런데
    # 「인프라 파일 생성」의 Compose 탭은 Dockerfile 탭보다 **먼저** 눌릴 수
    # 있고, 이 경로는 Dockerfile 을 만들지 않는다. 그대로 두면 제안은
    # "생성 성공" 으로 저장되지만 `docker compose up` 은 없는 파일을
    # 빌드하려다 실패한다 — 사용자는 왜 실패했는지 알 수 없다.
    #
    # 여기서 Dockerfile 을 대신 만들어 주지는 **않는다.** Dockerfile 은
    # 승인 등급이 더 높은(CONFIRM) 산출물이라, compose 생성이 조용히
    # 끼워 넣으면 사람 승인 원칙(D6)을 우회하게 된다. 대신 무엇을 먼저
    # 해야 하는지 그대로 말해 준다.
    risk_reasons: list[str] = []
    if not (project_root / "Dockerfile").exists():
        risk_reasons.append(
            "이 compose 는 같은 폴더의 Dockerfile 을 빌드합니다. 아직 "
            "Dockerfile 이 없으니 「Dockerfile」 탭에서 먼저 생성·승인하세요. "
            "그 전에는 docker compose up 이 빌드 단계에서 실패합니다."
        )

    return InfraFileProposal(
        proposal_id=uuid.uuid4().hex,
        file_type=FileType.DOCKER_COMPOSE,
        target_path="docker-compose.yml",
        content=content,
        base_template="db-multi" if db_engine else "single",
        risk_level=RiskLevel.LOW,
        approval_level=1,  # 로컬 파일 생성
        risk_reasons=risk_reasons,
    )


def _compose_health_path(project_profile: Optional[ProjectProfile], stack: str) -> str:
    """compose 헬스체크가 찌를 경로.

    스캐너의 `health_check_path` 는 **항상 "/health" 로 하드코딩**되어 있어서
    (project_scanner.py) 그대로 믿으면 Next.js 처럼 규약이 다른 스택에서
    없는 경로를 찌른다. 프로필 값이 기본값과 다르면 사람이 정한 것으로 보고
    존중하고, 기본값 그대로면 스택 관례를 쓴다.
    """
    configured = getattr(project_profile, "health_check_path", None) if project_profile else None
    if isinstance(configured, str) and configured.startswith("/") and configured != "/health":
        return configured
    if stack == "node-next":
        return "/api/health"
    return "/health"


# ReCoder Preflight 게이트 job — CI 워크플로 뒤에 append 된다.
# __CIJOB__ 는 앞선 CI job 이름(test/smoke)으로 치환된다.
# run 블록 안 python 들여쓰기는 YAML block-scalar 규칙상 모두 10칸 기준 — 수정 시 주의.
_PREFLIGHT_GATE_TEMPLATE = '''
  # ── ReCoder Preflight 게이트 (배포 전 안전성 자동 검증) ───────────────────
  # self-hosted 러너의 시스템 python 으로 로컬 ReCoder Core 를 호출해 위험한 변경을 차단.
  # setup-python 설치 안 함 + stdlib(urllib) 만 사용 → 러너에 추가 설치 불필요.
  # 셸 무관(단일 python -c 명령) — Windows PowerShell / bash 모두 동작.
  # 원격 Core(EC2)면 RECODER_API_URL 을 그 주소로, runs-on 을 ubuntu-latest 로 바꾸세요.
  # 사전 준비: ① self-hosted 러너 등록 ② Secret RECODER_SESSION_TOKEN 등록.
  preflight:
    name: ReCoder Preflight 게이트
    runs-on: self-hosted
    needs: __CIJOB__
    steps:
      - uses: actions/checkout@v4
      - name: ReCoder 배포 전 안전성 검증
        env:
          RECODER_API_URL: http://localhost:17894
          RECODER_API_TOKEN: ${{ secrets.RECODER_SESSION_TOKEN }}
          FAIL_ON_SCORE_AT_OR_ABOVE: "60"
          PREFLIGHT_PY: |
            import json, os, sys, urllib.request
            api = os.environ["RECODER_API_URL"].rstrip("/")
            tok = os.environ["RECODER_API_TOKEN"]
            thr = int(os.environ.get("FAIL_ON_SCORE_AT_OR_ABOVE", "60"))
            project = os.environ.get("GITHUB_WORKSPACE", ".")
            payload = json.dumps({"project_path": project, "source": "github-action"}).encode("utf-8")
            req = urllib.request.Request(api + "/workbench/preflight/run", data=payload, method="POST")
            req.add_header("X-Session-Token", tok)
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                print("ReCoder Core 호출 실패:", e)
                sys.exit(1)
            status = str(d.get("status", "")).lower()
            blockers = d.get("blockers", [])
            risk = d.get("risk_score")
            if risk is None and d.get("score") is not None:
                risk = max(0, 100 - int(d["score"]))
            risk = int(risk or 0)
            print("status=%s risk_score=%s blockers=%d" % (status, risk, len(blockers)))
            for b in blockers:
                print("  - BLOCKER:", b.get("message") if isinstance(b, dict) else b)
            ok = (not blockers) and status != "blocked" and (thr == 0 or risk < thr)
            if not ok:
                print("배포 차단: 위험 요소가 발견되었습니다.")
                sys.exit(1)
            print("Preflight 통과.")
        run: python -c "import os; exec(os.environ['PREFLIGHT_PY'])"
'''


def generate_github_actions(
    project_profile: Optional[ProjectProfile] = None,
    workspace_path: str = ".",
) -> InfraFileProposal:
    """
    GitHub Actions CI 워크플로우 생성.

    스택별 최소한의 CI/CD 파이프라인을 자동 생성.

    Args:
        project_profile: ProjectProfile (선택)
        workspace_path: 프로젝트 경로 (기본값: ".")

    Returns:
        InfraFileProposal: .github/workflows/deploy.yml 제안
    """
    if project_profile:
        project_root = Path(project_profile.workspace_path)
        stack = project_profile.stack.value
    else:
        project_root = _resolve_project_path(workspace_path)
        try:
            stack, _ = _detect_stack(str(project_root))
        except StackDetectionError:
            # 매니페스트가 없거나(예: stdlib 파이썬 앱) 프레임워크 미감지 →
            # generic CI 로 폴백해 워크플로 생성을 보장한다. (버튼이 에러나지 않게)
            stack = "custom"

    # 스택별 CI 워크플로우 생성
    if stack.startswith("python"):
        content = """\
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
        template_name = "python-ci"
    elif stack in {"node-express", "node-next"}:
        content = """\
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
        template_name = "generic-ci"

    required_secrets = [
        "RECODER_SESSION_TOKEN",  # Preflight 게이트가 로컬/원격 Core 인증에 사용
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "ECR_REGISTRY",
        "EC2_HOST",
        "EC2_SSH_KEY",
    ]

    secrets_comment = """\
# ─────────────────────────────────────────────────────────────────────
# 설계서 v6.4 §13 — Required GitHub Secrets / Required Secrets 안내
# ─────────────────────────────────────────────────────────────────────
# 이 워크플로는 .github/workflows/ 에 직접 저장해야 동작합니다.
# ReCoder는 자동 push하지 않습니다 (의도적 — 사용자 검증을 위해).
#
# AWS/ECR/EC2 배포 확장을 사용한다면 다음 GitHub Secrets를 등록하세요
# (Repository → Settings → Secrets and variables → Actions):
#
#   - AWS_ACCESS_KEY_ID
#       AWS IAM 액세스 키. 아래 IAM 최소 권한 정책만 부여한 전용 사용자 권장.
#   - AWS_SECRET_ACCESS_KEY
#       AWS IAM 시크릿 키. 절대 코드에 노출 금지.
#   - ECR_REGISTRY
#       예: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com
#   - EC2_HOST
#       배포 대상 EC2 공인 호스트 또는 도메인.
#   - EC2_SSH_KEY
#       EC2 접속용 개인 키 전체 본문 (-----BEGIN ... END----- 포함).
#
# IAM 최소 권한 정책 (Inline policy 권장):
# {
#   "Version": "2012-10-17",
#   "Statement": [
#     {"Effect":"Allow","Action":[
#         "ecr:GetAuthorizationToken","ecr:BatchCheckLayerAvailability",
#         "ecr:InitiateLayerUpload","ecr:UploadLayerPart","ecr:CompleteLayerUpload",
#         "ecr:PutImage","ecr:BatchGetImage"
#       ],"Resource":"*"},
#     {"Effect":"Allow","Action":["ec2:DescribeInstances"],"Resource":"*"}
#   ]
# }
#
# (배포 자동화에서 SSH 직접 접속을 사용한다면 추가 IAM 권한 불필요.)
# ─────────────────────────────────────────────────────────────────────

"""
    # ReCoder Preflight 게이트 job 은 self-hosted 러너 + RECODER_SESSION_TOKEN 이
    # 필요해서, 환경변수 RECODER_GHA_PREFLIGHT 로 옵트인할 때만 붙인다.
    # (기본 OFF → 생성 즉시 GitHub 호스팅 러너만으로 도는 CI-only 워크플로.)
    import os as _os
    _preflight_on = _os.environ.get("RECODER_GHA_PREFLIGHT", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if _preflight_on:
        ci_job_name = "smoke" if template_name == "generic-ci" else "test"
        content = content + _PREFLIGHT_GATE_TEMPLATE.replace("__CIJOB__", ci_job_name)

    content = secrets_comment + content

    return InfraFileProposal(
        proposal_id=uuid.uuid4().hex,
        file_type=FileType.GITHUB_ACTIONS,
        target_path=".github/workflows/deploy.yml",
        content=content,
        base_template=template_name,
        risk_level=RiskLevel.LOW,
        approval_level=1,
        required_secrets=required_secrets,
    )


# ── 싱글턴 접근 (향후 확장용) ─────────────────────────────────────────

_instance = None


def get_infra_agent():
    """Infra Agent 싱글턴 반환 (향후 클래스로 확장 시)."""
    return None  # 현재는 함수 기반 API만 제공


__all__ = [
    "generate_dockerfile",
    "generate_docker_compose",
    "generate_github_actions",
    "StackDetectionError",
]
