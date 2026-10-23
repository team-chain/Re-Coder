import React, { useEffect, useRef } from 'react';

/** Children stay mounted while closed so ongoing work and form state survive. */
export function CanvasDrawer({ open, title, sub, icon, onClose, busy = false, bare = false, children }: {
  open: boolean; title: string; onClose: () => void; busy?: boolean;
  /** 2.0.47 머리 — 아이콘 · 제목 · 한 줄 설명(설정 시트와 같은 모양). */
  sub?: string; icon?: React.ReactNode;
  /** 2.0.46 설정 시트 — 제목은 시트가 아이콘과 함께 그린다. 여기서는 닫기 단추만 오른쪽 위에. */
  bare?: boolean; children: React.ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) { e.preventDefault(); close.current(); }
      if (e.key !== 'Tab') return;
      const controls = Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],summary') || []).filter(el => el.getClientRects().length > 0);
      if (!controls.length) { e.preventDefault(); return; }
      const first = controls[0], last = controls[controls.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (document.activeElement === last || document.activeElement === panel.current)) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown',key);
    return () => { document.removeEventListener('keydown',key); if(previous?.isConnected) previous.focus(); };
  }, [open,busy]);
  return <div className="rc-drawer-shade" hidden={!open} onClick={e => {if(e.target === e.currentTarget && !busy) onClose();}}>
    <div className="rc-drawer rc-drawer-bare" ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-label={title}>
      <button className="rc-drawer-x" onClick={onClose} disabled={busy} aria-label="패널 닫기"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg></button>
      {!bare && <header className="rc-drawer-head">{icon && <span className="ic">{icon}</span>}<div><h3>{title}</h3>{sub && <p>{sub}</p>}</div></header>}
      {children}
    </div>
  </div>;
}
