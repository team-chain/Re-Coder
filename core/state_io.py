"""ReCoder Core — 상태 파일(JSON) 안전하게 쓰기 (2.0.32)

여러 스레드(API 요청 · 하루 한 번 재검사 · 정리 작업)가 같은 파일을 고친다. 공용 임시 파일(.tmp) 하나로 쓰면 서로의 내용을
덮거나 FileNotFoundError 가 났고, Windows 는 다른 스레드가 파일을 열어 둔 순간 os.replace 가 PermissionError 를 낸다.
  · 파일마다 잠금(같은 프로세스 안)
  · 쓰기마다 고유한 임시 파일(mkstemp) → os.replace, Windows 에서 잠깐 막히면 몇 번 다시
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_LOCKS: dict[str, threading.RLock] = {}
_GUARD = threading.Lock()


def lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(str(path)))
    with _GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def write_json(path: Path, data: Any, *, indent: int | None = 1) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_for(path):
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=indent)
            for attempt in range(10):
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def read_json(path: Path, default: Any) -> Any:
    try:
        with lock_for(Path(path)):
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default
