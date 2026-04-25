import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import apiClient from '../api/client';

export default function Login() {
  const [isRegister, setIsRegister] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    if (localStorage.getItem('token')) {
      navigate('/dashboard');
    }
  }, [navigate]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      if (isRegister) {
        await apiClient.post('/auth/register', { email, password, name });
      }
      
      const res = await apiClient.post('/auth/login', { email, password });
      const { access_token, user } = res.data;
      
      localStorage.setItem('token', access_token);
      localStorage.setItem('user_id', user.user_id);
      localStorage.setItem('user_email', user.email);
      if (user.name) localStorage.setItem('user_name', user.name);
      
      navigate('/dashboard');
    } catch (err) {
      setError(err.response?.data?.detail || err.response?.data?.message || '인증에 실패했습니다.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-md bg-zinc-900/70 border border-zinc-800 rounded-xl p-8 shadow-xl">
        <h1 className="text-2xl font-bold text-white mb-6 text-center">
          {isRegister ? '회원가입' : '로그인'}
        </h1>
        
        {error && (
          <div className="bg-rose-500/20 text-rose-400 p-3 rounded-lg text-sm mb-6 text-center">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-zinc-400 mb-1">이메일</label>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full bg-zinc-950 border border-zinc-700 rounded-lg px-4 py-2 text-zinc-100 focus:ring-2 focus:ring-indigo-500 focus:outline-none"
              placeholder="name@example.com"
            />
          </div>
          
          <div>
            <label className="block text-sm font-medium text-zinc-400 mb-1">비밀번호</label>
            <input
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full bg-zinc-950 border border-zinc-700 rounded-lg px-4 py-2 text-zinc-100 focus:ring-2 focus:ring-indigo-500 focus:outline-none"
              placeholder="••••••••"
            />
          </div>

          {isRegister && (
            <div>
              <label className="block text-sm font-medium text-zinc-400 mb-1">이름 (선택)</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full bg-zinc-950 border border-zinc-700 rounded-lg px-4 py-2 text-zinc-100 focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                placeholder="홍길동"
              />
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-medium py-2 rounded-lg transition-colors mt-6 disabled:opacity-50"
          >
            {loading ? '처리 중...' : (isRegister ? '가입하기' : '로그인')}
          </button>
        </form>

        <div className="mt-6 text-center text-sm text-zinc-500">
          {isRegister ? '이미 계정이 있으신가요?' : '계정이 없으신가요?'}
          <button
            type="button"
            onClick={() => {
              setIsRegister(!isRegister);
              setError('');
            }}
            className="ml-2 text-indigo-400 hover:text-indigo-300 font-medium"
          >
            {isRegister ? '로그인하기' : '회원가입하기'}
          </button>
        </div>
      </div>
    </div>
  );
}
