import { Link } from 'react-router-dom';

export default function ErrorHistory({ sessions }) {
  const errorSessions = sessions?.filter(s => s.error_count > 0) || [];

  if (errorSessions.length === 0) {
    return <div className="text-zinc-500 text-sm py-4">에러 기록이 없습니다.</div>;
  }

  return (
    <div className="space-y-3">
      {errorSessions.map((session) => (
        <Link
          key={session.session_id}
          to={`/sessions/${session.session_id}`}
          className="flex items-center justify-between bg-zinc-900/70 border border-zinc-800 rounded-xl p-3 hover:border-zinc-600 transition-colors"
        >
          <div className="flex-1 min-w-0 pr-4">
            <div className="text-sm font-medium text-zinc-200 truncate">
              {session.current_task || '작업 없음'}
            </div>
            <div className="text-xs text-zinc-500 mt-1">
              {new Date(session.start_time).toLocaleString('ko-KR')}
            </div>
          </div>
          <div>
            {session.resolved ? (
              <span className="bg-emerald-500/20 text-emerald-400 rounded-full px-2 py-0.5 text-xs whitespace-nowrap">
                해결됨
              </span>
            ) : (
              <span className="bg-rose-500/20 text-rose-400 rounded-full px-2 py-0.5 text-xs whitespace-nowrap">
                미해결
              </span>
            )}
          </div>
        </Link>
      ))}
    </div>
  );
}
