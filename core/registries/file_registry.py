"""
ReCoder v6.4 FileTemplate Registry (§14.3)
LLM은 커스터마이징할 부분만 제안하고, 실제 파일 조립은 이 Registry가 한다.
설계서 §14.3 기준.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from schemas import FileTemplate


# ── Dockerfile 템플릿 ─────────────────────────────────────────────────

_DOCKERFILE_PYTHON_FASTAPI = """\
FROM python:3.11-slim
WORKDIR /app
RUN adduser --disabled-password --gecos "" appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER appuser
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

_DOCKERFILE_PYTHON_FLASK = """\
FROM python:3.11-slim
WORKDIR /app
RUN adduser --disabled-password --gecos "" appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER appuser
EXPOSE 5000
CMD ["python", "app.py"]
"""

_DOCKERFILE_NODE_EXPRESS = """\
FROM node:20-slim
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
USER node
EXPOSE 3000
CMD ["node", "index.js"]
"""

_DOCKERFILE_NODE_NEXT = """\
FROM node:20-slim
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build
USER node
EXPOSE 3000
CMD ["npm", "start"]
"""


# ── docker-compose 템플릿 ─────────────────────────────────────────────
#
# UX 노트:
# - healthcheck 는 wget(기본)·curl(폴백) 둘 다 시도하는 sh -c 형태로 작성해
#   alpine/slim 이미지에서도 동작.
# - {env_file_block} 자리에는 .env.example 이 있으면 'env_file: [".env"]' 가 들어감.
# - {image} 는 'recoder-app:${{IMAGE_TAG:-latest}}' 형태로 받아 git SHA 태깅 가능.

_DOCKER_COMPOSE_BASE = """\
version: '3.9'
services:
  app:
    image: {image}
    container_name: {container_name}
    ports:
      - "{host_port}:{container_port}"
    restart: unless-stopped
{env_file_block}    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:{container_port}{health_check_path} || curl -fs http://localhost:{container_port}{health_check_path} || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
"""

_DOCKER_COMPOSE_WITH_DB = """\
version: '3.9'
services:
  app:
    image: {image}
    container_name: {container_name}
    ports:
      - "{host_port}:{container_port}"
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
{env_file_block}    environment:
      DATABASE_URL: postgresql://app:app@db:5432/app
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:{container_port}{health_check_path} || curl -fs http://localhost:{container_port}{health_check_path} || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s

  db:
    image: postgres:16-alpine
    container_name: {container_name}-db
    restart: unless-stopped
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: app
    volumes:
      - db_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app -d app"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  db_data:
"""


# ── GitOps 템플릿 (Q4 Must-Wedge) ────────────────────────────────────

_ARGOCD_APPLICATION = """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {app_name}
  namespace: argocd
  labels:
    app: {app_name}
    managed-by: recoder
spec:
  project: default
  source:
    repoURL: {repo_url}
    targetRevision: {target_revision}
    path: {helm_chart_path}
    helm:
      valueFiles:
        - values.yaml
  destination:
    server: https://kubernetes.default.svc
    namespace: {namespace}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
"""

_HELM_VALUES_FARGATE = """\
# Helm values for {app_name} — managed by ReCoder
replicaCount: {replica_count}

image:
  repository: {ecr_image_uri}
  tag: "{image_tag}"
  pullPolicy: Always

service:
  type: ClusterIP
  port: {container_port}

resources:
  requests:
    cpu: "{cpu_request}"
    memory: "{memory_request}"
  limits:
    cpu: "{cpu_limit}"
    memory: "{memory_limit}"

env: {env_yaml}

healthCheck:
  path: {health_check_path}
  initialDelaySeconds: 30
  periodSeconds: 10
"""

# ── ECS Task Definition 템플릿 (Q3-A) ────────────────────────────────

_ECS_TASK_DEFINITION = """\
{{
  "family": "{family}",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "{cpu}",
  "memory": "{memory}",
  "executionRoleArn": "{execution_role_arn}",
  "containerDefinitions": [
    {{
      "name": "{container_name}",
      "image": "{ecr_image_uri}",
      "portMappings": [
        {{
          "containerPort": {container_port},
          "protocol": "tcp"
        }}
      ],
      "essential": true,
      "environment": {env_vars_json},
      "logConfiguration": {{
        "logDriver": "awslogs",
        "options": {{
          "awslogs-group": "/ecs/{family}",
          "awslogs-region": "{aws_region}",
          "awslogs-stream-prefix": "ecs",
          "awslogs-create-group": "true"
        }}
      }},
      "healthCheck": {{
        "command": ["CMD-SHELL", "curl -fs http://localhost:{container_port}{health_check_path} || exit 1"],
        "interval": 30,
        "timeout": 5,
        "retries": 3,
        "startPeriod": 60
      }}
    }}
  ]
}}
"""

# ── GitHub Actions 워크플로우 템플릿 ──────────────────────────────────

_GITHUB_ACTIONS_EC2_DEPLOY = """\
name: Build and Deploy to EC2

on:
  push:
    branches: [ main, master ]

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ap-northeast-2

      - name: Log in to Amazon ECR
        id: login-ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build, tag, and push image to Amazon ECR
        env:
          ECR_REGISTRY: ${{ steps.login-ecr.outputs.registry }}
          ECR_REPOSITORY: {image}
          IMAGE_TAG: ${{ github.sha }}
        run: |
          docker build -t $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG .
          docker push $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG

      - name: Deploy to EC2 via SSH
        env:
          EC2_HOST: ${{ secrets.EC2_HOST }}
          EC2_USER: ec2-user
          EC2_SSH_KEY: ${{ secrets.EC2_SSH_KEY }}
          CONTAINER_NAME: {container_name}
          IMAGE_URI: ${{ steps.login-ecr.outputs.registry }}/{image}:${{ github.sha }}
        run: |
          mkdir -p ~/.ssh
          echo "$EC2_SSH_KEY" > ~/.ssh/deploy_key
          chmod 600 ~/.ssh/deploy_key
          ssh -o StrictHostKeyChecking=no -i ~/.ssh/deploy_key $EC2_USER@$EC2_HOST << 'EOF'
            docker pull $IMAGE_URI
            docker stop $CONTAINER_NAME || true
            docker rm $CONTAINER_NAME || true
            docker run -d --name $CONTAINER_NAME -p 80:8000 $IMAGE_URI
          EOF
"""


# ── FileTemplate Registry ─────────────────────────────────────────────

class FileRegistry:
    """FileTemplate Registry — 파일 템플릿 조회 및 렌더링."""

    def __init__(self):
        self._templates: dict[str, FileTemplate] = {}
        self._init_templates()

    def _init_templates(self) -> None:
        """6개의 FileTemplate 초기화."""
        self._templates = {
            "dockerfile-python-fastapi": FileTemplate(
                template_id="dockerfile-python-fastapi",
                file_type="Dockerfile",
                base_content=_DOCKERFILE_PYTHON_FASTAPI,
                customizable_sections=["base_image", "python_version", "port", "entrypoint"],
            ),
            "dockerfile-python-flask": FileTemplate(
                template_id="dockerfile-python-flask",
                file_type="Dockerfile",
                base_content=_DOCKERFILE_PYTHON_FLASK,
                customizable_sections=["base_image", "python_version", "port", "entrypoint"],
            ),
            "dockerfile-node-express": FileTemplate(
                template_id="dockerfile-node-express",
                file_type="Dockerfile",
                base_content=_DOCKERFILE_NODE_EXPRESS,
                customizable_sections=["base_image", "node_version", "port", "entrypoint"],
            ),
            "dockerfile-node-next": FileTemplate(
                template_id="dockerfile-node-next",
                file_type="Dockerfile",
                base_content=_DOCKERFILE_NODE_NEXT,
                customizable_sections=["base_image", "node_version", "port", "build_command", "start_command"],
            ),
            "docker-compose": FileTemplate(
                template_id="docker-compose",
                file_type="docker-compose",
                base_content=_DOCKER_COMPOSE_BASE,
                customizable_sections=["image", "container_name", "host_port", "container_port", "health_check_path", "env_file_block"],
            ),
            "docker-compose-db": FileTemplate(
                template_id="docker-compose-db",
                file_type="docker-compose",
                base_content=_DOCKER_COMPOSE_WITH_DB,
                customizable_sections=["image", "container_name", "host_port", "container_port", "health_check_path", "env_file_block"],
            ),
            "github-actions-deploy": FileTemplate(
                template_id="github-actions-deploy",
                file_type="github-actions",
                base_content=_GITHUB_ACTIONS_EC2_DEPLOY,
                customizable_sections=["image", "container_name", "region", "repository"],
            ),
            # Q4 Must-Wedge: GitOps 템플릿
            "argocd-application": FileTemplate(
                template_id="argocd-application",
                file_type="argocd-application",
                base_content=_ARGOCD_APPLICATION,
                customizable_sections=[
                    "app_name", "repo_url", "target_revision",
                    "helm_chart_path", "namespace",
                ],
            ),
            "helm-values-fargate": FileTemplate(
                template_id="helm-values-fargate",
                file_type="helm-values",
                base_content=_HELM_VALUES_FARGATE,
                customizable_sections=[
                    "app_name", "ecr_image_uri", "image_tag",
                    "container_port", "replica_count",
                    "cpu_request", "memory_request", "cpu_limit", "memory_limit",
                    "env_yaml", "health_check_path",
                ],
            ),
            # Q3-A: ECS Task Definition (JSON)
            "ecs-task-definition": FileTemplate(
                template_id="ecs-task-definition",
                file_type="ecs-task-definition",
                base_content=_ECS_TASK_DEFINITION,
                customizable_sections=[
                    "family", "cpu", "memory", "execution_role_arn",
                    "container_name", "ecr_image_uri", "container_port",
                    "env_vars_json", "aws_region", "health_check_path",
                ],
            ),
        }

    def get(self, template_id: str) -> Optional[FileTemplate]:
        """template_id로 FileTemplate 조회."""
        return self._templates.get(template_id)

    def render(self, template_id: str, overrides: dict) -> str:
        """base_content에 overrides를 적용해 최종 파일 내용 반환.

        Args:
            template_id: 템플릿 ID
            overrides: 덮어쓸 파라미터 딕셔너리
                예: {"image": "my-app:latest", "port": "9000"}

        Returns:
            렌더링된 파일 내용
        """
        template = self.get(template_id)
        if not template:
            raise ValueError(f"Unknown template_id: {template_id}")

        # base_content에서 {placeholder} 형식으로 format 적용
        content = template.base_content
        try:
            # overrides에 없는 플레이스홀더는 그대로 유지
            content = content.format_map(overrides)
        except KeyError as e:
            # 필수 파라미터 누락 시 에러 메시지 개선
            raise ValueError(
                f"Missing required override parameter for template '{template_id}': {e}"
            )

        return content

    def list_templates(self) -> list[FileTemplate]:
        """모든 FileTemplate 반환."""
        return list(self._templates.values())

    def detect_stack_template(self, workspace_path: str) -> str:
        """워크스페이스 경로를 스캔해 프로젝트 스택에 맞는 Dockerfile 템플릿 ID 반환.

        - requirements.txt 있음:
          - fastapi/uvicorn 포함 → dockerfile-python-fastapi
          - flask 포함 → dockerfile-python-flask
        - package.json 있음:
          - next 의존성 포함 → dockerfile-node-next
          - express 의존성 포함 → dockerfile-node-express

        Args:
            workspace_path: 프로젝트 루트 경로

        Returns:
            감지된 템플릿 ID

        Raises:
            ValueError: 스택을 감지하지 못했을 때
        """
        p = Path(workspace_path)

        # Python 스택 감지
        req_file = p / "requirements.txt"
        if req_file.exists():
            try:
                content = req_file.read_text(encoding='utf-8', errors='replace').lower()
                if 'fastapi' in content or 'uvicorn' in content:
                    return "dockerfile-python-fastapi"
                if 'flask' in content:
                    return "dockerfile-python-flask"
            except Exception:
                pass

        # Node 스택 감지
        pkg_file = p / "package.json"
        if pkg_file.exists():
            try:
                package = json.loads(
                    pkg_file.read_text(encoding='utf-8', errors='replace')
                )
                deps: dict[str, str] = {}
                if isinstance(package, dict):
                    for key in ('dependencies', 'devDependencies'):
                        values = package.get(key)
                        if isinstance(values, dict):
                            deps.update({str(k).lower(): str(v) for k, v in values.items()})
                if 'next' in deps:
                    return "dockerfile-node-next"
                if 'express' in deps:
                    return "dockerfile-node-express"
            except Exception:
                pass

        raise ValueError(
            "스택을 자동 감지하지 못했습니다. "
            "requirements.txt 또는 package.json이 필요합니다."
        )

    def render_ecs_task_definition(
        self,
        family: str,
        ecr_image_uri: str,
        container_name: str = "app",
        container_port: int = 8000,
        cpu: str = "256",
        memory: str = "512",
        aws_region: str = "ap-northeast-2",
        execution_role_arn: str = "",
        env_vars: list[dict] | None = None,
        health_check_path: str = "/health",
    ) -> str:
        """
        ECS Task Definition JSON 렌더링 (Q3-A Step 1).

        env_vars 형식: [{"name": "KEY", "value": "val"}, ...]
        반환: JSON 문자열 (바로 boto3 에 넘기거나 파일로 저장 가능)
        """
        env_list = env_vars or []
        env_vars_json = json.dumps(env_list, ensure_ascii=False)
        return self.render(
            "ecs-task-definition",
            {
                "family":             family,
                "cpu":                cpu,
                "memory":             memory,
                "execution_role_arn": execution_role_arn or f"arn:aws:iam::ACCOUNT_ID:role/ecsTaskExecutionRole",
                "container_name":     container_name,
                "ecr_image_uri":      ecr_image_uri,
                "container_port":     container_port,
                "env_vars_json":      env_vars_json,
                "aws_region":         aws_region,
                "health_check_path":  health_check_path,
            },
        )


# ── 싱글턴 ──────────────────────────────────────────────────────────

_instance: Optional[FileRegistry] = None


def get_file_registry() -> FileRegistry:
    global _instance
    if _instance is None:
        _instance = FileRegistry()
    return _instance


__all__ = ["FileRegistry", "get_file_registry"]
