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
import { INNER, OUTER, QuickSlot, QuickState, iconFor, labelPoint, nextTarget, petalPath, shortLabel, slotAngle, slotForKey, visibleInner } from "./quickAccessModel";

const CX = 150, CY = 150;
const OUT_R0 = 62, OUT_R1 = 146, IN_R0 = 30, IN_R1 = 58;
const GAP = 3; // 조각 사이 각도(도)

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

const Petal: React.FC<{ slot: QuickSlot; state: QuickState; mode: "pick" | "edit"; selected: boolean; onSlot: (s: QuickSlot) => void }> = ({ slot, state, mode, selected, onSlot }) => {
  const inner = slot.startsWith("shift+");
  const [r0, r1] = inner ? [IN_R0, IN_R1] : [OUT_R0, OUT_R1];
  const a = slotAngle(slot);
  const id = state.slots[slot];
  const empty = !id;
  const label = empty ? "비어 있음" : shortLabel(id, state.targets);
  const p = labelPoint(CX, CY, (r0 + r1) / 2, slot);
  const key = state.keys[slot];
  const name = empty ? `${key} 칸 비어 있음 — 기능 지정` : `${key} ${state.targets.find((t) => t.id === id)?.label ?? label}`;
  return (
    <g className="rc-qw-petal" role="button" tabIndex={-1} aria-label={name} data-slot={slot} data-target={id || ""} style={{ cursor: "pointer" }}
      onClick={() => onSlot(slot)}>
      <path d={petalPath(CX, CY, r0, r1, a - 45 + GAP, a + 45 - GAP)}
        fill={selected ? "rgba(0,120,212,.32)" : empty ? "transparent" : "rgba(255,255,255,.07)"}
        stroke={selected ? "#3794ff" : empty ? "rgba(255,255,255,.2)" : "rgba(255,255,255,.12)"}
        strokeWidth={selected ? 2 : 1} strokeDasharray={empty ? "4 4" : undefined} />
      {inner ? (
        //: 안쪽 칸은 좁다 — 아이콘과 짧은 이름만(전체 이름은 마우스를 올리면).
        <>
          <title>{name}</title>
          <text x={p.x} y={p.y + (empty ? 4 : 0)} textAnchor="middle" fontSize={empty ? 12 : 13} fill={empty ? "#8a8a8a" : "#9cdcfe"}>{empty ? (mode === "edit" ? "+" : "") : iconFor(id)}</text>
          {!empty && <text x={p.x} y={p.y + 12} textAnchor="middle" fontSize={8.5} fill="#cfcfcf">{label}</text>}
        </>
      ) : (
        <>
          {!empty && <text x={p.x} y={p.y - 12} textAnchor="middle" fontSize={17} fill="#9cdcfe">{iconFor(id)}</text>}
          <text x={p.x} y={p.y + (empty ? 4 : 9)} textAnchor="middle" fontSize={13} fontWeight={600} fill={empty ? "#8a8a8a" : "#e6e6e6"}>{label}</text>
          <text x={p.x} y={p.y + (empty ? 19 : 24)} textAnchor="middle" fontSize={10} fill="#8a8a8a">{key}</text>
        </>
      )}
    </g>
  );
};

export const QuickWheelView: React.FC<QuickWheelViewProps> = ({ state, mode, selected, onSlot, onAssign, onMode, onReset, onKeybindings, onClose, dialogRef }) => {
  const editing = mode === "edit";
  const sel = selected ?? "up";
  //: 고르기: 넣어 둔 Shift 칸만(빈 칸·중복은 숨김). 칸 바꾸기: 8칸 모두.
  const inner = editing ? INNER : visibleInner(state);
  const btn: React.CSSProperties = { font: "inherit", fontSize: 12, borderRadius: 6, padding: "5px 11px", cursor: "pointer", border: `1px solid ${C.line}`, background: "transparent", color: C.fg };
  return (
    <div ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-label="빠른 이동" data-testid="quick-wheel" data-mode={mode}
      style={{ position: "fixed", inset: 0, zIndex: 2000, display: "grid", placeItems: "center", background: "rgba(0,0,0,.45)", outline: "none" }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 16, alignItems: "center", justifyContent: "center", padding: 14, borderRadius: 14, background: C.panel, border: `1px solid ${C.line}`, boxShadow: "0 18px 48px rgba(0,0,0,.45)", maxWidth: "calc(100vw - 24px)" }}>
        <div>
          <style>{`.rc-qw-petal:hover path{fill:rgba(0,120,212,.22);stroke:#3794ff}@media (prefers-reduced-motion:no-preference){.rc-qw-petal path{transition:fill .12s,stroke .12s}}`}</style>
          <svg width={260} height={260} viewBox="0 0 300 300" role="group" aria-label="방향키로 고르기" style={{ display: "block", fontFamily: "var(--vscode-font-family)" }}>
            {OUTER.map((s) => <Petal key={s} slot={s} state={state} mode={mode} selected={editing && sel === s} onSlot={onSlot} />)}
            {inner.map((s) => <Petal key={s} slot={s} state={state} mode={mode} selected={editing && sel === s} onSlot={onSlot} />)}
          </svg>
          <p style={{ margin: "4px 0 0", textAlign: "center", fontSize: 11, color: C.faint }}>
            {editing ? "방향키로 칸 · 같은 키를 다시 누르면 다음 기능 · Esc 닫기" : "Esc 닫기 · E 칸 바꾸기"}
          </p>
          <div aria-live="polite" style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>
            {editing ? `${state.keys[sel]} 칸: ${state.slots[sel] ? state.targets.find((t) => t.id === state.slots[sel])?.label ?? "" : "비어 있음"}` : ""}
          </div>
        </div>

        {editing && (
          <div data-testid="quick-wheel-editor" style={{ width: 220, maxHeight: 320, display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: 12.5, fontWeight: 650, color: C.fg }}>{state.keys[sel]} 칸</div>
            <div role="radiogroup" data-quick-list aria-label={`${state.keys[sel]} 칸에 넣을 기능`} style={{ overflowY: "auto", display: "grid", gap: 2, border: `1px solid ${C.line}`, borderRadius: 8, padding: 4 }}>
              {state.targets.map((t) => {
                const on = state.slots[sel] === t.id;
                const usedBy = (Object.keys(state.slots) as QuickSlot[]).filter((s) => s !== sel && state.slots[s] === t.id).map((s) => state.keys[s]);
                return (
                  <button key={t.id} role="radio" aria-checked={on} tabIndex={on ? 0 : -1} onClick={() => onAssign(sel, t.id)}
                    title={usedBy.length ? `${usedBy.join(" ")} 칸과 맞바꿉니다` : undefined}
                    style={{ ...btn, border: 0, textAlign: "left", display: "flex", alignItems: "center", gap: 8, background: on ? "rgba(0,120,212,.28)" : "transparent", padding: "5px 8px" }}>
                    <span style={{ width: 22, textAlign: "center", color: "#9cdcfe" }}>{iconFor(t.id)}</span>
                    <span style={{ flex: 1 }}>{t.label}</span>
                    {usedBy.length > 0 && <span style={{ color: C.faint, fontSize: 10.5 }}>{usedBy.join(" ")}</span>}
                  </button>
                );
              })}
              <button role="radio" aria-checked={!state.slots[sel]} tabIndex={!state.slots[sel] ? 0 : -1} onClick={() => onAssign(sel, "")}
                style={{ ...btn, border: 0, textAlign: "left", color: C.dim, background: !state.slots[sel] ? "rgba(0,120,212,.28)" : "transparent", padding: "5px 8px" }}>비워 두기</button>
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
