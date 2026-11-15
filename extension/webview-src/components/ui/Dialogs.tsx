/**
 * ReCoder 화면 안 대화창(4.0.0) — 확장이 묻는 확인·입력·선택·알림을 이 화면 안에서 띄운다.
 *
 * VS Code 확인창은 Windows 에서 옛 운영체제 팝업으로, 입력·선택은 화면 위 명령줄로 따로 떠서
 * ReCoder 와 다른 앱처럼 보였다. 확장(src/ui/dialogs.ts)이 ui.ask 를 보내면 여기서 그리고 ui.answer 로 답한다.
 * 받자마자 ui.shown 을 보낸다 — 이 답이 없으면 확장은 VS Code 기본 창으로 다시 묻는다.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useVSCodeApi } from "../../hooks/useVSCodeApi";

export interface DialogItem { label: string; description?: string; detail?: string; primary?: boolean }
export interface DialogRequest {
  id: string; kind: "confirm" | "input" | "pick" | "toast"; tone?: "info" | "warn" | "error";
  title?: string; message?: string; detail?: string; items?: DialogItem[]; cancelLabel?: string;
  value?: string; placeholder?: string; password?: boolean; error?: string; modal?: boolean;
}

export const DIALOG_CSS = `
.rc-dlg-back *,.rc-toasts *{box-sizing:border-box}
.rc-dlg-back{position:fixed;inset:0;z-index:9000;background:rgba(3,6,10,.62);backdrop-filter:blur(3px);display:flex;align-items:center;justify-content:center;padding:20px}
.rc-dlg{width:min(520px,100%);max-height:calc(100vh - 40px);overflow:auto;background:var(--rc-solid,#0D131A);border:1px solid var(--rc-line2,rgba(160,190,220,.16));border-radius:18px;box-shadow:var(--rc-shadow,0 24px 60px -24px rgba(0,0,0,.85));padding:22px 22px 18px;color:var(--rc-fg,#EEF3F8);font-size:13px;line-height:1.55}
.rc-dlg-head{display:flex;gap:12px;align-items:flex-start}
.rc-dlg-mark{width:30px;height:30px;border-radius:10px;flex:none;display:grid;place-items:center;background:var(--rc-tile2,rgba(148,178,210,.065));color:var(--rc-teal,#35A9D1)}
.rc-dlg[data-tone=warn] .rc-dlg-mark{color:var(--rc-warn,#F2B544)}.rc-dlg[data-tone=error] .rc-dlg-mark{color:var(--rc-bad,#F27272)}
.rc-dlg h2{margin:3px 0 0;font-size:15.5px;font-weight:650;line-height:1.35;white-space:pre-line}
.rc-dlg .rc-dlg-msg{margin:4px 0 0;color:var(--rc-fg2,#9AA8B8);white-space:pre-line}
.rc-dlg .rc-dlg-detail{margin:14px 0 0;padding:12px 14px;border-radius:12px;background:var(--rc-tile,rgba(148,178,210,.035));color:var(--rc-fg2,#9AA8B8);font-size:12.5px;white-space:pre-line;overflow-wrap:anywhere}
.rc-dlg-more{margin-top:10px}.rc-dlg-more summary{cursor:pointer;color:var(--rc-teal,#35A9D1);list-style:none;font-size:12px;width:max-content}
.rc-dlg-more summary::-webkit-details-marker{display:none}.rc-dlg-more[open] summary{display:none}.rc-dlg-more .rc-dlg-detail{margin-top:0}
.rc-dlg-input{margin-top:14px;width:100%;height:38px;border-radius:11px;border:1px solid var(--rc-line2,rgba(160,190,220,.16));background:var(--vscode-input-background,#0A1017);color:var(--rc-fg,#EEF3F8);padding:0 12px;font-size:13px;font-family:inherit}
.rc-dlg-input:focus{outline:none;border-color:var(--rc-teal,#35A9D1)}
.rc-dlg-error{margin:6px 2px 0;color:var(--rc-bad,#F27272);font-size:12px}
.rc-dlg-list{margin:14px 0 0;display:flex;flex-direction:column;gap:6px}
.rc-dlg-list button{all:unset;cursor:pointer;display:block;padding:10px 12px;border-radius:12px;background:var(--rc-tile,rgba(148,178,210,.035));border:1px solid transparent}
.rc-dlg-list button:hover,.rc-dlg-list button:focus-visible{border-color:var(--rc-teal,#35A9D1);background:var(--rc-tile2,rgba(148,178,210,.065))}
.rc-dlg-list b{font-weight:600}.rc-dlg-list small{color:var(--rc-fg2,#9AA8B8);margin-left:6px}.rc-dlg-list p{margin:2px 0 0;color:var(--rc-fg3,#67768A);font-size:12px}
.rc-dlg-acts{display:flex;justify-content:flex-end;flex-wrap:wrap;gap:8px;margin-top:18px}
.rc-dlg-btn{all:unset;cursor:pointer;padding:8px 16px;border-radius:999px;font-size:12.5px;font-weight:550;background:var(--rc-tile2,rgba(148,178,210,.065));color:var(--rc-fg,#EEF3F8);border:1px solid var(--rc-line2,rgba(160,190,220,.16))}
.rc-dlg-btn.p{background:var(--rc-grad,linear-gradient(135deg,#36B1D9,#7B8FB6));color:#04121A;border-color:transparent;font-weight:650}
.rc-dlg-btn:focus-visible{outline:2px solid var(--rc-teal,#35A9D1);outline-offset:2px}
.rc-toasts{position:fixed;right:16px;bottom:16px;z-index:8999;display:flex;flex-direction:column;gap:8px;width:min(380px,calc(100vw - 32px))}
.rc-toast{background:var(--rc-solid,#0D131A);border:1px solid var(--rc-line2,rgba(160,190,220,.16));border-left:3px solid var(--rc-teal,#35A9D1);border-radius:14px;box-shadow:var(--rc-shadow,0 24px 60px -24px rgba(0,0,0,.85));padding:12px 14px;font-size:12.5px;color:var(--rc-fg,#EEF3F8);white-space:pre-line}
.rc-toast[data-tone=warn]{border-left-color:var(--rc-warn,#F2B544)}.rc-toast[data-tone=error]{border-left-color:var(--rc-bad,#F27272)}
.rc-toast .rc-dlg-acts{margin-top:10px}.rc-toast .rc-dlg-btn{padding:6px 12px;font-size:12px}
`;

/** 한 줄 제목 + 나머지. 긴 문장은 첫 문장만 제목으로 두고 나머지는 "자세히" 로 접는다(글이 많다는 피드백). */
export function shortText(text = ""): { head: string; rest: string } {
  const t = text.trim();
  if (t.length <= 60 && !t.includes("\n")) return { head: t, rest: "" };
  const m = /^(.{8,80}?(?:[.?!。](?=\s)|\n|요\.|다\.|까요\?))/s.exec(t);
  const head = (m ? m[1] : t.slice(0, 60)).replace(/\n+$/, "").trim();
  return { head, rest: t.slice(m ? m[1].length : 60).trim() };
}
/** 설명은 짧으면 그대로, 길면 접어 둔다. */
export const DETAIL_INLINE_MAX = 90;

const Mark: React.FC<{ tone?: string }> = ({ tone }) => (
  <span className="rc-dlg-mark" aria-hidden>
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      {tone === "warn" || tone === "error" ? <><path d="M12 3 2 20h20L12 3z" /><path d="M12 10v4" /><path d="M12 17h.01" /></> : <><circle cx="12" cy="12" r="9" /><path d="M12 11v5" /><path d="M12 8h.01" /></>}
    </svg>
  </span>
);

export const DialogView: React.FC<{ request: DialogRequest; onAnswer: (answer: { index?: number; value?: string; cancelled?: boolean }) => void }> = ({ request, onAnswer }) => {
  const [value, setValue] = useState(request.value ?? "");
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => { setValue(request.value ?? ""); }, [request.id, request.value]);
  useEffect(() => {
    const el = box.current?.querySelector<HTMLElement>("input,.rc-dlg-list button,.rc-dlg-btn.p");
    el?.focus();
  }, [request.id, request.error]);
  const cancel = () => onAnswer({ cancelled: true });
  const items = request.items ?? [];
  const main = shortText(request.kind === "confirm" ? request.message : request.title || request.message);
  const title = main.head;
  const sub = request.kind === "confirm" || !request.title ? "" : shortText(request.message).head;
  const more = [main.rest, request.kind !== "confirm" && request.title ? shortText(request.message).rest : "", request.detail ?? ""].filter(Boolean).join("\n\n");
  return (
    <div className="rc-dlg-back" onMouseDown={(e) => { if (e.target === e.currentTarget) cancel(); }}>
      <div className="rc-dlg" role="dialog" aria-modal="true" aria-label={title || "확인"} data-tone={request.tone} data-testid="rc-dialog" ref={box}
        onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); cancel(); } }}>
        <div className="rc-dlg-head"><Mark tone={request.tone} /><div style={{ minWidth: 0 }}>
          {title && <h2>{title}</h2>}
          {sub && <p className="rc-dlg-msg">{sub}</p>}
        </div></div>
        {more && (more.length > DETAIL_INLINE_MAX
          ? <details className="rc-dlg-more"><summary>자세히 ›</summary><div className="rc-dlg-detail">{more}</div></details>
          : <div className="rc-dlg-detail">{more}</div>)}
        {request.kind === "input" && <>
          <input className="rc-dlg-input" type={request.password ? "password" : "text"} value={value} placeholder={request.placeholder}
            autoComplete="off" spellCheck={false} aria-invalid={!!request.error}
            onChange={(e) => setValue(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); onAnswer({ value }); } }} />
          {request.error && <p className="rc-dlg-error" role="alert">{request.error}</p>}
        </>}
        {request.kind === "pick" && <div className="rc-dlg-list" role="listbox">
          {items.map((item, i) => <button key={i} type="button" role="option" aria-selected={false} onClick={() => onAnswer({ index: i })}>
            <b>{item.label}</b>{item.description && <small>{item.description}</small>}{item.detail && <p>{item.detail}</p>}
          </button>)}
        </div>}
        <div className="rc-dlg-acts">
          <button type="button" className="rc-dlg-btn" onClick={cancel}>{request.cancelLabel || "취소"}</button>
          {request.kind === "input" && <button type="button" className="rc-dlg-btn p" onClick={() => onAnswer({ value })}>확인</button>}
          {request.kind === "confirm" && items.map((item, i) =>
            <button key={i} type="button" className={`rc-dlg-btn${i === 0 ? " p" : ""}`} onClick={() => onAnswer({ index: i })}>{item.label}</button>)}
        </div>
      </div>
    </div>
  );
};

export const ToastView: React.FC<{ request: DialogRequest; onAnswer: (answer: { index?: number; cancelled?: boolean }) => void }> = ({ request, onAnswer }) => {
  const items = request.items ?? [];
  const answer = useRef(onAnswer); answer.current = onAnswer;
  useEffect(() => {
    //: 버튼 없는 알림은 잠시 뒤 스스로 닫힌다(VS Code 알림과 같게). 버튼이 있으면 고를 때까지 둔다.
    if (items.length) return;
    const t = window.setTimeout(() => answer.current({ cancelled: true }), request.tone === "error" ? 12000 : 7000);
    return () => window.clearTimeout(t);
  }, [items.length, request.id, request.tone]);
  return (
    <div className="rc-toast" role={request.tone === "error" ? "alert" : "status"} data-tone={request.tone} data-testid="rc-toast">
      {shortText(request.message).head}
      {(shortText(request.message).rest || request.detail) && <details className="rc-dlg-more"><summary>자세히 ›</summary><div className="rc-dlg-detail">{[shortText(request.message).rest, request.detail].filter(Boolean).join("\n\n")}</div></details>}
      <div className="rc-dlg-acts">
        <button type="button" className="rc-dlg-btn" onClick={() => onAnswer({ cancelled: true })}>{items.length ? request.cancelLabel || "닫기" : "닫기"}</button>
        {items.map((item, i) => <button key={i} type="button" className={`rc-dlg-btn${i === 0 ? " p" : ""}`} onClick={() => onAnswer({ index: i })}>{item.label}</button>)}
      </div>
    </div>
  );
};

/** 앱 맨 위에 한 번 둔다. 모달은 한 번에 하나(먼저 온 순서), 알림은 오른쪽 아래에 쌓는다. */
export const Dialogs: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [queue, setQueue] = useState<DialogRequest[]>([]);
  useMessage(useCallback(({ type, payload }) => {
    if (type === "ui.ask") {
      const req = payload as DialogRequest;
      if (!req?.id) return;
      postMessage("ui.shown", { id: req.id });
      setQueue(q => q.some(x => x.id === req.id) ? q.map(x => x.id === req.id ? req : x) : [...q, req]);
    } else if (type === "ui.close") {
      const id = (payload as { id?: string })?.id;
      setQueue(q => q.filter(x => x.id !== id));
    }
  }, [postMessage]));
  const answer = useCallback((id: string, a: { index?: number; value?: string; cancelled?: boolean }) => {
    //: 입력값(비밀값 포함)은 보낸 즉시 화면에서 지운다 — 검증 오류면 확장이 다시 보낸다.
    setQueue(q => q.filter(x => x.id !== id));
    postMessage("ui.answer", { id, ...a });
  }, [postMessage]);
  const modal = queue.find(x => x.kind !== "toast");
  const toasts = queue.filter(x => x.kind === "toast").slice(-3);
  return <>
    <style>{DIALOG_CSS}</style>
    {modal && <DialogView key={modal.id} request={modal} onAnswer={(a) => answer(modal.id, a)} />}
    {!!toasts.length && <div className="rc-toasts">{toasts.map(t => <ToastView key={t.id} request={t} onAnswer={(a) => answer(t.id, a)} />)}</div>}
  </>;
};

export default Dialogs;
