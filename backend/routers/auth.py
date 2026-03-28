# routers/auth.py
# 회원가입 / 로그인 / JWT 인증
# ⚠ 로컬 개발용 SQLite 버전

import os
import uuid
from datetime import datetime, timedelta

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from database import get_conn

router = APIRouter()
security = HTTPBearer()

SECRET = os.getenv('JWT_SECRET', 'dev-secret-key-change-in-production')


# ── 요청 모델 ─────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


# ── 회원가입 ──────────────────────────────────────────────────
@router.post('/register')
def register(req: RegisterRequest):
    user_id = str(uuid.uuid4())
    hashed = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()

    with get_conn() as conn:
        try:
            conn.execute(
                'INSERT INTO users (user_id, email, password, name) VALUES (?, ?, ?, ?)',
                (user_id, req.email, hashed, req.name)
            )
        except Exception:
            raise HTTPException(400, '이미 존재하는 이메일입니다.')

    return {'user_id': user_id, 'message': '회원가입 완료'}


# ── 로그인 ────────────────────────────────────────────────────
@router.post('/login')
def login(req: LoginRequest):
    with get_conn() as conn:
        row = conn.execute(
            'SELECT * FROM users WHERE email = ?', (req.email,)
        ).fetchone()

    if not row or not bcrypt.checkpw(req.password.encode(), row['password'].encode()):
        raise HTTPException(401, '이메일 또는 비밀번호가 틀렸습니다.')

    token = jwt.encode(
        {'user_id': row['user_id'], 'exp': datetime.utcnow() + timedelta(days=30)},
        SECRET,
        algorithm='HS256'
    )
    return {'token': token, 'user_id': row['user_id']}


# ── JWT 검증 (의존성) ─────────────────────────────────────────
def get_current_user(token=Depends(security)):
    try:
        return jwt.decode(token.credentials, SECRET, algorithms=['HS256'])
    except JWTError:
        raise HTTPException(401, '유효하지 않은 토큰입니다.')
