"""
github_agent.py — ReCoder GitHub 통합 에이전트

설계 §3.2.1 (VSCode OAuth + Local Core token relay), §5.2 (Approval Level 4),
§6.1 (Discord ChatOps), Appendix A.4 (InfraFileProposal github-actions).

주 기능:
  - VS Code OAuth 토큰 저장/검증
  - GitHub REST API 호출 (urllib 기반, 외부 의존성 최소화)
  - git subprocess wrapper (._git, ._ensure_git_init)
  - repo 생성 + push, secrets 등록 (libsodium 암호화), workflow runs 조회
  - PR 생성 (gitops_agent 에서 호출)

설계 원칙:
  - GitHub 토큰은 ~/.recoder/github.token 에 0600 권한으로 저장 (Windows: ACL)
  - 토큰이 없으면 status() 가 unauthenticated 반환, 다른 메서드는 명시적 에러
  - 캐시: status() 5분 / list_repos() 60초
  - 모든 메서드는 dict 를 반환 ({status: "ok"|"error", ...})
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── 상수 ─────────────────────────────────────────────────────────────

_GITHUB_API = "https://api.github.com"
_DEFAULT_TIMEOUT = 15.0
_USER_AGENT = "ReCoder/1.0"

_TOKEN_PATH = Path.home() / ".recoder" / "github.token"
_STATUS_TTL_SECONDS = 300  # 5분
_REPOS_TTL_SECONDS = 60    # 1분


# ── 헬퍼 ─────────────────────────────────────────────────────────────

def _redact(s: str) -> str:
    """로그 안전 토큰 마스킹."""
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "*" * (len(s) - 8) + s[-4:]


def _http(
    method: str,
    path: str,
    token: str | None = None,
    payload: dict | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    accept: str = "application/vnd.github+json",
) -> tuple[int, dict | list | str]:
    """GitHub REST API 호출.

    Returns:
        (status_code, parsed_body_or_text)
    """
    url = path if path.startswith("http") else f"{_GITHUB_API}{path}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", _USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") if resp.length != 0 else ""
            try:
                return resp.status, (json.loads(raw) if raw else {})
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8") if e.fp else ""
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {"message": str(e)}
        return e.code, body
    except urllib.error.URLError as e:
        return 0, {"message": f"network error: {e.reason}"}
    except (socket.timeout, TimeoutError):
        return 0, {"message": "timeout"}


def _secretbox_encrypt(public_key_b64: str, secret_value: str) -> str:
    """GitHub Actions secret 암호화 (libsodium sealed box).

    GitHub 은 secret 등록 시 repo public key 로 sealed box 암호화를 요구한다.
    PyNaCl 이 설치되어 있으면 사용하고, 없으면 RuntimeError.
    """
    try:
        from nacl import encoding, public  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "PyNaCl 이 필요합니다. `pip install pynacl` 로 설치해주세요. "
            "GitHub Actions secret 등록은 sealed box 암호화를 요구합니다."
        ) from e

    pk = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pk)
    enc = sealed.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(enc).decode("utf-8")


def _parse_owner_repo(repo: str, fallback_owner: str | None = None) -> tuple[str, str]:
    """'owner/repo' 또는 'repo' 형식을 분해. 후자는 fallback_owner 필요."""
    repo = repo.strip().rstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if "/" in repo:
        parts = [p for p in repo.split("/") if p]
        return parts[-2], parts[-1]
    if not fallback_owner:
        raise ValueError(f"owner 가 필요합니다 (입력: '{repo}').")
    return fallback_owner, repo


# ── 메인 클래스 ──────────────────────────────────────────────────────

class GitHubAgent:
    """GitHub REST API + 로컬 git 명령 래퍼."""

    def __init__(self) -> None:
        self._token: str = ""
        self._user_cache: dict[str, Any] = {}
        self._status_cache: dict[str, Any] = {}
        self._status_cached_at: float = 0.0
        self._repos_cache: list[dict] = []
        self._repos_cached_at: float = 0.0
        self._load_token()

    # ── 토큰 영속화 ────────────────────────────────────────────────────

    def _load_token(self) -> None:
        """디스크에서 토큰 로드 (있으면)."""
        try:
            if _TOKEN_PATH.exists():
                t = _TOKEN_PATH.read_text(encoding="utf-8").strip()
                if t:
                    self._token = t
                    logger.info(f"[github_agent] token loaded: {_redact(t)}")
        except Exception as e:
            logger.warning(f"[github_agent] token load failed: {e}")

    def _save_token(self, token: str) -> None:
        """토큰을 0600 권한으로 디스크 저장."""
        try:
            _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            _TOKEN_PATH.write_text(token, encoding="utf-8")
            try:
                os.chmod(_TOKEN_PATH, 0o600)
            except (OSError, NotImplementedError):
                pass  # Windows: ACL 별도 처리
        except Exception as e:
            logger.warning(f"[github_agent] token save failed: {e}")

    def _clear_token(self) -> None:
        """토큰 파일 제거 + 메모리 초기화."""
        self._token = ""
        self._user_cache = {}
        self._status_cache = {}
        try:
            if _TOKEN_PATH.exists():
                _TOKEN_PATH.unlink()
        except Exception as e:
            logger.warning(f"[github_agent] token clear failed: {e}")

    # ── git subprocess 래퍼 ────────────────────────────────────────────

    def _git(
        self,
        cwd: str | Path,
        args: list[str],
        timeout: int = 60,
    ) -> tuple[int, str, str]:
        """git 명령 실행. (rc, stdout, stderr) 반환.

        server.py 의 _ship_pipeline 에서 호출. timeout 인자는 위치 인자로도 받음.
        """
        try:
            proc = subprocess.run(
                ["git"] + list(args),
                cwd=str(cwd),
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

    def _ensure_git_init(self, workspace_path: str | Path) -> dict:
        """워크스페이스에 .git 이 없으면 init + .gitignore 보강 + user 설정.

        server.py 의 ship pipeline Step 1 에서 호출.
        """
        ws = Path(workspace_path).expanduser().resolve()
        if not ws.exists():
            return {"status": "error", "message": f"경로가 없습니다: {ws}"}

        # 1. git init (이미 있으면 no-op)
        if not (ws / ".git").exists():
            rc, _, err = self._git(ws, ["init"])
            if rc != 0:
                return {"status": "error", "message": f"git init 실패: {err}"}

        # 2. user.name / user.email 폴백 (커밋이 실패하지 않도록)
        rc, name_out, _ = self._git(ws, ["config", "user.name"])
        if rc != 0 or not name_out.strip():
            self._git(ws, ["config", "user.name", "ReCoder User"])
        rc, email_out, _ = self._git(ws, ["config", "user.email"])
        if rc != 0 or not email_out.strip():
            self._git(ws, ["config", "user.email", "recoder@local"])

        # 3. .gitignore 보강 (설계 §4.5.3 — env / 백업 / 빌드 산출물)
        gi = ws / ".gitignore"
        defaults = [
            "__pycache__/", "*.pyc", ".venv/", "venv/", "env/",
            ".env", ".env.local", ".env.*.local",
            "node_modules/", "dist/", "build/", ".DS_Store",
            ".recoder/", "*.log",
        ]
        existing = ""
        if gi.exists():
            existing = gi.read_text(encoding="utf-8", errors="ignore")
        appended = []
        lines = set(l.strip() for l in existing.splitlines())
        for entry in defaults:
            if entry not in lines:
                appended.append(entry)
        if appended:
            head = "" if existing.endswith("\n") or not existing else "\n"
            gi.write_text(existing + head + "\n".join(appended) + "\n", encoding="utf-8")

        # 4. 기본 브랜치 main 으로 (이미 main 이면 no-op)
        rc, cur_branch, _ = self._git(ws, ["symbolic-ref", "--short", "HEAD"])
        if rc != 0:
            # 아직 커밋이 없어 HEAD 가 없는 경우
            self._git(ws, ["symbolic-ref", "HEAD", "refs/heads/main"])

        return {"status": "ok", "message": "git init 완료", ".gitignore 추가": appended}

    # ── 인증 ───────────────────────────────────────────────────────────

    def set_token(self, token: str) -> dict:
        """VS Code OAuth 토큰 저장 + /user 로 유효성 검증.

        Returns:
            {status: ok|error, user: str, message: str}
        """
        token = (token or "").strip()
        if not token:
            return {"status": "error", "user": "", "message": "빈 토큰입니다."}

        code, body = _http("GET", "/user", token=token)
        if code != 200:
            msg = body.get("message", "검증 실패") if isinstance(body, dict) else "검증 실패"
            return {"status": "error", "user": "", "message": f"토큰 검증 실패 ({code}): {msg}"}

        self._token = token
        self._save_token(token)
        self._user_cache = body if isinstance(body, dict) else {}
        self._status_cached_at = 0.0  # 캐시 무효화
        login = self._user_cache.get("login", "")
        logger.info(f"[github_agent] token authenticated: user={login}")
        return {
            "status": "ok",
            "user": login,
            "message": f"GitHub 인증 완료: {login}",
        }

    def status(self, force: bool = False) -> dict:
        """현재 인증 상태 — 사이드바 초기화에서 호출.

        Returns:
            {status: authenticated|unauthenticated|error, user, scopes, message}
        """
        now = time.time()
        if not force and self._status_cache and (now - self._status_cached_at) < _STATUS_TTL_SECONDS:
            return self._status_cache

        if not self._token:
            res = {
                "status": "unauthenticated",
                "user": "",
                "scopes": [],
                "message": "GitHub 토큰이 설정되지 않았습니다.",
            }
            self._status_cache = res
            self._status_cached_at = now
            return res

        code, body = _http("GET", "/user", token=self._token)
        if code == 200 and isinstance(body, dict):
            self._user_cache = body
            res = {
                "status": "authenticated",
                "user": body.get("login", ""),
                "name": body.get("name", ""),
                "avatar_url": body.get("avatar_url", ""),
                "scopes": [],  # /user 에서는 scopes 헤더 별도 — 단순화
                "message": "ok",
            }
        elif code == 401:
            self._token = ""
            res = {
                "status": "unauthenticated",
                "user": "",
                "scopes": [],
                "message": "토큰이 만료되었거나 무효합니다.",
            }
        else:
            msg = body.get("message", "조회 실패") if isinstance(body, dict) else str(body)[:200]
            res = {
                "status": "error",
                "user": "",
                "scopes": [],
                "message": f"GitHub API 오류 ({code}): {msg}",
            }

        self._status_cache = res
        self._status_cached_at = now
        return res

    def logout(self) -> dict:
        """토큰 제거 (gh CLI 로그아웃 아님, 로컬 토큰 파일만 삭제)."""
        had = bool(self._token)
        self._clear_token()
        return {
            "status": "ok",
            "message": "로그아웃 완료" if had else "이미 로그아웃 상태입니다.",
        }

    # ── 레포지토리 ────────────────────────────────────────────────────

    def list_repos(self) -> dict:
        """인증된 사용자의 레포 목록 (최근 업데이트 100개)."""
        if not self._token:
            return {"status": "error", "repos": [], "message": "인증이 필요합니다."}

        now = time.time()
        if self._repos_cache and (now - self._repos_cached_at) < _REPOS_TTL_SECONDS:
            return {"status": "ok", "repos": self._repos_cache, "cached": True}

        code, body = _http(
            "GET",
            "/user/repos?sort=updated&per_page=100&affiliation=owner,collaborator",
            token=self._token,
        )
        if code != 200 or not isinstance(body, list):
            msg = body.get("message", str(code)) if isinstance(body, dict) else "조회 실패"
            return {"status": "error", "repos": [], "message": f"레포 조회 실패: {msg}"}

        repos = [
            {
                "name": r.get("full_name", ""),
                "private": r.get("private", False),
                "html_url": r.get("html_url", ""),
                "description": r.get("description") or "",
                "default_branch": r.get("default_branch", "main"),
                "updated_at": r.get("updated_at", ""),
            }
            for r in body
        ]
        self._repos_cache = repos
        self._repos_cached_at = now
        return {"status": "ok", "repos": repos, "cached": False}

    def list_branches(self, workspace_path: str = "") -> dict:
        """로컬 워크스페이스의 git 브랜치 목록.

        ※ 원격 브랜치가 아니라 로컬 .git 의 브랜치를 조회한다 (설계상).
        """
        if not workspace_path:
            return {"status": "error", "branches": [], "current": "", "message": "workspace_path 가 비어있습니다."}
        ws = Path(workspace_path).expanduser().resolve()
        if not (ws / ".git").exists():
            return {"status": "error", "branches": [], "current": "", "message": ".git 폴더가 없습니다."}

        rc, current, _ = self._git(ws, ["branch", "--show-current"])
        current = current.strip() if rc == 0 else ""

        rc, out, err = self._git(ws, ["branch", "--list", "--format=%(refname:short)"])
        if rc != 0:
            return {"status": "error", "branches": [], "current": current, "message": err}
        branches = [b.strip() for b in out.splitlines() if b.strip()]
        return {"status": "ok", "branches": branches, "current": current}

    def repo_create_and_push(
        self,
        workspace_path: str,
        name: str,
        private: bool = True,
        description: str = "",
    ) -> dict:
        """GitHub 레포 생성 + 로컬 → 원격 push.

        Args:
            name: 'NAME' 또는 'OWNER/NAME' (Org 레포지토리)

        Returns:
            {status, message, repo_url, repo_name, default_branch}
        """
        if not self._token:
            return {"status": "error", "message": "GitHub 인증이 필요합니다."}

        ws = Path(workspace_path).expanduser().resolve()
        if not ws.exists():
            return {"status": "error", "message": f"경로가 없습니다: {ws}"}

        # 1. owner / repo 결정
        if "/" in name:
            owner, repo = _parse_owner_repo(name)
        else:
            owner = self._user_cache.get("login") or self.status(force=True).get("user", "")
            if not owner:
                return {"status": "error", "message": "사용자 정보를 가져오지 못했습니다. 다시 인증해주세요."}
            repo = name

        full = f"{owner}/{repo}"

        # 2. 이미 존재하면 그대로 사용
        code, body = _http("GET", f"/repos/{full}", token=self._token)
        if code == 200 and isinstance(body, dict):
            html_url = body.get("html_url", f"https://github.com/{full}")
            default_branch = body.get("default_branch", "main")
            logger.info(f"[github_agent] repo already exists: {full}")
        else:
            # 3. 새로 생성 — 본인 계정인지 org 인지 분기
            user_login = (self._user_cache.get("login") or "").lower()
            payload = {
                "name": repo,
                "private": bool(private),
                "description": description or "",
                "auto_init": False,
            }
            if owner.lower() == user_login or not user_login:
                ep = "/user/repos"
            else:
                ep = f"/orgs/{owner}/repos"
            code, body = _http("POST", ep, token=self._token, payload=payload)
            if code not in (200, 201) or not isinstance(body, dict):
                msg = body.get("message", str(code)) if isinstance(body, dict) else "repo 생성 실패"
                return {"status": "error", "message": f"repo 생성 실패: {msg}"}
            html_url = body.get("html_url", f"https://github.com/{full}")
            default_branch = body.get("default_branch", "main")

        # 4. 로컬 git init 확인
        self._ensure_git_init(ws)

        # 5. 적어도 한 번은 커밋이 있어야 push 가능
        rc, _, _ = self._git(ws, ["rev-parse", "HEAD"])
        if rc != 0:
            # add + commit
            self._git(ws, ["add", "-A"])
            rc2, _, err2 = self._git(ws, ["commit", "-m", "Initial commit by ReCoder"])
            if rc2 != 0 and "nothing to commit" not in err2:
                return {"status": "error", "message": f"초기 커밋 실패: {err2}"}

        # 6. 현재 브랜치를 default_branch 로 정렬
        rc, cur, _ = self._git(ws, ["branch", "--show-current"])
        cur = cur.strip()
        if cur and cur != default_branch:
            self._git(ws, ["branch", "-M", default_branch])

        # 7. remote 설정 — 토큰을 URL 에 임베드해서 한 번만 push (보안: 이후 set-url 로 제거)
        token_url = f"https://x-access-token:{self._token}@github.com/{full}.git"
        clean_url = f"https://github.com/{full}.git"

        rc, _, _ = self._git(ws, ["remote", "get-url", "origin"])
        if rc != 0:
            self._git(ws, ["remote", "add", "origin", token_url])
        else:
            self._git(ws, ["remote", "set-url", "origin", token_url])

        # 8. push
        rc, _, err = self._git(ws, ["push", "-u", "origin", default_branch], timeout=180)
        # 9. 토큰을 URL 에서 즉시 제거
        self._git(ws, ["remote", "set-url", "origin", clean_url])

        if rc != 0:
            return {
                "status": "error",
                "message": f"push 실패: {err[:300]}",
                "repo_url": html_url,
                "repo_name": full,
            }

        return {
            "status": "ok",
            "message": f"{full} 생성 및 push 완료",
            "repo_url": html_url,
            "repo_name": full,
            "default_branch": default_branch,
        }

    def push(
        self,
        workspace_path: str,
        branch: str = "",
        force: bool = False,
        commit_message: str = "",
        auto_commit: bool = True,
    ) -> dict:
        """현재 워크스페이스의 origin 으로 push.

        토큰이 있으면 URL 에 임시 임베드 후 푸시 직후 제거.

        auto_commit=True(기본)면 push 전에 미커밋 변경을 stage+commit 한다.
        과거 버그: push 가 stage/commit 을 안 해서, 새로 생성된
        .github/workflows/deploy.yml 같은 untracked 파일이 영원히 안 올라갔다
        (-> GitHub Actions 가 워크플로를 받지 못해 CI/CD 미동작).
        """
        if not self._token:
            return {"status": "error", "message": "GitHub 인증이 필요합니다."}

        ws = Path(workspace_path).expanduser().resolve()
        if not (ws / ".git").exists():
            return {"status": "error", "message": ".git 폴더가 없습니다."}

        # 브랜치 결정
        if not branch:
            rc, cur, _ = self._git(ws, ["branch", "--show-current"])
            branch = cur.strip() or "main"

        # 미커밋 변경(워크플로 파일 포함)을 stage + commit — 안 하면 push 해도 안 올라감.
        committed = False
        if auto_commit:
            rc, status_out, _ = self._git(ws, ["status", "--porcelain"])
            if rc == 0 and status_out.strip():
                rc_n, name_out, _ = self._git(ws, ["config", "user.name"])
                if rc_n != 0 or not name_out.strip():
                    self._git(ws, ["config", "user.name", "ReCoder User"])
                rc_e, email_out, _ = self._git(ws, ["config", "user.email"])
                if rc_e != 0 or not email_out.strip():
                    self._git(ws, ["config", "user.email", "recoder@local"])
                self._git(ws, ["add", "-A"])
                msg = commit_message or "chore(recoder): CI/CD workflow & changes"
                rc_c, _, err_c = self._git(ws, ["commit", "-m", msg])
                if rc_c == 0:
                    committed = True
                elif "nothing to commit" not in err_c:
                    return {"status": "error", "message": f"커밋 실패: {err_c[:300]}"}

        # 원래 origin URL 보존
        rc, original_url, _ = self._git(ws, ["remote", "get-url", "origin"])
        original_url = original_url.strip()
        if rc != 0 or not original_url:
            return {"status": "error", "message": "origin 이 설정되지 않았습니다."}

        # owner/repo 추출
        try:
            owner, repo = _parse_owner_repo(original_url)
            full = f"{owner}/{repo}"
        except Exception as e:
            return {"status": "error", "message": f"origin URL 파싱 실패: {e}"}

        token_url = f"https://x-access-token:{self._token}@github.com/{full}.git"
        clean_url = f"https://github.com/{full}.git"

        self._git(ws, ["remote", "set-url", "origin", token_url])
        args = ["push", "-u", "origin", branch]
        if force:
            args.insert(1, "--force-with-lease")
        rc, out, err = self._git(ws, args, timeout=180)
        # 즉시 원복 (토큰 제거)
        self._git(ws, ["remote", "set-url", "origin", clean_url])

        if rc != 0:
            return {"status": "error", "message": f"push 실패: {err[:300]}"}
        return {
            "status": "ok",
            "message": f"push 완료: {full}:{branch}",
            "branch": branch,
            "repo": full,
            "committed": committed,
        }

    # ── Actions Secrets ───────────────────────────────────────────────

    def set_secret(self, repo: str, name: str, value: str) -> dict:
        """GitHub Actions secret 등록 (sealed box 암호화).

        설계 §5.2: Approval Level 4 작업. 호출 측에서 추가 인증 확인 필수.
        """
        if not self._token:
            return {"status": "error", "message": "GitHub 인증이 필요합니다."}
        if not name or not name.strip():
            return {"status": "error", "message": "secret 이름이 비어있습니다."}

        try:
            owner, name_repo = _parse_owner_repo(
                repo, fallback_owner=self._user_cache.get("login")
            )
            full = f"{owner}/{name_repo}"
        except Exception as e:
            return {"status": "error", "message": f"repo 파싱 실패: {e}"}

        # 1. public key 조회
        code, pk_body = _http(
            "GET", f"/repos/{full}/actions/secrets/public-key", token=self._token,
        )
        if code != 200 or not isinstance(pk_body, dict):
            msg = pk_body.get("message", str(code)) if isinstance(pk_body, dict) else "public key 조회 실패"
            return {"status": "error", "message": f"public key 조회 실패: {msg}"}

        # 2. 암호화 (libsodium sealed box)
        try:
            encrypted = _secretbox_encrypt(pk_body["key"], value)
        except RuntimeError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": f"암호화 실패: {e}"}

        # 3. PUT
        payload = {
            "encrypted_value": encrypted,
            "key_id": pk_body["key_id"],
        }
        code, body = _http(
            "PUT",
            f"/repos/{full}/actions/secrets/{urllib.parse.quote(name)}",
            token=self._token,
            payload=payload,
        )
        if code in (201, 204):
            return {"status": "ok", "message": f"{name} 등록 완료", "repo": full}
        msg = body.get("message", str(code)) if isinstance(body, dict) else "secret 등록 실패"
        return {"status": "error", "message": f"secret 등록 실패 ({code}): {msg}"}

    # ── Workflow Runs ─────────────────────────────────────────────────

    def list_runs(self, repo: str, limit: int = 5) -> dict:
        """최근 GitHub Actions workflow runs 조회."""
        if not self._token:
            return {"status": "error", "runs": [], "message": "GitHub 인증이 필요합니다."}

        try:
            owner, name_repo = _parse_owner_repo(
                repo, fallback_owner=self._user_cache.get("login")
            )
            full = f"{owner}/{name_repo}"
        except Exception as e:
            return {"status": "error", "runs": [], "message": f"repo 파싱 실패: {e}"}

        per = max(1, min(int(limit or 5), 20))
        code, body = _http(
            "GET",
            f"/repos/{full}/actions/runs?per_page={per}",
            token=self._token,
        )
        if code != 200 or not isinstance(body, dict):
            msg = body.get("message", str(code)) if isinstance(body, dict) else "조회 실패"
            return {"status": "error", "runs": [], "message": f"workflow runs 조회 실패: {msg}"}

        runs = []
        for r in body.get("workflow_runs", [])[:per]:
            runs.append({
                "id": r.get("id"),
                "name": r.get("name", ""),
                "status": r.get("status", ""),           # queued / in_progress / completed
                "conclusion": r.get("conclusion", ""),   # success / failure / cancelled / ...
                "html_url": r.get("html_url", ""),
                "head_branch": r.get("head_branch", ""),
                "head_sha": (r.get("head_sha", "") or "")[:8],
                "event": r.get("event", ""),
                "created_at": r.get("created_at", ""),
                "updated_at": r.get("updated_at", ""),
                "run_number": r.get("run_number"),
            })
        return {"status": "ok", "runs": runs, "repo": full}

    # ── Pull Request ──────────────────────────────────────────────────

    def create_pr(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> dict:
        """PR 생성 (gitops_agent.rollback PR 흐름에서 호출).

        Returns:
            {status, pr_url, pr_number, message}
        """
        if not self._token:
            return {"status": "error", "message": "GitHub 인증이 필요합니다.", "pr_url": "", "pr_number": 0}

        try:
            owner, name_repo = _parse_owner_repo(
                repo, fallback_owner=self._user_cache.get("login")
            )
            full = f"{owner}/{name_repo}"
        except Exception as e:
            return {"status": "error", "message": f"repo 파싱 실패: {e}", "pr_url": "", "pr_number": 0}

        payload = {
            "title": title,
            "body": body or "",
            "head": head,
            "base": base,
        }
        code, resp = _http("POST", f"/repos/{full}/pulls", token=self._token, payload=payload)
        if code in (200, 201) and isinstance(resp, dict):
            return {
                "status": "ok",
                "pr_url": resp.get("html_url", ""),
                "pr_number": resp.get("number", 0),
                "message": "PR 생성 완료",
            }
        msg = resp.get("message", str(code)) if isinstance(resp, dict) else "PR 생성 실패"
        return {
            "status": "error",
            "message": f"PR 생성 실패 ({code}): {msg}",
            "pr_url": "",
            "pr_number": 0,
        }


# ── 싱글턴 ────────────────────────────────────────────────────────────

_instance: Optional[GitHubAgent] = None


def get_github_agent() -> GitHubAgent:
    """GitHubAgent 싱글턴."""
    global _instance
    if _instance is None:
        _instance = GitHubAgent()
    return _instance


__all__ = ["GitHubAgent", "get_github_agent"]
