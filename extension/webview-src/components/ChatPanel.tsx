/**
 * ReCoder Workspace — 카카오톡처럼 쓰는 대화형 AI 패널.
 *
 * 대화는 /api/chat 으로만 보내며, 이 패널 자체는 워크스페이스 파일을 수정하지 않는다.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

type Role = "user" | "assistant";
type ChatMessage = {
  id: string;
  role: Role;
  content: string;
  pending?: boolean;
  error?: boolean;
  model?: string;
  sentAt?: string;
};

const initialMessages: ChatMessage[] = [{
  id: "welcome",
  role: "assistant",
  content: "안녕하세요, ReCoder예요. 프로젝트 구조, 오류, 배포 방법처럼 궁금한 것을 편하게 물어보세요.\n\n코드 변경이 필요하면 먼저 같이 방향을 정하고, 원할 때만 코드 생성으로 이어갈게요.",
  sentAt: "지금",
}];

let nextMessageId = 1;

const botAvatar = typeof document === "undefined"
  ? ""
  : document.documentElement.dataset.recoderBotAvatar ?? "";

const currentTime = () => new Intl.DateTimeFormat("ko-KR", {
  hour: "numeric", minute: "2-digit",
}).format(new Date());

export const ChatPanel: React.FC<{ isAiReady: boolean }> = ({ isAiReady }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [input, setInput] = useState("");
  const [deleteDialog, setDeleteDialog] = useState(false);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedDeleteIds, setSelectedDeleteIds] = useState<Set<string>>(() => new Set());
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useMessage(useCallback((msg) => {
    if (msg.type === "chat.response") {
      const payload = msg.payload as { id?: string; reply?: string; model?: string };
      const id = payload.id ?? "";
      setMessages((current) => current.flatMap((item) => {
        if (item.id !== id) return [item];
        return [
          { ...item, pending: false },
          {
            id: `assistant-${id}`,
            role: "assistant",
            content: payload.reply ?? "응답을 받지 못했어요. 다시 시도해 주세요.",
            model: payload.model,
            sentAt: currentTime(),
          },
        ];
      }));
    } else if (msg.type === "chat.error") {
      const payload = msg.payload as { id?: string; message?: string };
      const id = payload.id ?? "";
      setMessages((current) => current.map((item) => item.id === id
        ? { ...item, pending: false, error: true }
        : item));
    }
  }, []));

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  const send = useCallback(() => {
    const content = input.trim();
    if (!content || messages.some((message) => message.pending)) return;

    const id = `user-${Date.now()}-${nextMessageId++}`;
    const history = messages
      .filter((message) => !message.pending && !message.error && message.id !== "welcome")
      .slice(-10)
      .map((message) => ({ role: message.role, content: message.content }));

    setMessages((current) => [...current, { id, role: "user", content, pending: true, sentAt: currentTime() }]);
    setInput("");
    postMessage("chat.send", { id, message: content, history });
    requestAnimationFrame(() => textareaRef.current?.focus());
  }, [input, messages, postMessage]);

  const onKeyDown = useCallback((event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // 카카오톡처럼 Enter는 전송, Shift+Enter만 줄바꿈으로 쓴다.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    }
  }, [send]);

  const busy = messages.some((message) => message.pending);

  const deleteAllConversations = useCallback(() => {
    setMessages(initialMessages);
    setInput("");
    setSelectedDeleteIds(new Set());
    setDeleteDialog(false);
    setSelectionMode(false);
  }, []);

  const openSelectiveDelete = useCallback(() => {
    setSelectedDeleteIds(new Set());
    setDeleteDialog(false);
    setSelectionMode(true);
  }, []);

  const toggleDeleteSelection = useCallback((id: string) => {
    setSelectedDeleteIds((current) => {
      const next = new Set(current);
      if (next.has(id)) { next.delete(id); } else { next.add(id); }
      return next;
    });
  }, []);

  const deleteSelectedConversations = useCallback(() => {
    if (selectedDeleteIds.size === 0) { return; }
    setMessages((current) => current.filter((message) => message.id === "welcome" || !selectedDeleteIds.has(message.id)));
    setSelectedDeleteIds(new Set());
    setSelectionMode(false);
  }, [selectedDeleteIds]);

  return (
    <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <style>{`
        .rc-chat-scroll { scrollbar-color: var(--vscode-scrollbarSlider-background, #555) transparent; }
        .rc-chat-input:focus { border-color: var(--vscode-focusBorder, #3794ff) !important; box-shadow: 0 0 0 1px var(--vscode-focusBorder, #3794ff); }
        .rc-chat-send:hover:not(:disabled) { filter: brightness(1.1); }
        .rc-chat-bubble { position: relative; }
        .rc-chat-bubble--assistant::before { content: ""; position: absolute; top: 0; left: -6px; border-top: 7px solid #2d3037; border-left: 7px solid transparent; }
        .rc-chat-bubble--user::after { content: ""; position: absolute; top: 0; right: -6px; border-top: 7px solid #3188ae; border-right: 7px solid transparent; }
      `}</style>
      {deleteDialog && (
        <div role="dialog" aria-modal="true" aria-label="대화 삭제" style={{ position: "fixed", inset: 0, zIndex: 1100, display: "grid", placeItems: "center", padding: 18, background: "rgba(0,0,0,.45)" }}>
          <div style={{ width: "min(320px, 100%)", border: "1px solid var(--vscode-widget-border, #484848)", borderRadius: 9, background: "var(--vscode-editorWidget-background, #252526)", boxShadow: "0 16px 36px rgba(0,0,0,.42)", overflow: "hidden" }}>
            <div style={{ padding: "15px 16px 11px", borderBottom: "1px solid var(--vscode-panel-border, #3c3c3c)" }}>
              <strong style={{ fontSize: 14 }}>대화 삭제</strong>
              <div style={{ marginTop: 5, color: "var(--vscode-descriptionForeground, #aaa)", fontSize: 11 }}>삭제 방식을 선택하세요.</div>
            </div>
            <div style={{ padding: 10, display: "grid", gap: 7 }}>
              <button onClick={openSelectiveDelete} style={{ textAlign: "left", border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 6, padding: "10px 11px", background: "transparent", color: "var(--vscode-foreground, #ddd)", cursor: "pointer" }}>
                <strong style={{ display: "block", fontSize: 12 }}>선택 삭제</strong>
                <span style={{ display: "block", marginTop: 3, color: "var(--vscode-descriptionForeground, #999)", fontSize: 10.5 }}>채팅창에서 지울 말풍선을 직접 고릅니다.</span>
              </button>
              <button onClick={deleteAllConversations} style={{ textAlign: "left", border: "1px solid rgba(236, 96, 96, .55)", borderRadius: 6, padding: "10px 11px", background: "rgba(236, 96, 96, .08)", color: "#ff8b8b", cursor: "pointer" }}>
                <strong style={{ display: "block", fontSize: 12 }}>전체 삭제</strong>
                <span style={{ display: "block", marginTop: 3, color: "#e8a1a1", fontSize: 10.5 }}>안내 메시지를 제외한 대화를 모두 지웁니다.</span>
              </button>
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", padding: "0 12px 12px" }}>
              <button onClick={() => setDeleteDialog(false)} style={{ border: "none", background: "transparent", color: "var(--vscode-textLink-foreground, #3794ff)", cursor: "pointer", fontSize: 11 }}>취소</button>
            </div>
          </div>
        </div>
      )}

      <div className="rc-chat-scroll" style={{ flex: 1, minHeight: 0, overflowY: "auto", overscrollBehavior: "contain", padding: "10px 14px 10px", background: "linear-gradient(180deg, rgba(80, 131, 158, .12), transparent 230px)" }}>
        <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 7, marginBottom: 10 }}>
          {selectionMode ? <>
            <span style={{ marginRight: "auto", color: "var(--vscode-descriptionForeground, #aaa)", fontSize: 10.5 }}>말풍선을 눌러 선택하세요 · {selectedDeleteIds.size}개 선택</span>
            <button onClick={() => { setSelectionMode(false); setSelectedDeleteIds(new Set()); }} style={{ border: "none", background: "transparent", color: "var(--vscode-textLink-foreground, #3794ff)", cursor: "pointer", fontSize: 10.5 }}>취소</button>
            <button onClick={deleteSelectedConversations} disabled={selectedDeleteIds.size === 0} style={{ border: "none", borderRadius: 5, padding: "4px 7px", background: "#d94e4e", color: "#fff", cursor: selectedDeleteIds.size ? "pointer" : "default", opacity: selectedDeleteIds.size ? 1 : .45, fontSize: 10.5, fontWeight: 650 }}>선택 삭제</button>
          </> : <button onClick={() => setDeleteDialog(true)} title="현재 대화 내용 삭제" style={{ border: "1px solid var(--vscode-panel-border, #444)", borderRadius: 5, padding: "4px 7px", background: "transparent", color: "var(--vscode-descriptionForeground, #aaa)", cursor: "pointer", fontSize: 10.5 }}>대화 삭제</button>}
        </div>
        {!isAiReady && (
          <div style={{ margin: "0 0 14px", padding: "9px 10px", border: "1px solid var(--vscode-editorWarning-foreground, #cca700)", borderRadius: 7, color: "var(--vscode-editorWarning-foreground, #cca700)", fontSize: 11, lineHeight: 1.45 }}>
            AI 연결을 확인하는 중입니다. 메시지는 보낼 수 있지만, 설정이 완료되어야 답변을 받을 수 있어요.
          </div>
        )}
        {messages.map((message) => {
          const mine = message.role === "user";
          const selectable = selectionMode && message.id !== "welcome";
          const selected = selectedDeleteIds.has(message.id);
          return (
            <div key={message.id} role={selectable ? "checkbox" : undefined} aria-checked={selectable ? selected : undefined} onClick={selectable ? () => toggleDeleteSelection(message.id) : undefined} style={{ display: "flex", justifyContent: mine ? "flex-end" : "flex-start", alignItems: "flex-start", gap: 7, marginBottom: message.error ? 18 : 13, cursor: selectable ? "pointer" : "default", opacity: selectable && !selected ? .72 : 1 }}>
              {selectable && (
                <span aria-hidden="true" style={{ flex: "0 0 auto", width: 19, height: 19, marginTop: mine ? 8 : 22, borderRadius: "50%", display: "grid", placeItems: "center", border: `1px solid ${selected ? "#49a8d1" : "var(--vscode-panel-border, #5a5a5a)"}`, background: selected ? "#3188ae" : "transparent", color: "#fff", fontSize: 13, fontWeight: 800 }}>{selected ? "✓" : ""}</span>
              )}
              {!mine && (
                <div aria-label="ReCoder" style={{ flex: "0 0 auto", width: 29, height: 29, margin: "18px 7px 0 0", overflow: "hidden", borderRadius: "50%", background: "#17212d", boxShadow: "inset 0 0 0 1px rgba(255,255,255,.22)" }}>
                  {botAvatar ? <img src={botAvatar} alt="ReCoder 봇" style={{ width: "100%", height: "100%", display: "block", objectFit: "cover", objectPosition: "50% 43%" }} /> : "R"}
                </div>
              )}
              <div style={{ maxWidth: "calc(88% - 36px)" }}>
                {!mine && <div style={{ margin: "0 0 4px 2px", color: "var(--vscode-foreground, #d7d7d7)", fontSize: 10.5, fontWeight: 650 }}>ReCoder</div>}
                <div style={{ display: "flex", alignItems: "flex-end", gap: 5, flexDirection: mine ? "row" : "row" }}>
                  {mine && <span style={{ flex: "0 0 auto", color: "var(--vscode-descriptionForeground, #888)", fontSize: 9.5, whiteSpace: "nowrap" }}>{message.sentAt}</span>}
                  <div className={`rc-chat-bubble ${mine ? "rc-chat-bubble--user" : "rc-chat-bubble--assistant"}`} style={{
                    whiteSpace: "pre-wrap", overflowWrap: "anywhere", lineHeight: 1.52, fontSize: 12.5,
                    padding: "9px 11px", borderRadius: mine ? "13px 3px 13px 13px" : "3px 13px 13px 13px",
                    background: mine ? "#3188ae" : "#2d3037",
                    color: mine ? "#fff" : "var(--vscode-foreground, #e7e7e7)",
                    border: selectable && selected ? "1px solid #62c6ef" : mine ? "none" : "1px solid rgba(255,255,255,.055)",
                    opacity: message.pending ? 0.78 : 1,
                  }}>
                    {message.content}
                    {message.pending && <span style={{ display: "inline-flex", gap: 3, marginLeft: 7, color: "inherit" }}><span>·</span><span>·</span><span>·</span></span>}
                  </div>
                  {!mine && <span style={{ flex: "0 0 auto", color: "var(--vscode-descriptionForeground, #888)", fontSize: 9.5, whiteSpace: "nowrap" }}>{message.sentAt}</span>}
                </div>
                {message.error && (
                  <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 4, marginTop: 5, color: "#ff6b6b", fontSize: 10.5, fontWeight: 600 }}>
                    <span aria-hidden="true" style={{ width: 14, height: 14, borderRadius: "50%", display: "inline-grid", placeItems: "center", background: "#e55353", color: "#fff", fontSize: 10, fontWeight: 800 }}>!</span>
                    응답을 가져오지 못했어요
                  </div>
                )}
                {message.model && <div style={{ margin: "3px 3px 0", color: "var(--vscode-descriptionForeground, #777)", fontSize: 9 }}>{message.model}</div>}
              </div>
            </div>
          );
        })}
        <div ref={bottomRef} />
      </div>

      <div style={{ borderTop: "1px solid var(--vscode-panel-border, #333)", padding: "10px 12px 12px", background: "var(--vscode-sideBar-background, #1e1e1e)" }}>
        <div style={{ display: "flex", alignItems: "flex-end", gap: 7 }}>
          <textarea
            ref={textareaRef}
            className="rc-chat-input"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="메시지를 입력하세요"
            rows={2}
            disabled={busy}
            style={{ flex: 1, minWidth: 0, resize: "none", border: "1px solid var(--vscode-input-border, #3f3f3f)", borderRadius: 9, background: "var(--vscode-input-background, #3c3c3c)", color: "var(--vscode-input-foreground, #fff)", padding: "8px 9px", fontFamily: "inherit", fontSize: 12, lineHeight: 1.4, outline: "none" }}
          />
          <button className="rc-chat-send" onClick={send} disabled={!input.trim() || busy} style={{ border: "none", borderRadius: 8, padding: "9px 11px", minWidth: 48, background: "var(--vscode-button-background, #0e639c)", color: "var(--vscode-button-foreground, #fff)", cursor: busy ? "default" : "pointer", opacity: (!input.trim() || busy) ? 0.5 : 1, fontSize: 12, fontWeight: 600 }}>전송</button>
        </div>
        <div style={{ marginTop: 6, color: "var(--vscode-descriptionForeground, #777)", fontSize: 10 }}>Enter로 전송 · Shift + Enter로 줄바꿈 · 대화만으로 파일은 변경되지 않습니다</div>
      </div>
    </div>
  );
};

export default ChatPanel;
