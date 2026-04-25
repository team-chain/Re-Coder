import { useState, useEffect } from 'react';
import Header from '../components/Header';
import Timeline from '../components/Timeline';
import ErrorHistory from '../components/ErrorHistory';
import apiClient from '../api/client';

export default function Dashboard() {
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchSessions = async () => {
      try {
        const res = await apiClient.get('/sessions');
        setSessions(res.data || []);
      } catch (err) {
        // 에러 처리
      } finally {
        setLoading(false);
      }
    };
    fetchSessions();
  }, []);

  const latestSession = sessions.length > 0 ? sessions[0] : null;
  const errorCount = sessions.filter(s => s.error_count > 0).length;

  return (
    <div className="min-h-screen flex flex-col">
      <Header />
      <main className="flex-1 max-w-6xl w-full mx-auto px-4 py-6">
        <h2 className="text-2xl font-bold text-white mb-6">대시보드</h2>
        
        {loading ? (
          <div className="text-zinc-400">데이터를 불러오는 중...</div>
        ) : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
              <div className="bg-zinc-900/70 border border-zinc-800 rounded-xl p-5 md:col-span-2">
                <div className="text-sm text-zinc-400 mb-1">최근 작업</div>
                <div className="text-lg font-medium text-zinc-100 truncate">
                  {latestSession?.current_task || '진행 중인 작업 없음'}
                </div>
              </div>
              <div className="bg-zinc-900/70 border border-zinc-800 rounded-xl p-5">
                <div className="text-sm text-zinc-400 mb-1">최근 중요도</div>
                <div className="text-2xl font-bold text-indigo-400">
                  {latestSession?.importance_score || 0}
                </div>
              </div>
              <div className="bg-zinc-900/70 border border-zinc-800 rounded-xl p-5">
                <div className="text-sm text-zinc-400 mb-1">에러 / 총 세션</div>
                <div className="text-2xl font-bold text-zinc-100">
                  <span className="text-rose-400">{errorCount}</span>
                  <span className="text-zinc-600 mx-2">/</span>
                  <span>{sessions.length}</span>
                </div>
              </div>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
              <div>
                <h3 className="text-lg font-semibold text-zinc-200 mb-4">타임라인</h3>
                <div className="bg-zinc-900/40 rounded-xl p-4 border border-zinc-800/50 h-[500px] overflow-y-auto">
                  <Timeline sessions={sessions} />
                </div>
              </div>
              <div>
                <h3 className="text-lg font-semibold text-zinc-200 mb-4">에러 기록</h3>
                <div className="bg-zinc-900/40 rounded-xl p-4 border border-zinc-800/50 h-[500px] overflow-y-auto">
                  <ErrorHistory sessions={sessions} />
                </div>
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
