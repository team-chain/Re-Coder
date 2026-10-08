import React, { useEffect, useRef } from 'react';

/** Children stay mounted while closed so ongoing work and form state survive. */
export function CanvasDrawer({ open, title, onClose, busy = false, children }: {
  open: boolean; title: string; onClose: () => void; busy?: boolean; children: React.ReactNode;
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
    <div className="rc-drawer" ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-label={title}>
      <header><h3>{title}</h3><button onClick={onClose} disabled={busy} aria-label="패널 닫기">닫기</button></header>
      {children}
    </div>
  </div>;
}
