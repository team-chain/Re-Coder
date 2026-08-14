"""
Control Plane — Database Session 관리
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = os.environ.get(
    "CONTROL_PLANE_DATABASE_URL",
    "postgresql+asyncpg://recoder:recoder@localhost:5432/recoder_cp",
)

# SQLite(테스트)는 StaticPool 이라 pool_size/max_overflow 를 받지 않는다 —
# 넘기면 create_engine 이 TypeError 로 죽는다. 풀 튜닝은 서버 DB 에만 적용한다.
_engine_kwargs: dict = {"echo": False}
if not DATABASE_URL.startswith("sqlite"):
    _engine_kwargs.update(pool_size=10, max_overflow=20)

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
