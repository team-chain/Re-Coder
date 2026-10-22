/**
 * 빠른 이동 휠 (2.0.44) — Alt+Q 로 뜨고, 방향키 하나로 기능을 연다.
 *
 *  - 바깥 고리 4칸: ↑ → ↓ ← (기본: 코드 생성 · 아키텍처 · 배포 캔버스 · 에러 분석)
 *  - 안쪽 고리 4칸: Shift+방향키 — 사용자가 채우는 자리
 *  - Esc: 닫기(Alt+Q 전에 보던 편집기로 돌아감) · E: 칸 바꾸기
 *
 * QuickWheelView 는 그리기만 한다(훅 없음 — 서버 렌더 시험). 키 처리는 QuickWheel 이 한다.
 */
import React, { useEffect, useRef, useState } from "react";
import { INNER, OUTER, QuickSlot, QuickState, labelPoint, moveSelection, petalPath, shortLabel, slotAngle, slotForKey } from "./quickAccessModel";

const CX = 180, CY = 180;
const OUT_R0 = 96, OUT_R1 = 172, IN_R0 = 46, IN_R1 = 92;
const GAP = 3; // 조각 사이 각도(도)

const C = {
  fg: "var(--vscode-foreground, #e6e6e6)",
  dim: "var(--vscode-descriptionForeground, #9d9d9d)",
  faint: "var(--vscode-disabledForeground, #6f6f6f)",
  panel: "var(--vscode-editorWidget-background, #252526)",
  line: "var(--vscode-widget-border, var(--vscode-panel-border, #454545))",
  accent: "var(--vscode-focusBorder, #0078d4)",
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
}

const Petal: React.FC<{ slot: QuickSlot; state: QuickState; mode: "pick" | "edit"; selected: boolean; onSlot: (s: QuickSlot) => void }> = ({ slot, state, mode, selected, onSlot }) => {
  const inner = slot.startsWith("shift+");
  const [r0, r1] = inner ? [IN_R0, IN_R1] : [OUT_R0, OUT_R1];
  const a = slotAngle(slot);
  const id = state.slots[slot];
  const empty = !id;
  const label = empty ? "추가" : shortLabel(id, state.targets);
  const p = labelPoint(CX, CY, (r0 + r1) / 2, slot);
  const key = state.keys[slot];
  const name = empty ? `${key} 칸 비어 있음 — 기능 지정` : `${key} ${state.targets.find((t) => t.id === id)?.label ?? label}`;
  return (
    <g className="rc-qw-petal" role="button" tabIndex={-1} aria-label={name} data-slot={slot} data-target={id || ""} style={{ cursor: "pointer" }}
      onClick={() => onSlot(slot)}>
      <path d={petalPath(CX, CY, r0, r1, a - 45 + GAP, a + 45 - GAP)}
        fill={selected ? "rgba(0,120,212,.30)" : empty ? "transparent" : "rgba(255,255,255,.06)"}
        stroke={selected ? "#3794ff" : empty ? "rgba(255,255,255,.22)" : "rgba(255,255,255,.14)"}
        strokeWidth={selected ? 2 : 1} strokeDasharray={empty ? "4 4" : undefined} />
      <text x={p.x} y={p.y - (inner ? 2 : 6)} textAnchor="middle" fontSize={inner ? 10.5 : 13} fontWeight={600}
        fill={empty ? "#8a8a8a" : "#e6e6e6"}>{empty && mode === "pick" ? "+ 추가" : label}</text>
      <text x={p.x} y={p.y + (inner ? 11 : 12)} textAnchor="middle" fontSize={inner ? 9 : 10.5} fill="#9d9d9d">{key}</text>
    </g>
  );
};

export const QuickWheelView: React.FC<QuickWheelViewProps> = ({ state, mode, selected, onSlot, onAssign, onMode, onReset, onKeybindings, onClose }) => {
  const editing = mode === "edit";
  const sel = selected ?? "up";
  const btn: React.CSSProperties = { font: "inherit", fontSize: 12, borderRadius: 6, padding: "5px 11px", cursor: "pointer", border: `1px solid ${C.line}`, background: "transparent", color: C.fg };
  return (
    <div role="dialog" aria-modal="true" aria-label="빠른 이동" data-testid="quick-wheel" data-mode={mode}
      style={{ position: "fixed", inset: 0, zIndex: 2000, display: "grid", placeItems: "center", background: "rgba(0,0,0,.5)", backdropFilter: "blur(2px)" }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 18, alignItems: "center", justifyContent: "center", padding: 18, borderRadius: 14, background: C.panel, border: `1px solid ${C.line}`, boxShadow: "0 18px 48px rgba(0,0,0,.45)", maxWidth: "calc(100vw - 24px)" }}>
        <div>
          <style>{`.rc-qw-petal:hover path{fill:rgba(0,120,212,.22);stroke:#3794ff}@media (prefers-reduced-motion:no-preference){.rc-qw-petal path{transition:fill .12s,stroke .12s}}`}</style>
          <svg width={300} height={300} viewBox="0 0 360 360" role="group" aria-label="방향키로 고르기" style={{ display: "block", fontFamily: "var(--vscode-font-family)" }}>
            <circle cx={CX} cy={CY} r={OUT_R1 + 4} fill="rgba(255,255,255,.03)" stroke="rgba(255,255,255,.08)" />
            {OUTER.map((s) => <Petal key={s} slot={s} state={state} mode={mode} selected={editing && sel === s} onSlot={onSlot} />)}
            {INNER.map((s) => <Petal key={s} slot={s} state={state} mode={mode} selected={editing && sel === s} onSlot={onSlot} />)}
            <circle cx={CX} cy={CY} r={IN_R0 - 6} fill="rgba(0,0,0,.25)" stroke="rgba(255,255,255,.1)" />
            <text x={CX} y={CY - 3} textAnchor="middle" fontSize={12} fontWeight={700} fill="#e6e6e6">{editing ? "칸 바꾸기" : "빠른 이동"}</text>
            <text x={CX} y={CY + 13} textAnchor="middle" fontSize={9.5} fill="#9d9d9d">{state.wheelKey}</text>
          </svg>
          <p style={{ margin: "6px 0 0", textAlign: "center", fontSize: 11.5, color: C.dim }}>
            {editing ? "방향키로 칸 고르기 · Tab 안쪽/바깥 · 오른쪽에서 기능 선택" : "방향키로 열기 · Shift+방향키 안쪽 칸 · E 칸 바꾸기 · Esc 닫기"}
          </p>
        </div>

        {editing && (
          <div data-testid="quick-wheel-editor" style={{ width: 230, maxHeight: 340, display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: 12.5, fontWeight: 650, color: C.fg }}>{state.keys[sel]} 칸</div>
            <div role="radiogroup" aria-label={`${state.keys[sel]} 칸에 넣을 기능`} style={{ overflowY: "auto", display: "grid", gap: 2, border: `1px solid ${C.line}`, borderRadius: 8, padding: 4 }}>
              {state.targets.map((t) => {
                const on = state.slots[sel] === t.id;
                const usedBy = (Object.keys(state.slots) as QuickSlot[]).filter((s) => s !== sel && state.slots[s] === t.id).map((s) => state.keys[s]);
                return (
                  <button key={t.id} role="radio" aria-checked={on} onClick={() => onAssign(sel, t.id)}
                    style={{ ...btn, border: 0, textAlign: "left", display: "flex", justifyContent: "space-between", gap: 8, background: on ? "rgba(0,120,212,.28)" : "transparent", padding: "5px 8px" }}>
                    <span>{t.label}</span>{usedBy.length > 0 && <span style={{ color: C.faint, fontSize: 10.5 }}>{usedBy.join(" ")}</span>}
                  </button>
                );
              })}
              <button role="radio" aria-checked={!state.slots[sel]} onClick={() => onAssign(sel, "")}
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

export interface QuickWheelProps {
  state: QuickState;
  initialMode: "pick" | "edit";
  /** 기능을 골랐다 — 화면을 연다. */
  onOpen: (targetId: string) => void;
  /** 닫혔다. cancelled=true 면 아무것도 고르지 않고 닫은 것. */
  onClose: (cancelled: boolean) => void;
  onAssign: (slot: QuickSlot, target: string) => void;
  onReset: () => void;
  onKeybindings: () => void;
}

export const QuickWheel: React.FC<QuickWheelProps> = ({ state, initialMode, onOpen, onClose, onAssign, onReset, onKeybindings }) => {
  const [mode, setMode] = useState<"pick" | "edit">(initialMode);
  const [selected, setSelected] = useState<QuickSlot | null>(initialMode === "edit" ? "up" : null);
  const ref = useRef({ mode, selected, state });
  ref.current = { mode, selected, state };

  const pick = (slot: QuickSlot) => {
    const id = ref.current.state.slots[slot];
    if (!id) { setMode("edit"); setSelected(slot); return; }
    onOpen(id);
    onClose(false);
  };

  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const onKey = (e: KeyboardEvent) => {
      const { mode: m, selected: s } = ref.current;
      //: 휠이 떠 있는 동안 Esc·방향키는 휠만 받는다(뒤에 있는 결정 창·다이어그램이 같이 반응하지 않게).
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose(true); return; }
      if (m === "pick") {
        const slot = slotForKey(e.key, e.shiftKey);
        if (slot && !e.altKey && !e.ctrlKey && !e.metaKey) { e.preventDefault(); e.stopPropagation(); pick(slot); return; }
        if ((e.key === "e" || e.key === "E") && !e.altKey && !e.ctrlKey && !e.metaKey) { e.preventDefault(); setMode("edit"); setSelected("up"); }
        return;
      }
      if (e.key.startsWith("Arrow") || e.key === "Tab") {
        //: 편집 중에는 방향키·Tab 으로 칸을 옮긴다(목록 안 버튼에 포커스가 있으면 Tab 은 원래대로).
        if (e.key === "Tab" && (e.target as HTMLElement | null)?.closest?.("[data-testid=quick-wheel-editor]")) return;
        e.preventDefault(); e.stopPropagation();
        setSelected(moveSelection(s ?? "up", e.key));
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  return <QuickWheelView state={state} mode={mode} selected={selected}
    onSlot={(slot) => { if (ref.current.mode === "pick") pick(slot); else setSelected(slot); }}
    onAssign={onAssign} onMode={(next) => { setMode(next); if (next === "edit" && !ref.current.selected) setSelected("up"); }}
    onReset={onReset} onKeybindings={onKeybindings} onClose={() => onClose(true)} />;
};

export default QuickWheel;
