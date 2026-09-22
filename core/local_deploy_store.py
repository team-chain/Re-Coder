"""Durable records for the local deployment route; no Docker/AWS calls."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from schemas import DeploymentRecord

log = logging.getLogger(__name__)
load_incomplete = False
save_failed = False


def store_path() -> Path:
    override = os.getenv("RECODER_LOCAL_DEPLOY_STORE", "").strip()
    return Path(override) if override else Path.home() / ".recoder" / "local_deployments.json"


def timestamp(value: datetime) -> float:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


def load_records() -> dict[str, DeploymentRecord]:
    global load_incomplete
    load_incomplete = False
    records: dict[str, DeploymentRecord] = {}
    # Import older agent JSONL records, then let the current route's snapshot win.
    files = sorted((store_path().parent / "projects").glob("*_deployments.jsonl"))
    files.append(store_path())
    for path in files:
        if not path.exists():
            continue
        try:
            if path.suffix == ".jsonl":
                rows = []
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        load_incomplete = True
                        log.warning("Skipped an invalid local deployment history line")
            else:
                rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError("Expected a record list")
            for row in rows:
                try:
                    # Never manufacture IDs/timestamps for incomplete old records.
                    if not isinstance(row, dict) or not row.get("deployment_id") or not row.get("deployed_at"):
                        raise ValueError("Missing identity or time")
                    row = dict(row)
                    if row.get("status") == "deployed":
                        row["status"] = "success"
                    record = DeploymentRecord.model_validate(row)
                    if record.rollback_status == "running":
                        record.rollback_status = "unknown"
                        record.rollback_error = "Core가 재시작되어 롤백 결과를 확인하지 못했습니다. 현재 컨테이너 상태를 확인하세요."
                    records[record.deployment_id] = record
                except (ValueError, TypeError):
                    load_incomplete = True
                    log.warning("Skipped an invalid local deployment record")
        except (OSError, ValueError):
            load_incomplete = True
            log.warning("Could not read local deployment history: %s", path.name)
    return records


def save_records(records: dict[str, DeploymentRecord]) -> bool:
    """Atomic 0600 snapshot: environment values are required to restore a release."""
    global save_failed
    path = store_path()
    temp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve insertion order so equal timestamps retain their tie-break order.
        payload = [record.model_dump(mode="json") for record in records.values()]
        fd, name = tempfile.mkstemp(prefix=".local-deployments-", dir=path.parent)
        temp_path = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(path)
        save_failed = False
        return True
    except (OSError, TypeError, ValueError):
        save_failed = True
        log.warning("Could not save local deployment history")
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
