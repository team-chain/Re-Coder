import { useState, useEffect } from 'react';
import apiClient from '../api/client';

export default function AgentStatus() {
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const checkStatus = async () => {
      try {
        const res = await apiClient.get('/agent/status');
        setConnected(res.data.connected);
      } catch (err) {
        setConnected(false);
      }
    };

    checkStatus();
    const interval = setInterval(checkStatus, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="flex items-center space-x-2 text-xs bg-zinc-800/50 px-2 py-1 rounded-full border border-zinc-700">
      <div className={`w-2 h-2 rounded-full ${connected ? 'bg-emerald-500' : 'bg-rose-500'}`}></div>
      <span className="text-zinc-300">{connected ? '에이전트 연결됨' : '에이전트 끊김'}</span>
    </div>
  );
}
