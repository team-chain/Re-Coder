"""
rca_agent.py — RCA MVP (설계서 §Q4 RCA MVP 성공 기준).

설계서 명세 (그대로 옮김):
    RCA 는 "확정 원인" 을 말하지 않는다. 근거 기반 후보 제안 이 정의다.

    구조화 출력 4가지 (이것이 Must RCA 전부):
      1) 가장 의심되는 배포 이벤트와 근거
      2) 관련 변경 파일 목록
      3) 관측된 증상 (error rate, memory, restart event, health check failure)
      4) 가능성 높은 원인 후보 1~3 개와 각각의 근거

    표현 원칙:
      - "원인입니다" 사용 금지 → "가장 가능성 높은 원인 후보입니다"
      - confidence score 함께 표시
      - 관측 데이터 부족 시 "insufficient evidence" 표시

본 모듈은 두 가지 모드를 제공한다:
  A) deterministic 모드 — LLM 호출 없이 입력 신호/correlation 만으로 후보 산출.
     (테스트 가능, OTel 미연결 환경 fallback)
  B) llm-assisted 모드  — LLMProviderRouter.complete() 로 candidate hypothesis 문장만 보강.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

try:
    from schemas import (
        CorrelationResult,
        DeploymentRecord,
        IncidentTimeline,
        RCACandidate,
        RCAReport,
        RCASymptom,
    )
except ImportError:
    from core.schemas import (  # type: ignore
        CorrelationResult,
        DeploymentRecord,
        IncidentTimeline,
        RCACandidate,
        RCAReport,
        RCASymptom,
    )


_BANNED_PHRASES = (
    "원인입니다",
    "확정된 원인",
    "definite cause",
    "is the cause",
)
_REPLACEMENT_PHRASE = "가능성 높은 원인 후보입니다"


# ---------------------------------------------------------------------------
# Input dataclass
# ---------------------------------------------------------------------------


@dataclass
class RCAInput:
    incident_id: str
    timeline: IncidentTimeline
    correlation: Optional[CorrelationResult] = None
    suspected_deployment: Optional[DeploymentRecord] = None
    metric_snapshot: dict[str, Any] = field(default_factory=dict)  # OTelQueryService snapshot
    changed_files: list[str] = field(default_factory=list)
    log_excerpts: list[str] = field(default_factory=list)
    extra_context: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class RCAAgent:
    """RCA Agent. LLMProviderRouter 가 주입되면 후보 문장을 보강한다."""

    def __init__(self, llm_router: Optional[Any] = None) -> None:
        self.llm_router = llm_router

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, inp: RCAInput) -> RCAReport:
        """동기 entry point — 내부에서 async LLM 호출이 필요하면 새 loop 를 만든다."""

        report = self._build_deterministic(inp)
        if self.llm_router is not None and not report.insufficient_evidence:
            try:
                report = asyncio.run(self._augment_with_llm(report, inp))
            except RuntimeError:
                # 이미 event loop 안에서 호출된 경우 → 동기 fallback 유지
                log.debug("RCAAgent: nested loop — skip LLM augmentation")
        return _sanitize_report(report)

    async def analyze_async(self, inp: RCAInput) -> RCAReport:
        report = self._build_deterministic(inp)
        if self.llm_router is not None and not report.insufficient_evidence:
            report = await self._augment_with_llm(report, inp)
        return _sanitize_report(report)

    # ------------------------------------------------------------------
    # Deterministic skeleton — 입력만으로 4개 섹션을 채운다
    # ------------------------------------------------------------------

    def _build_deterministic(self, inp: RCAInput) -> RCAReport:
        sus_dep_id = (
            inp.suspected_deployment.deployment_id if inp.suspected_deployment else None
        )
        sus_reason = self._suspected_reason(inp)

        symptoms = self._build_symptoms(inp.metric_snapshot, inp.timeline)
        candidates = self._build_candidates(inp, symptoms)

        # 관측 데이터 빈약도 평가
        insufficient = (
            not symptoms
            and not candidates
            and not inp.timeline.events
        )

        # overall confidence = 후보 confidence 평균 (없으면 0)
        overall_conf = (
            round(sum(c.confidence for c in candidates) / len(candidates), 3)
            if candidates
            else 0.0
        )

        return RCAReport(
            incident_id=inp.incident_id,
            generated_at=datetime.now(timezone.utc),
            suspected_deployment_id=sus_dep_id,
            suspected_deployment_reason=sus_reason,
            related_files=list(inp.changed_files)[:50],
            symptoms=symptoms,
            candidates=candidates,
            overall_confidence=overall_conf,
            insufficient_evidence=insufficient,
        )

    def _suspected_reason(self, inp: RCAInput) -> str:
        if inp.correlation and inp.correlation.candidate_deployment_id:
            return (
                f"correlation_score={inp.correlation.correlation_score:.2f}, "
                f"label={inp.correlation.label}. {inp.correlation.rationale}"
            )
        if inp.suspected_deployment:
            return (
                f"가장 최근 배포 {inp.suspected_deployment.image} 가 "
                f"{inp.suspected_deployment.deployed_at:%Y-%m-%d %H:%M} 에 수행됨."
            )
        return "후보 배포 이벤트를 식별하지 못했습니다."

    def _build_symptoms(
        self,
        snapshot: dict[str, Any],
        timeline: IncidentTimeline,
    ) -> list[RCASymptom]:
        out: list[RCASymptom] = []
        if snapshot:
            er_now = snapshot.get("error_rate_now")
            er_base = snapshot.get("error_rate_baseline")
            if er_now is not None and er_base is not None:
                out.append(RCASymptom(
                    name="error_rate",
                    value=f"{er_now:.3f}",
                    delta=f"{er_now - er_base:+.3f}",
                    evidence="prometheus error_rate vs baseline",
                ))
            lat_now = snapshot.get("latency_p95_now")
            lat_base = snapshot.get("latency_p95_baseline")
            if lat_now is not None and lat_base is not None:
                out.append(RCASymptom(
                    name="latency_p95",
                    value=f"{lat_now:.2f}",
                    delta=f"{lat_now - lat_base:+.2f}",
                    evidence="prometheus latency p95 vs baseline",
                ))
            if snapshot.get("restart_count_recent") not in (None, 0):
                out.append(RCASymptom(
                    name="restart_count",
                    value=str(snapshot.get("restart_count_recent")),
                    evidence="container_restarts_total increase(10m)",
                ))
            if snapshot.get("memory_bytes"):
                out.append(RCASymptom(
                    name="memory_bytes",
                    value=str(int(snapshot["memory_bytes"])),
                    evidence="container_memory_usage_bytes",
                ))
        # Health check 실패가 타임라인에 있으면 증상으로 추가
        for ev in timeline.events:
            if ev.kind.value == "health_check":
                out.append(RCASymptom(
                    name="health_check_failure",
                    value="failed",
                    evidence=ev.detail or ev.title,
                ))
                break
        return out

    def _build_candidates(
        self,
        inp: RCAInput,
        symptoms: list[RCASymptom],
    ) -> list[RCACandidate]:
        candidates: list[RCACandidate] = []

        # 1) 가장 강한 후보 — correlation 이 candidate 라벨이고 changed_files 가 있는 경우
        if (
            inp.correlation
            and inp.correlation.label == "candidate"
            and inp.changed_files
        ):
            candidates.append(RCACandidate(
                hypothesis=(
                    f"최근 배포({inp.correlation.candidate_image_tag}) 의 코드 변경이 "
                    f"이번 인시던트의 {_REPLACEMENT_PHRASE}."
                ),
                evidence=[
                    f"correlation_score={inp.correlation.correlation_score:.2f}",
                    f"changed_files={len(inp.changed_files)}",
                ] + [s.evidence or "" for s in symptoms[:2] if s.evidence],
                confidence=min(0.85, 0.4 + inp.correlation.correlation_score * 0.5),
                rollback_hint=inp.correlation.candidate_image_tag,
                related_files=list(inp.changed_files)[:20],
            ))

        # 2) 설정/시크릿 변경 후보 — symptom 에 health_check_failure 가 있고 changed files 가 비어있는 경우
        if any(s.name == "health_check_failure" for s in symptoms) and not inp.changed_files:
            candidates.append(RCACandidate(
                hypothesis=(
                    f"환경 변수 / 시크릿 / 외부 의존성 변경이 health check 실패의 "
                    f"{_REPLACEMENT_PHRASE}."
                ),
                evidence=[
                    "코드 변경이 관측되지 않음에도 health check 가 실패",
                    "ENV / Secret 회전, 외부 API 인증 실패, DB 자격증명 만료 등을 점검 필요",
                ],
                confidence=0.45,
            ))

        # 3) 의존성 후보 — correlation 신호에 DEPENDENCY_ERROR 가 강하게 잡힌 경우
        if inp.correlation:
            dep_signals = [
                s for s in inp.correlation.signals
                if s.kind.value == "dependency_error" and s.score > 0.0
            ]
            if dep_signals:
                candidates.append(RCACandidate(
                    hypothesis=(
                        f"외부 의존성 장애가 {_REPLACEMENT_PHRASE}."
                    ),
                    evidence=[s.evidence for s in dep_signals],
                    confidence=min(0.55, 0.3 + dep_signals[0].score * 0.3),
                ))

        # 후보가 전혀 없으면 약한 일반 후보 1개 추가 (insufficient evidence 가 따로 마킹됨)
        if not candidates and (symptoms or inp.timeline.events):
            candidates.append(RCACandidate(
                hypothesis=(
                    f"명확한 변경 신호 없이 증상만 관측됨 — "
                    f"외부 부하 / 트래픽 패턴 변화가 {_REPLACEMENT_PHRASE}."
                ),
                evidence=[s.evidence or s.name for s in symptoms[:3] if s.evidence],
                confidence=0.25,
            ))

        return candidates[:3]

    # ------------------------------------------------------------------
    # LLM-assisted augmentation (optional)
    # ------------------------------------------------------------------

    async def _augment_with_llm(self, report: RCAReport, inp: RCAInput) -> RCAReport:
        if self.llm_router is None:
            return report

        prompt = self._build_llm_prompt(report, inp)
        schema = {
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "required": ["hypothesis", "confidence"],
                        "properties": {
                            "hypothesis": {"type": "string"},
                            "evidence":   {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": "number"},
                            "rollback_hint": {"type": "string"},
                        },
                    },
                }
            },
            "required": ["candidates"],
        }
        try:
            raw = await self.llm_router.complete(
                prompt=prompt,
                model_preference="sonnet",
                agent="rca_agent",
                operation="augment_candidates",
                schema=schema,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("RCA LLM augmentation failed: %s", exc)
            return report

        data = _coerce_json(raw)
        if not isinstance(data, dict) or "candidates" not in data:
            return report

        # 기존 후보를 보존하고 LLM 후보로 보강 (총 3개 cap)
        merged: list[RCACandidate] = list(report.candidates)
        for item in data["candidates"][: max(0, 3 - len(merged))]:
            try:
                cand = RCACandidate(
                    hypothesis=str(item.get("hypothesis", "")),
                    evidence=[str(e) for e in (item.get("evidence") or [])][:6],
                    confidence=float(item.get("confidence", 0.3)),
                    rollback_hint=item.get("rollback_hint"),
                )
                merged.append(cand)
            except Exception:  # noqa: BLE001
                continue

        return report.model_copy(update={
            "candidates": merged[:3],
            "overall_confidence": round(
                sum(c.confidence for c in merged) / len(merged), 3
            ) if merged else 0.0,
        })

    def _build_llm_prompt(self, report: RCAReport, inp: RCAInput) -> str:
        excerpts = "\n".join(f"- {l[:240]}" for l in inp.log_excerpts[:10])
        symptom_block = "\n".join(
            f"- {s.name}: {s.value} (Δ={s.delta or '?'}) — {s.evidence or ''}"
            for s in report.symptoms
        )
        return (
            "당신은 SRE 보조 도구입니다. 아래 인시던트 컨텍스트를 보고 가능성 높은 원인 후보를 "
            "최대 3개 제안하세요. 답변 톤은 반드시 '가능성 높은 원인 후보입니다' 라는 가능성 표현을 "
            "유지하고, '원인입니다' 같은 확정 표현은 사용하지 마세요. JSON schema 를 따르세요.\n\n"
            f"Incident: {report.incident_id}\n"
            f"Suspected deployment: {report.suspected_deployment_id} — "
            f"{report.suspected_deployment_reason}\n"
            f"Changed files: {len(report.related_files)} files\n"
            f"Symptoms:\n{symptom_block or '- none observed'}\n"
            f"Log excerpts:\n{excerpts or '- none'}\n"
        )


# ---------------------------------------------------------------------------
# Sanitizer — 금지 표현 차단 (LLM 출력 안전망)
# ---------------------------------------------------------------------------


def _sanitize_report(report: RCAReport) -> RCAReport:
    new_candidates = []
    for c in report.candidates:
        h = c.hypothesis
        for banned in _BANNED_PHRASES:
            if banned in h:
                h = h.replace(banned, _REPLACEMENT_PHRASE)
        new_candidates.append(c.model_copy(update={"hypothesis": h}))
    return report.model_copy(update={"candidates": new_candidates})


def _coerce_json(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        # JSON 코드 블록 안에 들어있는 경우 추출
        m = re.search(r"```json\s*(.*?)\s*```", text, re.S)
        if m:
            text = m.group(1)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return None


__all__ = ["RCAAgent", "RCAInput"]
