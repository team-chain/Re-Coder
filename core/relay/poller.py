"""
core/relay/poller.py — Hybrid Cloud Relay 흐름 1 백그라운드 polling.

PC 가 켜지면 RECODER_RELAY_ENABLED=true 일 때 자동 시작.
60 초 간격으로 DynamoCommandQueue.dequeue_pending 을 호출하고,
받은 각 명령을 적절한 핸들러로 dispatch 한다.

설계 §6.4.2 흐름 1.

환경변수:
  RECODER_RELAY_ENABLED         "true" 일 때만 server.py 가 start() 호출
  RECODER_RELAY_USER_IDS        콤마 구분 user_id 목록 (이 PC 가 polling 할 사용자)
                                미설정 시 RECODER_RELAY_USER_ID 단일 값 사용
  RECODER_RELAY_POLL_INTERVAL   초 단위, 기본 60
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 60.0


class RelayPoller:
    """
    DynamoCommandQueue 를 주기적으로 polling 해서 명령을 dispatch.

    자격증명이 없거나 boto3 import 실패 시 활성화 자체를 skip 한다 (catch).
    """

    def __init__(
        self,
        user_ids: Optional[List[str]] = None,
        interval_seconds: float = DEFAULT_POLL_INTERVAL,
        per_user_limit: int = 10,
    ) -> None:
        self.user_ids: List[str] = list(user_ids or self._load_user_ids_from_env())
        self.interval: float = float(
            os.getenv("RECODER_RELAY_POLL_INTERVAL", str(interval_seconds))
        )
        self.per_user_limit: int = per_user_limit
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._queue = None  # DynamoCommandQueue (lazy)
        self._enabled: bool = False

    # ── env 헬퍼 ───────────────────────────────────────────────────────

    @staticmethod
    def _load_user_ids_from_env() -> List[str]:
        raw = os.getenv("RECODER_RELAY_USER_IDS", "").strip()
        if raw:
            return [u.strip() for u in raw.split(",") if u.strip()]
        single = os.getenv("RECODER_RELAY_USER_ID", "").strip()
        return [single] if single else []

    # ── 라이프사이클 ──────────────────────────────────────────────────

    def start(self) -> Dict[str, Any]:
        """
        백그라운드 polling 시작.

        Returns:
            {status: "ok" | "disabled", reason?: str}
        """
        if self._task and not self._task.done():
            return {"status": "ok", "message": "already running"}

        if not self.user_ids:
            logger.warning(
                "[relay] RECODER_RELAY_USER_IDS 미설정 — poller 비활성화"
            )
            return {"status": "disabled", "reason": "no user_ids configured"}

        # DynamoCommandQueue 인스턴스 생성 — 자격증명 없으면 catch 하고 비활성화
        try:
            from .dynamo_queue import DynamoCommandQueue
            self._queue = DynamoCommandQueue()
        except RuntimeError as exc:
            logger.warning("[relay] poller disabled: %s", exc)
            return {"status": "disabled", "reason": str(exc)}
        except Exception as exc:  # pragma: no cover
            logger.exception("[relay] poller init failed")
            return {"status": "disabled", "reason": str(exc)}

        self._stop_event = asyncio.Event()
        self._enabled = True
        self._task = asyncio.create_task(self._run_loop(), name="relay-poller")
        logger.info(
            "[relay] poller started — interval=%ss user_ids=%s",
            self.interval, self.user_ids,
        )
        return {"status": "ok", "user_ids": self.user_ids, "interval": self.interval}

    async def stop(self) -> None:
        """graceful stop."""
        if self._stop_event:
            self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:  # pragma: no cover
                pass
        self._enabled = False
        logger.info("[relay] poller stopped")

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    # ── polling 루프 ──────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        assert self._stop_event is not None
        try:
            while not self._stop_event.is_set():
                for uid in self.user_ids:
                    try:
                        await self._poll_user(uid)
                    except Exception as exc:
                        logger.exception("[relay] poll error for user=%s: %s", uid, exc)

                # interval 만큼 대기하되 stop 시 즉시 깨움
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.interval,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:  # pragma: no cover
            logger.info("[relay] poller cancelled")
            raise

    async def _poll_user(self, user_id: str) -> None:
        assert self._queue is not None
        # boto3 호출은 blocking → thread 로
        result = await asyncio.to_thread(
            self._queue.dequeue_pending, user_id, self.per_user_limit
        )
        if result.get("status") != "ok":
            return
        items = result.get("items", [])
        if not items:
            return
        logger.info("[relay] user=%s dequeued %d commands", user_id, len(items))

        for item in items:
            await self._dispatch_one(user_id, item)

    # ── dispatcher ────────────────────────────────────────────────────

    async def _dispatch_one(self, user_id: str, item: Dict[str, Any]) -> None:
        assert self._queue is not None
        command_id = item.get("command_id", "")
        command_type = item.get("command_type", "")
        payload = item.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {"_raw": payload}

        logger.info(
            "[relay] dispatch user=%s command_id=%s type=%s",
            user_id, command_id, command_type,
        )

        try:
            result = await _run_handler(command_type, payload)
        except Exception as exc:
            logger.exception("[relay] handler crashed")
            await asyncio.to_thread(
                self._queue.mark_failed, user_id, command_id, str(exc)
            )
            return

        if isinstance(result, dict) and result.get("status") == "error":
            await asyncio.to_thread(
                self._queue.mark_failed,
                user_id,
                command_id,
                str(result.get("error") or "unknown"),
            )
        else:
            await asyncio.to_thread(
                self._queue.mark_done, user_id, command_id, result or {}
            )


# ── handler dispatch ──────────────────────────────────────────────────

async def _run_handler(command_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    명령 type 에 따라 적절한 에이전트/오케스트레이터 호출.

    현재 지원:
    - "analyze"   → analyzer.analyze (간소화)
    - "deploy"    → local_deploy_agent (간소화: 결과만 dict 반환)
    - "noop"      → 테스트용 echo
    - 기타        → status=error 반환

    핸들러를 늘릴 때는 본 함수에 case 추가 → mark_done/mark_failed 가 자동 처리.
    """
    ctype = (command_type or "").lower()

    if ctype == "noop":
        return {"status": "ok", "echo": payload}

    if ctype == "analyze":
        try:
            from analyzer import analyze as analyzer_analyze  # type: ignore
            from schemas import AnalyzeRequest  # type: ignore
        except Exception as exc:
            return {"status": "error", "error": f"analyzer not available: {exc}"}
        try:
            # 릴레이는 에러 본문을 `error_text` 로 따로 보낼 수 있는데,
            # AnalyzeRequest 스키마엔 그 필드가 없어 조용히 버려졌다. 그러면
            # analyzer 는 빈 문자열을 분석해 "에러 없음"을 성공으로 돌려준다.
            # terminal_output 이 비어 있을 때만 error_text 를 그 자리에 넣는다.
            terminal_output = payload.get("terminal_output", "") or payload.get("error_text", "")
            req = AnalyzeRequest(
                workspace_path=payload.get("workspace_path", ""),
                terminal_output=terminal_output,
                project_id=payload.get("project_id", ""),
                selected_text=payload.get("selected_text", ""),
                command=payload.get("command", ""),
                # analyzer 가 프롬프트·캐시 키에 쓰는 필드 — 빠뜨리면 클라우드
                # 분석이 프로젝트 맥락을 잃어 진단 품질이 떨어진다.
                project_files_summary=payload.get("project_files_summary", ""),
            )
            res = await analyzer_analyze(req, session_id=payload.get("session_id", ""))
            return {"status": "ok", "result": _safe_to_dict(res)}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    if ctype in ("deploy", "deploy_local"):
        # Local deploy 는 blocking 이라 결과만 표시. 실제 실행은 향후 OrchestratorFSM 으로.
        return {
            "status": "ok",
            "queued": True,
            "note": "deploy command received; trigger via OrchestratorFSM is TODO",
            "payload": payload,
        }

    return {"status": "error", "error": f"unknown command_type: {command_type}"}


def _safe_to_dict(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            return str(obj)
    if isinstance(obj, (dict, list, str, int, float, bool)) or obj is None:
        return obj
    return str(obj)
