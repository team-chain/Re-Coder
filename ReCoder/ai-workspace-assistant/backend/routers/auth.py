from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

ALGORITHM = 'HS256'
ACCESS_TOKEN_EXPIRE_DAYS = 30
JWT_SECRET = os.environ.get('JWT_SECRET', 'change-me')

router = APIRouter()
security = HTTPBearer(auto_error=False)


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


def _get_jwt_secret() -> str:
    return JWT_SECRET


@router.post('/register')
async def register(payload: RegisterRequest, request: Request) -> dict:
    pool = request.app.state.pool

    exists = await pool.fetchrow('SELECT user_id FROM users WHERE email=$1', payload.email)
    if exists:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Email already exists')

    user_id = str(uuid4())
    hashed = bcrypt.hashpw(payload.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    await pool.execute(
        'INSERT INTO users (user_id, email, password, name) VALUES ($1, $2, $3, $4)',
        user_id,
        payload.email,
        hashed,
        payload.name,
    )
    return {'user_id': user_id, 'email': payload.email, 'name': payload.name}


@router.post('/login')
async def login(payload: LoginRequest, request: Request) -> dict:
    pool = request.app.state.pool
    user = await pool.fetchrow('SELECT user_id, email, password, name FROM users WHERE email=$1', payload.email)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid credentials')

    if not bcrypt.checkpw(payload.password.encode('utf-8'), user['password'].encode('utf-8')):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid credentials')

    expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    token = jwt.encode({'sub': user['user_id'], 'exp': expire}, _get_jwt_secret(), algorithm=ALGORITHM)

    return {
        'access_token': token,
        'token_type': 'bearer',
        'expires_in_days': ACCESS_TOKEN_EXPIRE_DAYS,
        'user': {'user_id': user['user_id'], 'email': user['email'], 'name': user['name']},
    }


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')

    token = credentials.credentials
    try:
        payload = jwt.decode(token, _get_jwt_secret(), algorithms=[ALGORITHM])
        user_id = payload.get('sub')
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized') from exc

    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')

    user = await request.app.state.pool.fetchrow(
        'SELECT user_id, email, name, created_at FROM users WHERE user_id=$1', user_id
    )
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')

    return dict(user)
