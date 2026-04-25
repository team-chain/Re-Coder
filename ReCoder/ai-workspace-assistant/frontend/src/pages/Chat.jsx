import { useState, useRef, useEffect } from 'react';
import Header from '../components/Header';
import ChatBox from '../components/ChatBox';
import apiClient from '../api/client';

export default function Chat() {
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const handleSend = async (question) => {
    const userId = localStorage.getItem('user_id');
    const newUserMsg = { role: 'user', content: question, timestamp: new Date() };
    
    setMessages(prev => [...prev, newUserMsg]);
    setLoading(true);

    try {
      const res = await apiClient.post('/chat', { user_id: userId, question });
      const newAiMsg = { role: 'assistant', content: res.data.answer, timestamp: new Date() };
      setMessages(prev => [...prev, newAiMsg]);
    } catch (err) {
      let errorMsg = '채팅 전송에 실패했습니다.';
      if (err.response?.status === 404) {
        errorMsg = '에이전트가 오프라인 상태입니다.';
      } else if (err.response?.status === 504) {
        errorMsg = '에이전트 응답 시간이 초과되었습니다.';
      }
      
      setMessages(prev => [...prev, { role: 'system', content: errorMsg, timestamp: new Date() }]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex flex-col h-screen">
      <Header />
      <main className="flex-1 max-w-4xl w-full mx-auto px-4 py-6 flex flex-col min-h-0">
        <div className="flex-1 bg-zinc-900/50 border border-zinc-800 rounded-xl mb-4 overflow-y-auto p-4 space-y-6">
          {messages.length === 0 ? (
            <div className="h-full flex items-center justify-center text-zinc-500">
              에이전트에게 질문을 입력해보세요.
            </div>
          ) : (
            messages.map((msg, idx) => (
              <div 
                key={idx} 
                className={`flex ${msg.role === 'user' ? 'justify-end' : msg.role === 'system' ? 'justify-center' : 'justify-start'}`}
              >
                <div 
                  className={`max-w-[80%] rounded-2xl px-4 py-3 ${
                    msg.role === 'user' 
                      ? 'bg-indigo-600 text-white rounded-br-sm' 
                      : msg.role === 'system'
                      ? 'bg-rose-500/20 text-rose-400 text-sm py-2 rounded-full px-6'
                      : 'bg-zinc-800 text-zinc-200 rounded-bl-sm'
                  }`}
                >
                  <div className="whitespace-pre-wrap text-sm leading-relaxed">{msg.content}</div>
                  {msg.role !== 'system' && (
                    <div className={`text-[10px] mt-1 ${msg.role === 'user' ? 'text-indigo-200 text-right' : 'text-zinc-500'}`}>
                      {msg.timestamp.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })}
                    </div>
                  )}
                </div>
              </div>
            ))
          )}
          <div ref={messagesEndRef} />
        </div>
        <div className="shrink-0">
          <ChatBox onSend={handleSend} disabled={loading} />
        </div>
      </main>
    </div>
  );
}
