import { NavLink, useNavigate } from 'react-router-dom';
import AgentStatus from './AgentStatus';

export default function Header() {
  const navigate = useNavigate();
  const userEmail = localStorage.getItem('user_email');
  const userName = localStorage.getItem('user_name');
  const displayName = userName || userEmail || '사용자';

  const handleLogout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('user_id');
    localStorage.removeItem('user_email');
    localStorage.removeItem('user_name');
    navigate('/login');
  };

  const navClass = ({ isActive }) =>
    `px-3 py-2 rounded-md text-sm font-medium transition-colors ${
      isActive ? 'bg-zinc-800 text-white' : 'text-zinc-400 hover:text-white hover:bg-zinc-800/50'
    }`;

  return (
    <header className="bg-zinc-900/80 border-b border-zinc-800 sticky top-0 z-10 backdrop-blur-sm">
      <div className="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
        <div className="flex items-center space-x-8">
          <h1 className="text-lg font-bold text-white">AI 업무 어시스턴트</h1>
          <nav className="flex space-x-2">
            <NavLink to="/dashboard" className={navClass}>Dashboard</NavLink>
            <NavLink to="/sessions" className={navClass}>Sessions</NavLink>
            <NavLink to="/chat" className={navClass}>Chat</NavLink>
          </nav>
        </div>
        <div className="flex items-center space-x-4">
          <AgentStatus />
          <span className="text-sm text-zinc-300">{displayName}</span>
          <button
            onClick={handleLogout}
            className="bg-zinc-700 hover:bg-rose-600 text-zinc-200 px-3 py-1.5 rounded-lg text-sm transition-colors"
          >
            로그아웃
          </button>
        </div>
      </div>
    </header>
  );
}
