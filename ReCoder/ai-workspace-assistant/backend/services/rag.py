from __future__ import annotations

import asyncio
import os

from google import genai
from google.genai import types


def _embed_text_sync(summary_text: str) -> list[float]:
    api_key = os.getenv('GEMINI_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY is not configured')

    client = genai.Client(api_key=api_key)
    result = client.models.embed_content(
        model=os.getenv('EMBEDDING_MODEL', 'gemini-embedding-001'),
        contents=summary_text,
        config=types.EmbedContentConfig(output_dimensionality=768),
    )
    return result.embeddings[0].values


async def _embed_text(summary_text: str) -> list[float]:
    return await asyncio.to_thread(_embed_text_sync, summary_text)


async def store_embedding(session_id: str, summary: str, user_id: str, pool) -> None:
    if not summary:
        return

    vector = await _embed_text(summary)
    vector_text = str(vector)

    result = await pool.execute(
        '''
        UPDATE sessions
        SET ai_summary = $1,
            embedding = $2::vector
        WHERE session_id = $3 AND user_id = $4
        ''',
        summary,
        vector_text,
        session_id,
        user_id,
    )
    if result == 'UPDATE 0':
        print(
            f'[store_embedding] warning: session not found for embedding update '
            f'(session_id={session_id}, user_id={user_id})'
        )


async def search(question: str, user_id: str, pool, top_k: int = 3) -> list[dict]:
    if not question:
        return []

    vector = await _embed_text(question)
    vector_text = str(vector)
    rows = await pool.fetch(
        '''
        SELECT session_id, ai_summary,
               1-(embedding <=> $1::vector) AS similarity
        FROM sessions
        WHERE user_id=$2 AND embedding IS NOT NULL
        ORDER BY embedding <=> $1::vector
        LIMIT $3
        ''',
        vector_text,
        user_id,
        top_k,
    )

    return [
        {
            'session_id': row['session_id'],
            'summary': row['ai_summary'],
            'similarity': float(row['similarity']) if row['similarity'] is not None else 0.0,
        }
        for row in rows
    ]
