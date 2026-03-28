# monitor.py
# 비동기 루프 메인 코디네이터
#
# ▶ 핵심 설계: 캡처와 분석을 완전히 분리
# - 캡처는 5초마다 절대 멈추지 않고 계속 찍음
# - latest_capture 변수에 항상 최신 캡처만 덮어씀
# - 분석은 끝나는 대로 바로 최신 캡처를 처리
#
# 루프 1 - capture_loop():  5초마다 화면 캡처 → latest_capture 덮어쓰기
# 루프 2 - ocr_loop():      1초마다 latest_capture 확인 → OCR → 변화 감지 → 분석 요청
# 루프 3 - analysis_loop(): 최신 캡처로 Gemini Vision 호출 → 알림/음성
# 루프 4 - upload_loop():   1분마다 AI 요약 텍스트 서버 전송

import asyncio
import gc
import json
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import easyocr
import numpy as np
from PIL import Image
from dotenv import load_dotenv

import analyzer
import uploader
import ws_client
from collectors.collect import collect_os_snapshot, init_cmd_count, window_changed

load_dotenv()

user_token: str = os.getenv('USER_TOKEN', '')
user_id: str = os.getenv('USER_ID', '')

# EasyOCR: 프로그램 시작 시 한 번만 로드
print('🔄 EasyOCR 모델 로딩 중... (최초 1회, 30~60초 소요)')
reader = easyocr.Reader(['ko', 'en'], verbose=False)
print('✅ EasyOCR 로딩 완료')

executor = ThreadPoolExecutor(max_workers=2)

CAPTURE_INTERVAL: int = 5
SESSIONS_DIR = Path('output/sessions')

ERROR_KEYWORDS = [
    'error', 'exception', 'traceback', 'fatal', 'failed', 'undefined',
    'null', '404', '500', 'warning', 'syntax', 'nameerror', 'typeerror',
    'valueerror', 'importerror', 'indexerror', 'keyerror', 'attributeerror',
    '에러', '오류', '실패',
]

RESOLVE_KEYWORDS = [
    'success', 'done', 'completed', 'ok', '200', 'passed',
    'running', 'started', '성공', '완료',
]

# ── 최신 캡처 저장 변수 (큐 대신 변수로 관리) ─────────────────────────────
latest_capture: tuple | None = None        # (screenshot, os_snapshot)
is_analyzing: bool = False                 # 분석 중 여부 (중복 호출 방지)

# ── 세션 상태 ──────────────────────────────────────────────────────────────
frame_number: int = 0
session_index: dict = {
    'session_id':       str(uuid.uuid4())[:8],
    'start_time':       datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    'end_time':         None,
    'active_windows':   [],
    'events':           [],
    'error_count':      0,
    'resolved':         False,
    'key_frames':       [],
    'ai_summary':       None,
    'importance_score': 0,
    'current_task':     None,
    'ai_updated_at':    None,
}


# ── 화면 캡처 ─────────────────────────────────────────────────────────────
def capture_all_monitors() -> Image.Image:
    import mss
    with mss.mss() as sct:
        raw = sct.grab(sct.monitors[0])
        return Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX')


# ── OCR ───────────────────────────────────────────────────────────────────
def run_ocr(screenshot: Image.Image) -> tuple[list[str], list[str]]:
    w, h = screenshot.size
    small = screenshot.resize((w // 2, h // 2), Image.LANCZOS)
    result = reader.readtext(np.array(small))
    texts = [r[1].lower() for r in result if r[2] > 0.7]
    joined = ' '.join(texts)
    errors = [k for k in ERROR_KEYWORDS if k in joined]
    return texts, errors


# ── 세션 인덱스 저장 (로컬만) ─────────────────────────────────────────────
def save_to_index(screenshot, os_snapshot, texts, errors):
    global frame_number
    now = datetime.now().strftime('%H:%M:%S')
    frame_name = f'frame_{frame_number:04d}.png'
    frame_number += 1

    session_dir = SESSIONS_DIR / f'session_{session_index["session_id"]}'
    frames_dir = session_dir / 'frames'
    frames_dir.mkdir(parents=True, exist_ok=True)

    fg = os_snapshot.get('foreground_processes', [])
    if fg:
        last = session_index['active_windows']
        if not last or last[-1].get('title') != fg[0].get('title'):
            session_index['active_windows'].append({
                'title': fg[0].get('title', ''),
                'changed_at': now,
            })

    if errors:
        screenshot.save(frames_dir / frame_name)
        session_index['events'].append({
            'time': now, 'type': 'error_detected',
            'keywords': errors, 'count': len(errors), 'frame': frame_name,
        })
        session_index['error_count'] += 1
        session_index['key_frames'].append(frame_name)

    new_cmds = os_snapshot.get('terminal', {}).get('new_commands', [])
    if new_cmds:
        session_index['events'].append({
            'time': now, 'type': 'terminal_commands', 'commands': new_cmds,
        })

    if session_index['error_count'] > 0:
        if any(k in ' '.join(texts) for k in RESOLVE_KEYWORDS):
            screenshot.save(frames_dir / frame_name)
            session_index['resolved'] = True
            session_index['key_frames'].append(frame_name)
            session_index['events'].append({
                'time': now, 'type': 'resolved', 'frame': frame_name,
            })

    (session_dir / 'index.json').write_text(
        json.dumps(session_index, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


# ── 오래된 세션 정리 ──────────────────────────────────────────────────────
def cleanup_old_sessions():
    cutoff = datetime.now() - timedelta(days=7)
    if not SESSIONS_DIR.exists():
        return
    for d in SESSIONS_DIR.iterdir():
        if d.is_dir() and datetime.fromtimestamp(d.stat().st_mtime) < cutoff:
            shutil.rmtree(d)
            print(f'오래된 세션 삭제: {d.name}')


# ── 루프 1: 캡처 (절대 멈추지 않음) ──────────────────────────────────────
async def capture_loop():
    """5초마다 캡처 → latest_capture에 덮어씀. 분석을 기다리지 않음."""
    global latest_capture
    while True:
        try:
            screenshot = capture_all_monitors()
            os_snapshot = collect_os_snapshot()
            latest_capture = (screenshot, os_snapshot)  # 항상 최신으로 덮어쓰기
        except Exception as e:
            print(f'캡처 루프 오류: {e}')
        await asyncio.sleep(CAPTURE_INTERVAL)


# ── 루프 2: OCR + 변화 감지 ───────────────────────────────────────────────
async def ocr_loop():
    """1초마다 latest_capture 확인 → OCR → 변화 감지 → 분석 트리거."""
    global latest_capture, is_analyzing
    last_processed = None  # 마지막으로 처리한 캡처 (중복 처리 방지)

    while True:
        try:
            if latest_capture and latest_capture is not last_processed and not is_analyzing:
                screenshot, os_snapshot = latest_capture
                last_processed = latest_capture

                loop = asyncio.get_event_loop()
                texts, errors = await loop.run_in_executor(executor, run_ocr, screenshot)
                save_to_index(screenshot, os_snapshot, texts, errors)

                has_change = (
                    errors
                    or os_snapshot.get('terminal', {}).get('new_commands')
                    or window_changed()
                )
                if has_change:
                    asyncio.create_task(
                        run_analysis(screenshot, os_snapshot, errors)
                    )
        except Exception as e:
            print(f'OCR 루프 오류: {e}')
        finally:
            gc.collect()
        await asyncio.sleep(1)


# ── 루프 3: Gemini 분석 ───────────────────────────────────────────────────
async def run_analysis(screenshot, os_snapshot, errors):
    """Gemini Vision 분석 — 분석 중에도 캡처는 계속 진행됨."""
    global is_analyzing
    if is_analyzing:
        return  # 이미 분석 중이면 스킵

    is_analyzing = True
    try:
        await asyncio.to_thread(
            analyzer.analyze_context,
            screenshot,
            os_snapshot,
            session_index,
        )
    except Exception as e:
        print(f'분석 루프 오류: {e}')
    finally:
        is_analyzing = False  # 분석 완료 → 다음 분석 가능


# ── 루프 4: 업로드 ───────────────────────────────────────────────────────
async def upload_loop():
    """1분마다 AI 요약 텍스트를 서버로 전송합니다."""
    while True:
        await asyncio.sleep(60)
        try:
            if session_index.get('ai_summary') and user_token:
                success = await asyncio.to_thread(
                    uploader.upload_session_summary,
                    session_index,
                    user_token,
                )
                if success:
                    print(f'✅ 세션 업로드 완료 (session_id={session_index["session_id"]})')
        except Exception as e:
            print(f'업로드 루프 오류: {e}')


# ── 진입점 ───────────────────────────────────────────────────────────────
async def run():
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_sessions()
    init_cmd_count()

    print(f'🚀 AI 업무 어시스턴트 시작 (session_id={session_index["session_id"]})')
    print(f'   캡처 주기: {CAPTURE_INTERVAL}초 | 업로드 주기: 60초')

    await asyncio.gather(
        capture_loop(),
        ocr_loop(),
        upload_loop(),
        ws_client.ws_listen(session_index),
    )