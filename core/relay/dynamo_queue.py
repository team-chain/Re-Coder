"""
core/relay/dynamo_queue.py — Hybrid Cloud Relay 흐름 1 (설계서 §6.4.2)

PC 꺼짐 상태에서 Discord 로 들어온 명령을 DynamoDB 에 저장.
PC 가 켜지면 RelayPoller 가 큐를 polling 해서 비운다.

스키마:
  PK: user_id            (Discord user_id, string)
  SK: command_id         (uuid hex, prefix 로 created_at 의 epoch ms 정렬용 ULID 유사)
  Attrs:
    command_type   : "deploy" | "rollback" | "preflight" | "analyze" | ...
    payload        : JSON-encoded string
    source         : "discord" | "api" | ...
    created_at     : ISO8601 UTC
    status         : "pending" | "processing" | "done" | "failed" | "expired"
    ttl            : epoch seconds (24h)
    result         : (option) JSON-encoded result string
    error          : (option) 실패 사유
    updated_at     : ISO8601 UTC

설계 제약:
- boto3 는 lazy import (모듈 import 비용 회피)
- AWS 자격증명 미설정 시 RuntimeError("AWS credentials not configured")
- 모든 메서드는 dict 반환, 내부 exception 은 catch 해 dict 의 error 필드로 노출
- status 전환은 ConditionExpression 으로 동시성 안전
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TABLE_NAME = "recoder-command-queue"
TTL_SECONDS = 24 * 60 * 60  # 24h

VALID_STATUSES = {"pending", "processing", "done", "failed", "expired"}


# ── 유틸 ───────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_command_id() -> str:
    """epoch ms prefix + uuid4 hex → 사전순 정렬 가능한 ID (ULID 유사)."""
    ms = int(time.time() * 1000)
    return f"{ms:013d}-{uuid.uuid4().hex[:16]}"


# ── 클래스 ─────────────────────────────────────────────────────────────

class DynamoCommandQueue:
    """
    boto3 기반 DynamoDB 명령 큐 클라이언트.

    환경변수:
      RECODER_RELAY_QUEUE_TABLE   기본 "recoder-command-queue"
      AWS_REGION / AWS_DEFAULT_REGION / BEDROCK_REGION
      AWS_ACCESS_KEY_ID 또는 ~/.aws/credentials (boto3 표준 chain)
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        region: Optional[str] = None,
    ) -> None:
        self.table_name: str = (
            table_name
            or os.getenv("RECODER_RELAY_QUEUE_TABLE", DEFAULT_TABLE_NAME)
        )
        self.region: str = (
            region
            or os.getenv("AWS_DEFAULT_REGION")
            or os.getenv("AWS_REGION")
            or os.getenv("BEDROCK_REGION")
            or "ap-northeast-2"
        )

        # ── lazy import boto3 ────────────────────────────────────────
        try:
            import boto3  # noqa: WPS433 — lazy by design
            from botocore.exceptions import (  # noqa: WPS433
                NoCredentialsError,
                PartialCredentialsError,
                ClientError,
            )
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "boto3 not installed — pip install boto3"
            ) from exc

        self._ClientError = ClientError
        self._NoCredentialsError = NoCredentialsError

        try:
            self._client = boto3.client("dynamodb", region_name=self.region)
            # 자격증명 확인 — get_credentials 가 None 이면 raise
            session = boto3.session.Session(region_name=self.region)
            creds = session.get_credentials()
            if creds is None:
                raise RuntimeError("AWS credentials not configured")
        except (NoCredentialsError, PartialCredentialsError) as exc:
            raise RuntimeError("AWS credentials not configured") from exc
        except RuntimeError:
            raise
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                f"DynamoDB client initialization failed: {exc}"
            ) from exc

    # ── 테이블 관리 ────────────────────────────────────────────────────

    def ensure_table_exists(self) -> Dict[str, Any]:
        """
        테이블이 없으면 생성한다 (dev 편의용).
        - PK: user_id (S), SK: command_id (S)
        - TTL 속성: ttl
        """
        try:
            self._client.describe_table(TableName=self.table_name)
            return {"status": "ok", "created": False, "table": self.table_name}
        except self._ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                return {"status": "error", "error": str(exc)}

        try:
            self._client.create_table(
                TableName=self.table_name,
                KeySchema=[
                    {"AttributeName": "user_id", "KeyType": "HASH"},
                    {"AttributeName": "command_id", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "user_id", "AttributeType": "S"},
                    {"AttributeName": "command_id", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            waiter = self._client.get_waiter("table_exists")
            waiter.wait(TableName=self.table_name)
            # TTL 활성화
            try:
                self._client.update_time_to_live(
                    TableName=self.table_name,
                    TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
                )
            except Exception as ttl_exc:
                logger.warning("[relay] TTL setup failed: %s", ttl_exc)
            return {"status": "ok", "created": True, "table": self.table_name}
        except Exception as exc:
            logger.exception("[relay] ensure_table_exists failed")
            return {"status": "error", "error": str(exc)}

    def table_status(self) -> Dict[str, Any]:
        """테이블 존재 여부 + 상태 반환."""
        try:
            resp = self._client.describe_table(TableName=self.table_name)
            t = resp.get("Table", {})
            return {
                "exists": True,
                "table": self.table_name,
                "status": t.get("TableStatus", ""),
                "item_count": t.get("ItemCount", 0),
                "region": self.region,
            }
        except self._ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                return {"exists": False, "table": self.table_name, "region": self.region}
            return {"exists": False, "error": str(exc), "region": self.region}
        except Exception as exc:
            return {"exists": False, "error": str(exc), "region": self.region}

    # ── enqueue ───────────────────────────────────────────────────────

    def enqueue(
        self,
        user_id: str,
        command_type: str,
        payload: Dict[str, Any],
        source: str = "discord",
    ) -> Dict[str, Any]:
        """
        새 명령을 큐에 추가. status=pending.

        Returns:
            {status, command_id, created_at} 또는 {status: "error", error: ...}
        """
        if not user_id or not command_type:
            return {"status": "error", "error": "user_id and command_type are required"}

        command_id = _generate_command_id()
        now = _now_iso()
        ttl_epoch = int(time.time()) + TTL_SECONDS

        item = {
            "user_id":      {"S": str(user_id)},
            "command_id":   {"S": command_id},
            "command_type": {"S": command_type},
            "payload":      {"S": json.dumps(payload or {}, ensure_ascii=False)},
            "source":       {"S": source or "unknown"},
            "created_at":   {"S": now},
            "updated_at":   {"S": now},
            "status":       {"S": "pending"},
            "ttl":          {"N": str(ttl_epoch)},
        }

        try:
            self._client.put_item(
                TableName=self.table_name,
                Item=item,
                # 동일 command_id 가 이미 있으면 거부 (uuid 충돌 방지)
                ConditionExpression="attribute_not_exists(command_id)",
            )
            return {
                "status": "ok",
                "command_id": command_id,
                "created_at": now,
                "ttl_epoch": ttl_epoch,
            }
        except Exception as exc:
            logger.exception("[relay] enqueue failed")
            return {"status": "error", "error": str(exc)}

    # ── dequeue ───────────────────────────────────────────────────────

    def dequeue_pending(
        self,
        user_id: str,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """
        user_id 의 status=pending 명령을 최대 limit 건 가져와서
        status=processing 으로 conditional update.

        동시 polling 안전: 다른 워커가 먼저 잡으면 ConditionalCheckFailedException 으로 skip.

        Returns:
            {status: "ok", items: [...]} — items 는 dict 리스트
        """
        if not user_id:
            return {"status": "error", "error": "user_id is required", "items": []}

        try:
            resp = self._client.query(
                TableName=self.table_name,
                KeyConditionExpression="user_id = :u",
                FilterExpression="#st = :pending",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":u": {"S": str(user_id)},
                    ":pending": {"S": "pending"},
                },
                Limit=max(1, min(int(limit), 100)),
                ScanIndexForward=True,  # 오래된 명령 먼저
            )
        except Exception as exc:
            logger.exception("[relay] dequeue query failed")
            return {"status": "error", "error": str(exc), "items": []}

        claimed: List[Dict[str, Any]] = []
        now = _now_iso()
        for raw in resp.get("Items", []):
            command_id = raw.get("command_id", {}).get("S", "")
            if not command_id:
                continue
            try:
                self._client.update_item(
                    TableName=self.table_name,
                    Key={
                        "user_id":    {"S": str(user_id)},
                        "command_id": {"S": command_id},
                    },
                    UpdateExpression="SET #st = :proc, updated_at = :now",
                    ConditionExpression="#st = :pending",
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={
                        ":proc": {"S": "processing"},
                        ":pending": {"S": "pending"},
                        ":now": {"S": now},
                    },
                )
            except self._ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code == "ConditionalCheckFailedException":
                    # 다른 워커가 먼저 잡음 → skip
                    continue
                logger.warning("[relay] dequeue claim failed: %s", exc)
                continue
            except Exception as exc:
                logger.warning("[relay] dequeue claim failed: %s", exc)
                continue

            claimed.append(_to_plain(raw))

        return {"status": "ok", "items": claimed, "count": len(claimed)}

    # ── 완료 처리 ──────────────────────────────────────────────────────

    def mark_done(
        self,
        user_id: str,
        command_id: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """status: processing → done. result(JSON) 저장."""
        return self._terminal_update(
            user_id=user_id,
            command_id=command_id,
            new_status="done",
            extra_attrs={"result": json.dumps(result or {}, ensure_ascii=False)},
        )

    def mark_failed(
        self,
        user_id: str,
        command_id: str,
        error: str,
    ) -> Dict[str, Any]:
        """status: processing → failed. error 메시지 저장."""
        return self._terminal_update(
            user_id=user_id,
            command_id=command_id,
            new_status="failed",
            extra_attrs={"error": (error or "")[:1024]},
        )

    def _terminal_update(
        self,
        user_id: str,
        command_id: str,
        new_status: str,
        extra_attrs: Dict[str, str],
    ) -> Dict[str, Any]:
        if new_status not in VALID_STATUSES:
            return {"status": "error", "error": f"invalid status {new_status}"}
        now = _now_iso()
        # 동적 UpdateExpression
        set_parts = ["#st = :ns", "updated_at = :now"]
        names: Dict[str, str] = {"#st": "status"}
        values: Dict[str, Any] = {
            ":ns":  {"S": new_status},
            ":now": {"S": now},
            ":proc": {"S": "processing"},
        }
        for i, (k, v) in enumerate(extra_attrs.items()):
            placeholder = f":v{i}"
            name_alias = f"#k{i}"
            names[name_alias] = k
            values[placeholder] = {"S": str(v)}
            set_parts.append(f"{name_alias} = {placeholder}")

        try:
            self._client.update_item(
                TableName=self.table_name,
                Key={
                    "user_id":    {"S": str(user_id)},
                    "command_id": {"S": str(command_id)},
                },
                UpdateExpression="SET " + ", ".join(set_parts),
                # processing 상태에서만 종료 — 중복/경합 방지
                ConditionExpression="#st = :proc",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return {"status": "ok", "command_id": command_id, "new_status": new_status}
        except self._ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return {
                    "status": "error",
                    "error": "command not in processing state",
                    "command_id": command_id,
                }
            logger.exception("[relay] terminal_update failed")
            return {"status": "error", "error": str(exc)}
        except Exception as exc:
            logger.exception("[relay] terminal_update failed")
            return {"status": "error", "error": str(exc)}

    # ── 이력 조회 ─────────────────────────────────────────────────────

    def list_for_user(self, user_id: str, limit: int = 50) -> Dict[str, Any]:
        """user_id 의 최근 명령 이력 (최신순)."""
        if not user_id:
            return {"status": "error", "error": "user_id is required", "items": []}
        try:
            resp = self._client.query(
                TableName=self.table_name,
                KeyConditionExpression="user_id = :u",
                ExpressionAttributeValues={":u": {"S": str(user_id)}},
                Limit=max(1, min(int(limit), 200)),
                ScanIndexForward=False,  # 최신 먼저
            )
            items = [_to_plain(raw) for raw in resp.get("Items", [])]
            return {"status": "ok", "items": items, "count": len(items)}
        except Exception as exc:
            logger.exception("[relay] list_for_user failed")
            return {"status": "error", "error": str(exc), "items": []}


# ── 내부: DynamoDB raw item → plain dict ──────────────────────────────

def _to_plain(raw: Dict[str, Any]) -> Dict[str, Any]:
    """DynamoDB JSON ({"S": ...}) 형태를 평탄한 dict 로 변환."""
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            out[k] = v
            continue
        if "S" in v:
            out[k] = v["S"]
        elif "N" in v:
            try:
                out[k] = int(v["N"])
            except (ValueError, TypeError):
                try:
                    out[k] = float(v["N"])
                except Exception:
                    out[k] = v["N"]
        elif "BOOL" in v:
            out[k] = bool(v["BOOL"])
        elif "NULL" in v:
            out[k] = None
        else:
            out[k] = v
    # payload / result JSON decode 시도
    for jf in ("payload", "result"):
        if jf in out and isinstance(out[jf], str):
            try:
                out[jf] = json.loads(out[jf])
            except Exception:
                pass
    return out
