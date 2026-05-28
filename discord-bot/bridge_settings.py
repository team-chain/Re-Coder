"""
discord-bot/bridge_settings.py — ReCoder Bridge 동적 설정 저장소.

사용자가 .env 파일을 직접 만지지 않고 VSCode Workbench → Build 탭에서
채널 ID 같은 설정을 바꿀 수 있도록, 봇 프로세스가 디스크에 영속 저장한다.

저장 파일: discord-bot/bridge_settings.json
형식:
    {
      "make_channel_id": 123456789012345678,
      "updated_at": "2026-05-27T10:30:00Z"
    }

읽기/쓰기 우선순위:
  1. bridge_settings.json (Workbench UI로 설정됨)
  2. 환경변수 RECODER_MAKE_CHANNEL_ID (.env, fallback)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# 봇 디렉토리 기준 절대 경로 — 어디서 봇을 실행해도 같은 위치
_SETTINGS_PATH = Path(__file__).resolve().parent / "bridge_settings.json"

_lock = threading.Lock()
_cache: Optional[dict] = None


def _load() -> dict:
    """파일에서 설정을 로드(없으면 빈 dict). 캐시한다."""
    global _cache
    if _cache is not None:
        return _cache
    if _SETTINGS_PATH.exists():
        try:
            _cache = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(_cache, dict):
                _cache = {}
        except Exception as exc:
            log.warning("bridge_settings.json 파싱 실패 — 빈 설정으로 시작: %s", exc)
            _cache = {}
    else:
        _cache = {}
    return _cache


def _save(data: dict) -> None:
    """디스크에 atomic write (.tmp → rename)."""
    tmp = _SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(_SETTINGS_PATH)


def get_make_channel_id() -> int:
    """현재 활성 채널 ID를 반환. 우선순위: 저장된 값 → .env fallback → 0."""
    with _lock:
        data = _load()
        stored = data.get("make_channel_id")
        if isinstance(stored, int) and stored > 0:
            return stored
    # fallback: 환경변수
    try:
        env_val = int(os.getenv("RECODER_MAKE_CHANNEL_ID", "0") or 0)
        return env_val
    except ValueError:
        return 0


def set_make_channel_id(channel_id: int) -> None:
    """채널 ID를 저장. 0이면 비활성화로 간주하고 키 제거."""
    global _cache
    with _lock:
        data = _load()
        if channel_id and channel_id > 0:
            data["make_channel_id"] = int(channel_id)
        else:
            data.pop("make_channel_id", None)
        data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _save(data)
        _cache = data
    log.info("ReCoder Bridge 채널 ID 변경: %s", channel_id or "(해제됨)")


def get_settings_snapshot() -> dict:
    """UI 표시용 전체 스냅샷."""
    with _lock:
        data = dict(_load())
    # env fallback 정보 함께 노출
    env_id = os.getenv("RECODER_MAKE_CHANNEL_ID", "")
    if env_id and "make_channel_id" not in data:
        try:
            data["make_channel_id_env_fallback"] = int(env_id)
        except ValueError:
            pass
    return data
