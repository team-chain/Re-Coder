"""
git_agent.py - GitHub CLI backed git operations for ReCoder.

The dashboard calls these helpers through server.py so users can work with a
button-driven UI while git/gh remain implementation details.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


class GitAgentError(RuntimeError):
    def __init__(self, message: str, *, step: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.step = step or {}


_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAWS_SECRET_ACCESS_KEY\s*=", re.IGNORECASE),
    re.compile(r"\bAWS_ACCESS_KEY_ID\s*=\s*AKIA[0-9A-Z]{16}", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
]


def _gh_cmd() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "GitHub CLI" / "gh.exe",
        Path(os.environ.get("LocalAppData", "")) / "Programs" / "GitHub CLI" / "gh.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _run(
    args: list[str],
    cwd: Path,
    *,
    timeout: int = 120,
    check: bool = False,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        raise GitAgentError(f"{args[0]} CLI를 찾을 수 없습니다.")
    except subprocess.TimeoutExpired as e:
        output = "\n".join(part for part in (e.stdout or "", e.stderr or "") if part)
        raise GitAgentError(
            f"{args[0]} 명령 시간이 초과되었습니다.",
            step={"command": " ".join(args), "returncode": -1, "output": output[-8000:]},
        )

    output = "\n".join(part.rstrip() for part in (proc.stdout, proc.stderr) if part and part.strip())
    step = {
        "command": " ".join(args),
        "returncode": proc.returncode,
        "output": output[-8000:],
    }
    if check and proc.returncode != 0:
        raise GitAgentError(f"명령 실행 실패: {' '.join(args[:2])}", step=step)
    return step


def _ensure_git() -> None:
    if not shutil.which("git"):
        raise GitAgentError("Git CLI를 찾을 수 없습니다. Git 설치와 PATH 설정을 확인하세요.")


def _ensure_gh() -> None:
    if not _gh_cmd():
        raise GitAgentError("GitHub CLI(gh)를 찾을 수 없습니다. gh 설치와 PATH 설정을 확인하세요.")


def _repo_root(start: Path) -> Path:
    _ensure_git()
    step = _run(["git", "rev-parse", "--show-toplevel"], start, check=True)
    root = step["output"].splitlines()[0].strip()
    return Path(root).resolve()


def init_repository(project_root: Path) -> dict[str, Any]:
    _ensure_git()
    check = _run(["git", "rev-parse", "--is-inside-work-tree"], project_root)
    if check["returncode"] == 0:
        root = _repo_root(project_root)
        return {"status": "ok", "action": "init", "message": "Git repository already exists.", "state": github_status(root)}

    root = project_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    step = _run(["git", "init"], root, check=True)
    branch_step = _run(["git", "branch", "-M", "main"], root)
    return {
        "status": "ok",
        "action": "init",
        "step": step,
        "branch_step": branch_step,
        "state": github_status(root),
    }


def _current_branch(root: Path) -> str:
    step = _run(["git", "branch", "--show-current"], root)
    branch = step["output"].strip()
    if branch:
        return branch
    symbolic = _run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], root)
    if symbolic["returncode"] == 0 and symbolic["output"].strip():
        return symbolic["output"].strip()
    detached = _run(["git", "rev-parse", "--short", "HEAD"], root)
    if detached["returncode"] == 0 and detached["output"].strip():
        return f"DETACHED@{detached['output'].strip()}"
    return ""


def _remotes(root: Path) -> dict[str, list[str]]:
    step = _run(["git", "remote", "-v"], root)
    remotes: dict[str, list[str]] = {}
    for line in step["output"].splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes.setdefault(parts[0], [])
            if parts[1] not in remotes[parts[0]]:
                remotes[parts[0]].append(parts[1])
    return remotes


def _normalize_file_path(path: str) -> str:
    value = path.strip().replace("\\", "/")
    if not value:
        raise GitAgentError("파일 경로가 비어 있습니다.")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise GitAgentError("절대 경로는 git add 대상으로 사용할 수 없습니다.")
    parts = [part for part in value.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise GitAgentError("상위 디렉터리(..)가 포함된 경로는 사용할 수 없습니다.")
    return "/".join(parts)


def _is_sensitive_path(path: str) -> bool:
    value = _normalize_file_path(path).lower()
    name = value.rsplit("/", 1)[-1]
    if value.startswith(".recoder/") or "/.recoder/" in value:
        return True
    if name == ".env":
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    if name in {"id_rsa", "id_ed25519", "credentials", "secrets.json"}:
        return True
    if name.endswith((".pem", ".p12", ".pfx", ".key")):
        return True
    if "/.aws/" in f"/{value}/":
        return True
    return False


def _scan_for_secrets(root: Path, paths: list[str]) -> list[str]:
    flagged: list[str] = []
    for raw_path in paths:
        path = _normalize_file_path(raw_path)
        target = (root / path).resolve()
        if root != target and root not in target.parents:
            flagged.append(path)
            continue
        if not target.exists() or not target.is_file():
            continue
        try:
            if target.stat().st_size > 1024 * 1024:
                continue
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
            flagged.append(path)
    return flagged


def _changed_files(root: Path) -> list[dict[str, Any]]:
    step = _run(["git", "status", "--porcelain=v1"], root)
    files: list[dict[str, Any]] = []
    for line in step["output"].splitlines():
        if len(line) < 4:
            continue
        index_status = line[0]
        worktree_status = line[1]
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        files.append({
            "path": path,
            "status": f"{index_status}{worktree_status}",
            "staged": index_status not in {" ", "?"},
            "unstaged": worktree_status != " ",
            "untracked": index_status == "?" and worktree_status == "?",
            "sensitive": _is_sensitive_path(path),
        })
    return files


def _validate_branch_name(root: Path, branch: str) -> str:
    value = branch.strip()
    if not value:
        raise GitAgentError("브랜치 이름을 입력하세요.")
    step = _run(["git", "check-ref-format", "--branch", value], root)
    if step["returncode"] != 0:
        raise GitAgentError("Git 브랜치 이름으로 사용할 수 없는 값입니다.", step=step)
    return value


def _validate_repo_name(name: str) -> str:
    value = name.strip()
    if not value:
        raise GitAgentError("레포지토리 이름을 입력하세요.")
    if not re.match(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$", value):
        raise GitAgentError("레포지토리 이름은 repo 또는 owner/repo 형식만 사용할 수 있습니다.")
    return value


def github_status(project_root: Path) -> dict[str, Any]:
    git_available = shutil.which("git") is not None
    gh = _gh_cmd()
    gh_available = gh is not None
    result: dict[str, Any] = {
        "git_available": git_available,
        "gh_available": gh_available,
        "gh_authenticated": False,
        "gh_user": "",
        "inside_work_tree": False,
        "root": "",
        "branch": "",
        "remotes": {},
        "files": [],
        "clean": True,
        "last_commit": "",
        "gh_install_hint": "winget install --id GitHub.cli",
    }

    if not git_available:
        return result

    try:
        root = _repo_root(project_root)
    except GitAgentError as e:
        result["error"] = e.message
        return result

    result.update({
        "inside_work_tree": True,
        "root": str(root),
        "branch": _current_branch(root),
        "remotes": _remotes(root),
        "files": _changed_files(root),
    })
    result["clean"] = not result["files"]

    last = _run(["git", "log", "-1", "--pretty=%h %s"], root)
    if last["returncode"] == 0:
        result["last_commit"] = last["output"].strip()

    if gh_available:
        auth = _run([gh, "auth", "status"], root)
        result["gh_authenticated"] = auth["returncode"] == 0
        if result["gh_authenticated"]:
            user = _run([gh, "api", "user", "--jq", ".login"], root)
            if user["returncode"] == 0:
                result["gh_user"] = user["output"].strip()
    return result


def open_github_login(project_root: Path) -> dict[str, Any]:
    _ensure_gh()
    gh = _gh_cmd()
    root = _repo_root(project_root)
    shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
    command = f'& "{gh}" auth login --web; Write-Host ""; Read-Host "GitHub login finished. Press Enter to close"'
    subprocess.Popen([shell, "-NoExit", "-Command", command], cwd=str(root))
    return {"status": "ok", "message": "GitHub CLI login terminal opened."}


def create_repository(project_root: Path, name: str, visibility: str, description: str = "") -> dict[str, Any]:
    root = _repo_root(project_root)
    repo_name = _validate_repo_name(name)
    vis = visibility.strip().lower() or "private"
    if vis not in {"private", "public", "internal"}:
        raise GitAgentError("visibility는 private, public, internal 중 하나여야 합니다.")

    remotes = _remotes(root)
    if "origin" in remotes:
        raise GitAgentError("origin remote가 이미 있습니다. 현재 레포에서는 새 레포 생성 대신 push를 사용하세요.")

    _ensure_gh()
    gh = _gh_cmd()
    args = [gh, "repo", "create", repo_name, f"--{vis}", "--source", str(root)]
    args.extend(["--remote", "origin"])
    if description.strip():
        args.extend(["--description", description.strip()])
    step = _run(args, root, timeout=180, check=True)
    return {"status": "ok", "action": "repo_create", "step": step, "state": github_status(root)}


def diff_files(project_root: Path, files: list[str] | None = None) -> dict[str, Any]:
    root = _repo_root(project_root)
    normalized = [_normalize_file_path(path) for path in (files or [])]
    if not normalized:
        normalized = [
            item["path"]
            for item in _changed_files(root)
            if not item.get("sensitive")
        ]
    blocked = [path for path in normalized if _is_sensitive_path(path)]
    if blocked:
        raise GitAgentError("민감 파일은 diff 미리보기를 표시할 수 없습니다: " + ", ".join(blocked))

    flagged = _scan_for_secrets(root, normalized)
    if flagged:
        raise GitAgentError("시크릿으로 보이는 내용이 있어 diff 미리보기를 중단했습니다: " + ", ".join(flagged))

    suffix = ["--", *normalized] if normalized else []
    unstaged = _run(["git", "diff", *suffix], root, timeout=120)
    staged = _run(["git", "diff", "--cached", *suffix], root, timeout=120)
    stat = _run(["git", "diff", "--stat", *suffix], root, timeout=120)
    staged_stat = _run(["git", "diff", "--cached", "--stat", *suffix], root, timeout=120)
    untracked = [
        item["path"]
        for item in _changed_files(root)
        if item.get("untracked") and (not normalized or item["path"] in normalized)
    ]
    return {
        "status": "ok",
        "action": "diff",
        "files": normalized,
        "stat": stat["output"],
        "staged_stat": staged_stat["output"],
        "diff": unstaged["output"][-20000:],
        "staged_diff": staged["output"][-20000:],
        "untracked": untracked,
        "state": github_status(root),
    }


def create_or_switch_branch(project_root: Path, branch: str) -> dict[str, Any]:
    root = _repo_root(project_root)
    branch_name = _validate_branch_name(root, branch)
    exists = _run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"], root)
    if exists["returncode"] == 0:
        step = _run(["git", "switch", branch_name], root, check=True)
        action = "switch_branch"
    else:
        step = _run(["git", "switch", "-c", branch_name], root, check=True)
        action = "create_branch"
    return {"status": "ok", "action": action, "step": step, "state": github_status(root)}


def add_files(project_root: Path, files: list[str]) -> dict[str, Any]:
    root = _repo_root(project_root)
    normalized = [_normalize_file_path(path) for path in files]
    if not normalized:
        raise GitAgentError("add할 파일을 선택하세요.")

    blocked = [path for path in normalized if _is_sensitive_path(path)]
    if blocked:
        raise GitAgentError("민감 파일은 add할 수 없습니다: " + ", ".join(blocked))

    flagged = _scan_for_secrets(root, normalized)
    if flagged:
        raise GitAgentError("시크릿으로 보이는 내용이 있어 add를 중단했습니다: " + ", ".join(flagged))

    step = _run(["git", "add", "--", *normalized], root, check=True)
    return {"status": "ok", "action": "add", "files": normalized, "step": step, "state": github_status(root)}


def commit(project_root: Path, message: str) -> dict[str, Any]:
    root = _repo_root(project_root)
    msg = message.strip()
    if not msg:
        raise GitAgentError("커밋 메시지를 입력하세요.")

    diff = _run(["git", "diff", "--cached", "--quiet"], root)
    if diff["returncode"] == 0:
        raise GitAgentError("스테이징된 변경사항이 없습니다. 먼저 add를 실행하세요.")

    step = _run(["git", "commit", "-m", msg], root, timeout=180, check=True)
    return {"status": "ok", "action": "commit", "step": step, "state": github_status(root)}


def push(project_root: Path, branch: str = "") -> dict[str, Any]:
    root = _repo_root(project_root)
    remotes = _remotes(root)
    if "origin" not in remotes:
        raise GitAgentError("origin remote가 없습니다. 레포지토리를 생성하거나 remote를 먼저 설정하세요.")
    branch_name = branch.strip() or _current_branch(root)
    if branch_name.startswith("DETACHED@"):
        raise GitAgentError("Detached HEAD 상태에서는 push할 수 없습니다. 브랜치를 먼저 생성하세요.")
    step = _run(["git", "push", "-u", "origin", branch_name], root, timeout=300, check=True)
    return {"status": "ok", "action": "push", "step": step, "state": github_status(root)}


def commit_and_push(project_root: Path, files: list[str], message: str, branch: str = "") -> dict[str, Any]:
    root = _repo_root(project_root)
    steps = [
        add_files(root, files),
        commit(root, message),
        push(root, branch),
    ]
    return {"status": "ok", "action": "commit_push", "steps": steps, "state": github_status(root)}
