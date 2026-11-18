"""Copy, validate and approve exact file contents; model text is never a command."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import json
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from context_gate import mask_secrets
from grounded_repair.context import context_order
from grounded_repair.logs import summarize

SKIP = {
    ".git",
    ".recoder",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".cache",
    ".pytest_cache",
}
MAX_SNAPSHOT_BYTES = 32_000_000


def _run_tree(args, **kwargs):
    """시간이 지나면 docker 와 그 자식(buildx)까지 끝낸다(2.0.32, Windows 에서 멈추던 것)."""
    try:
        import proc_tools  # type: ignore
    except ImportError:  # pragma: no cover
        from core import proc_tools  # type: ignore
    return proc_tools.run(args, **kwargs)


def safe_path(root: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    if (
        not parts
        or name.startswith(("/", "\\"))
        or "\\" in name
        or ":" in name
        or any(p in {"..", "."} | SKIP for p in parts)
    ):
        raise ValueError("Unsafe repair path")
    if any(p.startswith(".env") or p in {".aws", ".ssh", ".npmrc", ".netrc"} for p in parts):
        raise ValueError("Secret file is not a repair target")
    path = root / name
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("Symlinks are not repair targets")
        current = current.parent
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Repair path escapes workspace")
    return path


def manifest(root: Path) -> dict[str, str]:
    result, total = {}, 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and d not in {".aws", ".ssh"})
        if any((Path(directory) / d).is_symlink() for d in dirs):
            raise ValueError("Workspace contains a symlink directory")
        for name in sorted(files):
            if name.startswith(".env") or name in {".npmrc", ".netrc"}:
                continue
            path = Path(directory) / name
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("Workspace contains a non-regular file")
            total += path.stat().st_size
            if total > MAX_SNAPSHOT_BYTES or len(result) >= 4000:
                raise ValueError("Repair workspace exceeds snapshot limit (32 MB / 4000 files)")
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def snapshot(root: Path, target: Path, expected: dict[str, str]):
    for name, digest in expected.items():
        src = safe_path(root, name)
        dst = target / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Workspace changed while preparing repair")
        dst.write_bytes(data)
        shutil.copymode(src, dst)


def select_context(root: Path, files: dict[str, str], evidence: str) -> dict[str, str]:
    chosen, remaining = {}, 24000
    for name in context_order(files, evidence):
        p = safe_path(root, name)
        if p.stat().st_size > min(12000, remaining):
            continue
        try:
            text = p.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        # Full-file replacements must never overwrite a redacted secret.
        if mask_secrets(text) != text:
            continue
        chosen[name] = text
        remaining -= len(text)
        if len(chosen) >= 12 or remaining < 100:
            break
    return chosen


_FAILED_SERVICE = (
    re.compile(r"target ([a-z][a-z0-9-]*): failed to solve"),
    re.compile(r"(?m)^\s*([a-z][a-z0-9-]*) 이미지 빌드 실패"),
    re.compile(r"=> (?:ERROR )?\[([a-z][a-z0-9-]*) (?:build|stage-\d+|\d+/\d+)"),
)


def failed_service(log: str) -> str:
    """docker compose 빌드 로그에서 실패한 서비스 이름."""
    for pattern in _FAILED_SERVICE:
        found = pattern.findall(log or "")
        if found:
            return found[-1]
    return ""


def compose_services(workspace: Path) -> dict[str, str]:
    """architecture.json 의 서비스 중 Dockerfile 이 있는 것 — 이름 → 폴더(상대 경로)."""
    try:
        arch = json.loads((workspace / "architecture.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for svc in arch.get("services") or [] if isinstance(arch, dict) else []:
        name, path = str(svc.get("name") or ""), str(svc.get("path") or "").replace("\\", "/").strip("/")
        if name and path and ".." not in path.split("/") and (workspace / path / "Dockerfile").is_file():
            out[name] = path
    return out


class DockerVerifier:
    """Build only. Cloud/runtime failures require a different environment verifier."""

    identity = "docker-build-v1"

    def __init__(self, service: str = ""):
        self.service = service

    def for_log(self, log: str) -> "DockerVerifier":
        """여러 서비스 프로젝트 — 빌드 로그에서 실패한 서비스를 찾아 그 폴더만 다시 빌드한다(실사용 2026-11-18)."""
        return DockerVerifier(failed_service(log))

    def context(self, workspace: Path) -> str | None:
        """빌드할 폴더 — 루트 Dockerfile 이 있으면 루트, 없으면 실패한 서비스(또는 하나뿐인 서비스) 폴더."""
        if (workspace / "Dockerfile").is_file():
            return "."
        services = compose_services(workspace)
        if self.service in services:
            return services[self.service]
        return next(iter(services.values())) if len(services) == 1 else None

    def __call__(self, workspace: Path) -> dict:
        target = self.context(workspace)
        if target is None:
            return {"passed": False, "available": False, "kind": self.identity,
                    "output": "Dockerfile is required for rebuild verification"}
        # A private output file bounds memory even for verbose compiler output.
        with tempfile.TemporaryFile() as output:
            try:
                p = _run_tree(
                    ["docker", "build", "--progress=plain", target],
                    cwd=workspace,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=300,
                    shell=False,
                )
                output.seek(0)
                log = output.read(1_000_000).decode("utf-8", "replace")
                available = not any(
                    s in log.lower()
                    for s in (
                        "cannot connect to the docker daemon",
                        "is the docker daemon running",
                        "permission denied while trying to connect",
                        "failed to update builder last activity",
                        "error during connect",
                    )
                )
                return {
                    "passed": p.returncode == 0,
                    "output": summarize(log).text,
                    "available": available,
                    "exit_code": p.returncode,
                    "kind": self.identity,
                }
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {
                    "passed": False,
                    "available": False,
                    "output": mask_secrets(str(exc)),
                    "kind": self.identity,
                }


def apply_exact(root: Path, expected: dict[str, str], edits: dict[str, str]):
    if manifest(root) != expected:
        raise ValueError("Workspace changed since verification; prepare again")
    originals = {}
    try:
        for name, content in edits.items():
            path = safe_path(root, name)
            originals[name] = path.read_bytes()
            path.write_bytes(content.encode("utf-8"))
    except Exception:
        for name, content in originals.items():
            safe_path(root, name).write_bytes(content)
        raise
