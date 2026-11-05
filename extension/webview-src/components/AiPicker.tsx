/**
 * 작성창 AI 칩과 메뉴 — Claude / GPT 중 하나를 한 번에 고른다.
 *
 *  - 칩: 지금 쓰는 AI 이름. 누르면 메뉴가 열린다.
 *  - 메뉴: 공급자 두 줄(복잡 · 간단 모델 이름), 사용 중이면 ✓, 키가 없으면 「키 연결」.
 *  - 고르면 Core 를 다시 시작한다 — 그동안 칩은 「바꾸는 중…」.
 *  - 세부 모델(Sonnet ↔ Opus 등)은 메뉴 아래 「모델 설정」 화면에서.
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { prettyModel } from "./teamConfig";

export type AiId = "anthropic" | "openai";
export const AI_IDS: AiId[] = ["anthropic", "openai"];
export const AI_NAME: Record<AiId, string> = { anthropic: "Claude", openai: "GPT" };
export const AI_VENDOR: Record<AiId, string> = { anthropic: "Anthropic API", openai: "OpenAI API" };

export interface AiProviderInfo { id: AiId; quality: string; fast: string; connected: boolean }

/** 메뉴 줄에서는 공급자 이름을 빼고 짧게 — "Claude Sonnet 5.5" → "Sonnet 5.5". */
export const shortModel = (m: string): string => prettyModel(m).replace(/^Claude /, "");

export const AI_CSS = `
.rc-ai-mark{display:inline-grid;place-items:center;flex:none;font-weight:800;line-height:1;letter-spacing:-.02em;color:#0A0E13;font-family:inherit}
.rc-ai-mark.anthropic{background:linear-gradient(140deg,#F2A77E,#D9805A)}
.rc-ai-mark.openai{background:linear-gradient(140deg,#7EE0BD,#3FB58C)}
.rc-ai-key{display:inline-flex;align-items:center;height:26px;padding:0 11px;border-radius:999px;border:1px solid color-mix(in srgb,var(--rc-teal) 60%,transparent);color:var(--vscode-textLink-foreground);font-size:12px;font-weight:600;white-space:nowrap;background:color-mix(in srgb,var(--rc-teal) 8%,transparent)}
.rc-ai-spin{width:14px;height:14px;flex:none;border-radius:50%;border:2px solid color-mix(in srgb,var(--rc-teal) 25%,transparent);border-top-color:var(--rc-teal);animation:rc-ai-spin .8s linear infinite}
@keyframes rc-ai-spin{to{transform:rotate(360deg)}}
`;

const css = `
.rc-ai-btn{display:inline-flex;align-items:center;gap:8px;height:36px;padding:0 9px 0 7px;border-radius:10px;border:1px solid transparent;background:transparent;color:var(--rc-fg);font:inherit;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap;transition:background .12s ease,border-color .12s ease}
.rc-ai-btn:hover,.rc-ai-btn[aria-expanded=true]{background:var(--rc-tile2);border-color:var(--rc-line2)}
.rc-ai-btn:disabled{opacity:.55;cursor:default}
.rc-ai-btn .sw{color:var(--rc-fg2);font-weight:500}
.rc-ai-btn svg.chev{opacity:.6}
.rc-ai-btn[aria-busy=true]{opacity:1;cursor:progress;border-color:color-mix(in srgb,var(--rc-teal) 35%,transparent);background:color-mix(in srgb,var(--rc-teal) 7%,transparent)}
.rc-ai-btn.done{border-color:color-mix(in srgb,var(--rc-teal) 55%,transparent);background:color-mix(in srgb,var(--rc-teal) 10%,transparent)}
.rc-ai-btn.done .rc-ai-check{margin-left:-2px}
.rc-ai-pop{position:fixed;z-index:1200;box-sizing:border-box;width:324px;max-width:calc(100vw - 24px);padding:6px;border-radius:14px;border:1px solid var(--rc-line2);background:var(--rc-solid);color:var(--rc-fg);box-shadow:0 28px 64px -16px rgba(0,0,0,.75),0 2px 8px rgba(0,0,0,.35);font-size:13px}
.rc-ai-pop:focus{outline:none}
.rc-ai-hd{display:flex;align-items:baseline;padding:8px 10px 6px;font-size:12px;font-weight:600;color:var(--rc-fg2)}
.rc-ai-hd span{margin-left:auto;font-weight:500;color:var(--rc-fg3);font-size:11.5px}
.rc-ai-row{display:flex;align-items:center;gap:12px;width:100%;box-sizing:border-box;padding:10px;border:0;border-radius:10px;background:transparent;color:var(--rc-fg);font:inherit;text-align:left;cursor:pointer}
.rc-ai-row:hover{background:var(--rc-tile2)}
.rc-ai-row[aria-checked=true]{background:color-mix(in srgb,var(--rc-teal) 11%,transparent)}
.rc-ai-row:focus-visible,.rc-ai-ft:focus-visible,.rc-ai-btn:focus-visible{outline:2px solid var(--rc-teal);outline-offset:1px}
.rc-ai-row .txt{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
.rc-ai-row b{font-size:14px;font-weight:650;line-height:1.2}
.rc-ai-row small{font-size:12px;line-height:1.3;color:var(--rc-fg2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-ai-row small i{font-style:normal;color:var(--rc-fg3);margin-right:4px}
.rc-ai-check{color:var(--rc-teal);flex:none}
.rc-ai-sep{height:1px;margin:6px 6px;background:var(--rc-line)}
.rc-ai-ft{display:flex;align-items:center;gap:9px;width:100%;box-sizing:border-box;padding:9px 10px;border:0;border-radius:10px;background:transparent;color:var(--rc-fg2);font:inherit;font-size:12.5px;cursor:pointer;text-align:left}
.rc-ai-ft:hover{background:var(--rc-tile2);color:var(--rc-fg)}
.rc-ai-ft .go{margin-left:auto;opacity:.6}
`;

export const AiMark: React.FC<{ id: AiId; size?: number }> = ({ id, size = 20 }) => (
  <span className={`rc-ai-mark ${id}`} style={{ width: size, height: size, fontSize: Math.round(size * 0.56), borderRadius: Math.round(size * 0.3) }} aria-hidden="true">
    {id === "anthropic" ? "C" : "G"}
  </span>
);

const Check: React.FC = () => (
  <svg className="rc-ai-check" width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m3.5 8.4 2.9 2.8 6.1-6.4" /></svg>
);

export interface AiPickerProps {
  current: AiId;
  providers: AiProviderInfo[];
  /** 바꾸는 중인 공급자(Core 다시 시작 중). */
  switching?: AiId | null;
  /** 방금 바뀌었다 — 칩에 잠깐 ✓ 를 보인다. */
  justSwitched?: boolean;
  disabled?: boolean;
  onPick: (id: AiId) => void;
  onConnect: (id: AiId) => void;
  onOpenModels?: () => void;
  defaultOpen?: boolean;
}

export const AiPicker: React.FC<AiPickerProps> = ({ current, providers, switching, justSwitched, disabled, onPick, onConnect, onOpenModels, defaultOpen }) => {
  const [open, setOpen] = useState(!!defaultOpen);
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);

  const place = () => {
    const b = btnRef.current?.getBoundingClientRect();
    if (!b || typeof window === "undefined") return;
    const width = Math.min(324, window.innerWidth - 24);
    //: 칩이 입력창 오른쪽에 있으므로 메뉴의 오른쪽 끝을 칩에 맞춘다.
    const left = Math.max(12, Math.min(b.right - width, window.innerWidth - width - 12));
    const below = window.innerHeight - b.bottom;
    setPos(below >= 260 ? { left, top: b.bottom + 8 } : { left, bottom: window.innerHeight - b.top + 8 });
  };
  useLayoutEffect(() => { if (open) place(); }, [open]);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (popRef.current?.contains(t) || btnRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); setOpen(false); btnRef.current?.focus(); }
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
        const rows = Array.from(popRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
        if (!rows.length) return;
        e.preventDefault();
        const index = rows.indexOf(document.activeElement as HTMLButtonElement);
        const next = e.key === "Home" ? 0 : e.key === "End" ? rows.length - 1 : (index + (e.key === "ArrowUp" ? -1 : 1) + rows.length) % rows.length;
        rows[next].focus();
      }
    };
    popRef.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", place);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey, true); window.removeEventListener("resize", place); };
  }, [open]);
  useEffect(() => { if (disabled || switching) setOpen(false); }, [disabled, switching]);

  return (
    <>
      <style>{AI_CSS}{css}</style>
      <button ref={btnRef} type="button" className={`rc-ai-btn${justSwitched && !switching ? " done" : ""}`} data-testid="ai-picker" disabled={disabled || !!switching}
        aria-haspopup="menu" aria-expanded={open} aria-busy={!!switching} title="AI 바꾸기"
        onClick={() => { if (!switching) setOpen(!open); }}>
        {switching ? <><span className="rc-ai-spin" aria-hidden="true" /><span className="sw">{AI_NAME[switching]}로 바꾸는 중…</span></>
          : <><AiMark id={current} size={20} /><span>{AI_NAME[current]}</span>
            {justSwitched ? <Check />
              : <svg className="chev" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="m4.5 6 3.5 3.5L11.5 6" /></svg>}</>}
      </button>
      {open && (
        <div ref={popRef} className="rc-ai-pop" role="menu" aria-label="AI 고르기" tabIndex={-1} data-testid="ai-menu"
          style={pos ? { left: pos.left, top: pos.top, bottom: pos.bottom } : { left: 12, top: 12 }}>
          <div className="rc-ai-hd">AI 모델<span>다음 요청부터 적용</span></div>
          {providers.map((p) => {
            const on = p.id === current;
            return (
              <button key={p.id} type="button" role="menuitemradio" aria-checked={on} className="rc-ai-row" data-provider={p.id} disabled={disabled || !!switching}
                onClick={() => { if (on) { setOpen(false); return; } if (!p.connected) { setOpen(false); onConnect(p.id); return; } setOpen(false); onPick(p.id); }}>
                <AiMark id={p.id} size={34} />
                <span className="txt">
                  <b>{AI_NAME[p.id]}</b>
                  {p.connected
                    ? <small><i>복잡</i>{shortModel(p.quality)}  ·  <i>간단</i>{shortModel(p.fast)}</small>
                    : <small>키를 연결하면 바로 쓸 수 있어요</small>}
                </span>
                {on ? <Check /> : !p.connected ? <span className="rc-ai-key">키 연결</span> : null}
              </button>
            );
          })}
          <div className="rc-ai-sep" />
          <button type="button" className="rc-ai-ft" onClick={() => { setOpen(false); onOpenModels?.(); }}>
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden="true"><path d="M2.5 4.5h6M11.5 4.5h2M2.5 11.5h2M7.5 11.5h6" /><circle cx="10" cy="4.5" r="1.6" /><circle cx="6" cy="11.5" r="1.6" /></svg>
            모델 · 비용 설정
            <svg className="go" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="m6 3.5 4.5 4.5L6 12.5" /></svg>
          </button>
        </div>
      )}
    </>
  );
};

export default AiPicker;
