/**
 * 구조 다이어그램(2.0.37) — 역할 레인 · 그룹 상자(폴더/파일) · 노드 · 선을 SVG 로 그린다.
 * 아키텍처 맵과 dry run 이 같이 쓴다. 배치는 structureLayout.ts.
 *
 * 읽는 법: 왼쪽에서 오른쪽으로 화면 → 입구(API) → 처리 → 데이터 → 외부. 상자는 폴더(또는 파일).
 * 노드에 마우스를 올리면 그 노드와 이어진 선만 진하게 남는다.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { Box, LEdge, LNode, Role, edgeRoute, fit, groupColor, layoutLanes, LaneOptions } from "./structureLayout";

export interface DiagramBadge { text: string; color: string; solid?: boolean; }
export interface DiagramNode extends LNode {
  title: string;
  sub?: string;
  accent?: string;
  badges?: DiagramBadge[];
  state?: "current" | "visited" | "future" | "normal";
  /** 렌더 시험·스크립트가 찾는 표시 */
  attrs?: Record<string, string>;
  tooltip?: string;
}
export interface DiagramEdge extends LEdge {
  color?: string;
  style?: "solid" | "branch" | "inferred" | "effect";
  label?: string;
  weight?: number;
  state?: "walked" | "now" | "idle";
}

const C = {
  bg: "var(--vscode-editor-background, #0c121a)",
  panel: "var(--vscode-input-background, #15202e)",
  line: "var(--vscode-panel-border, #2c3d4f)",
  ink: "var(--vscode-foreground, #eaf1f8)",
  dim: "var(--vscode-descriptionForeground, #93a8bc)",
  cyan: "#3fd6de",
  edge: "#5b95ff",
};
const LANE_TINT: Record<Role, string> = {
  ui: "#3fd6de", entry: "#5b95ff", logic: "#b48cff", data: "#f7b955", support: "#7f8b99", other: "#7f8b99", external: "#ff8f6b",
};

export interface StructureDiagramProps {
  nodes: DiagramNode[];
  edges: DiagramEdge[];
  onNodeClick?: (id: string) => void;
  onNodeDoubleClick?: (id: string) => void;
  /** 이 노드가 보이게 스크롤(dry run 재생) */
  focusId?: string;
  maxHeight?: number;
  layout?: LaneOptions;
  ariaLabel?: string;
}

export const StructureDiagram: React.FC<StructureDiagramProps> = ({ nodes, edges, onNodeClick, onNodeDoubleClick, focusId, maxHeight = 560, layout: options, ariaLabel }) => {
  const layout = useMemo(() => layoutLanes(nodes, edges, options), [nodes, edges, options]);
  const [scale, setScale] = useState(1);
  const [hover, setHover] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);

  // 처음 그릴 때 폭에 맞춘다(너무 작게는 줄이지 않는다 — 글자가 안 읽히면 의미가 없다).
  useEffect(() => {
    const box = scrollRef.current;
    if (!box || !layout.width) return;
    const fitScale = Math.min(1, Math.max(0.75, (box.clientWidth - 8) / layout.width));
    setScale(Number.isFinite(fitScale) && fitScale > 0 ? fitScale : 1);
  }, [layout.width]);

  useEffect(() => {
    const box = scrollRef.current;
    const at = focusId ? layout.nodes.get(focusId) : undefined;
    if (!box || !at) return;
    const cx = (at.x + at.w / 2) * scale, cy = (at.y + at.h / 2) * scale;
    const outX = cx < box.scrollLeft + 40 || cx > box.scrollLeft + box.clientWidth - 40;
    const outY = cy < box.scrollTop + 40 || cy > box.scrollTop + box.clientHeight - 40;
    if (outX || outY) {
      const reduce = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      box.scrollTo?.({ left: Math.max(0, cx - box.clientWidth / 2), top: Math.max(0, cy - box.clientHeight / 2), behavior: reduce ? "auto" : "smooth" });
    }
  }, [focusId, layout, scale]);

  const touching = (e: LEdge) => !hover || e.from === hover || e.to === hover;
  const neighbors = useMemo(() => {
    const set = new Set<string>();
    if (hover) for (const e of edges) { if (e.from === hover) set.add(e.to); if (e.to === hover) set.add(e.from); }
    return set;
  }, [hover, edges]);
  const W = Math.max(layout.width, 200), H = Math.max(layout.height, 120);
  //: 선이 많으면 기본은 흐리게 — 노드에 마우스를 올리면 그 노드의 선만 진해진다.
  const busy = edges.length > 40;
  const zoomBtn: React.CSSProperties = { background: "none", border: `1px solid ${C.line}`, color: C.ink, borderRadius: 5, fontSize: 11, padding: "1px 8px", cursor: "pointer" };

  return (
    <div data-structure-diagram>
      <div style={{ display: "flex", gap: 4, alignItems: "center", justifyContent: "flex-end", marginBottom: 4 }}>
        <span style={{ fontSize: 10, color: C.dim, marginRight: "auto" }}>{Math.round(scale * 100)}%</span>
        <button style={zoomBtn} aria-label="축소" onClick={() => setScale((s) => Math.max(0.4, +(s - 0.15).toFixed(2)))}>−</button>
        <button style={zoomBtn} aria-label="확대" onClick={() => setScale((s) => Math.min(1.8, +(s + 0.15).toFixed(2)))}>+</button>
        <button style={zoomBtn} aria-label="화면에 맞추기" onClick={() => {
          const box = scrollRef.current;
          if (box) setScale(Math.min(1, Math.max(0.4, (box.clientWidth - 8) / W)));
        }}>맞춤</button>
      </div>
      <div ref={scrollRef} style={{ overflow: "auto", maxHeight, border: `1px solid ${C.line}`, borderRadius: 10, background: C.bg }}>
        <svg role="img" aria-label={ariaLabel ?? "구조 다이어그램"} width={W * scale} height={H * scale} viewBox={`0 0 ${W} ${H}`}
          style={{ display: "block", fontFamily: "var(--vscode-font-family)" }} onMouseLeave={() => setHover(null)}>
          <defs>
            {["#5b95ff", "#3fd6de", "#f7b955", "#7f8b99", "#ff8f6b", "#b48cff", "#ff6573", "#3ddc97"].map((c) => (
              <marker key={c} id={`sd-arrow-${c.slice(1)}`} markerUnits="userSpaceOnUse" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto">
                <path d="M0,0 L8,4.5 L0,9 Z" fill={c} />
              </marker>
            ))}
          </defs>

          {/* 레인 */}
          {layout.lanes.map((lane) => (
            <g key={lane.role} data-lane={lane.role}>
              <rect x={lane.x} y={0} width={lane.w} height={H} rx={8} fill={LANE_TINT[lane.role]} fillOpacity={0.045} />
              <text x={lane.x + 10} y={18} fontSize={10.5} fontWeight={700} fill={LANE_TINT[lane.role]} style={{ letterSpacing: 0.4 }}>{lane.label}</text>
            </g>
          ))}

          {/* 그룹 상자 */}
          {layout.groups.map((g) => {
            const color = g.role === "external" ? LANE_TINT.external : groupColor(g.key);
            return (
              <g key={`${g.role}:${g.key}`} data-group={g.key}>
                <rect x={g.x + 4} y={g.y} width={g.w - 8} height={g.h} rx={8} fill={color} fillOpacity={0.06} stroke={color} strokeOpacity={0.45} />
                <text x={g.x + 12} y={g.y + 14} fontSize={9.5} fill={color} style={{ fontWeight: 600 }}>
                  {fit(g.label, g.w - 50, 9.5)}{g.count > 1 ? ` · ${g.count}` : ""}
                </text>
              </g>
            );
          })}

          {/* 선 */}
          {edges.map((e, i) => {
            const a = layout.nodes.get(e.from), b = layout.nodes.get(e.to);
            if (!a || !b || e.from === e.to) return null;
            const r = edgeRoute(a as Box, b as Box, i);
            const color = e.color ?? C.edge;
            const lit = touching(e);
            const idle = e.state === "idle";
            const opacity = !lit ? 0.06 : idle ? 0.22 : e.state === "now" ? 1 : hover ? 0.95 : busy ? (r.backward ? 0.18 : 0.35) : r.backward ? 0.45 : 0.75;
            const dash = e.style === "inferred" ? "2 4" : e.style === "branch" ? "6 4" : e.style === "effect" ? "3 3" : r.backward ? "5 4" : undefined;
            return (
              <g key={`${e.from}>${e.to}:${i}`} data-edge={`${e.from}>${e.to}`} data-edge-state={e.state ?? "solid"}>
                <path d={r.d} fill="none" stroke={color} strokeOpacity={opacity}
                  strokeWidth={e.state === "now" ? 2.6 : Math.min(3.5, (r.backward ? 1 : 1.3) + (e.weight ? Math.log2(e.weight) * 0.6 : 0))}
                  strokeDasharray={dash} markerEnd={`url(#sd-arrow-${color.replace("#", "")})`} />
                {e.label && !idle && (e.state === "now" || (hover !== null && lit)) && (
                  <text x={r.mid.x + 3} y={r.mid.y - 3} fontSize={8.5} fill={color}>{e.label}</text>
                )}
              </g>
            );
          })}

          {/* 노드 */}
          {[...layout.nodes.values()].map((p) => {
            const n = byId.get(p.id);
            if (!n) return null;
            const state = n.state ?? "normal";
            const accent = n.accent ?? (state === "current" ? C.cyan : C.line);
            const dim = state === "future" || (hover !== null && hover !== n.id && !neighbors.has(n.id));
            return (
              <g key={n.id} {...(n.attrs ?? {})} data-state={state} transform={`translate(${p.x},${p.y})`}
                opacity={dim ? 0.38 : 1} style={{ cursor: onNodeClick ? "pointer" : "default", transition: "opacity .2s" }}
                onMouseEnter={() => setHover(n.id)} onClick={() => onNodeClick?.(n.id)} onDoubleClick={() => onNodeDoubleClick?.(n.id)}>
                <title>{n.tooltip ?? n.title}</title>
                {state === "current" && <rect x={-4} y={-4} width={p.w + 8} height={p.h + 8} rx={12} fill={C.cyan} fillOpacity={0.18} />}
                <rect width={p.w} height={p.h} rx={9} style={{ fill: C.panel }} stroke={accent} strokeWidth={state === "current" ? 2 : 1} />
                <text x={9} y={p.h / 2 - (n.sub ? 3 : -4)} fontSize={11} fontWeight={600} style={{ fill: C.ink }}>{fit(n.title, p.w - 16)}</text>
                {n.sub && <text x={9} y={p.h / 2 + 11} fontSize={8.8} style={{ fill: C.dim }}>{fit(n.sub, p.w - 16, 8.8)}</text>}
                {(n.badges ?? []).length > 0 && (() => {
                  let bx = 8;
                  return (n.badges ?? []).map((b, i) => {
                    const w = Math.max(18, fit(b.text, 80, 8).length * 5.2 + (/[ㄱ-힝]/.test(b.text) ? b.text.length * 3 : 0) + 8);
                    const el = (
                      <g key={i} transform={`translate(${bx},-7)`}>
                        <rect width={w} height={12} rx={6} fill={b.color} fillOpacity={b.solid ? 1 : 0.2} stroke={b.color} strokeOpacity={0.6} />
                        <text x={w / 2} y={9} fontSize={8} textAnchor="middle" fontWeight={700} style={{ fill: b.solid ? "#10141a" : b.color }}>{b.text}</text>
                      </g>
                    );
                    bx += w + 3;
                    return el;
                  });
                })()}
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
};

export default StructureDiagram;
