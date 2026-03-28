# routers/rag.py
# RAG 검색 엔드포인트
# ⚠ 로컬 개발용: pgvector 없음 → SQLite LIKE 텍스트 검색으로 대체
# EC2 배포 시 services/rag.py의 pgvector 버전으로 교체

from fastapi import APIRouter, Depends

from database import get_conn
from routers.auth import get_current_user

router = APIRouter()


@router.get('')
def rag_search(q: str, user=Depends(get_current_user)):
    """과거 세션에서 질문과 유사한 내용을 텍스트 검색으로 반환.
    
    로컬 개발용: LIKE 검색 (pgvector 벡터 유사도 검색 아님)
    EC2 배포 시: services/rag.py의 pgvector 버전으로 교체
    """
    user_id = user['user_id']

    # 질문에서 키워드 추출 (공백 기준 분리, 2글자 이상만)
    keywords = [w for w in q.split() if len(w) >= 2]

    with get_conn() as conn:
        results = []
        for keyword in keywords[:3]:   # 최대 3개 키워드로 검색
            rows = conn.execute(
                '''SELECT session_id, ai_summary, current_task
                   FROM sessions
                   WHERE user_id = ?
                     AND (ai_summary LIKE ? OR current_task LIKE ?)
                   ORDER BY created_at DESC
                   LIMIT 3''',
                (user_id, f'%{keyword}%', f'%{keyword}%')
            ).fetchall()
            results.extend(rows)

    # 중복 제거
    seen, unique = set(), []
    for r in results:
        if r['session_id'] not in seen:
            seen.add(r['session_id'])
            unique.append({
                'session_id': r['session_id'],
                'summary': r['ai_summary'] or '',
                'similarity': 0.8,   # 텍스트 검색이므로 고정값
            })

    return unique[:3]
