"""4.0 프로젝트 진행 흐름 — 만들기 → 검증 → 보안 검사 → 로컬 실행 → 공격 테스트 → 운영 배포.

개발·보안·배포·운영 화면이 각자 따로 있어서 "지금 어디까지 했고 다음에 뭘 해야 하는지" 를 알 수 없었다
(실사용 피드백 2026-11-15). 각 단계가 끝날 때 결과를 프로젝트별로 남기고, 화면 맨 위의 진행 표시가 이것을 읽는다.

· 값(비밀값·코드 원문)은 남기지 않는다 — 상태·숫자·짧은 문장만.
· 기록이 없어도 폴더 내용으로 알 수 있는 것(코드가 있다, 다중 서비스 계약이 있다, 배포 이력이 있다)은 채운다.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STEPS = ("build", "verify", "security", "local", "redteam", "release")
TITLES = {"build": "만들기", "verify": "함께 띄워 검증", "security": "보안 검사", "local": "로컬 실행",
          "redteam": "공격 테스트", "release": "운영 배포"}
#: 확장이 직접 남길 수 있는 단계(나머지는 Core 가 실제 결과를 보고 남긴다).
CLIENT_STEPS = {"redteam"}
_lock = threading.Lock()
_SOURCE = (".js", ".ts", ".tsx", ".jsx", ".mjs", ".py", ".go", ".java", ".html")
_SKIP = {"node_modules", ".git", ".recoder", "dist", "build", "__pycache__", ".venv", "venv"}


def _home() -> Path:
    return Path(os.environ.get("RECODER_HOME") or (Path.home() / ".recoder"))


def _file(workspace: str | Path) -> Path:
    key = hashlib.sha256(str(Path(workspace).expanduser().resolve()).encode("utf-8")).hexdigest()[:24]
    return _home() / "journey" / f"{key}.json"


def load(workspace: str | Path) -> dict:
    try:
        data = json.loads(_file(workspace).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(workspace: str | Path | None, step: str, status: str, summary: str = "", **extra: Any) -> None:
    """단계 결과를 남긴다. 실패해도 본 작업을 막지 않는다."""
    if not workspace or step not in STEPS:
        return
    try:
        path = _file(workspace)
        with _lock:
            data = load(workspace)
            data[step] = {"status": status, "summary": str(summary)[:300],
                          "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          **{k: v for k, v in extra.items() if isinstance(v, (str, int, float, bool)) or v is None}}
            if step in ("build",) and status == "done":
                #: 다시 만들었으면 그 뒤 단계 결과는 옛 코드 기준이다 — "다시 확인 필요" 로 내린다.
                for later in ("security", "local", "redteam", "release"):
                    if later in data and data[later].get("status") == "done":
                        data[later] = {**data[later], "status": "stale"}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        print(f"[journey] 기록 실패({step}): {exc}", flush=True)


def _has_code(root: Path) -> bool:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP and not d.startswith(".")]
        for name in filenames:
            if name.endswith(_SOURCE):
                return True
        count += 1
        if count > 400:
            break
    return False


def state(workspace: str | Path) -> dict:
    """화면용 진행 상태 — 단계마다 status(done·failed·stale·todo·blocked)와 한 줄 설명, 그리고 다음 단계."""
    root = Path(workspace)
    if not root.is_dir():
        return {"available": False, "reason": "작업 폴더를 찾지 못했습니다."}
    saved = load(root)
    try:
        import compose_deploy
        arch = compose_deploy.load(root)
        stack_history = compose_deploy.history(root) if arch else []
        multi_other = bool(not arch and len(compose_deploy.built_services(root)) >= 2)
    except Exception:  # noqa: BLE001
        arch, stack_history, multi_other = None, [], False
    multi = bool(arch) or multi_other
    steps: dict[str, dict] = {}
    for key in STEPS:
        item = dict(saved.get(key) or {})
        steps[key] = {"key": key, "title": TITLES[key], "status": item.get("status") or "todo",
                      "summary": item.get("summary") or "", "at": item.get("at") or ""}
    if steps["build"]["status"] == "todo" and _has_code(root):
        steps["build"].update(status="done", summary="프로젝트에 코드가 있습니다")
    if arch and steps["verify"]["status"] == "todo":
        try:
            import contract_check
            summary = contract_check.check_workspace(root).get("summary") or {}
            if summary.get("error"):
                steps["verify"].update(status="failed", summary=f"설계도와 다른 코드 {summary['error']}건 — 코드 맵에서 확인")
        except Exception:  # noqa: BLE001
            pass
    if steps["local"]["status"] in ("todo", "stale") and stack_history:
        last = stack_history[0]
        if last.get("status") in ("success", "degraded"):
            steps["local"].update(status="done", summary=f"서비스 {len(last.get('services') or [])}개 실행 기록", at=last.get("at", ""))
    if multi:
        steps["release"].update(status="blocked", summary="다중 서비스의 AWS 배포는 다음 버전에서 지원합니다 — 로컬 실행까지가 이번 범위")
    if not steps["build"]["status"] == "done":
        for key in STEPS[1:]:
            if steps[key]["status"] == "todo":
                steps[key]["status"] = "locked"
    order = [steps[k] for k in STEPS]
    nxt = next((s["key"] for s in order if s["status"] in ("todo", "failed", "stale")), "")
    return {"available": True, "multi": multi, "stack": bool(arch), "services": len((arch or {}).get("services") or []),
            "databases": len((arch or {}).get("databases") or []), "steps": order, "next": nxt}
