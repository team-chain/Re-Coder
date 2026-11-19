"""
ReCoder Core — Code Analysis Routes

Handles LLM-powered patch proposal generation, approval, and listing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
try:
    from api.errors import internal_error as _internal_error
except ImportError:  # pragma: no cover
    from core.api.errors import internal_error as _internal_error  # type: ignore

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

    #: [중요] 여기서 그럴듯한 PatchProposal 을 200 OK 로 돌려주면 안 된다.
    #:
    #: 예전에는 `summary="[Placeholder] Orchestrator not yet available."` 를
    #: 정상 응답으로 내보냈다. 화면에서는 "분석했는데 고칠 게 없다"와 구분이
    #: 되지 않아서, **핵심 기능이 죽어 있는데 아무도 모르는** 상태가 됐다.
    #: 의존성 누락은 사용자가 할 수 있는 일이 있는 실패이므로, 실패로 알리고
    #: 무엇을 하면 되는지까지 말한다.
    raise HTTPException(
        status_code=503,
        detail=(
            "코드 분석 엔진(Orchestrator)을 불러오지 못했습니다. "
            "코어 로그에서 import 오류를 확인하거나 코어를 다시 시작해 주세요."
        ),
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

    # 4.6 승인 시 경계 검사에 쓸 워크스페이스 루트를 제안에 **고정**한다.
    #     워크스페이스가 없으면 활성 파일의 부모 폴더가 경계다. 둘 다 없으면
    #     빈 값으로 남고, 승인 핸들러가 fail-closed 로 거부한다.
    _root = (request.workspace_path or "").strip()
    if not _root and request.active_file_path:
        try:
            _root = str(Path(request.active_file_path).parent)
        except Exception:  # noqa: BLE001
            _root = ""
    proposal.workspace_root = _root

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

    # ── 워크스페이스 경계 (fail-closed) ──────────────────────────────
    # LLM 이 돌려준 patch.file 은 **비신뢰 입력**이다. 환각이나 프롬프트
    # 주입으로 경계 밖 절대경로가 와도, 승인 한 번으로 워크스페이스 밖의
    # 사용자 파일을 읽거나 덮어쓸 수 있으면 안 된다. 제안 생성 시 고정해 둔
    # workspace_root 를 기준으로, 해석된(resolve) 경로가 그 안에 있어야만
    # 진행한다. 루트가 비어 있으면 — 경계를 알 수 없으므로 — 거부한다.
    root_raw = (getattr(proposal, "workspace_root", "") or "").strip()
    if not root_raw:
        raise HTTPException(
            status_code=422,
            detail=(
                "이 제안에는 워크스페이스 경계 정보가 없어 적용할 수 없습니다. "
                "워크스페이스를 연 상태에서 다시 분석해 주세요."
            ),
        )
    try:
        workspace_root = Path(root_raw).resolve()
    except OSError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"워크스페이스 경계를 해석할 수 없습니다: {exc}",
        )

    try:
        for patch in proposal.patches:
            file_path = Path(patch.file)
            if not file_path.is_absolute():
                raise HTTPException(
                    status_code=422,
                    detail=f"Patch file path must be absolute: '{patch.file}'",
                )

            # resolve() 는 심볼릭 링크와 `..` 를 모두 푼다 — 문자열 비교로는
            # `/ws/../etc/passwd` 나 링크 우회를 막을 수 없다.
            resolved = file_path.resolve()
            if not (resolved == workspace_root or resolved.is_relative_to(workspace_root)):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"패치 대상이 워크스페이스 밖입니다: '{patch.file}' — "
                        "승인으로 워크스페이스 밖 파일을 수정할 수 없습니다."
                    ),
                )
            file_path = resolved

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
        raise _internal_error(exc, message="패치를 적용하지 못해 원래 파일로 되돌렸습니다.") from exc

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


# 게이트가 시크릿을 가릴 때 쓰는 토큰 형태. ContextGate의 세부 토큰
# ([MASKED], [MASKED_AWS_KEY], …)과 선택적 게이트를 불러오지 못했을 때
# _mask_request()가 쓰는 [REDACTED]를 모두 같은 마스킹 자리로 취급한다.
_MASK_TOKEN_RE = __import__("re").compile(r"\[(?:MASKED[A-Z_]*|REDACTED)\]")

#: 마스크 줄 전용 스캐너 — 따옴표 문자열 또는 마스크 토큰.
_MASK_SCAN_RE = __import__("re").compile(
    r'"[^"]*"|\'[^\']*\'|\[(?:MASKED[A-Z_]*|REDACTED)\]')


def _lines_match(diff_line: str, file_line: str) -> bool:
    """diff 의 한 줄이 파일의 한 줄과 일치하는가.

    예전 구현은 **모든** 따옴표 문자열을 와일드카드로 바꿨다. 그러면
    `app.get("/a")` 와 `app.get("/b")` 처럼 문자열로만 구분되는 블록이
    같아져서, diff 가 지목한 블록이 아니라 **파일에서 먼저 나오는 블록**에
    패치가 적용됐다 — 승인된 편집이 소리 없이 엉뚱한 곳을 고치는 형태다.

    지금은 두 단계다:
    1. 공백을 정돈한 정확 비교. 대부분 여기서 끝난다.
    2. diff 줄에 **마스크 토큰**([MASKED...] 또는 [REDACTED])이 있을 때만 관용 비교 —
       LLM 은 마스킹된 값을 보고 diff 를 쓰므로 실제 파일의 시크릿과
       글자가 다를 수밖에 없다. 이때도 와일드카드는 마스크가 있는 자리
       (마스크 품은 따옴표 문자열, 맨몸 마스크 토큰)에만 적용되고,
       같은 줄의 다른 문자열 리터럴은 그대로 비교한다.
    """
    a, b = diff_line.strip(), file_line.strip()
    if a == b:
        return True
    if not _MASK_TOKEN_RE.search(a):
        return False
    return _mask_tolerant_pattern(a).fullmatch(b) is not None


def _mask_tolerant_pattern(diff_line: str) -> "__import__('re').Pattern":
    """마스크 토큰이 든 diff 줄 → 파일 줄과 대조할 정규식.

    - `"…[MASKED]…"`/`"[REDACTED]"` → 같은 따옴표의 아무 내용
    - 맨몸 마스크 토큰 → 공백 아닌 아무 시퀀스 (`KEY=[MASKED]` ↔ `KEY=abc`)
    - 마스크 없는 따옴표 문자열·나머지 글자 → **그대로** (구분자 역할 유지)
    """
    import re as _re
    pieces: list[str] = []
    idx = 0
    for m in _MASK_SCAN_RE.finditer(diff_line):
        pieces.append(_re.escape(diff_line[idx:m.start()]))
        tok = m.group(0)
        if tok.startswith("["):
            pieces.append(r"\S+")
        elif _MASK_TOKEN_RE.search(tok):
            q = tok[0]
            pieces.append(q + ("[^%s]*" % q) + q)
        else:
            pieces.append(_re.escape(tok))
        idx = m.end()
    pieces.append(_re.escape(diff_line[idx:]))
    return _re.compile("".join(pieces) + r"\Z")


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
            found = -1
            for i in range(0, len(lines) - len(old_block) + 1):
                if all(_lines_match(old_block[j], lines[i + j]) for j in range(len(old_block))):
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
    file_path.write_text(new_text, encoding="utf-8", newline="")  # nl 로 이미 맞춘 줄바꿈을 그대로(2.0.32)
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
    #: "auto"(작은 요청은 한 번에) | "team"(처음부터 여러 에이전트가 나눠 만든다)
    mode: str = "auto"
    #: 멈춘 대규모 생성을 이어 만들 작업 ID(일시 정지 응답의 resume_job).
    resume_job: str = ""
    #: 팀 모드 동시 작업 에이전트 수(1~8). 0 이면 기본값.
    agents: int = 0
    #: 이어 만들 때 정해진 횟수를 넘겨 실패한 파일을 빼고 결과를 받는다(사용자가 [이 파일 빼고 결과 받기]를 고른 경우).
    skip_failed: bool = False
    #: 2.0.43 — 사용자가 확인·승인한 명세(목표·범위·합격 조건). 없으면 명세 없이 생성한 것과 같다.
    spec: dict | None = None
    #: 2.0.45 — 팀 구성 창에서 역할마다 고른 AI 공급자 {planner|dev|review|test: quality|fast|anthropic|openai|compatible}.
    models: dict | None = None
    #: 2.0.45 — 테스트 리코더(합격 조건을 띄운 앱에 시험). 끄면 예전과 같다.
    #: 2.0.45 테스트 리코더 — 확장이 팀 구성 창의 값을 늘 보낸다. 값을 모르는 예전 클라이언트는 예전처럼 끈다.
    test: bool = False
    lockSensitive: bool = True


@router.post("/api/code/generate")
async def generate_code_route(body: CodeGenerateRequest) -> dict:
    import asyncio as _asyncio
    import uuid as _uuid

    if not (body.instruction or "").strip():
        raise HTTPException(status_code=400, detail="instruction 이 비어 있습니다.")

    # NOTE: 예전에는 여기서 `RECODER_PROJECT_ROOT` 전역 env 에 워크스페이스
    # 경로를 심었다. 아래 to_thread 로 넘어가는 지점에서 실행이 양보되므로,
    # 그 사이 **다른 창의 요청**이 전역을 덮어쓰면 이 요청이 남의 워크스페이스
    # 를 기준으로 파일을 만든다(회차1 교차 오염 P1, 커밋 489e3be). 그때
    # `project_root` 인자 전달만 추가하고 전역 쓰기를 지우지 않아 사고 경로가
    # 남아 있었다 — 경로를 안 보낸 요청이 `_project_root()` 폴백으로 그 전역을
    # 읽으면 여전히 남의 워크스페이스를 잡는다. 그래서 쓰기 자체를 없앤다.
    # 요청별 경로는 항상 인자로만 넘긴다(아래 project_root=).

    open_file = None
    if body.open_file_path or body.open_file_content:
        open_file = {"path": body.open_file_path, "content": body.open_file_content}

    try:
        try:
            from code_agent import generate_code
            from generation_jobs import GenerationPaused
        except ImportError:
            from core.code_agent import generate_code
            from core.generation_jobs import GenerationPaused
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
            mode=_code_mode(body.mode),
            job_id=body.resume_job or "",
            skip_failed=bool(body.skip_failed and body.resume_job),
            spec=body.spec,
            models=body.models,
            test=body.test,
            lock_sensitive=body.lockSensitive,
        )
    except GenerationPaused as e:
        #: 다 만든 파일은 저장됐다 — 같은 요청을 resume_job 과 함께 다시 보내면 이어 만든다.
        raise HTTPException(status_code=409, detail={"message": str(e), **e.progress_payload()}) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise _internal_error(e, message="코드 생성에 실패했습니다.") from e

    return result


def _code_mode(mode: str) -> str:
    return "team" if str(mode or "").strip().lower() == "team" else "auto"


@router.post("/api/code/generate/stream")
async def generate_code_stream_route(body: CodeGenerateRequest):
    """대규모 생성 — 시간 상한 없이 진행 이벤트(SSE)를 흘린다. 보는 쪽이 끊겨도 작업은 끝까지 간다.

    이벤트: planning·planned·wave·file_start·file_part·file_done·fixing·retry·waiting·consistency·done·error.
    error 에 resumable=true 면 resume_job 으로 같은 요청을 다시 보내 멈춘 지점부터 이어 만든다.
    """
    import uuid as _uuid
    try:
        from code_agent import generate_code
        import generation_jobs as _jobs
        import generation_progress as _progress
    except ImportError:
        from core.code_agent import generate_code
        from core import generation_jobs as _jobs  # type: ignore
        from core import generation_progress as _progress  # type: ignore

    if not (body.instruction or "").strip():
        raise HTTPException(status_code=400, detail="instruction 이 비어 있습니다.")
    open_file = None
    if body.open_file_path or body.open_file_content:
        open_file = {"path": body.open_file_path, "content": body.open_file_content}
    job_id = body.resume_job if _jobs.valid_job_id(body.resume_job) else _jobs.new_job_id()
    agents = body.agents if 0 < int(body.agents or 0) <= 8 else None

    def operation():
        return generate_code(
            instruction=body.instruction,
            session_id=_uuid.uuid4().hex[:8],
            open_file=open_file,
            prior_files=body.prior_files or [],
            context_files=body.context_files or [],
            target_folder=body.target_folder or "",
            decisions=body.decisions or [],
            project_root=body.workspace_path or "",
            mode=_code_mode(body.mode),
            job_id=job_id,
            agents=agents,
            skip_failed=bool(body.skip_failed and body.resume_job),
            spec=body.spec,
            models=body.models,
            test=body.test,
            lock_sensitive=body.lockSensitive,
            #: 확장은 진행 이벤트를 받으며 기다리므로 자동 교정 시간을 넉넉히 준다.
            budget_seconds=3600,
        )

    return _progress.stream(operation, job_id)


# ---------------------------------------------------------------------------
# AI-DLC 1단계: 코드 대신 "설계 결정" 제시 (/api/code/plan)
#   ReCoder_AI-DLC_도입_설계서.md §3.2 — 회차1 범위.
#   자연어 요청 -> 설계 결정 목록(decisions). 코드는 아직 생성하지 않는다.
#   확장(webview)이 이 목록을 결정 카드로 렌더 -> 사용자가 옵션 선택·승인 ->
#   그 결과를 담아 /api/code/generate 를 다시 호출하는 흐름을 전제로 한다.
# ---------------------------------------------------------------------------


class CodePlanRequest(BaseModel):
    instruction: str = ""
    #: "custom" — 쇼핑몰 시작 방식에서 AI 자유 생성을 고른 뒤 이어서 묻는 결정을 요청한다.
    after_starter: str = ""
    workspace_path: str = ""
    open_file_path: str = ""
    open_file_content: str = ""
    context_files: list = []
    target_folder: str = ""


@router.post("/api/code/plan")
async def code_plan_route(body: CodePlanRequest) -> dict:
    import asyncio as _asyncio
    import uuid as _uuid

    if not (body.instruction or "").strip():
        raise HTTPException(status_code=400, detail="instruction 이 비어 있습니다.")

    # generate 와 동일 — 전역 env 쓰기 없음. 사유는 generate_code_route 주석 참조.

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
            after_starter="custom" if body.after_starter == "custom" else "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise _internal_error(e, message="설계 결정 생성에 실패했습니다.") from e

    return result


# ---------------------------------------------------------------------------
# 2.0.43 명세 · 합격 조건 (/api/code/spec)
#   설계 결정을 고른 뒤, 코드를 만들기 전에 "무엇이 되면 완성인가"(목표·범위·합격 조건) 초안을 받는다.
#   사용자가 화면에서 고친 명세가 /api/code/generate 의 spec 으로 다시 온다. 코드는 만들지 않는다.
# ---------------------------------------------------------------------------


class CodeSpecRequest(BaseModel):
    instruction: str = ""
    decisions: list = []
    workspace_path: str = ""
    open_file_path: str = ""
    open_file_content: str = ""
    context_files: list = []
    target_folder: str = ""


@router.post("/api/code/spec")
async def code_spec_route(body: CodeSpecRequest) -> dict:
    import asyncio as _asyncio

    if not (body.instruction or "").strip():
        raise HTTPException(status_code=400, detail="instruction 이 비어 있습니다.")
    open_file = None
    if body.open_file_path or body.open_file_content:
        open_file = {"path": body.open_file_path, "content": body.open_file_content}
    try:
        try:
            from code_agent import generate_spec
        except ImportError:
            from core.code_agent import generate_spec
        # 워크스페이스 경로는 전역 env 가 아니라 인자로 넘긴다(동시 요청 격리 — generate_code_route 주석 참조).
        return await _asyncio.to_thread(
            generate_spec,
            instruction=body.instruction,
            decisions=body.decisions or [],
            open_file=open_file,
            context_files=body.context_files or [],
            target_folder=body.target_folder or "",
            project_root=body.workspace_path or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise _internal_error(e, message="명세 초안 생성에 실패했습니다.") from e
