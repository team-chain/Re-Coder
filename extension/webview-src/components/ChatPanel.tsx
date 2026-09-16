/**
 * ReCoder Workspace — 카카오톡처럼 쓰는 대화형 AI 패널.
 *
 * 대화는 /api/chat 으로만 보내며, 이 패널 자체는 워크스페이스 파일을 수정하지 않는다.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

type Role = "user" | "assistant";

//: /api/chat 이 구현 요청을 감지했을 때 답변과 함께 오는 제안. 말풍선 아래 승인 카드로
//: 그려지고, [승인하고 생성]을 눌러야만 코드 생성으로 넘어간다.
export type ChatAction = {
  type: "code.plan";
  instruction: string;
  target_folder: string;
  target_source: "message" | "workspace";
  stack: string;
  files: string[];
  summary: string;
};
export type ChatTarget = { display: string; absolute: string; insideWorkspace: boolean; exists: boolean; workspaceName: string };
type ActionStep = { key: string; label: string; state: "ok" | "run" | "wait" | "fail"; note?: string };
type ActionState = {
  status: "proposed" | "approving" | "accepted" | "cancelled" | "error";
  requestId?: number;
  error?: string;
  steps: ActionStep[];
  filesDone: string[];
  //: 생성 결과. 카드의 [모두 적용]이 그대로 code.applyAll 로 보낸다 — 승인 후 파일을
  //: 쓰기까지 채팅을 떠나지 않아도 된다. 9/16 테스트에서 "생성됐다는데 폴더가 비어
  //: 있다"는 혼란의 원인은 적용 버튼이 다른 패널에만 있던 것.
  ops?: Array<{ file: string; content: string; action?: string }>;
  targetFolder?: string;
  applying?: boolean;
};

type ChatMessage = {
  id: string;
  role: Role;
  content: string;
  pending?: boolean;
  error?: boolean;
  //: 코어가 분류해 내려준 실패 원인("AI 제공자의 요청 한도에 도달했습니다." 등).
  //: 예전에는 chat.error 의 message 를 여기서 버리고 고정 문구만 보여줘서,
  //: rate limit 인지 자격증명 문제인지 사용자가 알 길이 없었다.
  errorReason?: string;
  model?: string;
  sentAt?: string;
  action?: ChatAction | null;
  target?: ChatTarget;
  actionState?: ActionState;
};

const initialMessages: ChatMessage[] = [{
  id: "welcome",
  role: "assistant",
  content: "안녕하세요, ReCoder예요. 프로젝트 구조, 오류, 배포 방법처럼 궁금한 것을 편하게 물어보세요.\n\n만들거나 고칠 것을 말하면 위치와 파일을 정리한 승인 카드를 띄울게요. 승인한 뒤에만 코드 생성으로 이어집니다.",
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
      const payload = msg.payload as { id?: string; reply?: string; model?: string; action?: ChatAction | null; target?: ChatTarget };
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
            action: payload.action ?? null,
            target: payload.target,
            actionState: payload.action ? { status: "proposed", steps: [], filesDone: [] } : undefined,
          },
        ];
      }));
    } else if (msg.type === "chat.actionFolderPicked") {
      const payload = msg.payload as { id?: string; folder?: string };
      const id = `assistant-${payload.id ?? ""}`;
      setMessages((current) => current.map((item) => item.id === id && item.action
        ? { ...item, action: { ...item.action, target_folder: payload.folder ?? "", target_source: "message" }, target: undefined }
        : item));
    } else if (msg.type === "chat.actionAccepted") {
      const payload = msg.payload as { id?: string; requestId?: number; targetFolder?: string; absolutePath?: string; folderCreated?: boolean; addedToWorkspace?: boolean };
      const id = `assistant-${payload.id ?? ""}`;
      const prep = payload.folderCreated
        ? (payload.addedToWorkspace ? "폴더 생성 · 워크스페이스에 추가" : "폴더 생성")
        : (payload.addedToWorkspace ? "워크스페이스에 추가" : "폴더 확인");
      setMessages((current) => current.map((item) => item.id === id && item.actionState
        ? { ...item, actionState: { ...item.actionState, status: "accepted", requestId: payload.requestId, targetFolder: payload.targetFolder ?? "", steps: [
            { key: "prep", label: prep, state: "ok" },
            { key: "plan", label: "설계 결정", state: "run" },
            { key: "gen", label: "코드 생성", state: "wait" },
          ] } }
        : item));
    } else if (msg.type === "chat.actionError") {
      const payload = msg.payload as { id?: string; message?: string };
      const id = `assistant-${payload.id ?? ""}`;
      setMessages((current) => current.map((item) => item.id === id && item.actionState
        ? { ...item, actionState: { ...item.actionState, status: "error", error: payload.message ?? "" } }
        : item));
    } else if (msg.type === "code.planResult" || msg.type === "code.result" || msg.type === "code.applied" || msg.type === "code.error") {
      //: CodeAgent 가 처리하는 같은 이벤트를 여기서도 듣고 카드의 진행 단계만 갱신한다.
      //: 결정 모달·diff·적용 버튼은 CodeAgent 가 그대로 담당한다.
      const payload = (msg.payload ?? {}) as { requestId?: number; ackKey?: string; ok?: boolean; message?: string; decisions?: unknown[]; ops?: Array<{ file: string; content: string; action?: string }> };
      const rid = payload.requestId ?? (payload.ackKey ? Number(String(payload.ackKey).split(":")[0]) : undefined);
      if (rid === undefined || Number.isNaN(rid)) return;
      setMessages((current) => current.map((item) => {
        const st = item.actionState;
        if (!st || st.requestId !== rid) return item;
        const steps = st.steps.map((s) => ({ ...s }));
        const set = (key: string, state: ActionStep["state"], note?: string) => { const s = steps.find((x) => x.key === key); if (s) { s.state = state; if (note !== undefined) s.note = note; } };
        if (msg.type === "code.planResult") {
          const n = Array.isArray(payload.decisions) ? payload.decisions.length : 0;
          set("plan", "ok", n === 0 ? "결정할 항목 없음" : `${n}개 확인 중`);
          set("gen", "run");
        } else if (msg.type === "code.result") {
          const ops = (payload.ops ?? []).map((op) => ({ file: op.file, content: op.content, action: op.action }));
          const files = ops.map((op) => op.file);
          set("gen", "ok", `${files.length}개 파일 생성됨 · 아직 저장 전`);
          return { ...item, actionState: { ...st, steps, filesDone: files, ops } };
        } else if (msg.type === "code.applied" && payload.ackKey) {
          const file = String(payload.ackKey).split(":").slice(1).join(":");
          const key = `apply:${file}`;
          if (!steps.some((s) => s.key === key)) steps.push({ key, label: file, state: payload.ok === false ? "fail" : "ok", note: payload.ok === false ? undefined : "저장됨" });
          else set(key, payload.ok === false ? "fail" : "ok", payload.ok === false ? undefined : "저장됨");
          const pendingLeft = steps.some((s) => s.key.startsWith("apply:") && s.state === "run");
          return { ...item, actionState: { ...st, steps, applying: pendingLeft } };
        } else if (msg.type === "code.error") {
          if (payload.ackKey) {
            const file = String(payload.ackKey).split(":").slice(1).join(":");
            const key = `apply:${file}`;
            if (!steps.some((s) => s.key === key)) steps.push({ key, label: file, state: "fail", note: payload.message });
            else set(key, "fail", payload.message);
          } else {
            const running = steps.find((s) => s.state === "run");
            if (running) { running.state = "fail"; running.note = payload.message; }
          }
        }
        return { ...item, actionState: { ...st, steps } };
      }));
    } else if (msg.type === "chat.error") {
      const payload = msg.payload as { id?: string; message?: string };
      const id = payload.id ?? "";
      const reason = (payload.message ?? "").trim();
      setMessages((current) => current.map((item) => item.id === id
        ? { ...item, pending: false, error: true, errorReason: reason || undefined }
        : item));
    }
  }, []));

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  const approveAction = useCallback((message: ChatMessage) => {
    if (!message.action) return;
    const id = message.id.replace(/^assistant-/, "");
    setMessages((current) => current.map((item) => item.id === message.id && item.actionState
      ? { ...item, actionState: { ...item.actionState, status: "approving" } } : item));
    postMessage("chat.approveAction", { id, instruction: message.action.instruction, targetFolder: message.action.target_folder });
  }, [postMessage]);

  const cancelAction = useCallback((message: ChatMessage) => {
    setMessages((current) => current.map((item) => item.id === message.id && item.actionState
      ? { ...item, actionState: { ...item.actionState, status: "cancelled" } } : item));
  }, []);

  const applyAllFromCard = useCallback((message: ChatMessage) => {
    const st = message.actionState;
    if (!st || !st.ops || st.ops.length === 0 || st.requestId === undefined) return;
    const rid = st.requestId;
    const ops = st.ops.map((op) => ({ file: op.file, content: op.content, ackKey: `${rid}:${op.file}` }));
    setMessages((current) => current.map((item) => {
      if (item.id !== message.id || !item.actionState) return item;
      const steps = item.actionState.steps.filter((s) => !s.key.startsWith("apply:"));
      for (const op of ops) steps.push({ key: `apply:${op.file}`, label: op.file, state: "run" });
      return { ...item, actionState: { ...item.actionState, steps, applying: true } };
    }));
    postMessage("code.applyAll", { ops, targetFolder: st.targetFolder ?? "" });
  }, [postMessage]);

  const pickActionFolder = useCallback((message: ChatMessage) => {
    postMessage("chat.pickActionFolder", { id: message.id.replace(/^assistant-/, "") });
  }, [postMessage]);

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
                {!mine && message.action && message.actionState && message.actionState.status !== "cancelled" && (
                  <ActionCard message={message} onApprove={() => approveAction(message)} onCancel={() => cancelAction(message)} onPickFolder={() => pickActionFolder(message)} onApplyAll={() => applyAllFromCard(message)} />
                )}
                {message.error && (
                  <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 3, marginTop: 5 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 4, color: "#ff6b6b", fontSize: 10.5, fontWeight: 600 }}>
                      <span aria-hidden="true" style={{ width: 14, height: 14, borderRadius: "50%", display: "inline-grid", placeItems: "center", background: "#e55353", color: "#fff", fontSize: 10, fontWeight: 800 }}>!</span>
                      응답을 가져오지 못했어요
                    </div>
                    {/* 코어가 내려준 원인이 있으면 그대로 보여준다 — 원인 없는
                        빨간 줄만으로는 사용자가 재시도 말고 할 수 있는 게 없다. */}
                    {message.errorReason && (
                      <div style={{ maxWidth: "100%", textAlign: "right", color: "var(--vscode-descriptionForeground, #c98080)", fontSize: 10, lineHeight: 1.45, overflowWrap: "anywhere" }}>
                        {message.errorReason}
                      </div>
                    )}
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
        <div style={{ marginTop: 6, color: "var(--vscode-descriptionForeground, #777)", fontSize: 10 }}>Enter로 전송 · Shift + Enter로 줄바꿈 · 파일 변경은 승인 카드에서 승인한 뒤에만 진행됩니다</div>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// 승인 카드 — 채팅 답변 아래에 붙는다.
//
// 상태: proposed(승인 대기) → approving → accepted(진행 단계 표시) / cancelled / error
// 파일이 생기기 전에 사람이 한 번 누르는 지점이 여기다. 위치·파일 목록·워크스페이스
// 밖 여부를 승인 전에 보여준다 — 9/16 테스트에서 "어디에 생겼는지 모르는" 문제의 대책.
// ---------------------------------------------------------------------------
const ActionCard: React.FC<{ message: ChatMessage; onApprove: () => void; onCancel: () => void; onPickFolder: () => void; onApplyAll: () => void }> = ({ message, onApprove, onCancel, onPickFolder, onApplyAll }) => {
  const action = message.action!;
  const st = message.actionState!;
  const target = message.target;
  const folderLabel = action.target_folder || (target?.workspaceName ? `${target.workspaceName} (프로젝트 루트)` : "프로젝트 루트");
  const outside = target ? !target.insideWorkspace : /^(\/|~|[A-Za-z]:)/.test(action.target_folder || "");
  const isNew = target ? !target.exists : false;
  const badge = outside ? (isNew ? "프로젝트 밖 · 새 폴더" : "프로젝트 밖") : (isNew ? "새 폴더" : "");
  const accepted = st.status === "accepted";
  const border = accepted ? "#2f6b4a" : st.status === "error" ? "#8b3a3a" : "#3f7fb5";
  const head = accepted ? "#1f3328" : st.status === "error" ? "#3a2222" : "#22303d";
  const mono: React.CSSProperties = { fontFamily: "var(--vscode-editor-font-family, Menlo, monospace)", fontSize: 11 };

  return (
    <div style={{ marginTop: 8, border: `1px solid ${border}`, borderRadius: 9, background: "#1b2530", overflow: "hidden", boxShadow: "0 6px 18px rgba(0,0,0,.3)", maxWidth: 420 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 11px", background: head, borderBottom: `1px solid ${border}`, fontWeight: 700, fontSize: 12, color: "#e6f0f8" }}>
        <span>{accepted ? "✅ 승인됨 · 진행 중" : st.status === "error" ? "⚠️ 진행 실패" : st.status === "approving" ? "⏳ 준비 중" : "📁 생성 승인"}</span>
        {!accepted && badge && <span style={{ marginLeft: "auto", fontSize: 10, fontWeight: 600, padding: "2px 7px", borderRadius: 999, background: "#3a2a10", color: "#f0b35b", border: "1px solid #6b4a17" }}>{badge}</span>}
        {accepted && <span style={{ marginLeft: "auto", ...mono, fontSize: 10, color: "#8fd9ad", padding: "2px 7px", borderRadius: 999, background: "#1f3328", border: "1px solid #2f6b4a", maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{folderLabel}</span>}
      </div>

      {!accepted && (
        <div style={{ padding: "9px 11px" }}>
          <div style={{ display: "grid", gridTemplateColumns: "44px 1fr", gap: "5px 10px", fontSize: 11.5 }}>
            <div style={{ color: "#8fa4b5" }}>위치</div><div style={{ color: "#e3e9ef", overflowWrap: "anywhere" }}><code style={{ ...mono, background: "#0f161d", padding: "1px 5px", borderRadius: 4, color: "#cfe3f2" }}>{folderLabel}</code></div>
            {action.summary && <><div style={{ color: "#8fa4b5" }}>작업</div><div style={{ color: "#e3e9ef" }}>{action.summary}</div></>}
            {action.stack && <><div style={{ color: "#8fa4b5" }}>스택</div><div style={{ color: "#e3e9ef" }}>{action.stack}</div></>}
          </div>
          {action.files.length > 0 && (
            <ul style={{ margin: "8px 0 0", padding: 0, listStyle: "none", fontSize: 11.5 }}>
              {action.files.map((f) => (
                <li key={f} style={{ display: "flex", gap: 8, padding: "2px 0", color: "#dfe6ec", alignItems: "center" }}>
                  <span style={{ fontSize: 10, color: "#7ed3a2", border: "1px solid #2f6b4a", borderRadius: 4, padding: "0 5px" }}>NEW</span>
                  <span style={mono}>{f}</span>
                </li>
              ))}
            </ul>
          )}
          {outside && (
            <div style={{ marginTop: 9, padding: "7px 9px", borderRadius: 6, background: "rgba(240,179,91,.08)", border: "1px solid rgba(240,179,91,.35)", color: "#f0c98a", fontSize: 11, lineHeight: 1.45 }}>
              이 폴더는 현재 열린 프로젝트{target?.workspaceName ? `(${target.workspaceName})` : ""} 밖이에요. 승인하면 {isNew ? "폴더를 새로 만들고 " : ""}워크스페이스에 추가한 뒤 그 안에만 파일을 씁니다.
            </div>
          )}
          {st.status === "error" && st.error && (
            <div style={{ marginTop: 9, color: "#ff8b8b", fontSize: 11 }}>{st.error}</div>
          )}
        </div>
      )}

      {accepted && (
        <div style={{ padding: "9px 11px" }}>
          <ul style={{ margin: 0, padding: 0, listStyle: "none", fontSize: 11.5 }}>
            {st.steps.map((s) => (
              <li key={s.key} style={{ display: "flex", gap: 9, alignItems: "center", padding: "3px 0", color: "#dfe6ec" }}>
                <span style={{ width: 14, height: 14, borderRadius: "50%", display: "grid", placeItems: "center", fontSize: 9, fontWeight: 800,
                  background: s.state === "ok" ? "#2f6b4a" : s.state === "run" ? "#3794ff" : s.state === "fail" ? "#8b3a3a" : "transparent",
                  border: s.state === "wait" ? "1px solid #4a5560" : "none",
                  color: s.state === "wait" ? "transparent" : "#fff" }}>{s.state === "ok" ? "✓" : s.state === "run" ? "…" : s.state === "fail" ? "!" : "·"}</span>
                <span style={s.key.startsWith("apply:") ? mono : undefined}>{s.label}</span>
                {s.note && <span style={{ color: "#8a95a0", fontSize: 10.5 }}>· {s.note}</span>}
              </li>
            ))}
          </ul>
          {(() => {
            const ops = st.ops ?? [];
            const applied = st.steps.filter((s) => s.key.startsWith("apply:") && s.state === "ok").length;
            const allApplied = ops.length > 0 && applied === ops.length;
            if (ops.length === 0) {
              return <div style={{ marginTop: 7, color: "#8a95a0", fontSize: 10.5 }}>결정 카드는 왼쪽 코드 작성 패널에 떠요. 선택을 마치면 여기서 이어집니다.</div>;
            }
            if (allApplied) {
              return <div style={{ marginTop: 8, color: "#8fd9ad", fontSize: 11 }}>✓ {ops.length}개 파일이 {st.targetFolder || "프로젝트 루트"}에 저장됐어요.</div>;
            }
            return (
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 9 }}>
                <button onClick={onApplyAll} disabled={!!st.applying} style={{ border: "1px solid transparent", borderRadius: 6, padding: "6px 11px", fontSize: 11.5, fontWeight: 650, cursor: "pointer", background: "#3794ff", color: "#fff", opacity: st.applying ? .6 : 1 }}>{st.applying ? "저장 중…" : `모두 적용 (${ops.length}개 파일 저장)`}</button>
                <span style={{ color: "#8a95a0", fontSize: 10.5 }}>파일별 diff·개별 적용은 왼쪽 코드 작성 패널</span>
              </div>
            );
          })()}
        </div>
      )}

      {!accepted && (
        <div style={{ display: "flex", gap: 8, padding: "9px 11px 11px", borderTop: `1px solid ${border}`, background: "#1a232c" }}>
          <button onClick={onApprove} disabled={st.status === "approving"} style={{ border: "1px solid transparent", borderRadius: 6, padding: "6px 11px", fontSize: 11.5, fontWeight: 650, cursor: "pointer", background: "#3794ff", color: "#fff", opacity: st.status === "approving" ? .6 : 1 }}>{st.status === "error" ? "다시 시도" : "승인하고 생성"}</button>
          <button onClick={onPickFolder} disabled={st.status === "approving"} style={{ border: "1px solid #4a5560", borderRadius: 6, padding: "6px 11px", fontSize: 11.5, fontWeight: 650, cursor: "pointer", background: "transparent", color: "#cfd8e0" }}>위치 변경</button>
          <button onClick={onCancel} style={{ marginLeft: "auto", border: "none", background: "transparent", color: "#9aa6b1", fontSize: 11.5, cursor: "pointer" }}>취소</button>
        </div>
      )}
    </div>
  );
};

export default ChatPanel;
