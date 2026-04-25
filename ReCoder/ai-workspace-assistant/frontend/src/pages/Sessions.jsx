import { useState, useEffect } from 'react';
import Header from '../components/Header';
import SessionDetail from '../components/SessionDetail';
import apiClient from '../api/client';

export default function Sessions() {
  const [sessions, setSessions] = useState([]);
  const [date, setDate] = useState('');
  const [loading, setLoading] = useState(true);
  const [selectedSessionId, setSelectedSessionId] = useState(null);

  useEffect(() => {
    const fetchSessions = async () => {
      try {
        setLoading(true);
        const url = date ? `/sessions?date=${date}` : '/sessions';
        const res = await apiClient.get(url);
        setSessions(res.data || []);
      } catch (err) {
        // 에러 처리
      } finally {
        setLoading(false);
      }
    };
    fetchSessions();
  }, [date]);

  return (
    <div className="min-h-screen flex flex-col">
      <Header />
      <main className="flex-1 max-w-6xl w-full mx-auto px-4 py-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-6 gap-4">
          <h2 className="text-2xl font-bold text-white">세션 목록</h2>
          <input
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className="bg-zinc-950 border border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-100 focus:ring-2 focus:ring-indigo-500 focus:outline-none"
          />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-1 space-y-4 max-h-[calc(100vh-200px)] overflow-y-auto pr-2">
            {loading ? (
              <div className="text-zinc-400">불러오는 중...</div>
            ) : sessions.length === 0 ? (
              <div className="text-zinc-500 bg-zinc-900/50 p-6 rounded-xl border border-zinc-800 text-center">
                세션이 없습니다.
              </div>
            ) : (
              sessions.map((session) => (
                <button
                  key={session.session_id}
                  onClick={() => setSelectedSessionId(session.session_id)}
                  className={`w-full text-left block bg-zinc-900/70 border rounded-xl p-4 transition-colors ${
                    selectedSessionId === session.session_id 
                      ? 'border-indigo-500 ring-1 ring-indigo-500' 
                      : 'border-zinc-800 hover:border-zinc-600'
                  }`}
                >
                  <div className="flex justify-between items-start mb-2">
                    <div className="text-xs text-zinc-500 font-mono">
                      {session.session_id.substring(0, 8)}
                    </div>
                    <div className="text-xs text-zinc-400">
                      {new Date(session.start_time).toLocaleString('ko-KR')}
                    </div>
                  </div>
                  <div className="text-sm font-medium text-zinc-200 mb-2 truncate">
                    {session.current_task || '작업 없음'}
                  </div>
                  <div className="text-xs text-zinc-400 mb-3 line-clamp-2">
                    {session.ai_summary || '요약 없음'}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <span className="text-xs bg-zinc-800 text-zinc-300 px-2 py-1 rounded-md">
                      중요도: {session.importance_score || 0}
                    </span>
                    {session.error_count > 0 && (
                      <span className="text-xs bg-rose-500/20 text-rose-400 px-2 py-1 rounded-full">
                        에러
                      </span>
                    )}
                    {session.error_count > 0 && session.resolved && (
                      <span className="text-xs bg-emerald-500/20 text-emerald-400 px-2 py-1 rounded-full">
                        해결됨
                      </span>
                    )}
                  </div>
                </button>
              ))
            )}
          </div>
          
          <div className="lg:col-span-2">
            {selectedSessionId ? (
              <SessionDetail sessionId={selectedSessionId} />
            ) : (
              <div className="h-full min-h-[300px] flex items-center justify-center bg-zinc-900/30 border border-zinc-800/50 rounded-xl text-zinc-500">
                왼쪽에서 세션을 선택해주세요.
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
