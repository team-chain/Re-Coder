import { Link } from 'react-router-dom';

export default function Timeline({ sessions }) {
  if (!sessions || sessions.length === 0) {
    return <div className="text-zinc-500 text-sm py-4">세션 기록이 없습니다.</div>;
  }

  return (
    <div className="space-y-4">
      {sessions.map((session) => (
        <Link
          key={session.session_id}
          to={`/sessions/${session.session_id}`}
          className="block bg-zinc-900/70 border border-zinc-800 rounded-xl p-4 hover:border-zinc-600 transition-colors"
        >
          <div className="flex justify-between items-start mb-2">
            <div className="text-xs text-zinc-500 font-mono">
              {session.session_id.substring(0, 8)}
            </div>
            <div className="text-xs text-zinc-400">
              {new Date(session.start_time).toLocaleString('ko-KR')}
            </div>
          </div>
          <div className="text-sm font-medium text-zinc-200 mb-2">
            {session.current_task || '작업 없음'}
          </div>
          <div className="flex items-center space-x-2">
            <span className="text-xs bg-zinc-800 text-zinc-300 px-2 py-1 rounded-md">
              중요도: {session.importance_score || 0}
            </span>
            {session.error_count > 0 && (
              <span className="text-xs bg-rose-500/20 text-rose-400 px-2 py-1 rounded-full">
                에러 발생
              </span>
            )}
          </div>
        </Link>
      ))}
    </div>
  );
}
