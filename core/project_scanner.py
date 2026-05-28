"""
Project Scanner (설계서 v6.4 §20.1)
워크스페이스를 스캔해 ProjectProfile을 자동 감지한다.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from schemas import ProjectProfile, ProjectStack

RECODER_HOME = Path.home() / ".recoder"


class ProjectScanner:
    def scan(self, workspace_path: str) -> ProjectProfile:
        """
        워크스페이스 스캔:
        1. stack 감지: requirements.txt → python, package.json → node
           - requirements.txt + fastapi → python-fastapi
           - requirements.txt + flask → python-flask
           - package.json + next → node-next
           - package.json → node-express
           - 없으면 custom
        2. package_manager 감지: poetry.lock→poetry, Pipfile→pipenv, requirements.txt→pip,
           yarn.lock→yarn, pnpm-lock.yaml→pnpm, package-lock.json→npm
        3. default_run_command 추정:
           - python-fastapi: "uvicorn main:app --reload"
           - python-flask: "python app.py"
           - node-express: "node index.js"
           - node-next: "npm run dev"
        4. default_port 추정: fastapi→8000, flask→5000, node→3000
        5. dockerfile_path: Dockerfile 존재 시 경로
        6. compose_path: docker-compose.yml 또는 docker-compose.yaml 존재 시 경로
        7. project_id: workspace_path의 SHA256 앞 16자리
        결과를 ~/.recoder/projects/{project_id}.json에 저장.
        """
        workspace = Path(workspace_path)

        # project_id 생성: workspace_path의 SHA256 앞 16자리
        path_hash = hashlib.sha256(str(workspace.absolute()).encode()).hexdigest()[:16]

        # Stack 감지
        stack = self._detect_stack(workspace)

        # Package Manager 감지
        package_manager = self._detect_package_manager(workspace, stack)

        # Default Run Command 추정
        default_run_command = self._detect_run_command(workspace, stack)

        # Default Port 추정
        default_port = self._detect_port(stack)

        # Dockerfile 경로 감지
        dockerfile_path = ""
        if (workspace / "Dockerfile").exists():
            dockerfile_path = str(workspace / "Dockerfile")

        # docker-compose 경로 감지
        compose_path = ""
        if (workspace / "docker-compose.yml").exists():
            compose_path = str(workspace / "docker-compose.yml")
        elif (workspace / "docker-compose.yaml").exists():
            compose_path = str(workspace / "docker-compose.yaml")

        # 현재 시간
        now = datetime.now(timezone.utc).isoformat()

        # ProjectProfile 생성
        profile = ProjectProfile(
            project_id=path_hash,
            workspace_path=str(workspace.absolute()),
            stack=stack,
            package_manager=package_manager,
            default_run_command=default_run_command,
            default_port=default_port,
            health_check_path="/health",
            dockerfile_path=dockerfile_path,
            compose_path=compose_path,
            deployment_target="local_docker",
            created_at=now,
            updated_at=now,
        )

        # 저장
        self.save(profile)

        return profile

    def load(self, project_id: str) -> Optional[ProjectProfile]:
        """저장된 ProjectProfile 로드"""
        profile_path = RECODER_HOME / "projects" / f"{project_id}.json"

        if not profile_path.exists():
            return None

        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            return ProjectProfile(
                project_id=data.get("project_id", ""),
                workspace_path=data.get("workspace_path", ""),
                stack=ProjectStack(data.get("stack", "custom")),
                package_manager=data.get("package_manager", ""),
                default_run_command=data.get("default_run_command", ""),
                default_port=data.get("default_port", 3000),
                health_check_path=data.get("health_check_path", "/health"),
                dockerfile_path=data.get("dockerfile_path", ""),
                compose_path=data.get("compose_path", ""),
                deployment_target=data.get("deployment_target", "local"),
                created_at=data.get("created_at", ""),
                updated_at=data.get("updated_at", ""),
            )
        except Exception:
            return None

    def save(self, profile: ProjectProfile) -> None:
        """ProjectProfile 저장"""
        projects_dir = RECODER_HOME / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)

        profile_path = projects_dir / f"{profile.project_id}.json"

        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile.to_dict(), f, indent=2)

    def _detect_stack(self, workspace: Path) -> ProjectStack:
        """워크스페이스의 stack 감지"""
        has_requirements = (workspace / "requirements.txt").exists()
        has_package_json = (workspace / "package.json").exists()

        if has_requirements:
            # requirements.txt 내용 확인
            try:
                content = (workspace / "requirements.txt").read_text(encoding="utf-8")
                if "fastapi" in content.lower():
                    return ProjectStack.PYTHON_FASTAPI
                elif "flask" in content.lower():
                    return ProjectStack.PYTHON_FLASK
                else:
                    return ProjectStack.CUSTOM
            except Exception:
                pass

            return ProjectStack.CUSTOM

        if has_package_json:
            # package.json 내용 확인
            try:
                content = (workspace / "package.json").read_text(encoding="utf-8")
                if "next" in content.lower():
                    return ProjectStack.NODE_NEXT
                else:
                    return ProjectStack.NODE_EXPRESS
            except Exception:
                pass

            return ProjectStack.NODE_EXPRESS

        return ProjectStack.CUSTOM

    def _detect_package_manager(self, workspace: Path, stack: ProjectStack) -> str:
        """패키지 매니저 감지"""
        # Python 스택
        if stack in [ProjectStack.PYTHON_FASTAPI, ProjectStack.PYTHON_FLASK]:
            if (workspace / "poetry.lock").exists():
                return "poetry"
            elif (workspace / "Pipfile").exists():
                return "pipenv"
            elif (workspace / "requirements.txt").exists():
                return "pip"
            return "pip"

        # Node 스택
        if stack in [ProjectStack.NODE_EXPRESS, ProjectStack.NODE_NEXT]:
            if (workspace / "yarn.lock").exists():
                return "yarn"
            elif (workspace / "pnpm-lock.yaml").exists():
                return "pnpm"
            elif (workspace / "package-lock.json").exists():
                return "npm"
            return "npm"

        return ""

    def _detect_run_command(self, workspace: Path, stack: ProjectStack) -> str:
        """default_run_command 추정"""
        if stack == ProjectStack.PYTHON_FASTAPI:
            return "uvicorn main:app --reload"
        elif stack == ProjectStack.PYTHON_FLASK:
            return "python app.py"
        elif stack == ProjectStack.NODE_EXPRESS:
            return "node index.js"
        elif stack == ProjectStack.NODE_NEXT:
            return "npm run dev"
        else:
            return ""

    def _detect_port(self, stack: ProjectStack) -> int:
        """default_port 추정"""
        if stack == ProjectStack.PYTHON_FASTAPI:
            return 8000
        elif stack == ProjectStack.PYTHON_FLASK:
            return 5000
        elif stack in [ProjectStack.NODE_EXPRESS, ProjectStack.NODE_NEXT]:
            return 3000
        else:
            return 3000


_scanner_instance: Optional[ProjectScanner] = None


def get_project_scanner() -> ProjectScanner:
    """싱글톤 인스턴스 반환"""
    global _scanner_instance
    if _scanner_instance is None:
        _scanner_instance = ProjectScanner()
    return _scanner_instance
