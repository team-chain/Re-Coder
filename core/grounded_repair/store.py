"""SQLite run history and verified cache, separate from raw logs/secrets."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from execution_policy import ExecutionDeniedError, ExecutionPolicy


def integrity(result: dict, key: bytes = b"") -> str:
    """기억(재사용) 대상의 지문 — 작업 위치·상태·고친 내용·출처. 저장 뒤 누가 바꾸면 다시 쓰지 않는다(OWASP ASI06, 2.0.29).
    2.0.32: 설치마다 다른 비밀 키로 서명(HMAC) — 키 없이 DB 만 고친 사람은 지문을 다시 계산할 수 없다."""
    core = {
        k: result.get(k)
        for k in (
            "id",
            "workspace",
            "manifest",
            "status",
            "edits",
            "corpus_version",
            "strategy",
            "sources",
            "execution_policy",
            "policy_checks",
            "policy_error",
        )
    }
    body = json.dumps(core, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    if key:
        return "h1:" + hmac.new(key, body, hashlib.sha256).hexdigest()
    return hashlib.sha256(body).hexdigest()


def _install_key(folder: Path) -> bytes:
    """설치마다 한 번 만드는 서명 키. 여러 스레드·프로세스가 동시에 처음 만들어도 같은 키를 쓰도록 배타적 생성(O_EXCL)."""
    path = folder / "repair_store.key"
    for _ in range(50):
        try:
            data = path.read_bytes()
            if len(data) >= 32:
                return data
        except OSError:
            pass
        try:
            fd = os.open(
                str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
            )
        except FileExistsError:
            import time as _t

            _t.sleep(0.02)  # 다른 쪽이 쓰는 중 — 다 쓸 때까지
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(32))
    #: 2.0.33 키 파일이 짧은 채로 남아 있으면(쓰다 꺼짐·디스크 가득) 예전에는 그 짧은 값(빈 값이면 키 없는 지문)을 그대로 썼다.
    #: 새 키를 임시 파일에 다 쓴 뒤 바꿔 끼우고(원자적) 실제로 남은 것을 다시 읽는다. 그래도 안 되면 이번 실행만 쓰는 키
    #: — 저장된 기억은 검증되지 않아 다시 쓰지 않을 뿐, 키 없이 서명하는 일은 없다.
    try:
        tmp = folder / f".repair_store.key.{os.getpid()}.{secrets.token_hex(4)}"
        fd = os.open(
            str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
        )
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(32))
        os.replace(tmp, path)
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    return secrets.token_bytes(32)


class RepairStore:
    def __init__(self, path: Path, policy=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.policy = policy or ExecutionPolicy()
        self._key = _install_key(path.parent)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS repair_runs (id TEXT PRIMARY KEY, cache_key TEXT, created REAL, payload TEXT NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS repair_cache ON repair_runs(cache_key, created)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def save(self, result: dict, cache_key: str = ""):
        result["integrity"] = integrity(result, self._key)
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO repair_runs VALUES (?, ?, ?, ?)",
                (result["id"], cache_key, time.time(), json.dumps(result, ensure_ascii=False)),
            )

    def get(self, run_id: str):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM repair_runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def cached(self, key: str):
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM repair_runs WHERE cache_key=? AND created>? ORDER BY created DESC LIMIT 20",
                (key, time.time() - 86400),
            ).fetchall()
        for (payload,) in rows:
            r = json.loads(payload)
            #: 검증을 통과한 결과만, 저장 때 지문이 그대로일 때만 다시 쓴다.
            if r["status"] in {"ready_for_approval", "applied"} and hmac.compare_digest(
                str(r.get("integrity") or ""), integrity(r, self._key)
            ):
                return r
        return None

    def approve(self, run_id: str, workspace: str | None = None):
        from grounded_repair.workspace import apply_exact

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload, created FROM repair_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not row:
                raise ValueError("Unknown repair run")
            r = json.loads(row[0])
            if (
                workspace is not None
                and Path(workspace).resolve() != Path(r["workspace"]).resolve()
            ):
                raise ValueError("Repair belongs to a different workspace")
            if r["status"] != "ready_for_approval" or time.time() - row[1] > 3600:
                raise ValueError("Repair is not verified, already applied, or expired")
            #: 2.0.32 — 파일을 쓰기 **전에** 지문을 확인한다(전에는 쓰고 나서 확인해, 바뀐 기록이 이미 적용됐다).
            #: 지문이 없는 기록도 거부한다.
            if not hmac.compare_digest(str(r.get("integrity") or ""), integrity(r, self._key)):
                raise ValueError("Repair record was modified after verification")
            # Approval is a new decision. Never reuse an allow recorded during preparation.
            try:
                check = self.policy.authorize(
                    "repair.apply",
                    r["workspace"],
                    paths=list(r["edits"]),
                    verified=True,
                    approved=True,
                    stage="build",
                )
            except ExecutionDeniedError as exc:
                r["policy_checks"] = (r.get("policy_checks", []) + [exc.record])[-32:]
                r["policy_error"] = exc.record["reason"]
                r["integrity"] = integrity(r, self._key)
                db.execute(
                    "UPDATE repair_runs SET payload=? WHERE id=?",
                    (json.dumps(r, ensure_ascii=False), run_id),
                )
                db.commit()  # Retain the denial without extending the verification lifetime.
                raise
            r["policy_checks"] = (r.get("policy_checks", []) + [check])[-32:]
            r.pop("policy_error", None)
            apply_exact(Path(r["workspace"]), r["manifest"], r["edits"])
            r["status"] = "applied"
            r["approval_required"] = False
            r["integrity"] = integrity(r, self._key)
            db.execute(
                "UPDATE repair_runs SET payload=? WHERE id=?",
                (json.dumps(r, ensure_ascii=False), run_id),
            )
        return r
