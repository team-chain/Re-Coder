"""
Control Plane — Q2-A3: AuditLog Service

설계서 §Q2-A3:
- hash chain: org_id 단위 monotonic sequence
- event_hash = SHA-256(previous_event_hash + canonical_json(event_body))
- INSERT 시 DB transaction 안에서 row-level lock
- tamper-evident (조작 흔적 사후 탐지 가능)
- UPDATE/DELETE는 DB 트리거로 금지
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.db.models import AuditEvent, AuditSeqCounter, PendingAuditQueue
from control_plane.models.schemas import (
    AuditAction,
    AuditEventCreate,
    AuditEventResponse,
    AuditSyncRequest,
    AuditSyncResponse,
    PendingAuditEvent,
)

logger = logging.getLogger(__name__)

_GENESIS_HASH = "0" * 64   # 첫 번째 이벤트의 previous_event_hash


class AuditService:
    """
    AuditLog 기록 + hash chain 무결성 유지.

    동시성 안전:
    - org_id 단위 AuditSeqCounter에 SELECT FOR UPDATE
    - 하나의 DB transaction 안에서 seq 증가 + hash 계산 + INSERT
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # 이벤트 기록
    # ------------------------------------------------------------------

    async def record(
        self,
        org_id: str,
        actor_user_id: str,
        event: AuditEventCreate,
        actor_device_id: Optional[str] = None,
        is_suspicious: bool = False,
    ) -> AuditEventResponse:
        """
        AuditLog에 이벤트 기록.

        1. AuditSeqCounter에서 마지막 seq + hash를 FOR UPDATE로 잠금
        2. 새 seq 계산
        3. event_hash 계산
        4. AuditEvent INSERT
        5. AuditSeqCounter 업데이트
        모두 하나의 transaction 안에서 실행된다.
        """
        # 1. Counter 조회 + 잠금 (row-level lock)
        counter = await self._get_or_create_counter(org_id)

        # 2. Seq 증가
        new_seq = counter.last_seq + 1

        # 3. Hash 계산
        event_body = self._canonical_json(
            org_id=org_id,
            seq=new_seq,
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            action=event.action.value,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            occurred_at=event.occurred_at.isoformat(),
            extra=event.extra,
        )
        event_hash = self._compute_hash(counter.last_event_hash, event_body)

        # 4. INSERT
        audit = AuditEvent(
            org_id=org_id,
            seq=new_seq,
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            before_state=event.before_state,
            after_state=event.after_state,
            ip_address=event.ip_address,
            occurred_at=event.occurred_at,
            event_hash=event_hash,
            previous_event_hash=counter.last_event_hash,
            policy_bundle_version=event.policy_bundle_version,
            extra=event.extra,
            is_suspicious=is_suspicious,
        )
        self._db.add(audit)

        # 5. Counter 업데이트
        counter.last_seq = new_seq
        counter.last_event_hash = event_hash

        await self._db.flush()
        logger.debug("AuditEvent recorded: org=%s seq=%d action=%s", org_id, new_seq, event.action.value)

        return AuditEventResponse(
            event_id=audit.event_id,
            org_id=org_id,
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            occurred_at=event.occurred_at,
            event_hash=event_hash,
            previous_event_hash=audit.previous_event_hash,
            policy_bundle_version=event.policy_bundle_version,
        )

    # ------------------------------------------------------------------
    # 오프라인 재전송
    # ------------------------------------------------------------------

    async def sync_pending(
        self,
        org_id: str,
        actor_user_id: str,
        request: AuditSyncRequest,
        device_is_suspicious: bool = False,
    ) -> AuditSyncResponse:
        """
        오프라인 중 쌓인 pending events 일괄 수신.

        device_is_suspicious=True이면 모든 이벤트를 suspicious 표시.
        """
        accepted = 0
        rejected = 0
        reasons: list[str] = []

        for ev in request.events:
            try:
                await self.record(
                    org_id=org_id,
                    actor_user_id=actor_user_id,
                    event=ev,
                    actor_device_id=request.device_id,
                    is_suspicious=device_is_suspicious,
                )
                accepted += 1
            except Exception as exc:
                rejected += 1
                reasons.append(f"{ev.action.value}: {exc}")
                logger.warning("Pending audit sync rejected: %s", exc)

        return AuditSyncResponse(
            accepted=accepted,
            rejected=rejected,
            rejected_reasons=reasons,
        )

    # ------------------------------------------------------------------
    # 무결성 검증
    # ------------------------------------------------------------------

    async def verify_chain(self, org_id: str, limit: int = 1000) -> tuple[bool, Optional[str]]:
        """
        hash chain 무결성 검증.

        Returns (valid: bool, error_message: str | None)
        """
        result = await self._db.execute(
            select(AuditEvent)
            .where(AuditEvent.org_id == org_id)
            .order_by(AuditEvent.seq)
            .limit(limit)
        )
        events = result.scalars().all()

        prev_hash = _GENESIS_HASH
        for event in events:
            if event.previous_event_hash != prev_hash:
                return False, (
                    f"Hash chain broken at seq={event.seq}: "
                    f"expected previous_hash={prev_hash[:12]}… "
                    f"got {event.previous_event_hash[:12]}…"
                )
            # 재계산 검증
            body = self._canonical_json(
                org_id=event.org_id,
                seq=event.seq,
                actor_user_id=event.actor_user_id,
                actor_device_id=event.actor_device_id,
                action=event.action.value,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                occurred_at=event.occurred_at.isoformat(),
                extra=event.extra or {},
            )
            expected_hash = self._compute_hash(prev_hash, body)
            if expected_hash != event.event_hash:
                return False, f"Hash mismatch at seq={event.seq}"
            prev_hash = event.event_hash

        return True, None

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------

    async def list_events(
        self,
        org_id: str,
        limit: int = 50,
        offset: int = 0,
        action_filter: Optional[AuditAction] = None,
    ) -> list[AuditEventResponse]:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.org_id == org_id)
            .order_by(AuditEvent.seq.desc())
            .limit(limit)
            .offset(offset)
        )
        if action_filter:
            stmt = stmt.where(AuditEvent.action == action_filter)

        result = await self._db.execute(stmt)
        events = result.scalars().all()
        return [
            AuditEventResponse(
                event_id=e.event_id,
                org_id=e.org_id,
                actor_user_id=e.actor_user_id,
                actor_device_id=e.actor_device_id,
                action=e.action,
                resource_type=e.resource_type,
                resource_id=e.resource_id,
                occurred_at=e.occurred_at,
                event_hash=e.event_hash,
                previous_event_hash=e.previous_event_hash,
                policy_bundle_version=e.policy_bundle_version,
            )
            for e in events
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_or_create_counter(self, org_id: str) -> AuditSeqCounter:
        """FOR UPDATE 잠금으로 counter 조회 (없으면 생성)"""
        # PostgreSQL SELECT FOR UPDATE
        result = await self._db.execute(
            select(AuditSeqCounter)
            .where(AuditSeqCounter.org_id == org_id)
            .with_for_update()
        )
        counter = result.scalar_one_or_none()
        if counter is None:
            counter = AuditSeqCounter(
                org_id=org_id,
                last_seq=0,
                last_event_hash=_GENESIS_HASH,
            )
            self._db.add(counter)
            await self._db.flush()
        return counter

    @staticmethod
    def _canonical_json(**kwargs: Any) -> str:
        """결정론적 JSON 직렬화 (key 정렬)"""
        return json.dumps(kwargs, sort_keys=True, ensure_ascii=False, default=str)

    @staticmethod
    def _compute_hash(previous_hash: str, event_body: str) -> str:
        """event_hash = SHA-256(previous_hash + canonical_json(body))"""
        raw = (previous_hash + event_body).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()
