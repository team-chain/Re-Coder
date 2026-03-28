# analyzer.py
# Gemini Vision AI 분석 + Windows 알림 + gTTS 음성 브리핑
#
# ▶ 설계 근거: EasyOCR이 1차 필터링을 담당하고, 변화 감지 시에만 Gemini Vision을 호출합니다.
# 항상 호출하면 월 $50 이상 비용이 발생하지만, 변화 감지 시에만 호출하면 약 1/10로 줄어듭니다.

import io
import json
import os
from datetime import datetime

import google.generativeai as genai
import pygame
from dotenv import load_dotenv
from gtts import gTTS
from plyer import notification
import re


load_dotenv()

genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-2.5-flash'
                              '')

# pygame 믹서 초기화 (음성 출력용)
try:
    pygame.mixer.init()
    _PYGAME_READY = True
except Exception:
    _PYGAME_READY = False


def analyze_context(
    screenshot,
    os_snapshot: dict,
    session_index: dict,
    user_question: str | None = None,
    past_sessions: list | None = None,
) -> dict:
    """Gemini Vision으로 현재 화면 + OS 스냅샷을 분석하고 결과를 반환합니다.

    Returns:
        dict: {current_task, summary, has_error, error_description,
               solution, next_action, importance_score, voice_briefing, answer}
        오류 시 빈 dict 반환.
    """
    # 과거 세션 컨텍스트 구성
    past_ctx = ''
    if past_sessions:
        past_ctx = '[과거 유사 세션]\n'
        for s in past_sessions:
            past_ctx += f'- {s["summary"]}\n'

    prompt = f'''
화면과 데이터를 분석해줘.
UI 메뉴/아이콘/툴바는 무시하고 실제 업무 내용만 파악해줘.

[OS 스냅샷]
포그라운드: {os_snapshot["foreground_processes"]}
새 터미널 명령어: {os_snapshot["terminal"]["new_commands"]}
클립보드: {os_snapshot["clipboard"]["content"]}

[최근 이벤트]
{json.dumps(session_index["events"][-5:], ensure_ascii=False)}

{past_ctx}
{f"[사용자 질문] {user_question}" if user_question else ""}

JSON으로만 응답 (마크다운 없이):
{{"current_task":"현재 작업","summary":"업무 요약",
"has_error":true/false,"error_description":"에러 내용",
"solution":"해결 방법","next_action":"다음 할 일",
"importance_score":0~100,"voice_briefing":"음성 브리핑 (한국어, 2문장 이내)",
"answer":"질문 답변 (없으면 빈 문자열)"}}
'''

    try:
        response = model.generate_content([screenshot, prompt])
        text = response.text.strip()

        # 마크다운 코드블록 제거
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            text = match.group(0)

        result = json.loads(text.strip())

        # 세션 인덱스 업데이트
        session_index['ai_summary'] = result.get('summary', '')
        session_index['importance_score'] = result.get('importance_score', 0)
        session_index['current_task'] = result.get('current_task', '')
        session_index['ai_updated_at'] = datetime.now().isoformat()

        # 음성 브리핑
        if result.get('voice_briefing'):
            speak(result['voice_briefing'])

        # 에러 감지 시 Windows 알림
        if result.get('has_error') and result.get('solution'):
            send_alert(
                result.get('error_description', '에러 감지'),
                result.get('solution', '')
            )

        return result

    except json.JSONDecodeError as e:
        print(f'AI 응답 JSON 파싱 실패: {e}')
        return {}
    except Exception as e:
        print(f'AI 분석 실패: {e}')
        return {}


def speak(text: str):
    """gTTS + pygame으로 한국어 음성 브리핑 출력."""
    if not _PYGAME_READY:
        print(f'[음성] {text}')
        return
    try:
        tts = gTTS(text=text, lang='ko')
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        pygame.mixer.music.load(fp)
        pygame.mixer.music.play()
    except Exception as e:
        print(f'음성 출력 실패: {e}')


def send_alert(error_desc: str, solution: str):
    """Windows 트레이 알림 전송 (plyer 사용)."""
    try:
        notification.notify(
            title='⚠ 에러 감지',
            message=f'{error_desc[:100]}\n→ {solution[:100]}',
            app_name='AI 업무 어시스턴트',
            timeout=10,
        )
    except Exception as e:
        print(f'알림 전송 실패: {e}')
