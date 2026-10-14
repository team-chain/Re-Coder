"""SQLite run history and verified cache, separate from raw logs/secrets."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path


def integrity(result: dict) -> str:
    """기억(재사용) 대상의 지문 — 작업 위치·상태·고친 내용·출처. 저장 뒤 누가 바꾸면 다시 쓰지 않는다(OWASP ASI06, 2.0.29)."""
    core = {k: result.get(k) for k in ("id", "workspace", "manifest", "status", "edits", "corpus_version", "strategy", "sources")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


class RepairStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS repair_runs (id TEXT PRIMARY KEY, cache_key TEXT, created REAL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS repair_cache ON repair_runs(cache_key, created)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def save(self, result: dict, cache_key: str = ""):
        result["integrity"] = integrity(result)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO repair_runs VALUES (?, ?, ?, ?)",
                       (result["id"], cache_key, time.time(), json.dumps(result, ensure_ascii=False)))

    def get(self, run_id: str):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM repair_runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def cached(self, key: str):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM repair_runs WHERE cache_key=? AND created>? ORDER BY created DESC LIMIT 20",
                              (key, time.time() - 86400)).fetchall()
        for (payload,) in rows:
            r = json.loads(payload)
            #: 검증을 통과한 결과만, 저장 때 지문이 그대로일 때만 다시 쓴다.
            if r["status"] in {"ready_for_approval", "applied"} and r.get("integrity") == integrity(r):
                return r
        return None

    def approve(self, run_id: str, workspace: str | None = None):
        from grounded_repair.workspace import apply_exact
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload, created FROM repair_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("Unknown repair run")
            r = json.loads(row[0])
            if workspace is not None and Path(workspace).resolve() != Path(r["workspace"]).resolve():
                raise ValueError("Repair belongs to a different workspace")
            if r["status"] != "ready_for_approval" or time.time() - row[1] > 3600:
                raise ValueError("Repair is not verified, already applied, or expired")
            apply_exact(Path(r["workspace"]), r["manifest"], r["edits"])
            if r.get("integrity") and r["integrity"] != integrity(r):
                raise ValueError("Repair record was modified after verification")
            r["status"] = "applied"
            r["approval_required"] = False
            r["integrity"] = integrity(r)
            db.execute("UPDATE repair_runs SET payload=? WHERE id=?", (json.dumps(r, ensure_ascii=False), run_id))
        return r
