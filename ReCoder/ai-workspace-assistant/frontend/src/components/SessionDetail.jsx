import { useState, useEffect } from 'react';
import apiClient from '../api/client';

export default function SessionDetail({ sessionId }) {
  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    const fetchSession = async () => {
      try {
        setLoading(true);
        const res = await apiClient.get(`/sessions/${sessionId}`);
        setSession(res.data);
        setError(false);
      } catch (err) {
        setError(true);
      } finally {
        setLoading(false);
      }
    };

    if (sessionId) {
      fetchSession();
    }
  }, [sessionId]);

  if (loading) {
    return (
      <div className="animate-pulse space-y-4 p-4">
        <div className="h-4 bg-zinc-800 rounded w-1/4"></div>
        <div className="h-8 bg-zinc-800 rounded w-1/2"></div>
        <div className="h-24 bg-zinc-800 rounded w-full"></div>
      </div>
    );
  }

  if (error || !session) {
    return <div className="p-4 text-rose-400 text-sm">세션을 찾을 수 없습니다.</div>;
  }

  return (
    <div className="bg-zinc-900/70 border border-zinc-800 rounded-xl p-6">
      <div className="flex items-center justify-between mb-4">
        <div className="text-xs text-zinc-500 font-mono">ID: {session.session_id}</div>
        <div className="text-xs text-zinc-400">
          생성: {new Date(session.created_at || session.start_time).toLocaleString('ko-KR')}
        </div>
      </div>

      <h2 className="text-xl font-bold text-zinc-100 mb-4">
        {session.current_task || '작업 없음'}
      </h2>

      <div className="grid grid-cols-2 gap-4 mb-6">
        <div className="bg-zinc-950 p-3 rounded-lg border border-zinc-800">
          <div className="text-xs text-zinc-500 mb-1">시작 시간</div>
          <div className="text-sm text-zinc-300">
            {session.start_time ? new Date(session.start_time).toLocaleString('ko-KR') : '-'}
          </div>
        </div>
        <div className="bg-zinc-950 p-3 rounded-lg border border-zinc-800">
          <div className="text-xs text-zinc-500 mb-1">종료 시간</div>
          <div className="text-sm text-zinc-300">
            {session.end_time ? new Date(session.end_time).toLocaleString('ko-KR') : '진행 중'}
          </div>
        </div>
      </div>

      <div className="flex flex-wrap gap-2 mb-6">
        <span className="bg-zinc-800 text-zinc-300 px-3 py-1 rounded-md text-sm">
          중요도: {session.importance_score || 0}
        </span>
        {session.error_count > 0 && (
          <span className="bg-rose-500/20 text-rose-400 px-3 py-1 rounded-md text-sm">
            에러 발생
          </span>
        )}
        {session.error_count > 0 && session.resolved && (
          <span className="bg-emerald-500/20 text-emerald-400 px-3 py-1 rounded-md text-sm">
            해결됨
          </span>
        )}
      </div>

      <div>
        <div className="text-sm font-medium text-zinc-400 mb-2">AI 요약</div>
        <div className="bg-zinc-950 border border-zinc-800 rounded-lg p-4 text-sm text-zinc-300 whitespace-pre-wrap leading-relaxed">
          {session.ai_summary || '요약이 없습니다.'}
        </div>
      </div>
    </div>
  );
}
