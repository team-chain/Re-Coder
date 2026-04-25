from __future__ import annotations

import os

import requests


def _clear_token() -> None:
    """USER_TOKEN/USER_ID를 .env와 프로세스 환경변수에서 제거합니다."""
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        with open(env_path, 'w', encoding='utf-8') as f:
            for line in lines:
                if not line.startswith('USER_TOKEN=') and not line.startswith('USER_ID='):
                    f.write(line)

    os.environ.pop('USER_TOKEN', None)
    os.environ.pop('USER_ID', None)


def upload_session_summary(session_index: dict, user_token: str) -> str:
    api_base_url = os.getenv('API_BASE_URL', '').strip()
    if not api_base_url:
        print('[upload_session_summary] API_BASE_URL이 없어 업로드를 건너뜁니다.')
        return 'error'

    payload = {
        'session_id': session_index['session_id'],
        'start_time': session_index['start_time'],
        'end_time': session_index['end_time'],
        'ai_summary': session_index.get('ai_summary', ''),
        'importance_score': session_index.get('importance_score', 0),
        'error_count': session_index.get('error_count', 0),
        'resolved': session_index.get('resolved', False),
        'current_task': session_index.get('current_task', ''),
        'shared': False,
    }

    try:
        response = requests.post(
            f'{api_base_url}/sessions',
            headers={'Authorization': f'Bearer {user_token}'},
            json=payload,
            timeout=10,
        )
    except requests.RequestException as e:
        print(f'[upload_session_summary] 서버 연결 실패: {e}')
        return 'error'

    if response.status_code == 401:
        print('[upload_session_summary] 인증 만료(401). USER_TOKEN을 제거하고 업로드를 중단합니다.')
        _clear_token()
        return 'auth_expired'

    return 'ok' if response.ok else 'error'
