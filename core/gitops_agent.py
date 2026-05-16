"""
gitops_agent.py — GitOps ArgoCD 연동 에이전트 (설계서 §Q4 Must-Wedge)

설계서 Q4 GitOps 흐름:
  1. FileTemplate Registry 기반으로 argocd-application.yaml / helm/values.yaml 생성
  2. 지정 Git 저장소에 커밋 & PR 생성
  3. 사용자가 PR 머지 시 ArgoCD 자동 sync
  4. ArgoCD API 폴링 → Sidebar 에 sync 상태 표시

ADR-005 (Production GitOps rollback):
  - staging/dev: ArgoCD API rollback 허용
  - production: Git revert PR 생성 (기본)
  - Severity 1: emergency rollback 허용 + 30분 이내 Git reconciliation PR 필수

환경변수:
  ARGOCD_URL    — ArgoCD API server URL (예: https://argocd.example.com)
  ARGOCD_TOKEN  — ArgoCD API bearer token
  GITHUB_TOKEN  — GitHub Personal Access Token (PR 생성용)
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── ArgoCD API 설정 ───────────────────────────────────────────────────
_ARGOCD_TIMEOUT  = 10   # 초
_SYNC_POLL_MAX   = 60   # 최대 폴링 횟수
_SYNC_POLL_SLEEP = 10   # 폴링 간격(초)


# ── 데이터 타입 ───────────────────────────────────────────────────────

@dataclass
class GitOpsConfig:
    """GitOps 배포 설정."""
    app_name:        str          # ArgoCD Application 이름
    repo_url:        str          # Git 저장소 URL (GitHub HTTPS)
    helm_chart_path: str = "helm" # Helm chart 디렉터리
    namespace:       str = "default"
    target_revision: str = "main"
    argocd_url:      str = ""     # 없으면 환경변수 ARGOCD_URL
    argocd_token:    str = ""     # 없으면 환경변수 ARGOCD_TOKEN
    github_token:    str = ""     # 없으면 환경변수 GITHUB_TOKEN

    def __post_init__(self):
        if not self.argocd_url:
            self.argocd_url = os.getenv("ARGOCD_URL", "").rstrip("/")
        if not self.argocd_token:
            self.argocd_token = os.getenv("ARGOCD_TOKEN", "")
        if not self.github_token:
            self.github_token = os.getenv("GITHUB_TOKEN", "")


@dataclass
class GitOpsShipPayload:
    """GitOps Ship 요청 페이로드."""
    config:         GitOpsConfig
    ecr_image_uri:  str           # 배포할 이미지 URI
    image_tag:      str           # 이미지 태그 (git SHA 권장)
    container_port: int  = 8000
    replica_count:  int  = 2
    cpu_request:    str  = "128m"
    memory_request: str  = "256Mi"
    cpu_limit:      str  = "512m"
    memory_limit:   str  = "512Mi"
    health_check_path: str = "/health"
    env_vars:       list[dict] = field(default_factory=list)  # [{"name":k,"value":v}]
    environment:    str  = "staging"   # staging | production


@dataclass
class GitOpsResult:
    """GitOps 배포 결과."""
    success:         bool
    pr_url:          str  = ""
    pr_number:       int  = 0
    argocd_app_name: str  = ""
    sync_status:     str  = ""   # Synced | OutOfSync | Unknown
    health_status:   str  = ""   # Healthy | Degraded | Progressing | Unknown
    committed_files: list[str] = field(default_factory=list)
    error:           str  = ""
    logs:            list[str] = field(default_factory=list)


# ── ArgoCD API 클라이언트 ─────────────────────────────────────────────

class ArgoCDClient:
    """ArgoCD REST API 클라이언트 (read + sync 전용)."""

    def __init__(self, url: str, token: str):
        self._url   = url.rstrip("/")
        self._token = token

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _get(self, path: str) -> Optional[dict]:
        try:
            import urllib.request, json as _json
            req = urllib.request.Request(
                f"{self._url}{path}", headers=self._headers()
            )
            with urllib.request.urlopen(req, timeout=_ARGOCD_TIMEOUT) as resp:
                return _json.loads(resp.read().decode())
        except Exception as e:
            logger.warning(f"[argocd] GET {path} 실패: {e}")
            return None

    def _post(self, path: str, body: dict) -> Optional[dict]:
        try:
            import urllib.request, json as _json
            data = _json.dumps(body).encode()
            req  = urllib.request.Request(
                f"{self._url}{path}", data=data,
                headers=self._headers(), method="POST"
            )
            with urllib.request.urlopen(req, timeout=_ARGOCD_TIMEOUT) as resp:
                return _json.loads(resp.read().decode())
        except Exception as e:
            logger.warning(f"[argocd] POST {path} 실패: {e}")
            return None

    def is_available(self) -> bool:
        if not self._url:
            return False
        result = self._get("/api/v1/version")
        return result is not None

    def get_app_status(self, app_name: str) -> dict:
        """
        ArgoCD Application 상태 조회.
        반환: { sync_status, health_status, message }
        """
        result = self._get(f"/api/v1/applications/{app_name}")
        if not result:
            return {"sync_status": "Unknown", "health_status": "Unknown", "message": "ArgoCD 미연결"}

        status = result.get("status", {})
        return {
            "sync_status":   status.get("sync",   {}).get("status",  "Unknown"),
            "health_status": status.get("health", {}).get("status",  "Unknown"),
            "message":       status.get("health", {}).get("message", ""),
            "revision":      status.get("sync",   {}).get("revision", ""),
        }

    def sync_app(self, app_name: str) -> bool:
        """ArgoCD Application sync 트리거 (staging/dev 전용 — ADR-005)."""
        result = self._post(f"/api/v1/applications/{app_name}/sync", {})
        return result is not None

    def rollback_app(self, app_name: str, revision: str) -> bool:
        """
        ArgoCD API rollback (staging/dev 전용 — ADR-005).
        production 은 Git revert PR 방식 사용 (rollback_pr_agent 참조).
        """
        result = self._post(
            f"/api/v1/applications/{app_name}/rollback",
            {"revision": revision},
        )
        return result is not None


# ── GitOps 에이전트 ───────────────────────────────────────────────────

class GitOpsAgent:
    """
    Q4 Must-Wedge: GitOps ArgoCD 배포 에이전트.

    - FileTemplate Registry 기반으로 YAML 파일 생성
    - GitHub PR 생성 (github_agent 활용)
    - ArgoCD 상태 폴링
    """

    def _generate_argocd_yaml(self, payload: GitOpsShipPayload) -> str:
        """argocd-application.yaml 렌더링."""
        from registries.file_registry import get_file_registry
        return get_file_registry().render(
            "argocd-application",
            {
                "app_name":        payload.config.app_name,
                "repo_url":        payload.config.repo_url,
                "target_revision": payload.config.target_revision,
                "helm_chart_path": payload.config.helm_chart_path,
                "namespace":       payload.config.namespace,
            },
        )

    def _generate_helm_values(self, payload: GitOpsShipPayload) -> str:
        """helm/values.yaml 렌더링."""
        import yaml as _yaml  # pyyaml 없으면 json 방식 폴백
        from registries.file_registry import get_file_registry

        # env_vars → YAML 블록 문자열 변환
        env_list = payload.env_vars or []
        try:
            env_yaml = _yaml.dump(env_list, default_flow_style=False).strip() if env_list else "[]"
        except Exception:
            env_yaml = str(env_list)

        return get_file_registry().render(
            "helm-values-fargate",
            {
                "app_name":          payload.config.app_name,
                "ecr_image_uri":     payload.ecr_image_uri,
                "image_tag":         payload.image_tag,
                "container_port":    payload.container_port,
                "replica_count":     payload.replica_count,
                "cpu_request":       payload.cpu_request,
                "memory_request":    payload.memory_request,
                "cpu_limit":         payload.cpu_limit,
                "memory_limit":      payload.memory_limit,
                "env_yaml":          env_yaml,
                "health_check_path": payload.health_check_path,
            },
        )

    def _commit_files(
        self,
        workspace_path: str,
        files: dict[str, str],  # {relative_path: content}
        commit_message: str,
        github_token: str,
        repo_url: str,
        branch: str,
    ) -> tuple[bool, str]:
        """
        파일을 로컬에 쓰고 git commit + push.
        반환: (success, error_message)
        """
        import subprocess
        ws = Path(workspace_path)
        try:
            for rel_path, content in files.items():
                target = ws / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            # git add
            r = subprocess.run(["git", "add"] + list(files.keys()),
                                cwd=str(ws), capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return False, f"git add 실패: {r.stderr}"

            # git commit
            r = subprocess.run(["git", "commit", "-m", commit_message],
                                cwd=str(ws), capture_output=True, text=True, timeout=30)
            if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
                return False, f"git commit 실패: {r.stderr}"

            # git push
            env = os.environ.copy()
            if github_token:
                # URL에 토큰 삽입 (HTTPS 방식)
                push_url = repo_url.replace("https://", f"https://{github_token}@")
                r = subprocess.run(
                    ["git", "push", push_url, f"HEAD:{branch}"],
                    cwd=str(ws), capture_output=True, text=True, timeout=60, env=env,
                )
            else:
                r = subprocess.run(
                    ["git", "push", "origin", branch],
                    cwd=str(ws), capture_output=True, text=True, timeout=60, env=env,
                )
            if r.returncode != 0:
                return False, f"git push 실패: {r.stderr}"

            return True, ""
        except Exception as e:
            return False, str(e)

    def _create_pr(
        self,
        repo_url: str,
        github_token: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> tuple[bool, str, int]:
        """
        GitHub PR 생성.
        반환: (success, pr_url, pr_number)
        """
        try:
            from github_agent import get_github_agent
            gh = get_github_agent()
            if github_token:
                gh.set_token(github_token)

            # repo_url → owner/repo 추출
            # https://github.com/owner/repo.git → owner/repo
            parts = repo_url.rstrip("/").rstrip(".git").split("/")
            repo_full = "/".join(parts[-2:])

            result = gh.create_pr(
                repo=repo_full,
                title=title,
                body=body,
                head=head_branch,
                base=base_branch,
            )
            if result.get("status") == "ok":
                return True, result.get("pr_url", ""), result.get("pr_number", 0)
            return False, result.get("message", "PR 생성 실패"), 0
        except Exception as e:
            logger.warning(f"[gitops] PR 생성 실패: {e}")
            return False, str(e), 0

    def _poll_argocd(
        self,
        client: ArgoCDClient,
        app_name: str,
        log_fn=None,
    ) -> tuple[str, str]:
        """
        ArgoCD sync 상태를 최대 10분 폴링.
        반환: (sync_status, health_status)
        """
        def _log(msg):
            logger.info(msg)
            if log_fn: log_fn(msg)

        for i in range(_SYNC_POLL_MAX):
            time.sleep(_SYNC_POLL_SLEEP)
            status = client.get_app_status(app_name)
            sync   = status["sync_status"]
            health = status["health_status"]
            _log(f"[ArgoCD] {app_name} — sync={sync} health={health}")
            if sync == "Synced" and health == "Healthy":
                return sync, health
            if health == "Degraded":
                return sync, health

        return "Unknown", "Unknown"

    # ── 전체 Ship 파이프라인 ────────────────────────────────────────────

    def ship(
        self,
        workspace_path: str,
        payload: GitOpsShipPayload,
        log_fn=None,
    ) -> GitOpsResult:
        """
        Q4 GitOps Ship 파이프라인.

        1. argocd-application.yaml + helm/values.yaml 생성
        2. feature 브랜치에 커밋 & push
        3. PR 생성 (base=main)
        4. ArgoCD API 폴링 (PR 머지 후 자동 sync 대기)
        """
        logs: list[str] = []
        committed_files: list[str] = []
        config = payload.config

        def _log(msg: str) -> None:
            logs.append(msg)
            logger.info(msg)
            if log_fn: log_fn(msg)

        _log(f"[GitOps] Ship 시작: {config.app_name} / {payload.image_tag}")

        # Step 1: 파일 생성
        _log("[GitOps] argocd-application.yaml + helm/values.yaml 생성 중...")
        try:
            argocd_yaml  = self._generate_argocd_yaml(payload)
            helm_values  = self._generate_helm_values(payload)
        except Exception as e:
            return GitOpsResult(success=False, error=f"파일 생성 실패: {e}", logs=logs)

        files = {
            "argocd/argocd-application.yaml": argocd_yaml,
            f"{config.helm_chart_path}/values.yaml": helm_values,
        }
        committed_files = list(files.keys())

        # Step 2: feature 브랜치 생성 및 커밋
        deploy_branch = f"deploy/{config.app_name}-{payload.image_tag[:8]}"
        commit_msg    = f"deploy: {config.app_name} → {payload.image_tag}"
        _log(f"[GitOps] 브랜치 {deploy_branch} 에 커밋 중...")

        # 브랜치 생성
        try:
            import subprocess
            subprocess.run(
                ["git", "checkout", "-b", deploy_branch],
                cwd=workspace_path, capture_output=True, text=True, timeout=15,
            )
        except Exception as e:
            _log(f"[GitOps] 브랜치 생성 경고: {e}")

        ok, err = self._commit_files(
            workspace_path, files, commit_msg,
            config.github_token, config.repo_url, deploy_branch,
        )
        if not ok:
            return GitOpsResult(
                success=False, error=f"커밋/push 실패: {err}",
                committed_files=committed_files, logs=logs,
            )
        _log("[GitOps] 커밋 & push 완료")

        # Step 3: PR 생성
        _log("[GitOps] GitHub PR 생성 중...")
        pr_body = (
            f"## Deploy: {config.app_name}\n\n"
            f"- **Image**: `{payload.ecr_image_uri}:{payload.image_tag}`\n"
            f"- **Environment**: `{payload.environment}`\n"
            f"- **Namespace**: `{config.namespace}`\n"
            f"- **Generated by**: ReCoder GitOps Agent\n\n"
            f"ArgoCD가 PR 머지 후 자동으로 sync합니다.\n"
        )
        pr_ok, pr_url, pr_number = self._create_pr(
            repo_url=config.repo_url,
            github_token=config.github_token,
            head_branch=deploy_branch,
            base_branch=config.target_revision,
            title=f"deploy: {config.app_name} → {payload.image_tag[:12]}",
            body=pr_body,
        )
        if pr_ok:
            _log(f"[GitOps] PR 생성 완료: {pr_url}")
        else:
            _log(f"[GitOps] PR 생성 실패 (계속 진행): {pr_url}")

        # Step 4: ArgoCD 폴링 (서버 있을 때만)
        sync_status = health_status = "Unknown"
        argocd_client = ArgoCDClient(config.argocd_url, config.argocd_token)
        if argocd_client.is_available():
            _log("[GitOps] ArgoCD 상태 폴링 시작 (PR 머지 후 자동 sync 대기)...")
            sync_status, health_status = self._poll_argocd(argocd_client, config.app_name, _log)
            _log(f"[GitOps] ArgoCD 최종: sync={sync_status} health={health_status}")
        else:
            _log("[GitOps] ArgoCD 미연결 — PR 머지 후 ArgoCD가 자동 sync합니다.")
            sync_status = "PendingMerge"

        _log("[GitOps] Ship 완료")
        return GitOpsResult(
            success=True,
            pr_url=pr_url,
            pr_number=pr_number,
            argocd_app_name=config.app_name,
            sync_status=sync_status,
            health_status=health_status,
            committed_files=committed_files,
            logs=logs,
        )

    def get_app_status(self, config: GitOpsConfig) -> dict:
        """ArgoCD Application 상태 단독 조회 (Sidebar 폴링용)."""
        client = ArgoCDClient(config.argocd_url, config.argocd_token)
        if not client.is_available():
            return {
                "available": False,
                "sync_status": "Unknown",
                "health_status": "Unknown",
                "note": "ArgoCD 미연결",
            }
        status = client.get_app_status(config.app_name)
        status["available"] = True
        return status

    def rollback_staging(self, config: GitOpsConfig, revision: str) -> dict:
        """
        staging/dev ArgoCD API rollback (ADR-005).
        production 은 rollback_pr_agent 를 사용.
        """
        client = ArgoCDClient(config.argocd_url, config.argocd_token)
        if not client.is_available():
            return {"success": False, "error": "ArgoCD 미연결"}
        ok = client.rollback_app(config.app_name, revision)
        return {"success": ok, "error": "" if ok else "ArgoCD rollback API 실패"}


# ── 싱글턴 ────────────────────────────────────────────────────────────

_instance: Optional[GitOpsAgent] = None


def get_gitops_agent() -> GitOpsAgent:
    global _instance
    if _instance is None:
        _instance = GitOpsAgent()
    return _instance


__all__ = [
    "GitOpsAgent", "GitOpsConfig", "GitOpsShipPayload",
    "GitOpsResult", "ArgoCDClient", "get_gitops_agent",
]
