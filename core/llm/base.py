"""
LLMProvider 추상 클래스 (설계서 v5.7 §3.1).

각 Agent는 LLM Provider를 직접 호출하지 않고
router를 통해서만 호출한다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class LLMErrorType(str, Enum):
    """설계서 v5.7 §3.5 -- LLM 실패 유형 분류."""
    MODEL_NOT_FOUND   = "model_not_found"
    ACCESS_DENIED     = "access_denied"
    THROTTLING        = "throttling"
    QUOTA_EXCEEDED    = "quota_exceeded"
    VALIDATION_ERROR  = "validation_error"
    CONTEXT_TOO_LONG  = "context_too_long"
    STRUCTURED_OUTPUT = "structured_output"
    SERVICE_ERROR     = "service_error"
    UNKNOWN           = "unknown"


class LLMError(Exception):
    """LLM 호출 실패 예외."""
    def __init__(
        self,
        message: str,
        error_type: LLMErrorType = LLMErrorType.UNKNOWN,
        retryable: bool = False,
        raw: Exception | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.retryable  = retryable
        self.raw        = raw
        # router 가 실패 레코드를 첨부할 수 있도록 예약 슬롯
        self.llm_call_record: dict | None = None


@dataclass
class LLMRequest:
    """Provider 공통 입력 구조."""
    prompt:      str
    system:      str             = ""
    json_schema: dict | None     = None       # Structured Output 스키마
    image_bytes: bytes | None    = None       # Vision 선택적 이미지
    image_mime:  str             = "image/png"
    max_tokens:  int             = 4096
    temperature: float           = 0.0
    metadata:    dict            = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Provider 공통 출력 구조."""
    text:          str                        # 원본 텍스트 응답
    parsed:        dict | None  = None        # JSON 파싱 결과 (json_schema 요청 시)
    model_used:    str          = ""          # 실제 사용된 모델 ID
    provider:      str          = ""          # "bedrock" | "gemini"
    region:        str          = ""          # Bedrock 전용 리전
    input_tokens:  int          = 0
    output_tokens: int          = 0
    # §3.7 CountTokens 폴백 정책
    token_source:  str          = "local_estimate"
    # router 가 측정 후 채워주는 필드 (설계서 §3.8)
    latency_ms:    int          = 0
    fallback_used: bool         = False
    retry_count:   int          = 0
    metadata:      dict         = field(default_factory=dict)


class LLMProvider(ABC):
    """모든 LLM Provider의 추상 기반 클래스 (설계서 §3.1)."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Provider 식별자 ("bedrock" | "gemini")."""

    @abstractmethod
    def call(self, request: LLMRequest) -> LLMResponse:
        """동기 LLM 호출. LLMError 를 raise 할 수 있다."""

    def is_retryable_error(self, error: LLMError) -> bool:
        """폴백 가능한 오류인지 여부 (설계서 §3.5)."""
        return error.error_type in (
            LLMErrorType.MODEL_NOT_FOUND,
            LLMErrorType.ACCESS_DENIED,
            LLMErrorType.THROTTLING,
            LLMErrorType.QUOTA_EXCEEDED,
            LLMErrorType.SERVICE_ERROR,
        )
