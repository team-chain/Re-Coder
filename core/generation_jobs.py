"""대규모 코드 생성 작업의 체크포인트 — 중간에 멈춰도 다 만든 것은 버리지 않는다.

파일 하나·조각 하나가 끝날 때마다 상태를 저장한다. 할당량 초과·네트워크 단절·창 닫힘으로
멈추면 "일시 정지"로 남고, 같은 요청을 job_id 와 함께 다시 보내면 멈춘 지점부터 이어 만든다.

저장 위치는 사용자 홈(~/.recoder/generation) — 프로젝트 폴더를 더럽히지 않고, 적용 전 결과가
워크스페이스에 섞여 들어가지도 않는다(적용은 여전히 사람이 미리보기를 보고 승인할 때만).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_ID = re.compile(r"^[a-f0-9]{12,32}$")
#: 오래된 체크포인트는 자동 정리한다(이어 만들기는 보통 몇 시간 안에 한다).
_KEEP_SECONDS = 7 * 24 * 3600


def _base() -> Path:
    root = os.environ.get("RECODER_GENERATION_DIR") or str(Path.home() / ".recoder" / "generation")
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_job_id() -> str:
    return uuid.uuid4().hex[:16]


def valid_job_id(job_id: str) -> bool:
    return bool(job_id) and bool(_ID.match(job_id))


def fingerprint(**parts: Any) -> str:
    """같은 요청인지 확인하는 지문. 다른 요청에 남의 체크포인트가 섞이면 안 된다."""
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _path(job_id: str) -> Path:
    if not valid_job_id(job_id):
        raise ValueError("작업 ID 형식이 올바르지 않습니다.")
    return _base() / f"{job_id}.json"


def load(job_id: str, fp: str) -> dict | None:
    """지문이 같은 체크포인트만 돌려준다. 없거나 다르면 None(처음부터)."""
    try:
        path = _path(job_id)
    except ValueError:
        return None
    try:
        with _LOCK:
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("fingerprint") != fp:
        return None
    return data


def save(job_id: str, state: dict) -> None:
    """원자적 저장(임시 파일 → 이름 바꾸기). 저장 실패는 생성을 멈추지 않는다."""
    try:
        path = _path(job_id)
        state = {**state, "updated_at": time.time()}
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        with _LOCK:
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def discard(job_id: str) -> None:
    try:
        with _LOCK:
            _path(job_id).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def prune() -> None:
    now = time.time()
    try:
        for path in _base().glob("*.json"):
            try:
                if now - path.stat().st_mtime > _KEEP_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass


class GenerationPaused(RuntimeError):
    """생성이 중간에 멈췄지만 만든 것은 저장됐다 — 같은 job_id 로 이어 만들 수 있다."""

    def __init__(self, message: str, job_id: str, done: int = 0, total: int = 0, reason: str = "",
                 failed: list | None = None) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.done = done
        self.total = total
        self.reason = reason
        #: 정해진 횟수를 넘겨 실패한 파일 [{file, kind, reason}] — 화면이 [다시 쓰기]/[빼고 받기]를 보여 준다.
        self.failed = [{"file": str(f.get("file") or ""), "kind": str(f.get("kind") or ""), "reason": str(f.get("reason") or "")}
                       for f in (failed or []) if isinstance(f, dict)]

    def progress_payload(self) -> dict:
        return {"resumable": True, "resume_job": self.job_id, "done_count": self.done,
                "total": self.total, "reason": self.reason, "failed": self.failed}
