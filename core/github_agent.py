"""
github_agent.py — GitHub 자동화 (VS Code OAuth + GitHub REST API)

설계 변경 (2026-05):
- gh CLI 의존성 완전 제거
- Extension 이 vscode.authentication.getSession('github', ['repo','workflow']) 로
  얻은 액세스 토큰을 /api/github/token 엔드포인트로 전달.
- 모든 GitHub 작업은 httpx 로 REST API 직접 호출.
- git push 시 토큰을 http.extraheader 로 전달 (설정 파일 무기록).
- Secrets 암호화는 PyNaCl sealed-box 사용.

API 흐름:
  1. Extension: vscode.authentication.getSession → token 획득
  2. Extension: POST /api/github/token  → Core 에 토큰 저장
  3. Extension: GET  /api/github/status → 인증 확인 후 UI 업데이트
  4. 이후: repo 생성·push·secret·runs 모두 REST API 로 처리
"""

from __future__ import annotations

import logging

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=5.0)

_DEFAULT_GITIGNORE = """\
# ReCoder default .gitignore

# Python
__pycache__/
*.py[cod]
*$py.class
.venv/
venv/
env/
**/venv/
**/.venv/
**/env/

# Node
node_modules/
**/node_modules/
dist/
build/
.next/
.cache/
*.tsbuildinfo
out/

# Logs & env
*.log
.env
.env.*
!.env.example

# OS / IDE
.idea/
.vscode/
.DS_Store
Thumbs.db

# Docker
*.tar
"""


class GitHubAgent:
    """
    GitHub 자동화 에이전트.

    토큰은 Extension 에서 set_token() 으로 주입받는다.
    모든 공개 메서드는 동기이며 server.py 에서 asyncio.to_thread 로 호출된다.
    """

    def __init__(self) -> None:
        self._token: str = ""
        self._user: str = ""

    # ── 토큰 관리 ─────────────────────────────────────────────────

    def set_token(self, token: str) -> dict:
        """
        Extension 에서 받은 VS Code GitHub OAuth 토큰 저장.
        저장 즉시 /user API 로 유효성 확인 후 사용자명 캐싱.
        """
        if not token:
            self._token = ""
            self._user = ""
            return {"status": "ok", "user": ""}

        self._token = token
        self._user = ""

        try:
            user_data = self._api_get("/user")
            self._user = user_data.get("login", "")
            if not self._user:
                raise ValueError("사용자 정보를 가져오지 못했습니다.")
            logger.info(f"[github_agent] 토큰 설정 완료 — 사용자: {self._user}")
            return {"status": "ok", "user": self._user}
        except httpx.HTTPStatusError as e:
            self._token = ""
            msg = f"토큰이 유효하지 않습니다 (HTTP {e.response.status_code})"
            logger.warning(f"[github_agent] set_token 실패: {msg}")
            return {"status": "error", "message": msg}
        except Exception as e:
            self._token = ""
            logger.warning(f"[github_agent] set_token 실패: {e}")
            return {"status": "error", "message": str(e)[:300]}

    def status(self, force: bool = False) -> dict:
        """
        인증 상태 반환. GhStatus 호환 dict.
        - installed: 항상 True (gh CLI 불필요)
        - authed: 유효한 토큰 보유 여부
        - user: GitHub 사용자명
        """
        if not self._token:
            return {
                "installed": True,
                "version": "vscode-oauth",
                "authed": False,
                "user": "",
                "install_hint": "",
            }
        # 강제 새로고침이거나 사용자명 미캐시
        if force or not self._user:
            try:
                user_data = self._api_get("/user")
                self._user = user_data.get("login", "")
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    # 토큰 만료 또는 스코프 부족
                    self._token = ""
                    self._user = ""
                    return {
                        "installed": True,
                        "version": "vscode-oauth",
                        "authed": False,
                        "user": "",
                        "install_hint": "",
                    }
            except Exception:
                pass  # 네트워크 오류 시 캐시 값 사용

        return {
            "installed": True,
            "version": "vscode-oauth",
            "authed": bool(self._user),
            "user": self._user,
            "install_hint": "",
        }

    def logout(self) -> dict:
        """토큰 삭제. VS Code 세션은 Extension 이 관리."""
        self._token = ""
        self._user = ""
        logger.info("[github_agent] 로그아웃 (토큰 삭제)")
        return {"status": "ok"}

    # ── Repo 생성 + Push ───────────────────────────────────────────

    def repo_create_and_push(
        self,
        workspace_path: str,
        repo_name: str,
        private: bool = True,
        description: str = "",
    ) -> dict:
        """
        GitHub REST API 로 repo 생성 후 push.
        - 이미 존재하면 해당 repo 에 push 재시도.
        - 토큰은 git http.extraheader 로 전달 (설정 파일에 기록 안됨).
        """
        if not self._token:
            return {"status": "error", "message": "GitHub 인증이 필요합니다. 연결하기를 눌러주세요."}

        ws = Path(workspace_path).expanduser().resolve()
        if not ws.exists():
            return {"status": "error", "message": f"경로가 없습니다: {workspace_path}"}

        self._ensure_git_init(ws)

        # repo_name 이 "owner/name" 형식이면 name 만 추출
        owner, name = self._parse_repo_name(repo_name)
        if not re.match(r"^[\w.-]+$", name):
            return {"status": "error", "message": f"유효하지 않은 repo 이름: {name}"}

        if owner and not re.match(r"^[\w.-]+$", owner):
            return {"status": "error", "message": f"Invalid repo owner: {owner}"}
        full_name_hint = f"{owner}/{name}" if owner else f"{self._user}/{name}"

        try:
            create_path = (
                f"/orgs/{owner}/repos"
                if owner and owner.lower() != (self._user or "").lower()
                else "/user/repos"
            )
            repo_data = self._api_post(create_path, {
                "name": name,
                "private": private,
                "description": description or "",
            })
            clone_url = repo_data.get("clone_url", "")
            repo_url  = repo_data.get("html_url", "")
            full_name = repo_data.get("full_name", "")
            logger.info(f"[github_agent] repo 생성 완료: {full_name}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:
                # Unprocessable Entity → 이름 중복 등 → 기존 repo 에 push
                logger.info(f"[github_agent] repo 이미 존재 → push 재시도: {name}")
                return self._push_to_existing_repo(ws, full_name_hint)
            body = e.response.text[:400]
            return {"status": "error", "message": f"repo 생성 실패 (HTTP {e.response.status_code}): {body}"}
        except Exception as e:
            return {"status": "error", "message": f"repo 생성 실패: {e!s:.300}"}

        # 생성된 repo 에 push
        rc, out, err = self._push_with_token(ws, clone_url)
        if rc != 0:
            return {
                "status": "error",
                "message": f"repo 생성됐지만 push 실패: {err[:300]}",
            }

        return {
            "status": "ok",
            "repo_url": repo_url,
            "repo_name": full_name,
            "message": "GitHub repo 생성 + push 완료",
        }

    def _push_to_existing_repo(self, ws: Path, repo_full_name: str) -> dict:
        """이미 존재하는 사용자 repo 에 push."""
        try:
            repo_data = self._api_get(f"/repos/{repo_full_name}")
            clone_url = repo_data.get("clone_url", "")
            repo_url  = repo_data.get("html_url", "")
            full_name = repo_data.get("full_name", "")
        except Exception as e:
            return {"status": "error", "message": f"기존 repo 조회 실패: {e!s:.300}"}

        rc, _, err = self._push_with_token(ws, clone_url)
        if rc != 0:
            return {"status": "error", "message": f"push 실패: {err[:300]}"}

        return {
            "status": "ok",
            "repo_url": repo_url,
            "repo_name": full_name,
            "message": "기존 repo 에 push 완료",
        }

    def push(self, workspace_path: str, branch: str = "", force: bool = False) -> dict:
        """git strip 의 Push 버튼용. GitHub remote 인 경우 반드시 토큰 필요."""
        ws = Path(workspace_path).expanduser().resolve()
        if not ws.exists():
            return {"status": "error", "message": f"경로 없음: {workspace_path}", "remote_url": ""}

        rc0, remote_url, _ = self._git(ws, ["remote", "get-url", "origin"])
        if rc0 != 0 or not remote_url.strip():
            return {"status": "no_remote", "message": "원격 저장소가 없습니다.", "remote_url": ""}

        remote_url = remote_url.strip()

        if "github.com" in remote_url and not self._token:
            return {
                "status": "error",
                "message": "GitHub Push 실패: ReCoder에서 GitHub에 먼저 연결해주세요. (GitHub Hub → GitHub 연결 버튼)",
                "remote_url": remote_url,
            }

        ref = branch or "HEAD"
        push_args = ["push", "-u"]
        if force:
            push_args.append("--force-with-lease")

        if self._token and "github.com" in remote_url:
            # 인증 URL을 push 명령에 직접 사용 (저장 안함, credential manager 우회)
            push_args += [self._auth_url(remote_url), ref]
        else:
            push_args += ["origin", ref]

        rc, _, err = self._git(ws, push_args, timeout=120)
        if rc != 0:
            return {"status": "error", "message": f"push 실패: {self._sanitize_err(err)[:300]}", "remote_url": remote_url}
        return {"status": "ok", "message": "push 완료", "remote_url": remote_url}

    # ── Secrets ────────────────────────────────────────────────────

    def set_secret(self, repo_name: str, name: str, value: str) -> dict:
        """
        GitHub Actions Secret 등록.
        PyNaCl sealed-box 암호화 사용 (requirements: PyNaCl>=1.5.0).
        """
        if not self._token:
            return {"status": "error", "message": "GitHub 인증 필요"}
        if not name or not value:
            return {"status": "error", "message": "name/value 가 비어있습니다."}

        try:
            from nacl.encoding import Base64Encoder
            from nacl.public import PublicKey, SealedBox
        except ImportError:
            return {
                "status": "error",
                "message": "PyNaCl 이 설치되어 있지 않습니다 (pip install PyNaCl)",
            }

        try:
            pk_resp = self._api_get(f"/repos/{repo_name}/actions/secrets/public-key")
            key_id  = pk_resp["key_id"]
            pub_key = PublicKey(pk_resp["key"], encoder=Base64Encoder)
            encrypted = SealedBox(pub_key).encrypt(value.encode(), encoder=Base64Encoder)
            self._api_put(
                f"/repos/{repo_name}/actions/secrets/{name}",
                {"encrypted_value": encrypted.decode(), "key_id": key_id},
            )
            return {"status": "ok", "message": f"{name} 등록 완료"}
        except httpx.HTTPStatusError as e:
            return {
                "status": "error",
                "message": f"Secret 등록 실패 (HTTP {e.response.status_code}): {e.response.text[:300]}",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)[:300]}

    # ── 조회 ─────────────────────────────────────────────────────

    def list_runs(self, repo_name: str, limit: int = 5) -> dict:
        """GitHub Actions 실행 목록."""
        if not self._token:
            return {"status": "error", "runs": [], "message": "GitHub 인증 필요"}
        try:
            data = self._api_get(
                f"/repos/{repo_name}/actions/runs",
                params={"per_page": limit},
            )
            runs = [
                {
                    "databaseId": r["id"],
                    "status":     r["status"],
                    "conclusion": r.get("conclusion"),
                    "name":       r.get("name", ""),
                    "headSha":    r.get("head_sha", ""),
                    "createdAt":  r.get("created_at", ""),
                    "url":        r.get("html_url", ""),
                }
                for r in data.get("workflow_runs", [])
            ]
            return {"status": "ok", "runs": runs}
        except Exception as e:
            return {"status": "error", "runs": [], "message": str(e)[:300]}

    def current_user(self) -> str:
        return self._user or ""

    def list_branches(self, workspace_path: str) -> dict:
        """로컬 git 브랜치 목록 (git CLI — GitHub API 아님)."""
        ws = Path(workspace_path).expanduser().resolve() if workspace_path else None
        if ws is None or not (ws / ".git").exists():
            return {"branches": [], "current": "", "error": "git 저장소 없음"}
        rc, out, err = self._git(ws, ["branch", "-a", "--format=%(refname:short)"])
        if rc != 0:
            return {"branches": [], "current": "", "error": err.strip()}
        branches = [b.strip() for b in out.strip().splitlines() if b.strip()]
        rc2, cur, _ = self._git(ws, ["branch", "--show-current"])
        return {
            "branches": branches,
            "current":  cur.strip() if rc2 == 0 else "",
            "error":    "",
        }

    def list_repos(self, limit: int = 30) -> dict:
        """인증된 사용자의 레포지토리 목록 (본인 소유 레포만)."""
        if not self._token:
            return {"status": "error", "repos": [], "message": "GitHub 인증 필요"}
        try:
            repos_raw = self._api_get(
                "/user/repos",
                params={"per_page": limit, "sort": "updated", "type": "owner"},
            )
            repos = [
                {
                    "name":        r["name"],
                    "full_name":   r["full_name"],
                    "private":     r["private"],
                    "url":         r["html_url"],
                    "description": r.get("description") or "",
                }
                for r in repos_raw
            ]
            return {"status": "ok", "repos": repos}
        except httpx.HTTPStatusError as e:
            return {
                "status": "error",
                "repos":  [],
                "message": f"레포지토리 목록 조회 실패 (HTTP {e.response.status_code})",
            }
        except Exception as e:
            return {"status": "error", "repos": [], "message": str(e)[:300]}

    # ── git 헬퍼 ─────────────────────────────────────────────────

    @staticmethod
    def _parse_repo_name(repo_name: str) -> tuple[str, str]:
        """Return (owner, name). owner is empty when user-owned repo is implied."""
        value = (repo_name or "").strip().strip("/")
        if "/" not in value:
            return "", value
        owner, name = value.split("/", 1)
        return owner.strip(), name.strip().strip("/")

    def _auth_url(self, url: str) -> str:
        """
        push 명령에만 사용할 인증 포함 URL.
        .git/config 에 저장하지 않으며, Windows Credential Manager 를 완전히 우회한다.
        형식: https://oauth2:TOKEN@github.com/user/repo.git
        """
        return re.sub(r'^https://', f'https://oauth2:{self._token}@', url.strip())

    def _sanitize_err(self, text: str) -> str:
        """에러 메시지에 토큰이 노출되지 않도록 마스킹."""
        if self._token and self._token in text:
            text = text.replace(self._token, '***')
        return text

    def _push_with_token(self, ws: Path, clone_url: str) -> tuple[int, str, str]:
        """
        origin 에는 토큰 없는 clean URL 저장,
        push 명령에는 인증 URL 직접 사용 → credential manager 완전 우회.
        """
        clean_url = clone_url.strip()

        # origin 을 토큰 없는 clean URL 로 설정 (저장됨)
        rc0, _, _ = self._git(ws, ["remote", "get-url", "origin"])
        if rc0 != 0:
            self._git(ws, ["remote", "add", "origin", clean_url])
        else:
            self._git(ws, ["remote", "set-url", "origin", clean_url])

        # push 는 인증 URL 로 직접 실행 (remote 이름 대신 URL 사용)
        rc, out, err = self._git(
            ws,
            ["push", "-u", self._auth_url(clean_url), "HEAD"],
            timeout=120,
        )
        return rc, out, self._sanitize_err(err)

    def _ensure_git_init(self, ws: Path) -> None:
        """
        git init / .gitignore / user.* 설정.
        add + commit 은 파이프라인 commit 스텝에서 별도 처리.
        """
        if not (ws / ".git").exists():
            self._git(ws, ["init", "-b", "main"])

        gi = ws / ".gitignore"
        if not gi.exists() or gi.stat().st_size == 0:
            try:
                gi.write_text(_DEFAULT_GITIGNORE, encoding="utf-8")
            except Exception:
                pass

        # 사용자 정보 없으면 기본값 설정 (로컬 설정만)
        rc, who, _ = self._git(ws, ["config", "--local", "user.email"])
        if rc != 0 or not who.strip():
            display = self._user or "recoder"
            self._git(ws, ["config", "--local", "user.email", f"{display}@users.noreply.github.com"])
            self._git(ws, ["config", "--local", "user.name",  display])

    @staticmethod
    def _normalize_repo(name: str, url: str) -> str:
        if "/" in name:
            return name
        m = re.search(r"github\.com/([\w.-]+/[\w.-]+)", url or "")
        return m.group(1) if m else name

    @staticmethod
    def _extract_repo_url(text: str) -> str:
        m = re.search(r"https?://github\.com/[\w.-]+/[\w.-]+", text or "")
        return m.group(0) if m else ""

    # ── subprocess 래퍼 ───────────────────────────────────────────

    def _git(
        self, cwd: Path, args: list[str], timeout: int = 60
    ) -> tuple[int, str, str]:
        try:
            env = os.environ.copy()
            env.setdefault("GIT_TERMINAL_PROMPT", "0")
            env.setdefault("GCM_INTERACTIVE", "Never")
            proc = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except FileNotFoundError:
            return -1, "", "git 이 PATH 에 없습니다."
        except subprocess.TimeoutExpired:
            return -1, "", "git timeout"
        except Exception as e:
            return -1, "", str(e)

    # ── REST API 헬퍼 ─────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization":        f"Bearer {self._token}",
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _api_get(self, path: str, params: Optional[dict] = None) -> object:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(f"{_GH_API}{path}", headers=self._headers(), params=params)
            resp.raise_for_status()
            return resp.json()

    def _api_post(self, path: str, body: dict) -> object:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(f"{_GH_API}{path}", headers=self._headers(), json=body)
            resp.raise_for_status()
            return resp.json()

    def _api_put(self, path: str, body: dict) -> object:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.put(f"{_GH_API}{path}", headers=self._headers(), json=body)
            resp.raise_for_status()
            return resp.json() if resp.content else {}


# ── 싱글턴 ────────────────────────────────────────────────────────────
_instance: Optional[GitHubAgent] = None


def get_github_agent() -> GitHubAgent:
    global _instance
    if _instance is None:
        _instance = GitHubAgent()
    return _instance
