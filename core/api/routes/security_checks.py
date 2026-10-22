"""AI 코드 보안 점수 · 배포 후 취약점 재검사 API (2.0.28)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

try:
    import code_security  # type: ignore
    import vuln_recheck  # type: ignore
except ImportError:  # pragma: no cover
    from core import code_security, vuln_recheck  # type: ignore

router = APIRouter(prefix="/api/security", tags=["security"])

_TEXT = (".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx", ".json")
_MAX_FILES = 4000


class CodeScanRequest(BaseModel):
    workspace_path: str = Field(..., min_length=1, max_length=4096)


def _project_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in root.rglob("*"):
        if len(files) >= _MAX_FILES:
            break
        if not path.is_file() or path.suffix not in _TEXT:
            continue
        rel = path.relative_to(root).as_posix()
        if code_security._SKIP.search(rel) or path.stat().st_size > 1_000_000:
            continue
        try:
            files[rel] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return files


@router.post("/code")
async def code_scan(body: CodeScanRequest) -> dict:
    root = Path(body.workspace_path)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="작업 폴더를 찾지 못했습니다.")
    files = await asyncio.to_thread(_project_files, root)
    return await asyncio.to_thread(code_security.report, files)


@router.post("/vulns/check")
async def vulns_check() -> dict:
    """지금 한 번 재검사(배포 기록의 SBOM ↔ OSV)."""
    raised = await asyncio.to_thread(vuln_recheck.Rechecker(vuln_recheck.records_from_store).tick)
    return {"raised": len(raised)}


@router.get("/vulns/history")
async def vulns_history() -> dict:
    return vuln_recheck.history()
