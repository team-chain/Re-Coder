"""프롬프트 생성 전용 모듈."""

from __future__ import annotations

import json
import os


def generate_fix_prompt(
    error_description: str,
    solution: str,
    current_task: str,
    recent_commands: list[str],
    session_index: dict,
    source_files: list[dict] | None = None,
) -> str:
    """에러 정보를 기반으로 AI 코딩 도구에 붙여넣을 해결 프롬프트를 생성합니다."""

    # 최근 이벤트에서 추가 컨텍스트 수집
    recent_events = session_index.get('events', [])[-5:]
    active_windows = session_index.get('active_windows', [])[-1:]
    window_names = []
    if active_windows:
        for w in active_windows[-1].get('windows', []):
            name = w.get('name', '')
            if name:
                window_names.append(name)

    request_prompt = (
        '다음 에러를 해결하기 위한 프롬프트를 작성해주세요. '
        '사용자가 AI 코딩 도구(Cursor, Copilot 등)에 바로 붙여넣을 수 있는 형식으로 작성하세요.\n\n'
        f'에러: {error_description}\n'
        f'컨텍스트: {current_task}\n'
        f'최근 명령어: {json.dumps(recent_commands, ensure_ascii=False)}\n'
        f'활성 창: {", ".join(window_names)}\n'
        f'제안된 해결법: {solution}'
    )

    if source_files:
        request_prompt += '\n\n## 관련 소스 코드\n'
        for sf in source_files:
            path = sf.get('path', '')
            content = sf.get('content', '')
            ext = path.split('.')[-1] if '.' in path else ''
            request_prompt += f'\n### 파일: {path}\n```{ext}\n{content}\n```\n'

    response_schema = {
        'type': 'object',
        'properties': {
            'fix_prompt': {
                'type': 'string',
                'description': '사용자가 복사할 프롬프트 전체 텍스트',
            },
            'explanation': {
                'type': 'string',
                'description': '왜 이 방법이 효과적인지 한 줄 설명',
            },
        },
        'required': ['fix_prompt', 'explanation'],
    }

    try:
        from analyzer import _get_gemini_client
        from google.genai import types

        client = _get_gemini_client()
        model_name = os.getenv('GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')
        response = client.models.generate_content(
            model=model_name,
            contents=[request_prompt],
            config=types.GenerateContentConfig(
                system_instruction=(
                    '당신은 개발 에러를 해결하는 전문가입니다. '
                    '제공된 소스 코드를 분석하여, 에러를 해결하기 위한 구체적인 코드 수정 사항을 diff 형태 또는 수정된 코드 블록으로 제시해 주세요. '
                    '사용자가 AI 코딩 도구에 붙여넣으면 바로 문제를 해결할 수 있는 '
                    '명확하고 구체적인 프롬프트를 JSON으로 작성합니다.'
                ),
                response_mime_type='application/json',
                response_json_schema=response_schema,
            ),
        )
        data = json.loads(response.text)
        return data.get('fix_prompt', _fallback_prompt(error_description, current_task, solution))
    except Exception as e:
        print(f'[prompt_generator] Gemini 호출 실패, 기본 템플릿을 사용합니다: {e}')
        return _fallback_prompt(error_description, current_task, solution)


def _fallback_prompt(error_description: str, current_task: str, solution: str) -> str:
    """Gemini 호출 실패 시 기본 템플릿을 반환합니다."""
    return (
        f'다음 에러를 수정해주세요: {error_description}\n'
        f'현재 작업: {current_task}\n'
        f'제안된 해결법: {solution}'
    )
