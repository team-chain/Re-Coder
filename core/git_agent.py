"""
git_agent.py — ReCoder v6.4 Git 자동 커밋 에이전트 (§S-8)

Extension 에서 POST /api/git/commit 호출 →
  1) git add -A
  2) git commit -m {message}
  3) commit_hash 반환

API 계약:
  Request : { workspace_path: str, message: str, session_id: str }
  Response: { status: "ok"|"error", commit_hash: str, message: str }
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class GitAgent:
    """로컬 Git 저장소에 대한 자동 커밋 기능."""

    def _run(
        self,
        args: list[str],
        cwd: str,
        timeout: int = 60,
    ) -> tuple[int, str, str]:
        """git 명령 실행 헬퍼. (returncode, stdout, stderr)"""
        try:
            proc = subprocess.run(
                ["git"] + args,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", "git command timed out"
        except FileNotFoundError:
            return -1, "", "git not found in PATH"
        except Exception as e:
            return -1, "", str(e)

    def commit(
        self,
        workspace_path: str,
        message: str,
        session_id: str = "",
    ) -> dict:
        """
        git add -A && git commit -m {message} 실행.

        Args:
            workspace_path: git 저장소 루트 경로
            message: 커밋 메시지
            session_id: 로그용 세션 ID (선택)

        Returns:
            { status, commit_hash, message }
        """
        workspace = Path(workspace_path).expanduser().resolve()
        if not workspace.exists():
            return {
                "status": "error",
                "commit_hash": "",
                "message": f"경로가 존재하지 않습니다: {workspace_path}",
            }

        cwd = str(workspace)

        # 1) git add -A
        rc, stdout, stderr = self._run(["add", "-A"], cwd=cwd)
        if rc != 0:
            logger.error(f"[git_agent] git add failed: {stderr}")
            return {
                "status": "error",
                "commit_hash": "",
                "message": f"git add 실패: {stderr}",
            }

        # 2) git commit
        rc, stdout, stderr = self._run(["commit", "-m", message], cwd=cwd)
        if rc != 0:
            # "nothing to commit" 은 에러가 아니라 경고로 처리
            if "nothing to commit" in stderr or "nothing to commit" in stdout:
                logger.info("[git_agent] nothing to commit")
                return {
                    "status": "ok",
                    "commit_hash": "",
                    "message": "커밋할 변경사항이 없습니다.",
                }
            logger.error(f"[git_agent] git commit failed: {stderr}")
            return {
                "status": "error",
                "commit_hash": "",
                "message": f"git commit 실패: {stderr}",
            }

        # 3) 최신 커밋 해시 조회
        rc2, hash_out, _ = self._run(["rev-parse", "HEAD"], cwd=cwd)
        commit_hash = hash_out if rc2 == 0 else ""

        logger.info(f"[git_agent] committed: {commit_hash[:8] if commit_hash else '?'} | {message[:60]}")
        return {
            "status": "ok",
            "commit_hash": commit_hash,
            "message": f"커밋 완료: {commit_hash[:8] if commit_hash else '?'}",
        }


# ── 싱글턴 ────────────────────────────────────────────────────────────

_instance: Optional[GitAgent] = None


def get_git_agent() -> GitAgent:
    """GitAgent 싱글턴 반환."""
    global _instance
    if _instance is None:
        _instance = GitAgent()
    return _instance


__all__ = ["GitAgent", "get_git_agent"]
