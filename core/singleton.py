"""
ReCoder Core — Singleton Manager

Ensures only one instance of the Core server runs at a time per user,
manages port discovery, and persists runtime configuration.
Supports multiple VSCode windows via a reference-counted window list.
"""

from __future__ import annotations

import json
import os
import platform
import signal
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from schemas import RuntimeConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RECODER_DIR = Path.home() / ".recoder"


class CoreSingleton:
    # Root directory for all ReCoder runtime state (§21).
    # Exposed as a class attribute so external code (main.py, session_logger,
    # rollback_policy, …) can reference the same location consistently.
    RECODER_HOME: Path = _RECODER_DIR
    LOCK_FILE: Path = _RECODER_DIR / "core.lock"
    RUNTIME_FILE: Path = _RECODER_DIR / "runtime.json"
    DEFAULT_PORT: int = 17894
    FALLBACK_PORTS: range = range(17895, 17911)

    # ------------------------------------------------------------------
    # Lock management
    # ------------------------------------------------------------------

    @classmethod
    def acquire_lock(cls, pid: int) -> bool:
        """
        Attempt to acquire the singleton lock.

        Returns True if the lock was successfully acquired (this process is
        the first), False if another live process already holds it.
        """
        with cls._state_guard():
            existing = cls._read_lock()
            owner = existing.get("pid") if existing else None
            if isinstance(owner, int) and owner > 0 and cls._pid_alive(owner):
                return owner == pid
            # An older Core may have lost its lock file but still own runtime.
            runtime = cls.read_runtime()
            if runtime and runtime.pid != pid and runtime.pid > 0 and cls._pid_alive(runtime.pid):
                return False
            cls.RUNTIME_FILE.unlink(missing_ok=True)
            cls._write_json(cls.LOCK_FILE, {
                "pid": pid,
                "started_at": datetime.utcnow().isoformat(),
                "windows": [pid],
            })
            return True

    @classmethod
    def release_lock(cls, pid: int) -> None:
        """
        Remove only metadata owned by this PID. A losing startup or an old
        process's repeated finally/atexit must never erase a newer Core.
        """
        with cls._state_guard():
            data = cls._read_lock()
            if data and data.get("pid") == pid:
                cls.LOCK_FILE.unlink(missing_ok=True)
            runtime = cls.read_runtime()
            if runtime and runtime.pid == pid:
                cls.RUNTIME_FILE.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Window management (multi-window VSCode support)
    # ------------------------------------------------------------------

    @classmethod
    def add_window(cls, pid: int) -> None:
        """Register an additional VSCode window PID with the running Core."""
        data = cls._read_lock()
        if data is None:
            return
        windows: list[int] = data.get("windows", [])
        if pid not in windows:
            windows.append(pid)
        data["windows"] = windows
        cls.LOCK_FILE.write_text(json.dumps(data), encoding="utf-8")

    @classmethod
    def remove_window(cls, pid: int) -> bool:
        """
        Remove a VSCode window PID from the active list.

        Returns True if this was the **last** window — caller should shut
        down the Core.  Returns False if other windows remain.
        """
        data = cls._read_lock()
        if data is None:
            return True  # No lock file → treat as last
        windows: list[int] = data.get("windows", [])
        windows = [w for w in windows if w != pid]
        if not windows:
            # Last window removed — clean up
            cls.LOCK_FILE.unlink(missing_ok=True)
            return True
        data["windows"] = windows
        cls.LOCK_FILE.write_text(json.dumps(data), encoding="utf-8")
        return False

    # ------------------------------------------------------------------
    # Port discovery
    # ------------------------------------------------------------------

    @classmethod
    def find_available_port(cls) -> int:
        """
        Return the first available TCP port starting with DEFAULT_PORT,
        falling back through FALLBACK_PORTS.

        Raises RuntimeError if no port is available.
        """
        import socket

        candidates = [cls.DEFAULT_PORT, *cls.FALLBACK_PORTS]
        for port in candidates:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise RuntimeError(
            f"No available port found in range "
            f"{cls.DEFAULT_PORT}–{cls.FALLBACK_PORTS.stop - 1}."
        )

    # ------------------------------------------------------------------
    # Runtime config
    # ------------------------------------------------------------------

    @staticmethod
    def current_entrypoint() -> str:
        """이 프로세스를 띄운 실행 파일의 절대경로.

        번들 바이너리(PyInstaller)면 실행 파일 자신, 소스 실행이면 main.py.
        확장이 "떠 있는 Core 가 이 워크스페이스의 것인가" 를 판단하는 근거다.
        """
        import sys

        if getattr(sys, "frozen", False):
            return str(Path(sys.executable).resolve())
        # sys.argv[0] 은 `python core/main.py` 의 main.py. 상대경로일 수 있어
        # 반드시 절대화한다 — 확장이 문자열로 비교한다.
        try:
            return str(Path(sys.argv[0]).resolve())
        except Exception:
            return ""

    @classmethod
    def write_runtime(cls, port: int, token: str, pid: int) -> None:
        """Persist runtime configuration to ~/.recoder/runtime.json."""
        _RECODER_DIR.mkdir(parents=True, exist_ok=True)
        config = RuntimeConfig(
            port=port,
            session_token=token,
            pid=pid,
            entrypoint=cls.current_entrypoint(),
        )
        cls._write_json(cls.RUNTIME_FILE, config.model_dump())

    @classmethod
    def read_runtime(cls) -> Optional[RuntimeConfig]:
        """Read and return the runtime config, or None if absent / malformed."""
        if not cls.RUNTIME_FILE.exists():
            return None
        try:
            data = json.loads(cls.RUNTIME_FILE.read_text(encoding="utf-8"))
            return RuntimeConfig(**data)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Stale process detection
    # ------------------------------------------------------------------

    @classmethod
    def check_stale_process(cls) -> bool:
        """
        Return True if the lock file refers to a PID that is no longer alive
        (stale process).
        """
        data = cls._read_lock()
        if data is None:
            return True  # No lock → nothing to be stale
        pid = data.get("pid")
        if pid is None:
            return True
        return not cls._pid_alive(pid)

    @classmethod
    def kill_stale_process(cls) -> None:
        """Remove stale lock and runtime files (the process is already gone)."""
        with cls._state_guard():
            if not cls.check_stale_process():
                return
            runtime = cls.read_runtime()
            if runtime and runtime.pid > 0 and cls._pid_alive(runtime.pid):
                return
            cls.LOCK_FILE.unlink(missing_ok=True)
            cls.RUNTIME_FILE.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # File permission hardening
    # ------------------------------------------------------------------

    @classmethod
    def set_file_permissions(cls, path: Path) -> None:
        """
        Restrict *path* to owner read/write only (0600 for files, 0700 for
        directories) on POSIX.  On Windows, attempt an ACL-based restriction
        but soft-fail if the tooling is unavailable.
        """
        if not path.exists():
            return

        if platform.system() == "Windows":
            cls._set_windows_permissions(path)
        else:
            try:
                mode = 0o700 if path.is_dir() else 0o600
                path.chmod(mode)
            except OSError:
                pass  # Soft fail

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @classmethod
    @contextmanager
    def _state_guard(cls):
        """Serialize metadata ownership changes across Core processes.

        Keep this guard file in place: unlinking a locked file would let another
        process lock a different inode. The OS releases the lock on process exit.
        """
        cls.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        cls.set_file_permissions(cls.LOCK_FILE.parent)
        guard = cls.LOCK_FILE.with_suffix(".guard")
        fd = os.open(guard, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "r+b") as stream:
            cls.set_file_permissions(guard)
            if os.name == "nt":
                import msvcrt
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @classmethod
    def _write_json(cls, path: Path, data: dict) -> None:
        """Publish complete metadata with private permissions from creation."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, default=str)
                stream.flush()
                os.fsync(stream.fileno())
            cls.set_file_permissions(Path(temporary))
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @classmethod
    def _read_lock(cls) -> Optional[dict]:
        if not cls.LOCK_FILE.exists():
            return None
        try:
            return json.loads(cls.LOCK_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Return True if *pid* corresponds to a running process."""
        if platform.system() == "Windows":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        else:
            try:
                os.kill(pid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False

    @staticmethod
    def _set_windows_permissions(path: Path) -> None:
        """
        Windows permission hardening — soft fail only (§11.3).

        The files live under the user's own home directory (~/.recoder/)
        which Windows already protects via NTFS user-account security.
        Attempting aggressive icacls /inheritance:r can accidentally lock
        out the same user's own PowerShell/CMD sessions, breaking runtime.json
        readability.  Per design spec §11.3: "보안보다 데모 가용성이 우선".
        We therefore log a notice and skip the icacls call on Windows.
        """
        import logging
        logging.getLogger(__name__).debug(
            "Windows ACL hardening skipped for %s "
            "(NTFS home-directory protection is sufficient for demo/dev use).",
            path,
        )
