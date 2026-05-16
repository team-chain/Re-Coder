"""
postmortem_agent.py — Postmortem skeleton 자동 생성 에이전트 (설계서 §Q4 Must-Wedge)

설계서 명세:
  - 인시던트 발생 시 설계서 템플릿 기반의 Postmortem 초안 자동 생성
  - OTel(OpenTelemetry) 연결 시 trace 정보 포함
  - OTel 미연결 시 Watchdog 로그 / AuditLog fallback
  - Postmortem 파일: ~/.recoder/postmortems/{incident_id}.md

Postmortem 섹션 (설계서 템플릿):
  1. 개요 (Summary)
  2. 타임라인 (Timeline)
  3. 근본 원인 분석 (RCA)
  4. 영향 범위 (Impact)
  5. 조치 사항 (Action Items)
  6. 재발 방지 (Prevention)
  7. 참조 (References)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_POSTMORTEM_DIR = Path.home() / ".recoder" / "postmortems"
_AUDIT_LOG_DIR  = Path.home() / ".recoder" / "audit"
_WATCHDOG_LOG   = Path.home() / ".recoder" / "watchdog.log"


# ── 데이터 타입 ───────────────────────────────────────────────────────────

@dataclass
class OTelSpan:
    """OTel span 정보 (연결된 경우)."""
    trace_id:   str
    span_id:    str
    service:    str
    operation:  str
    duration_ms: float
    status:     str   # "OK" | "ERROR"
    error_msg:  str = ""


@dataclass
class PostmortemInput:
    """Postmortem 생성 입력."""
    incident_id:            str
    app_name:               str
    environment:            str                  # "production" | "staging" | "dev"
    severity:               int = 2              # 1~4
    title:                  str = ""             # 비어 있으면 자동 생성
    failed_image_tag:       str = ""
    last_healthy_image_tag: str = ""
    argocd_app_name:        str = ""
    rollback_pr_url:        str = ""
    failed_at:              str = ""             # ISO 8601
    resolved_at:            str = ""             # ISO 8601
    deployed_by:            str = ""
    cluster:                str = ""
    namespace:              str = "default"
    error_summary:          str = ""
    affected_users:         str = ""             # "~1,200 users" 형식
    revenue_impact:         str = ""             # "$X,XXX estimated" 형식
    otel_trace_id:          str = ""             # OTel trace ID (있으면 조회)
    otel_endpoint:          str = field(
        default_factory=lambda: os.environ.get("OTEL_ENDPOINT", "")
    )
    extra_refs:             list[str] = field(default_factory=list)


@dataclass
class PostmortemResult:
    """Postmortem 생성 결과."""
    success:          bool
    file_path:        str = ""
    incident_id:      str = ""
    markdown_preview: str = ""   # 첫 500자
    error:            str = ""
    logs:             list[str] = field(default_factory=list)

    def to_summary(self) -> dict:
        return {
            "success":     self.success,
            "file_path":   self.file_path,
            "incident_id": self.incident_id,
            "error":       self.error,
        }


# ── OTel 조회 (선택) ─────────────────────────────────────────────────────

def _fetch_otel_spans(endpoint: str, trace_id: str) -> list[OTelSpan]:
    """
    OTel Jaeger/Tempo REST API 에서 trace 조회.
    실패 시 빈 리스트 반환 (graceful fallback).
    """
    if not endpoint or not trace_id:
        return []
    import urllib.request, urllib.error
    # Jaeger 형식: GET /api/traces/{trace_id}
    url = f"{endpoint.rstrip('/')}/api/traces/{trace_id}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        spans = []
        for trace in data.get("data", []):
            for span in trace.get("spans", []):
                tags = {t["key"]: t["value"] for t in span.get("tags", [])}
                spans.append(OTelSpan(
                    trace_id=trace_id,
                    span_id=span.get("spanID", ""),
                    service=span.get("processID", ""),
                    operation=span.get("operationName", ""),
                    duration_ms=span.get("duration", 0) / 1000,
                    status="ERROR" if tags.get("error") else "OK",
                    error_msg=tags.get("error.message", ""),
                ))
        return spans
    except Exception as e:
        logger.debug(f"OTel 조회 실패 (fallback 사용): {e}")
        return []


# ── Watchdog / AuditLog fallback ─────────────────────────────────────────

def _read_watchdog_tail(n: int = 30) -> str:
    """Watchdog 로그에서 마지막 n줄 읽기."""
    if not _WATCHDOG_LOG.exists():
        return "(Watchdog 로그 없음)"
    lines = _WATCHDOG_LOG.read_text(errors="replace").splitlines()
    return "\n".join(lines[-n:])


def _read_recent_audit_logs(incident_id: str) -> list[dict]:
    """AuditLog 에서 incident_id 관련 항목 읽기."""
    if not _AUDIT_LOG_DIR.exists():
        return []
    results = []
    for f in sorted(_AUDIT_LOG_DIR.glob("*.json"), reverse=True)[:20]:
        try:
            data = json.loads(f.read_text())
            if incident_id and data.get("incident_id") == incident_id:
                results.append(data)
            elif not incident_id:
                results.append(data)
        except Exception:
            pass
    return results


# ── Postmortem 마크다운 생성 ─────────────────────────────────────────────

def _severity_label(s: int) -> str:
    return {1: "SEV-1 (Critical)", 2: "SEV-2 (High)", 3: "SEV-3 (Medium)", 4: "SEV-4 (Low)"}.get(s, f"SEV-{s}")


def _severity_emoji(s: int) -> str:
    return {1: "🔴", 2: "🟠", 3: "🟡", 4: "🟢"}.get(s, "⚪")


def _calc_duration(failed_at: str, resolved_at: str) -> str:
    if not failed_at or not resolved_at:
        return "(측정 중)"
    try:
        from datetime import datetime
        fmt = "%Y-%m-%dT%H:%M:%S"
        a = datetime.fromisoformat(failed_at.replace("Z", "+00:00"))
        b = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
        delta = abs((b - a).total_seconds())
        h, m = int(delta // 3600), int((delta % 3600) // 60)
        return f"{h}h {m}m" if h else f"{m}m {int(delta%60)}s"
    except Exception:
        return "(계산 불가)"


def _build_postmortem_md(inp: PostmortemInput,
                         spans: list[OTelSpan],
                         audit_logs: list[dict],
                         watchdog_tail: str) -> str:
    now_iso = datetime.now(timezone.utc).isoformat()
    sev_emoji = _severity_emoji(inp.severity)
    sev_label = _severity_label(inp.severity)
    title = inp.title or f"[{inp.incident_id}] {inp.app_name} 배포 실패 — {inp.failed_image_tag or 'unknown tag'}"
    duration = _calc_duration(inp.failed_at, inp.resolved_at)

    # ── OTel / fallback 섹션 ──────────────────────────────────────────
    if spans:
        error_spans = [s for s in spans if s.status == "ERROR"]
        otel_section = "### OTel Trace 분석\n\n"
        otel_section += f"- Trace ID: `{inp.otel_trace_id}`\n"
        otel_section += f"- 전체 span: {len(spans)}개 / ERROR span: {len(error_spans)}개\n\n"
        if error_spans:
            otel_section += "**오류 span 목록:**\n\n"
            otel_section += "| Service | Operation | Duration | Error |\n"
            otel_section += "|---------|-----------|----------|-------|\n"
            for s in error_spans[:10]:
                otel_section += f"| {s.service} | {s.operation} | {s.duration_ms:.1f}ms | {s.error_msg[:60]} |\n"
    else:
        # Watchdog + AuditLog fallback
        otel_section = "### 로그 Fallback (OTel 미연결)\n\n"
        otel_section += "> ⚠️ OTel 연결이 설정되지 않아 Watchdog 로그 / AuditLog 를 사용합니다.\n\n"
        if audit_logs:
            otel_section += "**AuditLog 관련 이벤트:**\n\n"
            for log in audit_logs[:5]:
                otel_section += f"- `{log.get('timestamp', '')}` — `{log.get('event', '')}` ({log.get('environment', '')})\n"
            otel_section += "\n"
        otel_section += "**Watchdog 최근 로그:**\n\n"
        otel_section += "```\n" + watchdog_tail + "\n```\n"

    # ── Action Items 초안 ─────────────────────────────────────────────
    action_items = [
        f"- [ ] 실패 이미지 `{inp.failed_image_tag}` 의 변경점 코드 리뷰",
        "- [ ] 스테이징 환경에서 rollback 이미지 재검증",
        "- [ ] 근본 원인 파악 및 수정",
        "- [ ] 수정 이미지 재배포 (정상 배포 파이프라인 통과 확인)",
    ]
    if inp.severity <= 1:
        action_items.insert(0, "- [ ] Sev-1: 30분 이내 Git reconciliation PR 생성 (ADR-005)")
        action_items.append("- [ ] Sev-1: 이해관계자 최종 보고")

    # ── 참조 섹션 ────────────────────────────────────────────────────
    refs = []
    if inp.rollback_pr_url:
        refs.append(f"- [Rollback PR]({inp.rollback_pr_url})")
    if inp.otel_trace_id and inp.otel_endpoint:
        refs.append(f"- [OTel Trace]({inp.otel_endpoint.rstrip('/')}/trace/{inp.otel_trace_id})")
    for r in inp.extra_refs:
        refs.append(f"- {r}")
    refs_section = "\n".join(refs) if refs else "- (참조 없음)"

    # ── 마크다운 조립 ────────────────────────────────────────────────
    return f"""# {sev_emoji} Postmortem: {title}

> **상태**: 초안 (Draft) — 담당자가 검토 후 완성해 주세요.
> **작성 일시**: {now_iso}
> **자동 생성**: ReCoder `postmortem_agent`

---

## 1. 개요 (Summary)

| 항목 | 값 |
|------|-----|
| 인시던트 ID | `{inp.incident_id}` |
| 심각도 | {sev_label} |
| 애플리케이션 | `{inp.app_name}` |
| 환경 | `{inp.environment}` |
| 실패 이미지 | `{inp.failed_image_tag or "N/A"}` |
| 복구 이미지 | `{inp.last_healthy_image_tag or "N/A"}` |
| ArgoCD App | `{inp.argocd_app_name or "N/A"}` |
| 클러스터 | `{inp.cluster or "N/A"}` |
| 네임스페이스 | `{inp.namespace}` |
| 발생 시각 | `{inp.failed_at or "N/A"}` |
| 해결 시각 | `{inp.resolved_at or "(진행 중)"}` |
| 장애 지속 시간 | `{duration}` |
| 영향 사용자 | {inp.affected_users or "(조사 필요)"} |
| 매출 영향 | {inp.revenue_impact or "(조사 필요)"} |

**오류 요약:**
```
{inp.error_summary or "(오류 정보 없음 — 로그 확인 필요)"}
```

---

## 2. 타임라인 (Timeline)

> ⚠️ 이 타임라인은 자동 생성된 초안입니다. 실제 시각과 이벤트를 검토/추가해 주세요.

| 시각 | 이벤트 |
|------|--------|
| `{inp.failed_at or "?"}` | 이미지 `{inp.failed_image_tag}` 배포 시작 (`{inp.deployed_by or "unknown"}`) |
| `?` | 배포 파이프라인 이상 감지 (Circuit Breaker / CloudWatch) |
| `?` | ReCoder 알림 발생 |
| `?` | Rollback PR 생성 및 머지 |
| `{inp.resolved_at or "?"}` | 서비스 정상화 확인 |

---

## 3. 근본 원인 분석 (RCA)

> ⚠️ 이 섹션은 자동 생성된 초안입니다. 조사 후 상세 내용을 채워 주세요.

### 즉각적 원인 (Immediate Cause)

- (조사 필요) 이미지 `{inp.failed_image_tag}` 에 포함된 변경사항으로 인한 장애 추정

### 근본 원인 (Root Cause)

- (조사 필요)

### 기여 요인 (Contributing Factors)

- (조사 필요)

{otel_section}

---

## 4. 영향 범위 (Impact)

- **영향 환경**: `{inp.environment}`
- **영향 사용자**: {inp.affected_users or "(조사 필요)"}
- **매출/비즈니스 영향**: {inp.revenue_impact or "(조사 필요)"}
- **데이터 손실**: 없음 (추정) — 확인 필요
- **SLA 위반**: (조사 필요)

---

## 5. 조치 사항 (Action Items)

### 즉각 조치 (완료)

- [x] Rollback PR 생성: {inp.rollback_pr_url or "(없음)"}
- [x] 이미지 `{inp.last_healthy_image_tag}` 로 복구 (ArgoCD 자동 sync)

### 후속 조치 (Action Items)

{chr(10).join(action_items)}

---

## 6. 재발 방지 (Prevention)

> ⚠️ 조사 완료 후 작성해 주세요.

- [ ] 배포 전 단계에서 추가 검증 항목 도입
- [ ] 모니터링/알림 임계값 조정
- [ ] 런북(runbook) 업데이트
- [ ] 팀 공유 및 학습 세션

---

## 7. 참조 (References)

{refs_section}

---

*이 문서는 ReCoder `postmortem_agent` 에 의해 자동 생성된 초안입니다.*
*[설계서 §Postmortem 템플릿] 을 기반으로 작성되었습니다.*
"""


# ── PostmortemAgent ───────────────────────────────────────────────────────

class PostmortemAgent:
    """
    인시던트 Postmortem skeleton 자동 생성 에이전트.

    호출 예::

        agent = PostmortemAgent()
        result = agent.generate(inp, log_fn=print)
    """

    def generate(
        self,
        inp: PostmortemInput,
        log_fn=None,
    ) -> PostmortemResult:
        logs: list[str] = []

        def _log(msg: str) -> None:
            logs.append(msg)
            logger.info(msg)
            if log_fn:
                log_fn(msg)

        result = PostmortemResult(success=False, incident_id=inp.incident_id, logs=logs)

        _log(f"[postmortem] 인시던트 {inp.incident_id} — Postmortem 생성 시작")

        # ── OTel 조회 (있으면) ────────────────────────────────────────
        spans: list[OTelSpan] = []
        if inp.otel_endpoint and inp.otel_trace_id:
            _log(f"[postmortem] OTel trace 조회: {inp.otel_trace_id}")
            spans = _fetch_otel_spans(inp.otel_endpoint, inp.otel_trace_id)
            _log(f"[postmortem] OTel span {len(spans)}개 수집")
        else:
            _log("[postmortem] OTel 미연결 — Watchdog/AuditLog fallback 사용")

        # ── Watchdog / AuditLog 읽기 ──────────────────────────────────
        watchdog_tail = _read_watchdog_tail(30) if not spans else ""
        audit_logs    = _read_recent_audit_logs(inp.incident_id) if not spans else []

        # ── Postmortem 마크다운 생성 ──────────────────────────────────
        _log("[postmortem] 마크다운 생성 중...")
        md = _build_postmortem_md(inp, spans, audit_logs, watchdog_tail)

        # ── 파일 저장 ─────────────────────────────────────────────────
        _POSTMORTEM_DIR.mkdir(parents=True, exist_ok=True)
        file_path = _POSTMORTEM_DIR / f"{inp.incident_id}.md"
        file_path.write_text(md, encoding="utf-8")
        _log(f"[postmortem] 저장 완료: {file_path}")

        # ── AuditLog 기록 ─────────────────────────────────────────────
        try:
            _AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            audit_entry = {
                "event":       "postmortem_generated",
                "timestamp":   datetime.now(timezone.utc).isoformat(),
                "incident_id": inp.incident_id,
                "app_name":    inp.app_name,
                "environment": inp.environment,
                "severity":    inp.severity,
                "file_path":   str(file_path),
                "otel_used":   bool(spans),
            }
            audit_path = _AUDIT_LOG_DIR / f"postmortem_{inp.incident_id}.json"
            audit_path.write_text(json.dumps(audit_entry, ensure_ascii=False, indent=2))
            _log(f"[postmortem] AuditLog 기록: {audit_path}")
        except Exception as e:
            _log(f"[postmortem] AuditLog 기록 실패 (무시): {e}")

        result.success          = True
        result.file_path        = str(file_path)
        result.markdown_preview = md[:500]
        return result


# ── 싱글턴 ───────────────────────────────────────────────────────────────

_postmortem_agent: Optional[PostmortemAgent] = None


def get_postmortem_agent() -> PostmortemAgent:
    global _postmortem_agent
    if _postmortem_agent is None:
        _postmortem_agent = PostmortemAgent()
    return _postmortem_agent
