/**
 * 빠른 이동 휠 (2.0.44) — Alt+Q 로 뜨고, 방향키 한 번으로 기능을 연다. 클릭·입력·Enter 는 필요 없다.
 *
 *  - 방향키 4칸: ↑ → ↓ ← (기본: 코드 생성 · 아키텍처 · 배포 캔버스 · 에러 분석)
 *  - Shift+방향키: 사용자가 넣은 칸 — 넣은 것만 안쪽에 작게 보인다(빈 칸은 숨김)
 *  - Esc: 닫기(Alt+Q 전에 보던 편집기로 돌아감) · E: 칸 바꾸기
 *
 * Alt+Q 로 열면 키보드 포커스는 치던 편집기에 그대로 있고, 방향키는 VS Code 단축키가 받는다
 * (확장 quickAccessHost). 머리글 버튼으로 열었거나 이 창을 누른 뒤에는 여기 키 처리가 받는다.
 *
 * QuickWheelView 는 그리기만 한다(훅 없음 — 서버 렌더 시험). 키 처리는 QuickWheel 이 한다.
 */
import React, { useEffect, useRef, useState } from "react";
import { INNER, OUTER, QuickSlot, QuickState, iconFor, nextTarget, shortLabel, slotForKey, visibleInner } from "./quickAccessModel";
import { Icon } from "./ui/icons";
import { CHARACTER_SRC } from "./recoderCharacter";

const C = {
  fg: "var(--vscode-foreground, #e6e6e6)",
  dim: "var(--vscode-descriptionForeground, #9d9d9d)",
  faint: "var(--vscode-disabledForeground, #6f6f6f)",
  panel: "var(--vscode-editorWidget-background, #252526)",
  line: "var(--vscode-widget-border, var(--vscode-panel-border, #454545))",
  btn: "var(--vscode-button-background, #0078d4)",
  btnFg: "var(--vscode-button-foreground, #fff)",
  warn: "var(--vscode-editorWarning-foreground, #cca700)",
};

//: 2.0.46 카드 모양 — 배포 시트와 같은 면·모서리. 방향키 칸은 십자 배치, 가운데는 리코더.
const WHEEL_CSS = `
.rc-qw{--qw-surface:color-mix(in srgb,var(--vscode-foreground,#fff) 6%,var(--vscode-editorWidget-background,#252526));--qw-hair:color-mix(in srgb,var(--vscode-foreground,#fff) 10%,transparent)}
.rc-qw-grid{display:grid;grid-template-columns:repeat(3,112px);grid-template-rows:repeat(3,84px);gap:10px;justify-content:center}
.rc-qw-card{all:unset;box-sizing:border-box;cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;border-radius:16px;
  background:var(--qw-surface);box-shadow:0 0 0 1px var(--qw-hair),0 10px 24px rgba(0,0,0,.28);color:var(--vscode-foreground,#e6e6e6);transition:transform .12s,background .12s}
.rc-qw-card b{font-size:13px;font-weight:600;line-height:1.2;text-align:center;padding:0 6px}
.rc-qw-card kbd{font:600 10.5px var(--vscode-font-family,sans-serif);color:var(--vscode-descriptionForeground,#9d9d9d);background:color-mix(in srgb,var(--vscode-foreground,#fff) 9%,transparent);border-radius:5px;padding:0 6px}
.rc-qw-card:hover,.rc-qw-card[data-selected=true]{background:var(--vscode-button-background,#0078d4);color:var(--vscode-button-foreground,#fff);transform:scale(1.04)}
.rc-qw-card:hover kbd,.rc-qw-card[data-selected=true] kbd{background:rgba(255,255,255,.22);color:inherit}
.rc-qw-card[data-empty=true]{background:transparent;box-shadow:inset 0 0 0 1.5px var(--qw-hair);color:var(--vscode-descriptionForeground,#9d9d9d)}
.rc-qw-card:focus-visible{outline:2px solid var(--vscode-focusBorder,#3794ff);outline-offset:2px}
.rc-qw-hub{display:grid;place-items:center}.rc-qw-hub span{width:70px;height:70px;border-radius:50%;display:grid;place-items:center;background:color-mix(in srgb,var(--vscode-foreground,#fff) 4%,transparent);box-shadow:0 0 0 1px var(--qw-hair)}
.rc-qw-hub img{width:44px;height:44px}
.rc-qw-inner{display:flex;flex-wrap:wrap;justify-content:center;gap:6px;margin-top:12px;max-width:356px}
.rc-qw-pill{all:unset;box-sizing:border-box;cursor:pointer;display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 10px;border-radius:9px;background:var(--qw-surface);box-shadow:0 0 0 1px var(--qw-hair);font-size:12px;color:var(--vscode-foreground,#e6e6e6)}
.rc-qw-pill kbd{font:600 10px var(--vscode-font-family,sans-serif);color:var(--vscode-descriptionForeground,#9d9d9d)}
.rc-qw-pill:hover,.rc-qw-pill[data-selected=true]{background:var(--vscode-button-background,#0078d4);color:var(--vscode-button-foreground,#fff)}
.rc-qw-pill:hover kbd,.rc-qw-pill[data-selected=true] kbd{color:inherit}
.rc-qw-pill[data-empty=true]{background:transparent;box-shadow:inset 0 0 0 1px var(--qw-hair);color:var(--vscode-descriptionForeground,#9d9d9d)}
.rc-qw-card[data-selected=true][data-empty=true],.rc-qw-pill[data-selected=true][data-empty=true]{background:var(--vscode-button-background,#0078d4);color:var(--vscode-button-foreground,#fff);box-shadow:none}
@media (prefers-reduced-motion: reduce){.rc-qw-card{transition:none}}
`;

export interface QuickWheelViewProps {
  state: QuickState;
  mode: "pick" | "edit";
  selected: QuickSlot | null;
  /** 칸을 눌렀다(고르기: 열기, 편집: 그 칸 선택). */
  onSlot: (slot: QuickSlot) => void;
  onAssign: (slot: QuickSlot, target: string) => void;
  onMode: (mode: "pick" | "edit") => void;
  onReset: () => void;
  onKeybindings: () => void;
  onClose: () => void;
  /** 대화상자 자체(칸 바꾸기에서 키보드 포커스를 받는 곳). */
  dialogRef?: React.Ref<HTMLDivElement>;
}

const AREA: Record<string, React.CSSProperties> = {
  up: { gridColumn: 2, gridRow: 1 }, right: { gridColumn: 3, gridRow: 2 }, down: { gridColumn: 2, gridRow: 3 }, left: { gridColumn: 1, gridRow: 2 },
};

const Slot: React.FC<{ slot: QuickSlot; state: QuickState; mode: "pick" | "edit"; selected: boolean; onSlot: (s: QuickSlot) => void }> = ({ slot, state, mode, selected, onSlot }) => {
  const inner = slot.startsWith("shift+");
  const id = state.slots[slot];
  const empty = !id;
  const label = empty ? "비어 있음" : shortLabel(id, state.targets);
  const key = state.keys[slot];
  const name = empty ? `${key} 칸 비어 있음 — 기능 지정` : `${key} ${state.targets.find((t) => t.id === id)?.label ?? label}`;
  if (inner) {
    return (
      <button type="button" className="rc-qw-pill" aria-label={name} title={name} data-slot={slot} data-target={id || ""} data-empty={empty} data-selected={selected}
        tabIndex={-1} onClick={() => onSlot(slot)}>
        {empty ? (mode === "edit" ? <Icon name="plus" size={13} /> : null) : <Icon name={iconFor(id)} size={13} />}<span>{label}</span><kbd>{key}</kbd>
      </button>
    );
  }
  return (
    <button type="button" className="rc-qw-card" aria-label={name} data-slot={slot} data-target={id || ""} data-empty={empty} data-selected={selected}
      tabIndex={-1} style={AREA[slot]} onClick={() => onSlot(slot)}>
      {!empty && <Icon name={iconFor(id)} size={20} />}<b>{label}</b><kbd>{key}</kbd>
    </button>
  );
};

export const QuickWheelView: React.FC<QuickWheelViewProps> = ({ state, mode, selected, onSlot, onAssign, onMode, onReset, onKeybindings, onClose, dialogRef }) => {
  const editing = mode === "edit";
  const sel = selected ?? "up";
  //: 고르기: 넣어 둔 Shift 칸만(빈 칸·중복은 숨김). 칸 바꾸기: 8칸 모두.
  const inner = editing ? INNER : visibleInner(state);
  const btn: React.CSSProperties = { font: "inherit", fontSize: 12, borderRadius: 8, padding: "6px 12px", cursor: "pointer", border: `1px solid ${C.line}`, background: "transparent", color: C.fg };
  return (
    <div ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-label="빠른 이동" data-testid="quick-wheel" data-mode={mode} className="rc-qw"
      style={{ position: "fixed", inset: 0, zIndex: 2000, display: "grid", placeItems: "center", background: "rgba(0,0,0,.45)", backdropFilter: "blur(2px)", outline: "none" }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <style>{WHEEL_CSS}</style>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 20, alignItems: "center", justifyContent: "center", padding: 20, borderRadius: 20, background: C.panel, border: `1px solid ${C.line}`, boxShadow: "0 24px 60px rgba(0,0,0,.45)", maxWidth: "calc(100vw - 24px)" }}>
        <div>
          <div className="rc-qw-grid" role="group" aria-label="방향키로 고르기">
            {OUTER.map((s) => <Slot key={s} slot={s} state={state} mode={mode} selected={editing && sel === s} onSlot={onSlot} />)}
            <div className="rc-qw-hub" style={{ gridColumn: 2, gridRow: 2 }} aria-hidden="true"><span><img src={CHARACTER_SRC} alt="" /></span></div>
          </div>
          {inner.length > 0 && <div className="rc-qw-inner" role="group" aria-label="Shift+방향키">
            {inner.map((s) => <Slot key={s} slot={s} state={state} mode={mode} selected={editing && sel === s} onSlot={onSlot} />)}
          </div>}
          <p style={{ margin: "12px 0 0", textAlign: "center", fontSize: 11.5, color: C.dim }}>
            {editing ? "방향키로 칸 · 같은 키를 다시 누르면 다음 기능 · Esc 닫기" : "Esc 닫기 · E 칸 바꾸기"}
          </p>
          <div aria-live="polite" style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>
            {editing ? `${state.keys[sel]} 칸: ${state.slots[sel] ? state.targets.find((t) => t.id === state.slots[sel])?.label ?? "" : "비어 있음"}` : ""}
          </div>
        </div>

        {editing && (
          <div data-testid="quick-wheel-editor" style={{ width: 230, maxHeight: 340, display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: 12.5, fontWeight: 650, color: C.fg }}>{state.keys[sel]} 칸</div>
            <div role="radiogroup" data-quick-list aria-label={`${state.keys[sel]} 칸에 넣을 기능`} style={{ overflowY: "auto", display: "grid", gap: 2, borderRadius: 12, padding: 4, background: "color-mix(in srgb,var(--vscode-foreground,#fff) 4%,transparent)" }}>
              {state.targets.map((t) => {
                const on = state.slots[sel] === t.id;
                const usedBy = (Object.keys(state.slots) as QuickSlot[]).filter((s) => s !== sel && state.slots[s] === t.id).map((s) => state.keys[s]);
                return (
                  <button key={t.id} role="radio" aria-checked={on} tabIndex={on ? 0 : -1} onClick={() => onAssign(sel, t.id)}
                    title={usedBy.length ? `${usedBy.join(" ")} 칸과 맞바꿉니다` : undefined}
                    style={{ ...btn, border: 0, textAlign: "left", display: "flex", alignItems: "center", gap: 9, background: on ? "rgba(0,120,212,.28)" : "transparent", padding: "6px 9px" }}>
                    <span style={{ width: 18, display: "grid", placeItems: "center", color: C.dim }}><Icon name={iconFor(t.id)} size={15} /></span>
                    <span style={{ flex: 1 }}>{t.label}</span>
                    {usedBy.length > 0 && <span style={{ color: C.faint, fontSize: 10.5 }}>{usedBy.join(" ")}</span>}
                  </button>
                );
              })}
              <button role="radio" aria-checked={!state.slots[sel]} tabIndex={!state.slots[sel] ? 0 : -1} onClick={() => onAssign(sel, "")}
                style={{ ...btn, border: 0, textAlign: "left", color: C.dim, background: !state.slots[sel] ? "rgba(0,120,212,.28)" : "transparent", padding: "6px 9px" }}>비워 두기</button>
            </div>
            {state.problems.length > 0 && (
              <div role="note" style={{ fontSize: 11, color: C.warn, lineHeight: 1.45 }}>{state.problems.map((p, i) => <div key={i}>· {p}</div>)}</div>
            )}
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <button style={btn} onClick={onReset}>기본값으로</button>
              <button style={btn} onClick={onKeybindings} title="VS Code 단축키 설정에서 Alt+Q 대신 다른 키로">여는 키 바꾸기</button>
              <button style={{ ...btn, marginLeft: "auto", background: C.btn, color: C.btnFg, border: 0 }} onClick={() => onMode("pick")}>완료</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export type CloseReason = "cancel" | "pick" | "dismiss";

export interface QuickWheelProps {
  state: QuickState;
  initialMode: "pick" | "edit";
  /** 칸 바꾸기로 열 때 처음 고른 칸(빈 Shift 칸을 눌러 들어온 경우). */
  initialSlot?: QuickSlot | null;
  /** 기능을 골랐다 — 화면을 연다. */
  onOpen: (targetId: string) => void;
  /** 닫혔다. cancel: Esc(원래 편집기로) · pick: 기능을 고름 · dismiss: 다른 곳으로 떠남. */
  onClose: (reason: CloseReason) => void;
  onAssign: (slot: QuickSlot, target: string) => void;
  onReset: () => void;
  onKeybindings: () => void;
}

const isPlain = (e: KeyboardEvent) => !e.altKey && !e.ctrlKey && !e.metaKey;

export const QuickWheel: React.FC<QuickWheelProps> = ({ state, initialMode, initialSlot, onOpen, onClose, onAssign, onReset, onKeybindings }) => {
  const [mode, setMode] = useState<"pick" | "edit">(initialMode);
  const [selected, setSelected] = useState<QuickSlot | null>(initialMode === "edit" ? initialSlot ?? "up" : null);
  const dialog = useRef<HTMLDivElement | null>(null);
  const ref = useRef({ mode, selected, state, onAssign });
  ref.current = { mode, selected, state, onAssign };

  //: 같은 방향키로 기능을 넘길 때 목록에서도 지금 기능이 보이게.
  useEffect(() => {
    if (mode !== "edit" || typeof document === "undefined") return;
    document.querySelector<HTMLElement>("[data-quick-list] [aria-checked=true]")?.scrollIntoView?.({ block: "nearest" });
  }, [mode, selected, selected ? state.slots[selected] : ""]);

  const pick = (slot: QuickSlot) => {
    const id = ref.current.state.slots[slot];
    if (!id) { setMode("edit"); setSelected(slot); return; }
    onOpen(id);
    onClose("pick");
  };

  //: 칸 바꾸기는 이 창에서 키를 받는다 — 그때만 포커스를 가져오고, 닫히면 원래 자리로 돌려준다.
  //: 고르기(Alt+Q)는 포커스를 가져오지 않는다 — 치던 편집기에 그대로 두고 방향키는 VS Code 가 받는다.
  useEffect(() => {
    if (typeof document === "undefined" || mode !== "edit") return undefined;
    const before = document.activeElement as HTMLElement | null;
    dialog.current?.focus({ preventScroll: true });
    return () => {
      if (before && typeof before.focus === "function" && document.contains(before)) before.focus({ preventScroll: true });
    };
  }, [mode]);

  //: 이 창을 쓰다가(포커스가 있었다가) 다른 곳으로 떠나면 닫는다. 그 자리로 되돌리지는 않는다.
  useEffect(() => {
    if (typeof document === "undefined") return undefined;
    const onHide = () => { if (document.visibilityState === "hidden") onClose("dismiss"); };
    let focusedOnce = document.hasFocus();
    const onFocusIn = () => { focusedOnce = true; };
    const onBlur = () => { if (focusedOnce) onClose("dismiss"); };
    document.addEventListener("visibilitychange", onHide);
    window.addEventListener("focus", onFocusIn);
    window.addEventListener("blur", onBlur);
    return () => {
      document.removeEventListener("visibilitychange", onHide);
      window.removeEventListener("focus", onFocusIn);
      window.removeEventListener("blur", onBlur);
    };
  }, [onClose]);

  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const onKey = (e: KeyboardEvent) => {
      const { mode: m, selected: s, state: st } = ref.current;
      //: 휠이 떠 있는 동안 키는 휠만 받는다(뒤의 결정 창·다이어그램·입력칸이 같이 반응하지 않게).
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose("cancel"); return; }
      //: Alt+Q 는 VS Code 로 넘긴다("휠 닫기") — 입력칸에 글자가 들어가지 않게만 막는다.
      if (e.altKey && e.code === "KeyQ") { e.preventDefault(); return; }
      if (e.key === "Tab") return;
      //: 한글 입력 상태에서는 e.key 가 "ㄷ"·"Process" 로 온다 — 실제 키 위치(e.code)로 본다.
      const arrow = e.code.startsWith("Arrow") ? e.code : e.key;
      const slot = isPlain(e) ? slotForKey(arrow, e.shiftKey) : null;
      e.stopPropagation();
      e.preventDefault();
      if (m === "pick") {
        if (slot) { pick(slot); return; }
        if (e.code === "KeyE" && isPlain(e)) { setMode("edit"); setSelected("up"); }
        return;
      }
      // ── 칸 바꾸기: 방향키로 칸을 고르고, 같은 방향키를 한 번 더 누르면 다음 기능으로(Enter 없이 바로 저장) ──
      if (!slot) return;
      if (slot !== s) { setSelected(slot); return; }
      const next = nextTarget(st, slot);
      if (next !== st.slots[slot]) ref.current.onAssign(slot, next);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  return <QuickWheelView state={state} mode={mode} selected={selected} dialogRef={dialog}
    onSlot={(slot) => { if (ref.current.mode === "pick") pick(slot); else setSelected(slot); }}
    onAssign={onAssign} onMode={(next) => { setMode(next); if (next === "edit" && !ref.current.selected) setSelected("up"); }}
    onReset={onReset} onKeybindings={onKeybindings} onClose={() => onClose("cancel")} />;
};

export default QuickWheel;
