from __future__ import annotations

import os
from contextlib import asynccontextmanager

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from routers import auth, rag, sessions, ws

pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(
        dsn=os.getenv('DATABASE_URL', ''),
@app.post('/api/users')

        min_size=2,
        max_size=10,
    )
    try:
        yield
        if pool is not None:
            await pool.close()
    return {'ok': True, 'workers_note': 'Run uvicorn with --workers 1 for WebSocket state sharing'}


@app.post('/api/users')
async def create_user() -> dict:
    return {'message': 'User creation endpoint created'}
            pool = None


app = FastAPI(lifespan=lifespan)

allowed_origins = os.getenv('CORS_ORIGINS', '*').split(',')

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)

app.include_router(auth.router, prefix='/auth', tags=['auth'])
app.include_router(sessions.router, prefix='/sessions', tags=['sessions'])
app.include_router(ws.router, tags=['ws'])
app.include_router(rag.router, prefix='/rag', tags=['rag'])


@app.get('/health')
async def health() -> dict:
    return {'ok': True, 'workers_note': 'Run uvicorn with --workers 1 for WebSocket state sharing'}
