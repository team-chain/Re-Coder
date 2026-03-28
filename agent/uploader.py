# uploader.py
# 서버 전송 모듈 — AI 요약 텍스트만 전송 (스크린샷은 절대 전송하지 않음)
#
# ▶ 설계 근거: 스크린샷은 절대 서버로 전송하지 않습니다.
# AI가 생성한 텍스트 요약만 전송합니다.
# 401 수신 시 토큰 만료로 판단하고 .env에서 USER_TOKEN 삭제 후 프로그램을 강제 종료합니다.
# 재실행 시 main.py가 USER_TOKEN 없음을 감지하여 first_run.py 팝업을 자동으로 띄웁니다.

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.getenv('API_BASE_URL', 'http://your-ec2-ip')


def upload_session_summary(
    session_index: dict,
    user_token: str,
    share_to_team: bool = False,
) -> bool:
    """세션 요약을 서버(PostgreSQL)로 전송합니다.

    ▶ 전송 데이터: AI 요약 텍스트, 중요도 점수, 에러 카운트, 해결 여부, 현재 작업
    ▶ 미전송 데이터: 스크린샷, index.json 원시 이벤트 로그

    Returns:
        True: 전송 성공
        False: 전송 실패 (네트워크 오류 등)
    
    Side effects:
        401 수신 시 _clear_token_and_exit() 호출 → 프로세스 강제 종료
    """
    payload = {
        'session_id':      session_index['session_id'],
        'start_time':      session_index['start_time'],
        'end_time':        session_index.get('end_time'),
        'ai_summary':      session_index.get('ai_summary', ''),
        'importance_score': session_index.get('importance_score', 0),
        'error_count':     session_index.get('error_count', 0),
        'resolved':        session_index.get('resolved', False),
        'current_task':    session_index.get('current_task', ''),
        'shared':          share_to_team,
    }

    headers = {'Authorization': f'Bearer {user_token}'}

    try:
        resp = requests.post(
            f'{API_BASE_URL}/sessions',
            json=payload,
            headers=headers,
            timeout=15,
        )

        if resp.status_code == 401:
            print('토큰 만료 감지 → 재로그인 필요')
            _clear_token_and_exit()

        if resp.status_code == 200:
            return True
        else:
            print(f'세션 업로드 실패: HTTP {resp.status_code} — {resp.text[:200]}')
            return False

    except requests.exceptions.ConnectionError:
        print('서버 연결 불가 — 다음 업로드 주기에 재시도합니다.')
        return False
    except Exception as e:
        print(f'업로드 오류: {e}')
        return False


def _clear_token_and_exit():
    """토큰 만료 시 .env에서 USER_TOKEN 삭제 후 강제 종료.
    
    재실행 시 main.py가 USER_TOKEN 없음을 감지 → first_run.py 팝업 자동 실행.
    """
    env_path = Path('.env')
    if env_path.exists():
        lines = [
            l for l in env_path.read_text(encoding='utf-8').splitlines()
            if not l.startswith('USER_TOKEN')
        ]
        env_path.write_text('\n'.join(lines), encoding='utf-8')
        print('.env에서 USER_TOKEN 삭제 완료')

    os._exit(1)  # 강제 종료 → 재실행 시 first_run.py 팝업
