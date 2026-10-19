/**
 * ReCoder — dry run 다이어그램(2.0.34)
 *
 * 진입점(API 경로·화면 동작·실행 파일)을 고르면, 코드를 실행하지 않고 호출 그래프를 따라 한 단계씩 지나간다.
 *   - 지금 단계는 강조, 지난 곳은 진하게, 아직 안 간 곳은 흐리게
 *   - 노드마다 하는 일(DB·외부 API·AWS·비밀값·파일·명령 실행), 바뀐 함수(적용 전 미리 보기), 보안 발견
 *   - 선: 조건·반복·오류 처리로 갈리는 호출은 주황 점선, 이름만으로 고른(추정) 호출은 회색 점선
 * 정적 분석이라 실제 실행 순서·값과 다를 수 있다는 것을 화면에 함께 적는다.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BRANCH_LABEL, DryRunStep, EFFECT_LABEL, EffectKind, FlowGraph, FlowNode,
  dryRun, findingsByNode, layoutDryRun,
} from "./dryRunModel";

const C = {
  bg: "var(--vscode-editor-background, #0c121a)",
  panel: "var(--vscode-input-background, #15202e)",
  line: "var(--vscode-panel-border, #2c3d4f)",
  ink: "var(--vscode-foreground, #eaf1f8)",
  dim: "var(--vscode-descriptionForeground, #93a8bc)",
  cyan: "#3fd6de",
  amber: "#f7b955",
  red: "#ff6573",
  green: "#3ddc97",
  violet: "#b48cff",
  edge: "var(--vscode-charts-blue, #5b95ff)",
};
const EFFECT_COLOR: Record<EffectKind, string> = {
  db: "#b48cff", http: "#5b95ff", aws: "#f7b955", secret: "#ff6573", fs: "#3ddc97", exec: "#ff6573",
};
const EFFECT_ORDER: EffectKind[] = ["db", "http", "aws", "secret", "fs", "exec"];

export interface FlowFinding { rule?: string; label?: string; file: string; line: number; message: string; severity?: string; }

export interface FlowDryRunProps {
  graph: FlowGraph;
  /** 적용 전 미리 보기: 바뀐 노드 */
  changed?: Record<string, "added" | "modified">;
  removed?: Array<{ id: string; file: string; name: string }>;
  /** 바뀐 함수를 지나가는 진입점 — 먼저 보여 준다 */
  affected?: string[];
  findings?: FlowFinding[];
  /** 보안 발견의 파일 경로가 이 폴더 기준이면 붙인다 */
  findingsPrefix?: string;
  onOpen?: (file: string, line: number) => void;
  /** 재생 간격(ms) — 시험용 */
  intervalMs?: number;
}

/**
 * 선 모양: 아래로 이어지면 부드러운 곡선, 같은 열에서 여러 줄을 건너뛰면 옆으로 비켜 가 중간 노드를 가리지 않는다.
 * 위로 되돌아가는 호출(재귀·이미 지남)은 오른쪽으로 크게 돌아간다.
 */
export function edgePath(a: { x: number; y: number }, b: { x: number; y: number }, index: number): { path: string; mid: { x: number; y: number } } {
  const sx = a.x, sy = a.y + 18, ex = b.x, ey = b.y - 22;
  if (ey <= sy) {
    const side = 95 + (index % 3) * 12;
    const path = `M${sx + 75},${a.y} C${sx + 75 + side},${a.y} ${ex + 75 + side},${b.y} ${ex + 75},${b.y}`;
    return { path, mid: { x: Math.max(sx, ex) + 75 + side * 0.75, y: (a.y + b.y) / 2 } };
  }
  const skips = Math.abs(sx - ex) < 10 && ey - sy > 110;
  const bend = skips ? (index % 2 ? -1 : 1) * 88 : 0;
  const k = Math.min(40, (ey - sy) / 2);
  const path = `M${sx},${sy} C${sx + bend},${sy + k} ${ex + bend},${ey - k} ${ex},${ey}`;
  return { path, mid: { x: (sx + ex) / 2 + bend * 0.75, y: (sy + ey) / 2 } };
}

const nodeLabel = (n: FlowNode) => (n.cls ? `${n.cls}.${n.name}` : n.name);
const shortFile = (f: string) => { const parts = f.split("/"); return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : f; };

export const FlowDryRun: React.FC<FlowDryRunProps> = ({ graph, changed, removed, affected, findings, findingsPrefix, onOpen, intervalMs = 1100 }) => {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(540);
  const byId = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph]);
  const affectedSet = useMemo(() => new Set(affected ?? []), [affected]);

  // 진입점 목록: 변경이 지나가는 곳 → 경로 → 화면 동작 → 실행. 진입점이 없으면 함수에서 시작.
  const choices = useMemo(() => {
    const entries = graph.entries.map((e) => ({ id: e.id, label: e.label, group: affectedSet.has(e.id) ? "이번 변경이 지나가는 곳" : e.kind === "route" ? "API 경로" : e.kind === "ui" ? "화면 동작" : "실행" }));
    const extra = graph.entries.length ? [] : graph.nodes.filter((n) => n.kind === "function" || n.kind === "handler")
      .sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line)
      .slice(0, 300)
      .map((n) => ({ id: n.id, label: `${nodeLabel(n)} — ${shortFile(n.file)}`, group: "함수에서 시작" }));
    // 바뀐 함수가 어느 진입점에서도 닿지 않으면 그 함수에서 바로 시작할 수 있게 한다
    const unreached = changed && !(affected ?? []).length
      ? Object.keys(changed).filter((id) => byId.get(id)?.kind === "function" || byId.get(id)?.kind === "handler")
        .map((id) => ({ id, label: `${nodeLabel(byId.get(id)!)} — ${shortFile(byId.get(id)!.file)}`, group: "바뀐 함수에서 시작" }))
      : [];
    const order = ["이번 변경이 지나가는 곳", "바뀐 함수에서 시작", "API 경로", "화면 동작", "실행", "함수에서 시작"];
    return [...entries, ...unreached, ...extra].sort((a, b) => order.indexOf(a.group) - order.indexOf(b.group));
  }, [graph, affectedSet, affected, changed, byId]);

  const [entry, setEntry] = useState<string>(() => choices[0]?.id ?? "");
  useEffect(() => { if (!choices.some((c) => c.id === entry)) setEntry(choices[0]?.id ?? ""); }, [choices, entry]);

  const result = useMemo(() => (entry ? dryRun(graph, entry, { changed }) : null), [graph, entry, changed]);
  const steps = result?.steps ?? [];
  const [step, setStep] = useState(0);
  const [playing, setPlaying] = useState(false);
  useEffect(() => { setStep(0); setPlaying(false); }, [entry, graph]);
  useEffect(() => {
    if (!playing) return undefined;
    if (step >= steps.length - 1) { setPlaying(false); return undefined; }
    const t = setTimeout(() => setStep((s) => Math.min(s + 1, steps.length - 1)), intervalMs);
    return () => clearTimeout(t);
  }, [playing, step, steps.length, intervalMs]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(() => setWidth(Math.max(320, el.clientWidth - 4)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const layout = useMemo(() => (result ? layoutDryRun(result, width) : null), [result, width]);
  const scrollRef = useRef<HTMLDivElement>(null);
  // 지금 단계의 노드가 보이게 스크롤한다(긴 흐름에서 재생하면 화면 밖으로 나갔다).
  useEffect(() => {
    const box = scrollRef.current;
    const at = layout && steps[step] ? layout.pos.get(steps[step].node) : undefined;
    if (!box || !at) return;
    if (at.y - 50 < box.scrollTop || at.y + 50 > box.scrollTop + box.clientHeight) {
      const reduce = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      box.scrollTo?.({ top: Math.max(0, at.y - box.clientHeight / 2), behavior: reduce ? "auto" : "smooth" });
    }
  }, [step, layout, steps]);
  const findingMap = useMemo(() => findingsByNode(graph, findings ?? [], findingsPrefix ?? ""), [graph, findings, findingsPrefix]);

  // 지금까지 지난 노드·선
  const visited = new Set<string>();
  const walkedEdges = new Set<string>();
  steps.slice(0, step + 1).forEach((s) => { visited.add(s.node); if (s.from) walkedEdges.add(`${s.from} ${s.node}`); });
  const current = steps[step];
  const edges = useMemo(() => {
    const seen = new Map<string, DryRunStep>();
    for (const s of steps) if (s.from && !seen.has(`${s.from} ${s.node}`)) seen.set(`${s.from} ${s.node}`, s);
    return [...seen.values()];
  }, [steps]);

  const onKey = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "ArrowRight") { e.preventDefault(); setPlaying(false); setStep((s) => Math.min(s + 1, steps.length - 1)); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); setPlaying(false); setStep((s) => Math.max(s - 1, 0)); }
    else if (e.key === " ") { e.preventDefault(); setPlaying((p) => !p); }
    else if (e.key === "Home") { e.preventDefault(); setPlaying(false); setStep(0); }
  }, [steps.length]);

  const btn: React.CSSProperties = {
    background: "none", border: `1px solid ${C.line}`, color: C.ink, fontSize: 11.5,
    borderRadius: 6, padding: "4px 9px", cursor: "pointer", minWidth: 30,
  };

  if (!choices.length) {
    return (
      <div data-flow-empty style={{ fontSize: 11.5, color: C.dim, padding: "10px 0", lineHeight: 1.6 }}>
        dry run 할 수 있는 함수를 찾지 못했습니다. 지원하는 언어는 JavaScript·TypeScript·Python 입니다.
        {graph.notes.map((n, i) => <div key={i}>{n}</div>)}
      </div>
    );
  }

  const currentNode = current ? byId.get(current.node) : undefined;
  const fromNode = current?.from ? byId.get(current.from) : undefined;

  return (
    <div ref={wrapRef} tabIndex={0} onKeyDown={onKey} aria-label="dry run 다이어그램 — 왼쪽·오른쪽 화살표로 한 단계씩, 스페이스로 재생"
      style={{ fontFamily: "var(--vscode-font-family)", color: C.ink, outline: "none" }}>
      {/* 시작점 + 조작 */}
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
        <select aria-label="dry run 시작점" value={entry} onChange={(e) => setEntry(e.target.value)}
          style={{ flex: "1 1 220px", minWidth: 0, background: C.panel, color: C.ink, border: `1px solid ${C.line}`, borderRadius: 6, padding: "5px 6px", fontSize: 11.5 }}>
          {[...new Set(choices.map((c) => c.group))].map((g) => (
            <optgroup key={g} label={g}>
              {choices.filter((c) => c.group === g).map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </optgroup>
          ))}
        </select>
        <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
          <button style={btn} aria-label="처음으로" title="처음으로 (Home)" onClick={() => { setPlaying(false); setStep(0); }}>⏮</button>
          <button style={btn} aria-label="이전 단계" title="이전 단계 (←)" disabled={step <= 0} onClick={() => { setPlaying(false); setStep((s) => Math.max(0, s - 1)); }}>◀</button>
          <button style={{ ...btn, borderColor: C.cyan, color: C.cyan }} aria-label={playing ? "일시정지" : "재생"} title="재생/일시정지 (스페이스)"
            onClick={() => { if (!playing && step >= steps.length - 1) setStep(0); setPlaying((p) => !p); }}>{playing ? "⏸" : "▶"}</button>
          <button style={btn} aria-label="다음 단계" title="다음 단계 (→)" disabled={step >= steps.length - 1} onClick={() => { setPlaying(false); setStep((s) => Math.min(steps.length - 1, s + 1)); }}>▶|</button>
          <span data-flow-progress style={{ fontSize: 11, color: C.dim, marginLeft: 4, whiteSpace: "nowrap" }}>{steps.length ? step + 1 : 0} / {steps.length}</span>
        </div>
      </div>

      {/* 지금 단계 설명 */}
      {current && currentNode && (
        <div data-flow-current style={{ border: `1px solid ${C.cyan}66`, borderLeft: `3px solid ${C.cyan}`, background: C.panel, borderRadius: 8, padding: "8px 10px", marginBottom: 8, fontSize: 11.5, lineHeight: 1.55 }}>
          <div style={{ fontWeight: 700 }}>
            {step + 1}단계 · {nodeLabel(currentNode)}
            <span style={{ color: C.dim, fontWeight: 400 }}> — {currentNode.kind === "route" ? "API 경로" : `${shortFile(currentNode.file)}:${currentNode.line}`}</span>
            {changed?.[currentNode.id] && <Badge color={C.violet}>{changed[currentNode.id] === "added" ? "새로 생김" : "바뀜"}</Badge>}
          </div>
          {fromNode && (
            <div style={{ color: C.dim }}>
              {nodeLabel(fromNode)} {current.line ? `${current.line}행` : ""}에서 호출
              {current.branch ? ` · ${BRANCH_LABEL[current.branch]}` : ""}
              {current.certainty === "inferred" ? " · 이름으로 추정한 연결" : ""}
              {current.revisit === "seen" ? " · 앞에서 이미 지남(다시 들어가지 않음)" : current.revisit === "cycle" ? " · 지금 실행 중인 함수로 되돌아감(재귀)" : ""}
            </div>
          )}
          {currentNode.effects.length > 0 && (
            <ul style={{ margin: "4px 0 0", paddingLeft: 16 }}>
              {currentNode.effects.map((f, i) => (
                <li key={i}><b style={{ color: EFFECT_COLOR[f.kind] }}>{EFFECT_LABEL[f.kind]}</b> {f.detail} <span style={{ color: C.dim }}>({f.line}행)</span></li>
              ))}
            </ul>
          )}
          {(findingMap.get(currentNode.id) ?? []).map((f, i) => (
            <div key={`s${i}`} style={{ color: C.red, marginTop: 3 }}>보안{f.label ? `(${f.label})` : ""} {f.line}행: {f.message}</div>
          ))}
          {onOpen && currentNode.kind !== "route" && (
            <button style={{ ...btn, marginTop: 6, fontSize: 10.5, padding: "2px 8px", color: C.cyan, borderColor: `${C.cyan}55` }}
              onClick={() => onOpen(currentNode.file, currentNode.line)}>코드 열기</button>
          )}
        </div>
      )}

      {/* 다이어그램 */}
      {layout && (
        <div ref={scrollRef} style={{ overflow: "auto", maxHeight: 520 }}>
          <div style={{
            position: "relative", width, height: layout.height, minHeight: 180,
            background: `linear-gradient(${C.line}22 1px, transparent 1px), linear-gradient(90deg, ${C.line}22 1px, transparent 1px)`,
            backgroundSize: "26px 26px", border: `1px solid ${C.line}`, borderRadius: 10, overflow: "hidden",
          }}>
            <svg width={width} height={layout.height} style={{ position: "absolute", inset: 0, overflow: "visible" }} aria-hidden="true">
              <defs>
                <marker id="rfd-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L6,3.5 L0,7 Z" fill={C.cyan} /></marker>
                <marker id="rfd-arrow-dim" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L6,3.5 L0,7 Z" fill="#7f8b99" /></marker>
              </defs>
              {layout.bands.map((b, i) => (
                <text key={i} x={8} y={b.y - 36} fill={C.dim} fontSize={8.5} style={{ letterSpacing: 0.6 }}>{b.label}</text>
              ))}
              {edges.map((s, i) => {
                const a = layout.pos.get(s.from!), b = layout.pos.get(s.node);
                if (!a || !b) return null;
                const walked = walkedEdges.has(`${s.from} ${s.node}`);
                const isNow = current && current.from === s.from && current.node === s.node;
                const color = s.certainty === "inferred" ? "#7f8b99" : s.branch ? C.amber : walked ? C.cyan : C.edge;
                const d = edgePath(a, b, i);
                return (
                  <g key={i}>
                    <path d={d.path} fill="none"
                      stroke={color} strokeOpacity={walked ? 0.95 : 0.25} strokeWidth={isNow ? 3 : walked ? 2 : 1.3}
                      strokeDasharray={s.certainty === "inferred" ? "2 4" : s.branch ? "6 4" : undefined}
                      markerEnd={walked ? "url(#rfd-arrow)" : "url(#rfd-arrow-dim)"} />
                    {s.branch && walked && (
                      <text x={d.mid.x + 4} y={d.mid.y} fill={C.amber} fontSize={8.5}>{BRANCH_LABEL[s.branch]}</text>
                    )}
                  </g>
                );
              })}
            </svg>
            {[...layout.pos.entries()].map(([id, p]) => {
              const n = byId.get(id);
              if (!n) return null;
              const isCurrent = current?.node === id;
              const done = visited.has(id);
              const kinds = EFFECT_ORDER.filter((k) => n.effects.some((f) => f.kind === k));
              const change = changed?.[id];
              const sec = findingMap.get(id) ?? [];
              const firstStep = steps.findIndex((s) => s.node === id);
              return (
                <div key={id} data-flow-node={id} data-state={isCurrent ? "current" : done ? "visited" : "future"}
                  role="button" tabIndex={-1} title={`${nodeLabel(n)} — ${n.file}:${n.line}`}
                  onClick={() => { setPlaying(false); if (firstStep >= 0) setStep(firstStep); }}
                  onDoubleClick={() => { if (onOpen && n.kind !== "route") onOpen(n.file, n.line); }}
                  style={{
                    position: "absolute", left: p.x, top: p.y, transform: "translate(-50%,-50%)", zIndex: 2, cursor: "pointer",
                    opacity: done ? 1 : 0.35, transition: "opacity .25s",
                  }}>
                  {(change || sec.length > 0) && (
                    <div style={{ position: "absolute", top: -10, left: 8, display: "flex", gap: 3 }}>
                      {change && <Badge color={C.violet} solid>{change === "added" ? "새로" : "바뀜"}</Badge>}
                      {sec.length > 0 && <Badge color={C.red} solid>보안 {sec.length}</Badge>}
                    </div>
                  )}
                  <div style={{
                    border: `1px solid ${isCurrent ? C.cyan : change ? C.violet : C.line}`, borderRadius: 10, background: C.panel,
                    boxShadow: isCurrent ? `0 0 16px ${C.cyan}80` : "0 4px 12px rgba(0,0,0,.3)",
                    padding: "6px 9px", width: 150, boxSizing: "border-box",
                  }}>
                    <div style={{ fontSize: 11.5, fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {n.kind === "route" ? "🛣 " : n.kind === "main" ? "▶ " : ""}{nodeLabel(n)}
                    </div>
                    <div style={{ fontSize: 9, color: C.dim, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {n.kind === "route" ? "API 경로" : `${shortFile(n.file)}:${n.line}`}
                    </div>
                    {kinds.length > 0 && (
                      <div style={{ display: "flex", gap: 3, flexWrap: "wrap", marginTop: 3 }}>
                        {kinds.map((k) => <Badge key={k} color={EFFECT_COLOR[k]}>{EFFECT_LABEL[k]}</Badge>)}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* 범례 */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 8, fontSize: 10, color: C.dim }}>
        <span style={{ display: "flex", alignItems: "center", gap: 5 }}><i style={{ width: 14, height: 2, background: C.cyan, display: "inline-block" }} />지난 호출</span>
        <span style={{ display: "flex", alignItems: "center", gap: 5 }}><i style={{ width: 14, height: 0, borderTop: `2px dashed ${C.amber}`, display: "inline-block" }} />조건·반복·오류 처리로 갈림</span>
        <span style={{ display: "flex", alignItems: "center", gap: 5 }}><i style={{ width: 14, height: 0, borderTop: "2px dotted #7f8b99", display: "inline-block" }} />이름으로 추정한 연결</span>
        {changed && <span style={{ display: "flex", alignItems: "center", gap: 5 }}><i style={{ width: 9, height: 9, border: `1px solid ${C.violet}`, display: "inline-block", borderRadius: 2 }} />이번 변경</span>}
      </div>

      {/* 요약 */}
      {result && (
        <div data-flow-summary style={{ marginTop: 8, fontSize: 11, color: C.dim, lineHeight: 1.6 }}>
          함수 {result.summary.nodes}개 · 파일 {result.summary.files}개
          {EFFECT_ORDER.filter((k) => result.summary.effects[k]).map((k) => ` · ${EFFECT_LABEL[k]} ${result.summary.effects[k]}곳`).join("")}
          {result.summary.branches ? ` · 갈림 ${result.summary.branches}` : ""}
          {result.summary.inferred ? ` · 추정 연결 ${result.summary.inferred}` : ""}
          {changed ? ` · 바뀐 함수 ${result.summary.changed}` : ""}
          {result.truncated === "steps" && <div style={{ color: C.amber }}>단계가 너무 많아 300단계까지만 보여 줍니다.</div>}
          {result.truncated === "depth" && <div style={{ color: C.amber }}>호출이 너무 깊어 24단계 아래는 생략했습니다.</div>}
        </div>
      )}
      {removed && removed.length > 0 && (
        <div style={{ marginTop: 6, fontSize: 11, color: C.amber }}>
          이번 변경으로 없어지는 함수: {removed.slice(0, 8).map((r) => r.name).join(", ")}{removed.length > 8 ? ` 외 ${removed.length - 8}개` : ""}
        </div>
      )}

      {/* 단계 기록 */}
      <ol data-flow-log style={{ listStyle: "none", padding: 0, margin: "10px 0 0", maxHeight: 220, overflow: "auto", fontSize: 11, lineHeight: 1.6 }}>
        {steps.slice(0, step + 1).map((s) => {
          const n = byId.get(s.node);
          if (!n) return null;
          const kinds = EFFECT_ORDER.filter((k) => n.effects.some((f) => f.kind === k));
          return (
            <li key={s.index} onClick={() => { setPlaying(false); setStep(s.index); }}
              style={{ paddingLeft: Math.min(s.depth, 10) * 12, cursor: "pointer", color: s.index === step ? C.cyan : C.ink, opacity: s.revisit ? 0.6 : 1 }}>
              {s.index + 1}. {nodeLabel(n)}
              <span style={{ color: C.dim }}> {n.kind === "route" ? "" : `${shortFile(n.file)}:${n.line}`}{s.branch ? ` · ${BRANCH_LABEL[s.branch]}` : ""}{s.revisit === "seen" ? " · 이미 지남" : s.revisit === "cycle" ? " · 재귀" : ""}</span>
              {kinds.map((k) => <span key={k} style={{ color: EFFECT_COLOR[k] }}> · {EFFECT_LABEL[k]}</span>)}
              {changed?.[s.node] && <span style={{ color: C.violet }}> · 바뀜</span>}
            </li>
          );
        })}
      </ol>

      <div style={{ marginTop: 8, fontSize: 10, color: C.dim, lineHeight: 1.5 }}>
        코드를 실행하지 않는 정적 분석입니다. 실제 실행에서는 조건에 따라 일부 단계만 지나가고, 콜백·동적 호출은 빠지거나 추정으로 표시됩니다.
        {graph.notes.map((n, i) => <div key={i}>{n}</div>)}
      </div>
    </div>
  );
};

const Badge: React.FC<{ color: string; solid?: boolean; children: React.ReactNode }> = ({ color, solid, children }) => (
  <span style={{
    display: "inline-block", fontSize: 8.5, fontWeight: 700, padding: "0 5px", borderRadius: 8, marginLeft: solid ? 0 : 0,
    background: solid ? color : `${color}22`, color: solid ? "#10141a" : color, border: solid ? "none" : `1px solid ${color}66`, whiteSpace: "nowrap",
  }}>{children}</span>
);

export default FlowDryRun;
