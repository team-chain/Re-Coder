"""AI-DLC 설계 결정 품질 — 보드 이슈 「설계 결정이 제대로 나오지 않음」 수정 검증.

사용자가 겪은 증상은 셋이었다.
  (a) "설계 결정 생성 실패" — 응답이 잘려 JSON 이 깨졌는데 재시도가 없었다.
  (b) 확인 카드 한 장만 뜸 — 결정이 필터에 걸러졌는데 그 사실이 안 보였다.
  (c) 뻔한/빈약한 결정 — 기본 모델이 Haiku 3 로 고정돼 있었다(.env.example).

여기서는 (a) 재시도, (b) dropped 노출, 그리고 토큰 상한 상향을 검사한다.
(c) 는 설정 파일 수정이라 코드 테스트 대상이 아니다.
"""
from __future__ import annotations

import json

import pytest

import code_agent


class _FakeLLMResponse:
    def __init__(self, text: str, model_used: str = "fake-model"):
        self.text = text
        self.model_used = model_used


class _FakeRouter:
    """호출될 때마다 미리 정해둔 raw 텍스트를 순서대로 반환하는 가짜 라우터."""

    def __init__(self, texts: list[str]):
        self._texts = list(texts)
        self.calls: list = []

    def call(self, request, agent=None, operation=None):
        self.calls.append(request)
        if not self._texts:
            raise AssertionError("예상보다 많은 LLM 호출")
        return _FakeLLMResponse(self._texts.pop(0))


_GOOD = json.dumps({"decisions": [{
    "id": "storage",
    "question": "데이터를 어디에 저장할까요?",
    "impact": "저장 위치가 배포 대상을 결정합니다.",
    "options": [
        {"key": "local", "label": "로컬", "summary": "서버 없음",
         "pros": ["간단"], "cons": [], "recommended": True},
        {"key": "db", "label": "DB", "summary": "확장성",
         "pros": [], "cons": ["무거움"], "recommended": False},
    ],
}]})

#: 상한에 잘린 응답의 전형 — 배열이 열린 채 끝난다.
_TRUNCATED = '{"decisions": [{"id": "storage", "question": "어디에 저장", "options": [{"key": "a"'


def test_깨진_JSON_은_한_번_재시도해서_살린다(monkeypatch) -> None:
    fake = _FakeRouter([_TRUNCATED, _GOOD])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)

    result = code_agent.generate_plan("할일 앱 만들어줘")

    assert len(fake.calls) == 2, "재시도가 없다 — 잘린 응답 한 번에 그대로 실패한다"
    assert result["decisions"][0]["id"] == "storage"
    #: 재시도 프롬프트에는 교정 지시가 붙어야 한다. 같은 프롬프트를 그대로
    #: 다시 보내면 같은 실패를 반복할 확률이 높다.
    assert "재시도" in fake.calls[1].prompt
    assert fake.calls[0].prompt != fake.calls[1].prompt


def test_두_번_다_깨지면_원인을_담아_실패한다(monkeypatch) -> None:
    fake = _FakeRouter([_TRUNCATED, _TRUNCATED])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)

    with pytest.raises(RuntimeError) as exc:
        code_agent.generate_plan("할일 앱 만들어줘")
    assert "2회" in str(exc.value)


def test_빈_응답도_재시도_대상이다(monkeypatch) -> None:
    fake = _FakeRouter(["", _GOOD])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)

    result = code_agent.generate_plan("할일 앱 만들어줘")
    assert len(fake.calls) == 2
    assert result["decisions"], "빈 응답 후 재시도 결과가 버려졌다"


def test_걸러진_결정은_dropped_로_노출된다(monkeypatch) -> None:
    """선택지 1개짜리 결정은 버려진다 — 그 사실이 응답에 실려야 한다.

    안 실리면 사용자에게는 'AI 가 설계를 안 해준다'로 보인다(보드 이슈 증상 b).
    """
    one_option = json.dumps({"decisions": [{
        "id": "auth",
        "question": "인증 방식은?",
        "options": [{"key": "jwt", "label": "JWT", "summary": "", "pros": [], "cons": []}],
    }]})
    fake = _FakeRouter([one_option])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)

    result = code_agent.generate_plan("로그인 붙여줘")

    #: 결정은 확인 카드로 대체되고(기존 동작), 이유는 dropped 에 남는다(신규).
    assert result["decisions"][0]["id"].startswith("__")
    assert result["dropped"], "걸러진 이유가 응답에 없다 — 화면에 아무것도 못 보여준다"
    assert any("선택지" in reason for reason in result["dropped"])


def test_정상_경로에서는_dropped_가_빈_목록이다(monkeypatch) -> None:
    """[음성 대조] 아무것도 안 걸러졌는데 경고가 뜨면 그것대로 소음이다."""
    fake = _FakeRouter([_GOOD])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)

    result = code_agent.generate_plan("할일 앱 만들어줘")
    assert result["dropped"] == []


def test_토큰_상한이_한국어_결정_3개를_담을_크기다(monkeypatch) -> None:
    """2048 은 한국어 결정 3개 × 선택지 3~4개에 잘린다 — 상향을 고정한다."""
    fake = _FakeRouter([_GOOD])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)

    code_agent.generate_plan("할일 앱 만들어줘")
    assert fake.calls[0].max_tokens >= 4096, "상한이 다시 내려가면 잘림 실패가 재발한다"
