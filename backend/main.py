# main.py
# FastAPI 앱 진입점 (로컬 개발용 SQLite 버전)
#
# 실행: uvicorn main:app --reload --port 8000
# API 문서: http://localhost:8000/docs
#
# ⚠ EC2 배포 시: uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
# ⚠ --workers 1 필수 (WebSocket connected_agents 딕셔너리 공유 문제)

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import init_db
from routers import auth, sessions, ws, rag


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 DB 초기화
    init_db()
    yield
    # 서버 종료 시 정리 작업 (필요 시 추가)


app = FastAPI(
    title='AI 업무 어시스턴트 API',
    description='로컬 개발용 SQLite 버전. EC2 배포 시 PostgreSQL로 교체.',
    version='1.0.0',
    lifespan=lifespan,
)

# CORS 설정 (로컬 개발용: 전체 허용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

# 라우터 등록
app.include_router(auth.router,     prefix='/auth',     tags=['인증'])
app.include_router(sessions.router, prefix='/sessions', tags=['세션'])
app.include_router(ws.router,                           tags=['WebSocket/채팅'])
app.include_router(rag.router,      prefix='/rag',      tags=['RAG'])


@app.get('/')
def root():
    return {'status': 'ok', 'message': 'AI 업무 어시스턴트 API 서버 실행 중'}
