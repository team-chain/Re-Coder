"""같은 요청을 두 AI 공급자로 — 비용·교정 횟수·보안 점수 비교(2.0.36).

무엇을 재나
    실제 코드 생성 경로(code_agent.generate_code)를 공급자만 바꿔 두 번 돌린다. 파일은 쓰지 않는다
    (생성 결과는 승인 전 ops 목록일 뿐이다).
    - 비용·토큰: 그 실행의 원장 기록(task_tier.run_tag 로 골라냄). 단가를 모르는 호출은 "단가 미설정"으로 따로 센다.
    - 교정 횟수: 첫 생성 뒤 고치려고 다시 부른 호출(일관성 교정·파일 수정·검토 + 생성 재시도).
    - 남은 문제: 결과의 consistency_issues 중 오류 수.
    - 보안 점수: 생성된 파일 전체에 code_security.report — 실제 생성 경로와 같은 규칙.

공정성
    한 실행 안의 **모든** 호출(절약 등급 포함)을 고른 공급자로 고정한다(task_tier.pinned).
    고정한 공급자가 실패해도 다른 공급자로 몰래 바꾸지 않는다 — 실패는 실패로 보여 준다.
    두 공급자는 차례로 돈다(동시에 돌리면 속도 제한·컴퓨터 부하가 결과를 흔든다).

AI 요청이 공급자마다 나가므로 비용이 두 번 든다 — 확장은 시작 전에 이걸 알린다.
한 번에 하나만 돈다. 결과는 메모리에 최근 5개만 둔다(프롬프트·생성 코드는 남기지 않는다).
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from llm import cost_ledger, task_tier
except ImportError:  # pragma: no cover
    from core.llm import cost_ledger, task_tier  # type: ignore

SPECS = ("quality", "economy", "fast", "anthropic", "openai", "compatible")
MAX_INSTRUCTION = 4000
_KEEP = 5
#: 공급자마다 자동 교정에 쓸 시간 — 두 쪽에 같은 예산을 준다.
BUDGET_SECONDS = 420

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_ORDER: list[str] = []


class CompareError(ValueError):
    pass


class CompareBusy(RuntimeError):
    pass


def _generate(**kwargs: Any) -> dict:  # 테스트가 바꿔 끼운다
    try:
        from code_agent import generate_code
    except ImportError:  # pragma: no cover
        from core.code_agent import generate_code  # type: ignore
    return generate_code(**kwargs)


def _approval(instruction: str) -> list[dict]:
    """두 쪽에 같은 승인 — "요청대로 진행" 확인 카드.

    생성 경로는 사람이 승인한 결정 없이는 코드를 만들지 않는다(FR-02-05). 비교를 시작할 때 확장이
    비용 확인(모달)을 받으므로 그것이 이 요청의 승인이다. 설계 선택은 두 공급자가 같은 조건에서 각자 정한다.
    """
    try:
        from code_agent import _build_confirm_decision
    except ImportError:  # pragma: no cover
        from core.code_agent import _build_confirm_decision  # type: ignore
    return [{**_build_confirm_decision(instruction), "chosen_key": "proceed"}]


def _router():
    try:
        from llm.provider_router import get_provider_router
    except ImportError:  # pragma: no cover
        from core.llm.provider_router import get_provider_router  # type: ignore
    return get_provider_router()


def measure(records: list[dict], result: Optional[dict]) -> dict:
    """한 실행의 원장 기록·결과 → 비교 숫자."""
    ops = Counter(str(r.get("operation") or "") for r in records)
    repairs = sum(n for op, n in ops.items() if op in task_tier.REPAIR_OPERATIONS) + max(0, ops.get("generate_code", 0) - 1)
    known = [r for r in records if r.get("estimated_cost_usd") is not None]
    models = Counter(f"{r.get('provider')}/{r.get('model')}" for r in records)
    out: dict[str, Any] = {
        "calls": len(records),
        "repair_calls": repairs,
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in records),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in records),
        "estimated_token_calls": sum(1 for r in records if r.get("token_source") != "api"),
        "cost_usd": round(sum(float(r["estimated_cost_usd"]) for r in known), 6) if known else None,
        "unknown_cost_calls": len(records) - len(known),
        "masked": sum(int(r.get("masked") or 0) for r in records),
        "used": [{"name": k, "calls": v} for k, v in models.most_common(3)],
        "files": 0, "remaining_errors": None, "security_score": None, "security_findings": None,
    }
    if result:
        ops_out = [op for op in (result.get("ops") or []) if isinstance(op, dict)]
        out["files"] = len(ops_out)
        issues = result.get("consistency_issues") or []
        out["remaining_errors"] = sum(1 for i in issues if isinstance(i, dict) and i.get("severity") == "error")
        try:
            try:
                import code_security
            except ImportError:  # pragma: no cover
                from core import code_security  # type: ignore
            sec = code_security.report(code_security.ops_files(ops_out))
            out["security_score"] = sec.get("score")
            out["security_findings"] = len(sec.get("findings") or [])
        except Exception:  # noqa: BLE001 — 점수가 없어도 비교는 보여 준다
            pass
    return out


def verdict(sides: list[dict]) -> dict:
    """항목마다 더 나은 쪽(같거나 비교할 수 없으면 None). 종합 승자는 정하지 않는다 — 사람이 판단한다."""
    ok = [s for s in sides if s.get("status") == "succeeded"]
    if len(ok) != 2:
        return {"cheaper": None, "fewer_repairs": None, "safer": None, "fewer_errors": None}
    a, b = ok

    def better(key: str, lower: bool) -> Optional[str]:
        x, y = a["metrics"].get(key), b["metrics"].get(key)
        if x is None or y is None or x == y:
            return None
        return a["spec"] if (x < y) == lower else b["spec"]

    cheaper = better("cost_usd", True) if not (a["metrics"]["unknown_cost_calls"] or b["metrics"]["unknown_cost_calls"]) else None
    return {"cheaper": cheaper, "fewer_repairs": better("repair_calls", True),
            "safer": better("security_score", False), "fewer_errors": better("remaining_errors", True)}


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def start(instruction: str, workspace: str, specs: list[str], *,
          runner: Optional[Callable[[Callable[[], None]], None]] = None) -> dict:
    instruction = (instruction or "").strip()
    if not instruction:
        raise CompareError("비교할 요청을 적어 주세요.")
    if len(instruction) > MAX_INSTRUCTION:
        raise CompareError(f"요청이 너무 깁니다({MAX_INSTRUCTION}자 이하).")
    specs = [str(s or "").strip().lower() for s in (specs or [])]
    if len(specs) != 2 or any(s not in SPECS for s in specs):
        raise CompareError("비교할 공급자 두 개를 고르세요.")
    root = ""
    if workspace:
        path = Path(workspace)
        if not path.is_absolute() or not path.is_dir():
            raise CompareError("프로젝트 폴더를 찾을 수 없습니다.")
        root = str(path.resolve())

    router = _router()
    resolved = []
    for spec in specs:
        provider = router._spec_provider(spec)
        if provider is None:
            raise CompareError(f"{spec} 공급자의 키(호환 API 는 주소까지)가 없습니다. 먼저 연결하세요.")
        resolved.append((provider.provider_name, provider.model_id))
    if resolved[0] == resolved[1]:
        raise CompareError("두 쪽이 같은 공급자·모델입니다. 다른 공급자를 고르세요.")

    with _LOCK:
        if any(j["status"] == "running" for j in _JOBS.values()):
            raise CompareBusy("이미 비교가 진행 중입니다. 끝난 뒤 다시 시작하세요.")
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "status": "running", "started_at": time.time(), "finished_at": None,
               "instruction_chars": len(instruction),
               "sides": [{"spec": s, "provider": p, "model": m, "status": "waiting", "metrics": None,
                          "error": "", "seconds": None} for s, (p, m) in zip(specs, resolved)],
               "verdict": None}
        _JOBS[job_id] = job
        _ORDER.append(job_id)
        finished = [j for j in _ORDER if _JOBS[j]["status"] != "running"]
        while len(_ORDER) > _KEEP and finished:
            old = finished.pop(0)
            _ORDER.remove(old)
            _JOBS.pop(old, None)

    def work() -> None:
        try:
            for side in job["sides"]:
                _run_side(job_id, side, instruction, root)
        finally:
            with _LOCK:
                job["verdict"] = verdict(job["sides"])
                job["status"] = "finished"
                job["finished_at"] = time.time()

    (runner or _thread)(work)
    return get(job_id)


def _thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="model-compare", daemon=True).start()


def _run_side(job_id: str, side: dict, instruction: str, root: str) -> None:
    tag = f"compare-{job_id}-{side['spec']}"
    with _LOCK:
        side["status"] = "running"
    started = time.monotonic()
    result = None
    error = ""
    with task_tier.pinned(side["spec"], tag):
        try:
            result = _generate(instruction=instruction, session_id=tag[-8:], project_root=root,
                               decisions=_approval(instruction), mode="auto", budget_seconds=BUDGET_SECONDS)
        except Exception as exc:  # noqa: BLE001 — 한쪽 실패도 비교 결과다
            error = str(exc)[:400] or exc.__class__.__name__
    records = [r for r in cost_ledger.all_records() if r.get("run") == tag]
    with _LOCK:
        side["seconds"] = round(time.monotonic() - started, 1)
        side["metrics"] = measure(records, result)
        side["status"] = "succeeded" if result is not None else "failed"
        side["error"] = error


def get(job_id: str) -> Optional[dict]:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        import copy
        return copy.deepcopy(_public(job))


def latest() -> Optional[dict]:
    with _LOCK:
        job_id = _ORDER[-1] if _ORDER else None
    return get(job_id) if job_id else None


def reset() -> None:
    """테스트 전용."""
    with _LOCK:
        _JOBS.clear()
        _ORDER.clear()
