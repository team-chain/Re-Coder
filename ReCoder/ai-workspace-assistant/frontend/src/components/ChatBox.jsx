import { useState } from 'react';
import { Send } from 'lucide-react';

export default function ChatBox({ onSend, disabled }) {
  const [text, setText] = useState('');

  const handleSubmit = (e) => {
    e.preventDefault();
    if (text.trim() && !disabled) {
      onSend(text.trim());
      setText('');
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="flex items-end space-x-2 bg-zinc-900/70 p-3 rounded-xl border border-zinc-800">
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="메시지를 입력하세요..."
        className="flex-1 bg-zinc-950 border border-zinc-700 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none resize-none h-12 max-h-32 text-zinc-100"
        disabled={disabled}
      />
      <button
        type="submit"
        disabled={disabled || !text.trim()}
        className={`bg-indigo-600 hover:bg-indigo-500 text-white p-2 rounded-lg transition-colors flex items-center justify-center h-12 w-12 ${
          (disabled || !text.trim()) ? 'opacity-50 cursor-not-allowed' : ''
        }`}
      >
        {disabled ? (
          <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
        ) : (
          <Send size={20} />
        )}
      </button>
    </form>
  );
}
