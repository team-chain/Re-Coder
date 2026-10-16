"""LLM 비용 원장 — 모든 호출 기록이 모이는 **한 곳**.

보드 이슈 「LLM 라우터가 두 벌 — 비용 집계와 폴백이 갈라짐」의 일부.
예전에는 llm/router.py 가 response.metadata 에, provider_router 가 자기
인스턴스 리스트에 따로 적었다 — 어느 쪽을 읽어도 전체 비용이 아니었다.
이제 두 진입점 모두 여기로 적는다. 읽는 쪽은 total_cost_usd() 하나면 된다.

2.0.36
    - 기록마다 작업 등급(tier: economy|quality)·실제 공급자·토큰 출처·가린 비밀값 수를 남긴다.
    - 디스크에도 남긴다(`RECODER_COST_LEDGER_FILE`, 기본 ~/.recoder/model_costs.jsonl). Core 를
      다시 켜도 30일 비용을 볼 수 있다. 프롬프트·응답 본문은 적지 않는다 — 정해 둔 칸(숫자·이름)만.
    - summarize() 가 등급·공급자·모델별로 묶는다. 단가를 모르는 호출(cost None)은 0 으로 더하지 않고 따로 센다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.Lock()
_RECORDS: list[dict[str, Any]] = []

#: 무한히 자라지 않게 — 코어는 장수 프로세스다.
_MAX_RECORDS = 5000
#: 디스크 파일 상한 — 넘으면 최근 절반만 남긴다.
_MAX_FILE_BYTES = 8 * 1024 * 1024
#: 디스크에 남기는 칸(이 밖의 값은 버린다 — 실수로 본문이 들어와도 디스크에 쓰지 않게).
_FIELDS = (
    "call_id", "ts", "agent", "operation", "tier", "provider", "model", "input_tokens", "output_tokens",
    "token_source", "latency_ms", "fallback_used", "retry_count", "estimated_cost_usd", "pricing_source",
    "quality_equivalent_usd", "masked", "run", "status", "run_id", "transport_retries",
)


def ledger_file() -> Optional[Path]:
    """원장 파일. RECODER_COST_LEDGER_FILE 을 빈 값이나 0 으로 두면 디스크에 쓰지 않는다."""
    raw = os.environ.get("RECODER_COST_LEDGER_FILE")
    if raw is not None:
        raw = raw.strip()
        return Path(raw) if raw and raw != "0" else None
    home = os.environ.get("RECODER_HOME", "").strip()
    return (Path(home) if home else Path.home() / ".recoder") / "model_costs.jsonl"


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    out = {k: entry[k] for k in _FIELDS if k in entry}
    for k, v in list(out.items()):
        if not (v is None or isinstance(v, (str, int, float, bool))):
            out[k] = str(v)[:200]
        elif isinstance(v, str):
            out[k] = v[:200]
    return out


def _persist(entry: dict[str, Any]) -> None:
    path = ledger_file()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_clean(entry), ensure_ascii=False) + "\n")
        if path.stat().st_size > _MAX_FILE_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()
            keep = lines[len(lines) // 2:]
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(tmp, path)
    except OSError:
        pass  # 비용 기록 때문에 AI 호출이 실패하면 안 된다


def record(entry: dict[str, Any]) -> dict[str, Any]:
    """호출 기록 하나를 원장에 적는다. ts 가 없으면 채운다."""
    entry = dict(entry)
    entry.setdefault("ts", time.time())
    with _LOCK:
        _RECORDS.append(entry)
        if len(_RECORDS) > _MAX_RECORDS:
            del _RECORDS[: len(_RECORDS) - _MAX_RECORDS]
        _persist(entry)
    return entry


def all_records() -> list[dict[str, Any]]:
    with _LOCK:
        return list(_RECORDS)


def total_cost_usd() -> float:
    with _LOCK:
        return sum(float(r.get("estimated_cost_usd") or 0.0) for r in _RECORDS)


def reset() -> None:
    """테스트 전용."""
    with _LOCK:
        _RECORDS.clear()


def prune(before_ts: float) -> int:
    """원장 파일에서 before_ts 보다 오래된 기록을 지운다(보존 기간). 지운 수."""
    path = ledger_file()
    if path is None or not path.exists():
        return 0
    with _LOCK:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        keep = []
        for line in lines:
            try:
                if float(json.loads(line).get("ts") or 0) < before_ts:
                    continue
            except (ValueError, TypeError, AttributeError):
                pass  # 못 읽는 줄은 남긴다(지우는 쪽이 실수하지 않게)
            keep.append(line)
        removed = len(lines) - len(keep)
        if removed:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
            os.replace(tmp, path)
        return removed


def stored_records(since: float = 0.0, limit: int = 50_000) -> list[dict[str, Any]]:
    """디스크 원장(있으면)의 기록. 디스크를 안 쓰거나 못 읽으면 이 프로세스의 기록."""
    path = ledger_file()
    if path is not None:
        try:
            with _LOCK:
                lines = path.read_text(encoding="utf-8").splitlines()[-limit:] if path.exists() else []
            rows: list[dict[str, Any]] = []
            for line in lines:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and float(row.get("ts") or 0) >= since:
                        rows.append(row)
                except (ValueError, TypeError):
                    continue
            return rows
        except OSError:
            pass
    return [r for r in all_records() if float(r.get("ts") or 0) >= since]


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """등급·공급자·모델별 합계."""
    groups: dict[tuple, dict[str, Any]] = {}
    total = 0.0
    unknown = 0
    saved = 0.0
    for r in records:
        tier = "economy" if r.get("tier") == "economy" else "quality"
        key = (tier, str(r.get("provider") or ""), str(r.get("model") or ""))
        g = groups.setdefault(key, {"tier": key[0], "provider": key[1], "model": key[2], "calls": 0,
                                    "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                                    "unknown_cost_calls": 0, "estimated_token_calls": 0, "masked": 0})
        g["calls"] += 1
        g["input_tokens"] += int(r.get("input_tokens") or 0)
        g["output_tokens"] += int(r.get("output_tokens") or 0)
        g["masked"] += int(r.get("masked") or 0)
        if r.get("token_source") != "api":
            g["estimated_token_calls"] += 1
        cost = r.get("estimated_cost_usd")
        if cost is None:
            g["unknown_cost_calls"] += 1
            unknown += 1
            continue
        g["cost_usd"] += float(cost)
        total += float(cost)
        eq = r.get("quality_equivalent_usd")
        if tier == "economy" and eq is not None:
            saved += max(0.0, float(eq) - float(cost))
    rows = sorted(groups.values(), key=lambda g: (g["tier"] != "quality", -g["cost_usd"], -g["calls"]))
    for g in rows:
        g["cost_usd"] = round(g["cost_usd"], 6)
    return {"calls": len(records), "total_cost_usd": round(total, 6), "unknown_cost_calls": unknown,
            "economy_saved_usd": round(saved, 6), "groups": rows}


def summary(days: int = 30) -> dict[str, Any]:
    days = max(1, min(int(days), 365))
    out = summarize(stored_records(time.time() - days * 86400))
    out["days"] = days
    out["persisted"] = ledger_file() is not None
    return out
