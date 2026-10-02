"""Copy, validate and approve exact file contents; model text is never a command."""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from context_gate import mask_secrets
from grounded_repair.logs import summarize

SKIP = {".git", ".recoder", "node_modules", ".venv", "venv", "__pycache__", ".cache", ".pytest_cache"}
MAX_SNAPSHOT_BYTES = 32_000_000


def safe_path(root: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    if not parts or name.startswith(("/", "\\")) or "\\" in name or ":" in name or any(p in {"..", "."} | SKIP for p in parts):
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
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
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
    def priority(name):
        return (0 if name in evidence else 1 if Path(name).name in {
            "Dockerfile", "package.json", "docker-compose.yml", "vite.config.js",
            "vite.config.ts", "tsconfig.json", "requirements.txt", "requirements.md", ".dockerignore", "task-definition.json"
        } else 2, name)
    chosen, remaining = {}, 24000
    for name in sorted(files, key=priority):
        if priority(name)[0] == 2 and Path(name).suffix not in {".js", ".cjs", ".mjs", ".ts", ".py", ".json", ".yaml", ".yml", ".c", ".txt"}:
            continue
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


class DockerVerifier:
    """Build only. Cloud/runtime failures require a different environment verifier."""
    identity = "docker-build-v1"

    def __call__(self, workspace: Path) -> dict:
        if not (workspace / "Dockerfile").is_file():
            return {"passed": False, "output": "Dockerfile is required for rebuild verification"}
        # A private output file bounds memory even for verbose compiler output.
        with tempfile.TemporaryFile() as output:
            try:
                p = subprocess.run(
                    ["docker", "build", "--progress=plain", "."], cwd=workspace,
                    stdout=output, stderr=subprocess.STDOUT, timeout=300, shell=False,
                )
                output.seek(0)
                log = output.read(1_000_000).decode("utf-8", "replace")
                available = not any(s in log.lower() for s in (
                    "cannot connect to the docker daemon", "is the docker daemon running",
                    "permission denied while trying to connect", "failed to update builder last activity",
                    "error during connect"))
                return {"passed": p.returncode == 0, "output": summarize(log).text,
                        "available": available, "exit_code": p.returncode, "kind": self.identity}
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"passed": False, "available": False, "output": mask_secrets(str(exc)), "kind": self.identity}


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
