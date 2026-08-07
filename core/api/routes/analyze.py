"""
ReCoder Core — Code Analysis Routes

Handles LLM-powered patch proposal generation, approval, and listing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from schemas import AnalyzeRequest, ApprovalLevel, FilePatch, PatchProposal, RiskLevel

router = APIRouter(tags=["analyze"])

# ---------------------------------------------------------------------------
# In-process stores (per server lifetime)
# ---------------------------------------------------------------------------

# proposal_id -> PatchProposal
_proposals: dict[str, PatchProposal] = {}

# fingerprint -> proposal_id (§19.1 4-stage filter, step 3: dedup cache)
# Keyed by error fingerprint; values point into _proposals. Lifetime is
# bounded by the ContextGate's 60-second TTL check — we don't garbage-collect
# this map explicitly because cache misses simply create a fresh proposal.
_fingerprint_to_proposal: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Lazy singletons — optional deps don't block server startup
# ---------------------------------------------------------------------------

_orchestrator = None   # type: ignore
_gate = None           # type: ignore  (ContextGate)


def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        try:
            from llm.provider_router import LLMProviderRouter  # type: ignore
            from context_gate import ContextGate  # type: ignore
            from orchestrator import Orchestrator  # type: ignore
            _orchestrator = Orchestrator(
                provider_router=LLMProviderRouter(),
                context_gate=_get_gate(),
            )
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("Orchestrator unavailable: %s", exc)
    return _orchestrator


def _get_gate():
    """Return the shared ContextGate singleton."""
    global _gate
    if _gate is None:
        try:
            from context_gate import ContextGate  # type: ignore
            _gate = ContextGate()
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("ContextGate unavailable: %s", exc)
    return _gate


# ---------------------------------------------------------------------------
# Context masking helpers
# ---------------------------------------------------------------------------


async def _mask_request(request: AnalyzeRequest) -> AnalyzeRequest:
    """
    Run all context fields through the ContextGate masker.

    Returns a copy of the request with PII / secrets redacted.
    Falls back to a simple regex-based scrub when ContextGate is unavailable.
    """
    gate = _get_gate()
    if gate is not None:
        # Mask each text field independently
        fields: dict[str, Optional[str]] = {
            "terminal_output": request.terminal_output,
            "selected_text": request.selected_text,
            "project_files_summary": request.project_files_summary,
        }
        masked: dict[str, Optional[str]] = {}
        for key, value in fields.items():
            if value is not None:
                result = await gate.mask(value)
                masked[key] = result.masked_content
            else:
                masked[key] = None
        return request.model_copy(update=masked)

    # Fallback minimal masking when ContextGate is absent
    import re
    _SECRET_RE = re.compile(
        r"(AKIA[0-9A-Z]{16}|(?:password|secret|token|key)\s*=\s*\S+)",
        re.IGNORECASE,
    )

    def _redact(text: Optional[str]) -> Optional[str]:
        return _SECRET_RE.sub("[REDACTED]", text) if text else text

    return request.model_copy(update={
        "terminal_output": _redact(request.terminal_output),
        "selected_text": _redact(request.selected_text),
        "project_files_summary": _redact(request.project_files_summary),
    })


def _compute_trigger_score(request: AnalyzeRequest) -> float:
    """
    Use the ContextGate's trigger scorer when available;
    fall back to a simple rule-based heuristic in [0.0, 1.0].
    """
    gate = _get_gate()
    if gate is not None and request.terminal_output:
        # ContextGate returns score in [0, 100]; normalise to [0, 1]
        return gate.compute_trigger_score(request.terminal_output) / 100.0

    # Inline fallback
    score = 0.0
    if request.terminal_output:
        output = request.terminal_output.lower()
        if "traceback" in output or "error" in output:
            score += 0.4
        if "exception" in output:
            score += 0.2
        if "warning" in output:
            score += 0.1
    if request.selected_text:
        score += 0.1
    if request.command:
        score += 0.1
    if request.active_file_path:
        score += 0.1
    return min(score, 1.0)


def _compute_quality_score(masked_request: AnalyzeRequest) -> float:
    """
    Use the ContextGate's quality scorer when available;
    fall back to a simple heuristic in [0.0, 1.0].
    """
    gate = _get_gate()
    if gate is not None:
        combined = " ".join(filter(None, [
            masked_request.terminal_output,
            masked_request.selected_text,
            masked_request.project_files_summary,
        ]))
        qs = gate.compute_quality_score(combined, masked_request)
        return qs.score

    # Inline fallback
    score = 0.0
    if masked_request.terminal_output and len(masked_request.terminal_output) > 50:
        score += 0.3
    if masked_request.selected_text and len(masked_request.selected_text) > 10:
        score += 0.2
    if masked_request.project_files_summary:
        score += 0.2
    if masked_request.workspace_path:
        score += 0.15
    if masked_request.active_file_path:
        score += 0.15
    return min(score, 1.0)


def _get_fingerprint(masked_request: AnalyzeRequest) -> str:
    """
    Use the ContextGate's error fingerprinting when available;
    fall back to SHA-256 of key fields.
    """
    gate = _get_gate()
    combined = " ".join(filter(None, [
        masked_request.terminal_output,
        masked_request.selected_text,
    ]))
    if gate is not None:
        return gate.compute_error_fingerprint(combined, masked_request)

    raw = (
        (masked_request.terminal_output or "")
        + (masked_request.active_file_path or "")
        + (masked_request.selected_text or "")
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _check_fingerprint_cache(fingerprint: str) -> bool:
    """Check the ContextGate's 60-second dedup cache."""
    gate = _get_gate()
    if gate is not None:
        return gate.check_fingerprint_cache(fingerprint)
    return False


def _update_fingerprint_cache(fingerprint: str) -> None:
    gate = _get_gate()
    if gate is not None:
        gate.update_fingerprint_cache(fingerprint)


# ---------------------------------------------------------------------------
# Orchestrator delegation
# ---------------------------------------------------------------------------


async def _delegate_to_orchestrator(
    request: AnalyzeRequest,
    trigger_score: float,
    quality_score: float,
) -> PatchProposal:
    """
    Delegate analysis to the Orchestrator (Code Agent) via process_analyze_request().

    Falls back to a structured placeholder when:
      - Orchestrator module is unavailable
      - Trigger/quality score is below the lower threshold (returns None)
      - Quality score is in the WARN band (raises ValueError per §18.3 —
        "사용자에게 추가 정보 요청"). We translate that to a friendly placeholder
        so the HTTP endpoint never returns a raw 500.
    """
    orch = _get_orchestrator()
    if orch is not None:
        try:
            result = await orch.process_analyze_request(request)
        except ValueError as exc:
            # §18.3 quality WARN band — orchestrator asks for richer context.
            # Convert to a structured "needs more context" PatchProposal so the
            # caller (Extension) can prompt the user to add traceback / file etc.
            import logging
            logging.getLogger(__name__).info(
                "[Analyze] Orchestrator requested more context: %s", exc,
            )
            return PatchProposal(
                summary="추가 컨텍스트가 필요합니다.",
                risk_level=RiskLevel.LOW,
                risk_reasons=[
                    "Quality score in WARN band — more context required.",
                    str(exc),
                ],
                approval_level=ApprovalLevel.AUTO,
                patches=[],
                test_command=None,
            )

        if result is not None:
            return result

        # Orchestrator filtered the request (low trigger/quality score)
        return PatchProposal(
            summary="No significant issues detected in the current context.",
            risk_level=RiskLevel.LOW,
            risk_reasons=["Trigger or quality score below threshold — no LLM call made."],
            approval_level=ApprovalLevel.AUTO,
            patches=[],
            test_command=None,
        )

    return PatchProposal(
        summary="[Placeholder] Orchestrator not yet available.",
        risk_level=RiskLevel.LOW,
        risk_reasons=["Orchestrator module not loaded — placeholder response."],
        approval_level=ApprovalLevel.AUTO,
        patches=[],
        test_command=None,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/analyze")
async def analyze(request: AnalyzeRequest) -> PatchProposal:
    """
    Analyse the provided workspace context and return a PatchProposal.

    Steps:
    1. Mask secrets / PII via the Context Gate (async, thread-pooled).
    2. Compute rule-based trigger and quality scores via ContextGate.
    3. Check the 60-second error fingerprint dedup cache.
    4. Delegate to the Orchestrator (Code Agent).
    5. Store the proposal for later approval.
    """
    # 0. (Fix A) 활성 파일 내용을 컨텍스트에 포함한다.
    #    드래그 선택이 없으면 모델이 파일명만 보고 환각하므로, 파일 전체를
    #    selected_text 에 실어 준다. selected_text 는 아래 _mask_request 에서
    #    Context Gate 로 마스킹되므로 시크릿은 LLM 에 노출되지 않는다.
    if request.active_file_path and not (request.selected_text and request.selected_text.strip()):
        try:
            _afp = Path(request.active_file_path)
            if _afp.is_file():
                _file_text = _afp.read_text(encoding="utf-8", errors="ignore")[:30000]
                if _file_text.strip():
                    request = request.model_copy(update={"selected_text": _file_text})
        except Exception:
            pass

    # 1. Masking (async — offloads CPU work to thread pool via ContextGate)
    masked_request = await _mask_request(request)

    # 2. Scores via ContextGate methods (§19.1 stages 1 & 2)
    trigger_score = _compute_trigger_score(masked_request)
    quality_score = _compute_quality_score(masked_request)

    # 3. Fingerprint dedup via ContextGate 60-second cache (§19.1 stage 3)
    fingerprint = _get_fingerprint(masked_request)
    if _check_fingerprint_cache(fingerprint):
        cached_id = _fingerprint_to_proposal.get(fingerprint)
        if cached_id is not None:
            cached = _proposals.get(cached_id)
            if cached is not None:
                # Same error within 60s — return the prior proposal verbatim,
                # avoiding a duplicate LLM call. (설계서 §19.1 단계 3)
                import logging
                logging.getLogger(__name__).info(
                    "[FingerprintCache] hit fingerprint=%s proposal_id=%s — skipping LLM",
                    fingerprint[:12], cached_id,
                )
                return cached
        # Cache flag was set but no proposal stored — fall through (treat as miss)

    # 4. Delegate to Orchestrator (stages 1+2 thresholds applied internally;
    #    stage 4 AST chunking happens inside the orchestrator's LLM prompt build)
    proposal = await _delegate_to_orchestrator(masked_request, trigger_score, quality_score)

    # 4.5 (Fix) LLM 이 돌려준 patch.file 은 보통 상대경로/파일명/빈값이다.
    #     승인 핸들러는 절대경로가 아니면 422 를 내므로, 여기서 활성 파일·워크스페이스
    #     기준 절대경로로 정규화한다. (이게 'must be absolute' 422 의 직접 원인이었음)
    try:
        _afp = request.active_file_path or ""
        _ws = request.workspace_path or ""
        for _pt in getattr(proposal, "patches", []) or []:
            _f = (getattr(_pt, "file", "") or "").strip()
            if _f and Path(_f).is_absolute():
                continue
            if _afp and (not _f or Path(_f).name == Path(_afp).name):
                _pt.file = _afp
            elif _ws and _f:
                _pt.file = str(Path(_ws) / _f)
            elif _afp:
                _pt.file = _afp
    except Exception:
        pass

    # 5. Store and update both caches
    _proposals[proposal.proposal_id] = proposal
    _fingerprint_to_proposal[fingerprint] = proposal.proposal_id
    _update_fingerprint_cache(fingerprint)

    return proposal


@router.post("/api/analyze/approve")
async def approve_patch(proposal_id: str, approved: bool) -> dict:
    """
    Approve or reject a PatchProposal.

    On approval:
    - Validate base_sha256 for each patch.
    - Create backups of affected files.
    - Apply unified diffs.
    - Roll back on any failure.

    On rejection: simply removes the proposal from the store.
    """
    proposal = _proposals.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Proposal '{proposal_id}' not found.")

    if not approved:
        del _proposals[proposal_id]
        return {"status": "rejected", "proposal_id": proposal_id}

    applied: list[str] = []
    unchanged: list[str] = []
    backups: dict[str, bytes] = {}

    try:
        for patch in proposal.patches:
            file_path = Path(patch.file)
            if not file_path.is_absolute():
                raise HTTPException(
                    status_code=422,
                    detail=f"Patch file path must be absolute: '{patch.file}'",
                )

            # Validate base_sha256
            if file_path.exists() and patch.base_sha256:
                current_digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
                if current_digest != patch.base_sha256:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"File '{patch.file}' has been modified since the proposal "
                            f"was generated (SHA-256 mismatch)."
                        ),
                    )
            if file_path.exists():
                backups[patch.file] = file_path.read_bytes()

            # (Fix B) 실제로 변경됐는지 확인. diff 문맥이 파일과 안 맞으면 False.
            changed = _apply_unified_diff(file_path, patch)
            if changed:
                applied.append(patch.file)
            else:
                unchanged.append(patch.file)

    except HTTPException:
        # Rollback already-applied patches
        for fpath_str, original_bytes in backups.items():
            try:
                Path(fpath_str).write_bytes(original_bytes)
            except Exception:
                pass
        raise
    except Exception as exc:
        # Rollback on unexpected errors
        for fpath_str, original_bytes in backups.items():
            try:
                Path(fpath_str).write_bytes(original_bytes)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Patch application failed: {exc}") from exc

    del _proposals[proposal_id]

    # (Fix B) 적용된 변경이 하나도 없으면 "성공"으로 속이지 않는다.
    if not applied:
        raise HTTPException(
            status_code=422,
            detail="패치가 현재 파일과 일치하지 않아 적용된 변경이 없습니다. 코드를 선택한 뒤 다시 분석하거나 실제 에러로 분석하세요 (diff 환각 가능성).",
        )

    return {
        "status": "applied",
        "proposal_id": proposal_id,
        "applied_files": applied,
        "unchanged_files": unchanged,
    }


@router.get("/api/analyze/proposals")
async def list_proposals() -> list[PatchProposal]:
    """Return all pending (unapproved) PatchProposals in the current session."""
    return list(_proposals.values())


# ---------------------------------------------------------------------------
# Diff application helper
# ---------------------------------------------------------------------------


def _norm_line(s: str) -> str:
    """비교용 정규화: 따옴표 안 문자열은 와일드카드로(마스킹된 시크릿 ↔ 실제 시크릿
    매칭), 앞뒤 공백 무시. 'KEY = "[REDACTED]"' 와 'KEY = "AKIA..."' 가 같다고 본다."""
    import re as _re
    out = _re.sub(r'"[^"]*"', '""', s)
    out = _re.sub(r"'[^']*'", "''", out)
    return out.strip()


def _apply_unified_diff(file_path: Path, patch: FilePatch) -> bool:
    """
    Apply a unified diff to *file_path*, **tolerantly**.

    엄격한 라인 일치 대신:
      - 따옴표 안 문자열을 와일드카드로 취급(마스킹된 시크릿 줄도 매칭).
      - 앞뒤 공백 차이 무시.
      - @@ 줄번호를 신뢰하지 않고 파일에서 직접 위치를 찾는다(LLM 줄번호 드리프트 허용).

    Returns
    -------
    bool : 파일 내용이 실제로 변경됐으면 True, 매칭 실패/무변경이면 False.
    """
    import re as _re

    original_text = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    nl = "\r\n" if "\r\n" in original_text else "\n"
    lines = original_text.split(nl)

    diff = patch.unified_diff or ""
    diff_lines = diff.splitlines()

    # ── 훅(hunk) 단위 파싱: (old_block[], new_block[]) ──
    hunks: list[tuple[list[str], list[str]]] = []
    cur_old: list[str] | None = None
    cur_new: list[str] | None = None
    for dl in diff_lines:
        if dl.startswith("@@"):
            if cur_old is not None:
                hunks.append((cur_old, cur_new))
            cur_old, cur_new = [], []
            continue
        if cur_old is None:
            continue  # 헤더(--- / +++) 이전
        if dl.startswith("\\"):  # "\ No newline at end of file"
            continue
        tag, val = (dl[:1], dl[1:]) if dl[:1] in {" ", "+", "-"} else (" ", dl)
        if tag == " ":
            cur_old.append(val)
            cur_new.append(val)
        elif tag == "-":
            cur_old.append(val)
        elif tag == "+":
            cur_new.append(val)
    if cur_old is not None:
        hunks.append((cur_old, cur_new))

    if not hunks:
        return False

    changed = False
    for old_block, new_block in hunks:
        if old_block == new_block:
            continue  # 변경 없는 훅
        # old_block 을 파일에서 (정규화 기준) 찾는다.
        if old_block:
            norm_old = [_norm_line(x) for x in old_block]
            found = -1
            for i in range(0, len(lines) - len(norm_old) + 1):
                if [_norm_line(lines[i + j]) for j in range(len(norm_old))] == norm_old:
                    found = i
                    break
            if found < 0:
                continue  # 이 훅은 매칭 실패 → 건너뜀
            lines[found:found + len(old_block)] = new_block
            changed = True
        else:
            # 순수 삽입(old 없음): 적용 위치 모호 → 건너뜀(안전)
            continue

    if not changed:
        return False

    new_text = nl.join(lines)
    if new_text == original_text:
        return False

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(new_text, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Code generation (Build 탭 "코드 작성 및 수정")
#   server.py 에만 있던 /api/code/generate 를 main.py 스택에도 추가.
#   자연어 instruction → 파일 작업(ops) 목록. 적용은 확장이 직접 writeFile.
# ---------------------------------------------------------------------------


class CodeGenerateRequest(BaseModel):
    instruction: str = ""
    workspace_path: str = ""
    open_file_path: str = ""
    open_file_content: str = ""
    prior_files: list = []
    context_files: list = []
    target_folder: str = ""
    # AI-DLC 결정 모달에서 사용자가 확정한 설계 선택. generate_code 프롬프트에 반영한다.
    decisions: list = []


@router.post("/api/code/generate")
async def generate_code_route(body: CodeGenerateRequest) -> dict:
    import asyncio as _asyncio
    import os as _os
    import uuid as _uuid

    if not (body.instruction or "").strip():
        raise HTTPException(status_code=400, detail="instruction 이 비어 있습니다.")

    if body.workspace_path and Path(body.workspace_path).exists():
        _os.environ["RECODER_PROJECT_ROOT"] = body.workspace_path

    open_file = None
    if body.open_file_path or body.open_file_content:
        open_file = {"path": body.open_file_path, "content": body.open_file_content}

    try:
        try:
            from code_agent import generate_code
        except ImportError:
            from core.code_agent import generate_code
        # LLM 호출은 수 초~수십 초 걸린다. 동기 함수를 그대로 await 없이 부르면
        # 이벤트 루프가 묶여 health 폴링·채팅 등 다른 요청이 전부 막히고,
        # 확장이 Core 를 "응답 없음"으로 판정해 복구 로직을 돌린다.
        #
        # 단, 스레드로 넘기면 여기서 실행이 양보되므로 워크스페이스 경로를
        # 전역 env 로 전달하면 안 된다. 다른 창의 요청이 그 사이 env 를
        # 덮어쓰면 이 요청이 남의 워크스페이스를 기준으로 동작한다.
        # → 요청별 경로는 인자로 직접 넘긴다.
        result = await _asyncio.to_thread(
            generate_code,
            instruction=body.instruction,
            session_id=_uuid.uuid4().hex[:8],
            open_file=open_file,
            prior_files=body.prior_files or [],
            context_files=body.context_files or [],
            target_folder=body.target_folder or "",
            decisions=body.decisions or [],
            project_root=body.workspace_path or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"코드 생성 실패: {e}") from e

    return result


# ---------------------------------------------------------------------------
# AI-DLC 1단계: 코드 대신 "설계 결정" 제시 (/api/code/plan)
#   ReCoder_AI-DLC_도입_설계서.md §3.2 — 회차1 범위.
#   자연어 요청 -> 설계 결정 목록(decisions). 코드는 아직 생성하지 않는다.
#   확장(webview)이 이 목록을 결정 카드로 렌더 -> 사용자가 옵션 선택·승인 ->
#   그 결과를 담아 /api/code/generate 를 다시 호출하는 흐름을 전제로 한다.
# ---------------------------------------------------------------------------


class CodePlanRequest(BaseModel):
    instruction: str = ""
    workspace_path: str = ""
    open_file_path: str = ""
    open_file_content: str = ""
    context_files: list = []
    target_folder: str = ""


# ---------------------------------------------------------------------------
# Workspace chat — 오른쪽 "AI와 대화" 패널용
# ---------------------------------------------------------------------------


class ChatHistoryMessage(BaseModel):
    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    message: str = ""
    history: list[ChatHistoryMessage] = []
    workspace_path: str = ""


@router.post("/api/chat")
async def chat_route(body: ChatRequest) -> dict:
    """ReCoder 작업 맥락을 아는 일반 대화형 AI 응답을 반환한다.

    코드 변경이 필요한 경우에도 이 API는 파일을 쓰지 않는다. 사용자가 대화로
    방향을 정한 뒤 Build의 코드 생성 기능을 사용하도록 안내해, 대화만으로
    워크스페이스가 변경되는 일을 막는다.
    """
    import asyncio

    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message 가 비어 있습니다.")

    # 대화 기록은 최근 10개만, 각 메시지는 2천 자까지만 넣어 비용과 컨텍스트를 제한한다.
    history_lines: list[str] = []
    for item in (body.history or [])[-10:]:
        role = "사용자" if item.role == "user" else "ReCoder"
        content = (item.content or "").strip()[:2000]
        if content:
            history_lines.append(f"{role}: {content}")
    history_text = "\n".join(history_lines) or "(이전 대화 없음)"

    workspace_name = "현재 워크스페이스"
    if body.workspace_path:
        try:
            workspace_name = Path(body.workspace_path).name or workspace_name
        except Exception:
            pass

    prompt = f"""당신은 VS Code 확장 ReCoder의 친절한 개발 도우미입니다.
사용자는 '{workspace_name}' 프로젝트에서 작업 중입니다.
한국어로 자연스럽고 짧게 답하세요. 질문의 의도를 먼저 파악하고, 필요한 경우
실행 순서·주의점·예시를 제시하세요. 정보가 부족하면 한 가지 확인 질문을 하세요.
코드나 파일을 실제로 변경했다고 말하지 마세요. 사용자가 구현을 원하면 대화로
방향을 정한 뒤 ReCoder의 코드 생성 기능을 사용하도록 안내하세요.

이전 대화:
{history_text}

사용자: {message}
ReCoder:"""

    try:
        try:
            from llm.base import LLMRequest
            from llm.router import get_router
        except ImportError:
            from core.llm.base import LLMRequest
            from core.llm.router import get_router

        # 동기 Provider 호출이므로 이벤트 루프를 막지 않도록 별도 스레드에서 실행한다.
        response = await asyncio.to_thread(
            get_router().call,
            LLMRequest(prompt=prompt, max_tokens=1000, temperature=0.45),
            "workspace_chat",
            "chat",
        )
        reply = (response.text or "").strip()
        if not reply:
            raise RuntimeError("AI가 빈 응답을 반환했습니다.")
        return {"reply": reply, "model": getattr(response, "model_used", "")}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"AI 대화 실패: {exc}") from exc


@router.post("/api/code/plan")
async def code_plan_route(body: CodePlanRequest) -> dict:
    import asyncio as _asyncio
    import os as _os
    import uuid as _uuid

    if not (body.instruction or "").strip():
        raise HTTPException(status_code=400, detail="instruction 이 비어 있습니다.")

    if body.workspace_path and Path(body.workspace_path).exists():
        _os.environ["RECODER_PROJECT_ROOT"] = body.workspace_path

    open_file = None
    if body.open_file_path or body.open_file_content:
        open_file = {"path": body.open_file_path, "content": body.open_file_content}

    try:
        try:
            from code_agent import generate_plan
        except ImportError:
            from core.code_agent import generate_plan
        # generate 와 동일 — 동기 LLM 호출을 이벤트 루프 밖으로 빼되,
        # 워크스페이스 경로는 전역 env 가 아니라 인자로 넘긴다(동시 요청 격리).
        result = await _asyncio.to_thread(
            generate_plan,
            instruction=body.instruction,
            session_id=_uuid.uuid4().hex[:8],
            open_file=open_file,
            context_files=body.context_files or [],
            target_folder=body.target_folder or "",
            project_root=body.workspace_path or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"설계 결정 생성 실패: {e}") from e

    return result
