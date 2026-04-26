"""
ReCoder 팀 공통 데이터 계약 (schemas.py)
모든 에이전트가 이 파일을 import해서 사용한다.
변경 시 팀 전체 합의 필요.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


# ── 공통 열거형 ───────────────────────────────────────────────────────

class ContextSource(str, Enum):
    UIA       = "uia"
    RAPIDOCR  = "rapidocr"
    VISION    = "vision"
    TERMINAL  = "terminal"
    USER_INPUT = "user_input"

class ContextWeight(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"

class EventType(str, Enum):
    ERROR_DETECTED = "error_detected"
    TASK_CHANGE    = "task_change"
    USER_QUESTION  = "user_question"
    TERMINAL_ERROR = "terminal_error"
    RESOLVED       = "resolved"

class OrchestratorState(str, Enum):
    IDLE                 = "IDLE"
    ERROR_DETECTED       = "ERROR_DETECTED"
    ANALYZING            = "ANALYZING"
    WAITING_USER_ACTION  = "WAITING_USER_ACTION"
    CODE_PATCH_PROPOSED  = "CODE_PATCH_PROPOSED"
    APPLYING_PATCH       = "APPLYING_PATCH"
    CODE_READY           = "CODE_READY"
    INFRA_PROPOSED       = "INFRA_PROPOSED"
    INFRA_READY          = "INFRA_READY"

class UserAction(str, Enum):
    FIX_CODE    = "fix_code"
    EXPLAIN     = "explain_error"
    SEARCH      = "search"
    IGNORE      = "ignore"
    DOCKERFILE  = "generate_dockerfile"
    DOCKER_COMPOSE = "generate_docker_compose"
    GITHUB_ACTIONS = "generate_github_actions"

class RiskLevel(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"


# ── 9.1 ExtractedContext ──────────────────────────────────────────────

@dataclass
class ExtractedContext:
    context_id:    str
    source:        ContextSource
    app_name:      str
    window_title:  str
    text:          str
    weight:        ContextWeight
    quality_score: float          # 0.0 ~ 1.0
    failure_flag:  bool           # UIA 실패 여부
    captured_at:   str            # ISO8601

    def to_dict(self) -> dict:
        return {
            "context_id":    self.context_id,
            "source":        self.source.value,
            "app_name":      self.app_name,
            "window_title":  self.window_title,
            "text":          self.text,
            "weight":        self.weight.value,
            "quality_score": self.quality_score,
            "failure_flag":  self.failure_flag,
            "captured_at":   self.captured_at,
        }


# ── 9.2 AgentEvent ────────────────────────────────────────────────────

@dataclass
class AgentEvent:
    event_id:          str
    event_type:        EventType
    summary:           str
    contexts:          list[str]          # context_id 목록
    importance_score:  int                # 0 ~ 100
    suggested_actions: list[UserAction]
    created_at:        str                # ISO8601
    raw_errors:        list[str] = field(default_factory=list)
    error_text:        str = ""           # 원본 에러 텍스트

    def to_dict(self) -> dict:
        return {
            "event_id":          self.event_id,
            "event_type":        self.event_type.value,
            "summary":           self.summary,
            "contexts":          self.contexts,
            "importance_score":  self.importance_score,
            "suggested_actions": [a.value for a in self.suggested_actions],
            "created_at":        self.created_at,
            "raw_errors":        self.raw_errors,
            "error_text":        self.error_text,
        }


# ── 9.3 PatchProposal ─────────────────────────────────────────────────

@dataclass
class FilePatch:
    file:         str
    base_sha256:  str
    unified_diff: str

    def to_dict(self) -> dict:
        return {
            "file":         self.file,
            "base_sha256":  self.base_sha256,
            "unified_diff": self.unified_diff,
        }

@dataclass
class PatchProposal:
    proposal_id:  str
    summary:      str
    risk:         RiskLevel
    test_command: str
    patches:      list[FilePatch]

    def to_dict(self) -> dict:
        return {
            "proposal_id":  self.proposal_id,
            "summary":      self.summary,
            "risk":         self.risk.value,
            "test_command": self.test_command,
            "patches":      [p.to_dict() for p in self.patches],
        }


# ── 9.4 InfraFileProposal ─────────────────────────────────────────────

@dataclass
class InfraFileProposal:
    proposal_id:   str
    file_type:     Literal["Dockerfile", "docker-compose", "github-actions"]
    target_path:   str
    content:       str
    base_template: str
    risk:          RiskLevel

    def to_dict(self) -> dict:
        return {
            "proposal_id":   self.proposal_id,
            "file_type":     self.file_type,
            "target_path":   self.target_path,
            "content":       self.content,
            "base_template": self.base_template,
            "risk":          self.risk.value,
        }


# ── 9.5 SessionRecord ─────────────────────────────────────────────────

@dataclass
class SessionError:
    time:                  str
    error_summary:         str
    error_fingerprint:     str
    related_file_names:    list[str]
    ai_suggestion_summary: str
    user_action:           Literal["approved", "rejected", "ignored"]
    result:                Literal["success", "failed", "pending"]
    validation:            Literal["test_passed", "syntax_ok", "unknown"]

@dataclass
class SessionRecord:
    session_id:           str
    start_time:           str
    end_time:             str | None
    project_name_hash:    str
    errors:               list[SessionError] = field(default_factory=list)
    generated_file_types: list[str]         = field(default_factory=list)
    raw_content_saved:    bool              = False
    deployments:          list[dict]        = field(default_factory=list)


# ── Orchestrator 상태 이벤트 (server → widget 전송용) ─────────────────

@dataclass
class OrchestratorUpdate:
    """server.py가 widget에 전송하는 상태 업데이트."""
    state:          OrchestratorState
    event:          AgentEvent | None         = None
    patch_proposal: PatchProposal | None      = None
    infra_proposal: InfraFileProposal | None  = None
    message:        str                       = ""

    def to_dict(self) -> dict:
        return {
            "type":           "orchestrator_update",
            "state":          self.state.value,
            "event":          self.event.to_dict() if self.event else None,
            "patch_proposal": self.patch_proposal.to_dict() if self.patch_proposal else None,
            "infra_proposal": self.infra_proposal.to_dict() if self.infra_proposal else None,
            "message":        self.message,
        }
