"""
git_agent.py — ReCoder v6.4 Git 에이전트 (§S-8, §GUI-Git)

Extension 에서 호출 가능한 로컬 Git 작업 모음:
  - commit     : git add -A + git commit
  - info       : 현재 브랜치, remote URL, 변경 파일 수 조회
  - branches   : 전체 브랜치 목록 + 현재 브랜치
  - checkout   : 브랜치 전환
  - branch_create : 새 브랜치 생성 (+ checkout)
  - push       : 원격 push (upstream 자동 설정 포함)
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 원격 접근 가능 여부 캐시: {remote_url: (timestamp, is_reachable)}
_remote_reachable_cache: dict[str, tuple[float, bool]] = {}
_REMOTE_CACHE_TTL = 60.0  # 60초마다 재확인


class GitAgent:
    """로컬 Git 저장소에 대한 자동 커밋 기능."""

    @staticmethod
    def _is_submodule_error(stderr: str) -> bool:
        """서브모듈 관련 git 오류인지 확인."""
        low = stderr.lower()
        return "is in submodule" in low or "isinsubmodule" in low

    def _check_remote_reachable(self, cwd: str, remote_url: str, force: bool = False) -> bool:
        """
        원격 저장소가 실제로 존재하는지 확인 (캐시 60초).
        git ls-remote --exit-code origin HEAD 로 빠르게 체크.

        Args:
            force: True 이면 캐시를 무시하고 즉시 재확인.
        """
        global _remote_reachable_cache
        now = time.time()
        if not force:
            cached = _remote_reachable_cache.get(remote_url)
            if cached and (now - cached[0]) < _REMOTE_CACHE_TTL:
                return cached[1]

        rc, _, err = self._run(
            ["ls-remote", "--exit-code", "origin", "HEAD"],
            cwd=cwd, timeout=8,
        )
        # rc=0: 접근 성공 / rc=2: 원격에 ref 없음(빈 repo) → 존재는 함
        # rc=128: 저장소 없음 / 인증 실패 / 네트워크 에러
        reachable = rc in (0, 2)
        _remote_reachable_cache[remote_url] = (now, reachable)
        logger.debug(f"[git_agent] remote reachable={reachable} for {remote_url!r} (rc={rc}, force={force})")
        return reachable

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

        # 0) 보호 브랜치 차단 (main/master 직접 커밋 금지)
        rc0, branch, _ = self._run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        if rc0 == 0 and branch in ("main", "master"):
            logger.warning(f"[git_agent] blocked commit to protected branch: {branch}")
            return {
                "status": "error",
                "commit_hash": "",
                "message": f"보호 브랜치({branch})에는 직접 커밋할 수 없습니다.",
            }

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

    # ── 저장소 정보 ────────────────────────────────────────────────────

    def info(self, workspace_path: str, force_refresh: bool = False) -> dict:
        """
        현재 저장소 상태 조회.

        Args:
            force_refresh: True 이면 원격 접근 가능 여부 캐시를 무시하고 즉시 재확인.

        Returns:
            {
              status, branch, remote_url, has_remote,
              uncommitted, ahead, behind,
              gh_user,        # gh CLI 사용자 (없으면 "")
              is_git_repo,
            }
        """
        workspace = Path(workspace_path).expanduser().resolve() if workspace_path else None
        if not workspace or not workspace.exists():
            return {"status": "error", "message": "경로 없음", "is_git_repo": False}

        cwd = str(workspace)

        # git 저장소 여부
        rc, _, stderr = self._run(["rev-parse", "--git-dir"], cwd=cwd)
        if rc != 0:
            if self._is_submodule_error(stderr):
                # 서브모듈 내부에서 실행된 경우 — 조용히 처리
                logger.debug(f"[git_agent] submodule workspace detected, skipping: {workspace_path}")
                return {
                    "status": "ok", "is_git_repo": False, "branch": "", "remote_url": "",
                    "has_remote": False, "uncommitted": 0, "ahead": 0, "behind": 0,
                    "gh_user": "", "submodule_warning": True,
                }
            return {
                "status": "ok", "is_git_repo": False, "branch": "", "remote_url": "",
                "has_remote": False, "uncommitted": 0, "ahead": 0, "behind": 0, "gh_user": "",
            }

        # 현재 브랜치
        _, branch, _ = self._run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        branch = branch.strip() or "HEAD"

        # remote URL
        _, remote_url, _ = self._run(["remote", "get-url", "origin"], cwd=cwd)
        remote_url = remote_url.strip()
        has_remote = bool(remote_url)

        # 변경 파일 수 (uncommitted) — 서브모듈 오류는 무시
        _, status_out, status_err = self._run(["status", "--porcelain"], cwd=cwd)
        if self._is_submodule_error(status_err):
            logger.debug(f"[git_agent] submodule error in git status — suppressed")
        uncommitted = len([l for l in status_out.splitlines() if l.strip()])

        # 원격 저장소 존재 여부 검증 (캐시 60초 / force_refresh 시 즉시 재확인)
        if has_remote:
            if not self._check_remote_reachable(cwd, remote_url, force=force_refresh):
                logger.info(f"[git_agent] remote unreachable (deleted?): {remote_url!r}")
                remote_url = ""
                has_remote = False

        # ahead / behind (remote 있을 때만)
        ahead = behind = 0
        if has_remote:
            _, ab_out, ab_err = self._run(
                ["rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"],
                cwd=cwd, timeout=10,
            )
            if self._is_submodule_error(ab_err):
                logger.debug("[git_agent] submodule error in rev-list — suppressed")
            else:
                parts = ab_out.strip().split()
                if len(parts) == 2:
                    try:
                        behind, ahead = int(parts[0]), int(parts[1])
                    except ValueError:
                        pass

        return {
            "status": "ok",
            "is_git_repo": True,
            "branch": branch,
            "remote_url": remote_url,
            "has_remote": has_remote,
            "uncommitted": uncommitted,
            "ahead": ahead,
            "behind": behind,
            "gh_user": "",   # gh 인증 정보는 gh_status 엔드포인트에서만 관리
        }

    def branches(self, workspace_path: str) -> dict:
        """
        전체 브랜치 목록 반환.

        Returns: { status, branches: [str], current: str, remote_branches: [str] }
        """
        workspace = Path(workspace_path).expanduser().resolve() if workspace_path else None
        if not workspace or not workspace.exists():
            return {"status": "error", "message": "경로 없음", "branches": [], "current": "", "remote_branches": []}

        cwd = str(workspace)

        # 로컬 브랜치
        _, out, _ = self._run(["branch", "--format=%(refname:short)"], cwd=cwd)
        local_branches = [b.strip() for b in out.splitlines() if b.strip()]

        # 현재 브랜치
        _, current, _ = self._run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        current = current.strip()

        # 원격 브랜치 — fetch 먼저 실행해 최신 ref 동기화 (실패해도 무시)
        self._run(["fetch", "--prune", "-q"], cwd=cwd, timeout=20)
        _, rout, _ = self._run(["branch", "-r", "--format=%(refname:short)"], cwd=cwd)
        remote_branches = [
            b.strip().replace("origin/", "")
            for b in rout.splitlines()
            if b.strip() and "HEAD" not in b
        ]

        return {
            "status": "ok",
            "branches": local_branches,
            "current": current,
            "remote_branches": remote_branches,
        }

    def checkout(self, workspace_path: str, branch: str) -> dict:
        """브랜치 전환. Returns: { status, branch, message }"""
        workspace = Path(workspace_path).expanduser().resolve()
        if not workspace.exists():
            return {"status": "error", "message": f"경로 없음: {workspace_path}"}

        cwd = str(workspace)
        rc, out, err = self._run(["checkout", branch], cwd=cwd)
        if rc != 0:
            return {"status": "error", "message": (err or out)[:400]}

        _, current, _ = self._run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        logger.info(f"[git_agent] checkout → {current.strip()}")
        return {"status": "ok", "branch": current.strip(), "message": f"브랜치 전환: {current.strip()}"}

    def branch_create(self, workspace_path: str, branch_name: str, checkout: bool = True) -> dict:
        """
        새 브랜치 생성 (선택적으로 전환).
        Returns: { status, branch, message }
        """
        workspace = Path(workspace_path).expanduser().resolve()
        if not workspace.exists():
            return {"status": "error", "message": f"경로 없음: {workspace_path}"}

        # 브랜치명 안전 검사
        safe = re.sub(r"[^\w\-/.]", "-", branch_name).strip("-")
        if not safe:
            return {"status": "error", "message": "올바른 브랜치 이름을 입력해주세요"}

        cwd = str(workspace)
        args = ["checkout", "-b", safe] if checkout else ["branch", safe]
        rc, out, err = self._run(args, cwd=cwd)
        if rc != 0:
            return {"status": "error", "message": (err or out)[:400]}

        logger.info(f"[git_agent] branch_create → {safe}")
        return {"status": "ok", "branch": safe, "message": f"브랜치 생성{'·전환' if checkout else ''}: {safe}"}

    def set_remote(self, workspace_path: str, repo_full_name: str) -> dict:
        """
        원격 저장소 URL 변경 (git remote set-url origin / git remote add origin).

        Args:
            workspace_path: 로컬 git 저장소 경로
            repo_full_name: "owner/repo" 형식 (예: LDK511/my-app)

        Returns:
            { status, remote_url, message }
        """
        workspace = Path(workspace_path).expanduser().resolve()
        if not workspace.exists():
            return {"status": "error", "message": f"경로 없음: {workspace_path}"}

        cwd = str(workspace)
        remote_url = f"https://github.com/{repo_full_name}.git"

        # origin 이 이미 있는지 확인
        rc_check, existing, _ = self._run(["remote", "get-url", "origin"], cwd=cwd)
        if rc_check == 0:
            # 이미 있으면 set-url
            rc, _, err = self._run(["remote", "set-url", "origin", remote_url], cwd=cwd)
        else:
            # 없으면 add
            rc, _, err = self._run(["remote", "add", "origin", remote_url], cwd=cwd)

        if rc != 0:
            return {"status": "error", "message": err[:300], "remote_url": ""}

        # 캐시 무효화 (변경된 remote URL 즉시 재확인)
        global _remote_reachable_cache
        _remote_reachable_cache.pop(existing.strip(), None)

        logger.info(f"[git_agent] remote origin → {remote_url}")
        return {
            "status": "ok",
            "remote_url": remote_url,
            "message": f"원격 저장소 변경 완료: {repo_full_name}",
        }

    def push(self, workspace_path: str, branch: str = "", force: bool = False) -> dict:
        """
        원격 push. upstream 없으면 --set-upstream origin <branch> 로 자동 설정.
        Returns: { status, message, remote_url }
        """
        workspace = Path(workspace_path).expanduser().resolve()
        if not workspace.exists():
            return {"status": "error", "message": f"경로 없음: {workspace_path}"}

        cwd = str(workspace)

        # 현재 브랜치 확인
        _, cur_branch, _ = self._run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        target = (branch.strip() or cur_branch.strip()) or "HEAD"

        # remote 있는지 확인
        _, remote_url, _ = self._run(["remote", "get-url", "origin"], cwd=cwd)
        if not remote_url.strip():
            return {"status": "no_remote", "message": "원격 저장소(origin)가 설정되지 않았습니다. GitHub 새 repo를 먼저 생성해주세요.", "remote_url": ""}

        # push 실행 (upstream 자동 설정)
        push_args = ["push", "--set-upstream", "origin", target]
        if force:
            push_args.append("--force-with-lease")

        rc, out, err = self._run(push_args, cwd=cwd, timeout=60)
        combined = (out + "\n" + err).strip()

        if rc != 0:
            logger.error(f"[git_agent] push failed: {combined[:300]}")
            return {"status": "error", "message": combined[:400], "remote_url": remote_url.strip()}

        logger.info(f"[git_agent] pushed {target} → origin")
        return {"status": "ok", "message": f"push 완료 ({target} → origin)", "remote_url": remote_url.strip()}


# ── 싱글턴 ────────────────────────────────────────────────────────────

_instance: Optional[GitAgent] = None


def get_git_agent() -> GitAgent:
    """GitAgent 싱글턴 반환."""
    global _instance
    if _instance is None:
        _instance = GitAgent()
    return _instance


__all__ = ["GitAgent", "get_git_agent"]
