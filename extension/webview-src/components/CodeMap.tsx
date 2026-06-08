/**
 * ReCoder — CodeMap component (구조 지도 / Architecture & Code Map)
 *
 * 2단계 줌:
 *   - 프로젝트(폴더) 뷰: 파일 노드 + 내부 import 엣지. 계층(entry→service→data→other)
 *     으로 위에서 아래로 배치. 고립(빨강)·과부하(노랑)를 색으로.
 *   - 파일 뷰: 파일을 누르면 그 안의 함수/메서드 호출 그래프.
 *
 * 관통 원칙: 선은 사실(import/call), 색·위치는 해석(고립/과부하/계층).
 * 데이터는 Core 정적 분석(/api/map/*)에서 옴 — LLM 아님.
 */
import React, { useState, useCallback, useEffect, useRef } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

// ── Types (Core 응답과 1:1) ──────────────────────────────────────────────────
interface Finding {
  severity: "bad" | "warn";
  kind: string;
  node: string;
  title: string;
  detail: string;
  fix: string;
}
interface ProjNode {
  id: string; name: string; module: string; layer: string;
  in_degree: number; out_degree: number; flags: string[];
}
interface FnNode {
  id: string; name: string; cls: string | null;
  in_degree: number; out_degree: number; flags: string[];
}
interface Edge { from: string; to: string; }
interface ProjectGraph {
  kind: "project"; root: string; files_scanned: number;
  nodes: ProjNode[]; edges: Edge[]; findings: Finding[];
}
interface FileGraph {
  kind: "file"; path: string; name: string; functions_scanned: number;
  nodes: FnNode[]; edges: Edge[]; findings: Finding[];
}

// ── 색 ───────────────────────────────────────────────────────────────────────
const C = {
  bg: "var(--vscode-editor-background, #0c121a)",
  panel: "var(--vscode-input-background, #15202e)",
  line: "var(--vscode-panel-border, #2c3d4f)",
  ink: "var(--vscode-foreground, #eaf1f8)",
  dim: "var(--vscode-descriptionForeground, #93a8bc)",
  cyan: "#3fd6de",
  amber: "#f7b955",
  red: "#ff6573",
  edge: "var(--vscode-charts-blue, #5b95ff)",
};

const LAYER_ORDER = ["entry", "service", "data", "other"];
const LAYER_LABEL: Record<string, string> = {
  entry: "entry · 입구", service: "service · 처리", data: "data · 저장", other: "기타",
};

interface Pos { x: number; y: number; }

// ── 레이아웃 계산 ────────────────────────────────────────────────────────────
function layoutProject(nodes: ProjNode[], width: number): { pos: Map<string, Pos>; height: number; rows: string[] } {
  const rows = LAYER_ORDER.filter((l) => nodes.some((n) => n.layer === l));
  const topPad = 34, rowH = 86;
  const pos = new Map<string, Pos>();
  rows.forEach((layer, li) => {
    const y = topPad + li * rowH;
    const ns = nodes.filter((n) => n.layer === layer);
    ns.forEach((n, ni) => {
      const x = ((ni + 1) / (ns.length + 1)) * width;
      pos.set(n.id, { x, y });
    });
  });
  return { pos, height: topPad + rows.length * rowH + 16, rows };
}

function layoutGrid(nodes: { id: string }[], width: number, cols: number): { pos: Map<string, Pos>; height: number } {
  const topPad = 30, cellH = 78;
  const pos = new Map<string, Pos>();
  nodes.forEach((n, i) => {
    const r = Math.floor(i / cols), c = i % cols;
    const x = ((c + 1) / (cols + 1)) * width;
    const y = topPad + r * cellH;
    pos.set(n.id, { x, y });
  });
  const rowCount = Math.ceil(nodes.length / cols) || 1;
  return { pos, height: topPad + rowCount * cellH + 12 };
}

// ── 노드 칩 ──────────────────────────────────────────────────────────────────
const NodeChip: React.FC<{
  x: number; y: number; title: string; sub?: string;
  flags: string[]; degree?: number; onClick?: () => void; clickable?: boolean;
}> = ({ x, y, title, sub, flags, degree, onClick, clickable }) => {
  const orphan = flags.includes("orphan");
  const over = flags.includes("overloaded");
  const accent = orphan ? C.red : over ? C.amber : C.line;
  const flagLabel = orphan ? "고립" : over ? "과부하" : "";
  return (
    <div
      onClick={onClick}
      title={clickable ? "열기" : undefined}
      style={{
        position: "absolute", left: x, top: y, transform: "translate(-50%,-50%)",
        cursor: clickable ? "pointer" : "default", zIndex: 2,
      }}
    >
      {flagLabel && (
        <div style={{
          position: "absolute", top: -9, left: 8, fontSize: 8.5, fontWeight: 700,
          padding: "1px 6px", borderRadius: 10, whiteSpace: "nowrap",
          background: orphan ? C.red : C.amber, color: "#10141a",
        }}>{flagLabel}</div>
      )}
      <div style={{
        display: "flex", alignItems: "center", gap: 7,
        border: `1px solid ${accent}`, borderRadius: 10,
        background: C.panel,
        boxShadow: over || orphan ? `0 0 14px ${accent}40` : "0 4px 12px rgba(0,0,0,.3)",
        padding: "6px 9px", maxWidth: 150,
      }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 11.5, fontWeight: 600, color: C.ink, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 130 }}>{title}</div>
          {sub && <div style={{ fontSize: 9, color: C.dim, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 130 }}>{sub}</div>}
        </div>
      </div>
      {typeof degree === "number" && degree > 0 && (
        <div style={{
          position: "absolute", bottom: -7, right: -6, fontSize: 8.5,
          background: C.bg, border: `1px solid ${over ? C.amber : C.line}`,
          color: over ? C.amber : C.dim, borderRadius: 10, padding: "0 6px",
        }}>in {degree}</div>
      )}
    </div>
  );
};

// ── 캔버스 (노드 + 엣지) ─────────────────────────────────────────────────────
const Canvas: React.FC<{
  pos: Map<string, Pos>; edges: Edge[]; height: number; width: number;
  children: React.ReactNode; bands?: { label: string; y: number }[];
}> = ({ pos, edges, height, width, children, bands }) => (
  <div style={{
    position: "relative", width, height, minHeight: 180,
    background: `linear-gradient(${C.line}22 1px, transparent 1px), linear-gradient(90deg, ${C.line}22 1px, transparent 1px)`,
    backgroundSize: "26px 26px",
    border: `1px solid ${C.line}`, borderRadius: 10, overflow: "hidden",
  }}>
    <svg width={width} height={height} style={{ position: "absolute", inset: 0, overflow: "visible" }}>
      <defs>
        <marker id="rcm-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
          <path d="M0,0 L6,3.5 L0,7 Z" fill={C.cyan} />
        </marker>
      </defs>
      {bands?.map((b, i) => (
        <text key={i} x={8} y={b.y - 18} fill={C.dim} fontSize={8.5} style={{ textTransform: "uppercase", letterSpacing: 0.6 }}>{b.label}</text>
      ))}
      {edges.map((e, i) => {
        const a = pos.get(e.from), b = pos.get(e.to);
        if (!a || !b) return null;
        const over = false;
        return (
          <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
            stroke={C.edge} strokeOpacity={0.5} strokeWidth={1.5} markerEnd="url(#rcm-arrow)" />
        );
      })}
    </svg>
    {children}
  </div>
);

// ── Findings ─────────────────────────────────────────────────────────────────
const FindingsList: React.FC<{ findings: Finding[]; onOpen?: (id: string) => void; openLabel?: string }> = ({ findings, onOpen, openLabel }) => {
  if (!findings.length) {
    return (
      <div style={{ marginTop: 12, fontSize: 11.5, color: C.dim, display: "flex", alignItems: "center", gap: 7 }}>
        <span style={{ width: 7, height: 7, borderRadius: "50%", background: "#3ddc97", display: "inline-block" }} />
        고립·과부하 신호 없음 — 구조가 깔끔합니다.
      </div>
    );
  }
  return (
    <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
      {findings.map((f, i) => {
        const bad = f.severity === "bad";
        const accent = bad ? C.red : C.amber;
        return (
          <div key={i} style={{
            border: `1px solid ${accent}55`, borderLeft: `3px solid ${accent}`,
            background: C.panel, borderRadius: 8, padding: "9px 11px",
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: C.ink, flex: 1 }}>{f.title}</div>
              {onOpen && (
                <button onClick={() => onOpen(f.node)} style={{
                  background: "none", border: `1px solid ${C.cyan}55`, color: C.cyan,
                  fontSize: 10.5, borderRadius: 6, padding: "3px 8px", cursor: "pointer", whiteSpace: "nowrap",
                }}>{openLabel ?? "열어보기"}</button>
              )}
            </div>
            <div style={{ fontSize: 11, color: C.dim, lineHeight: 1.5, marginTop: 3 }}>{f.detail}</div>
            <div style={{ fontSize: 10.5, color: "#3ddc97", marginTop: 5, fontFamily: "var(--vscode-editor-font-family, monospace)" }}>→ {f.fix}</div>
          </div>
        );
      })}
    </div>
  );
};

// ── Main ─────────────────────────────────────────────────────────────────────
const CodeMap: React.FC<{ isActive?: boolean }> = ({ isActive = true }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(540);
  const [project, setProject] = useState<ProjectGraph | null>(null);
  const [file, setFile] = useState<FileGraph | null>(null);
  const [view, setView] = useState<"project" | "file">("project");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 캔버스 폭: 컨테이너 폭과 노드 수 중 큰 값(좁으면 가로 스크롤).
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.max(320, el.clientWidth - 4)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const loadProject = useCallback(() => {
    setLoading(true); setError(null); setView("project");
    postMessage("map.project", {});
  }, [postMessage]);

  useMessage(useCallback((msg) => {
    const { type, payload } = msg;
    if (type === "map.projectResult") {
      setProject(payload as ProjectGraph); setLoading(false); setError(null);
    } else if (type === "map.fileResult") {
      setFile(payload as FileGraph); setView("file"); setLoading(false); setError(null);
    } else if (type === "map.error") {
      setError(String((payload as { message?: string })?.message ?? payload)); setLoading(false);
    }
  }, []));

  useEffect(() => { if (isActive && !project) loadProject(); }, [isActive, project, loadProject]);

  const openFileMap = useCallback((id: string) => {
    setLoading(true); setError(null);
    postMessage("map.file", { id });
  }, [postMessage]);

  // ── 프로젝트 뷰 레이아웃 ──
  const projLayout = project ? layoutProject(project.nodes, width) : null;
  const projBands = project && projLayout
    ? projLayout.rows.map((layer, li) => ({ label: LAYER_LABEL[layer] ?? layer, y: 34 + li * 86 }))
    : [];

  // ── 파일 뷰 레이아웃 ──
  const fileCols = width < 380 ? 2 : 3;
  const fileLayout = file ? layoutGrid(file.nodes, width, fileCols) : null;

  const headerBtn: React.CSSProperties = {
    background: "none", border: `1px solid ${C.line}`, color: C.dim,
    fontSize: 11, borderRadius: 6, padding: "4px 9px", cursor: "pointer",
  };

  return (
    <div ref={wrapRef} style={{ fontFamily: "var(--vscode-font-family)", color: C.ink }}>
      {/* 헤더 */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
        <div style={{ fontSize: 13, fontWeight: 700 }}>
          {view === "project" ? "구조 지도 — 전체 아키텍처" : `${file?.name ?? ""} — 내부 지도`}
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          {view === "file" && (
            <button style={headerBtn} onClick={() => setView("project")}>← 아키텍처</button>
          )}
          <button style={headerBtn} onClick={loadProject}>다시 스캔</button>
        </div>
      </div>

      <div style={{ fontSize: 11, color: C.dim, lineHeight: 1.5, marginBottom: 10 }}>
        {view === "project"
          ? <>선은 실제 <b style={{ color: C.ink }}>import</b>(사실), 위치는 계층(입구→저장). 파일을 누르면 그 안으로 들어갑니다. 정적 분석이라 동적 호출은 표시되지 않습니다.</>
          : <>파일 안 <b style={{ color: C.ink }}>함수·메서드 호출</b>(사실). 노랑은 호출이 몰린 과부하 지점입니다.</>}
      </div>

      {loading && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, color: C.dim, padding: "16px 0" }}>
          <div style={{ width: 13, height: 13, border: `2px solid ${C.line}`, borderTopColor: C.cyan, borderRadius: "50%", animation: "rcmspin 0.8s linear infinite" }} />
          <style>{`@keyframes rcmspin { to { transform: rotate(360deg); } }`}</style>
          정적 분석 중…
        </div>
      )}

      {error && (
        <div style={{ background: `${C.red}1a`, border: `1px solid ${C.red}`, color: C.red, borderRadius: 6, padding: "8px 10px", fontSize: 11.5 }}>
          {error}
        </div>
      )}

      {/* 프로젝트 뷰 */}
      {!loading && view === "project" && project && projLayout && (
        <>
          <div style={{ fontSize: 10.5, color: C.dim, marginBottom: 6 }}>
            {project.files_scanned}개 파일 · {project.edges.length}개 의존
          </div>
          <div style={{ overflowX: "auto" }}>
            <Canvas pos={projLayout.pos} edges={project.edges} height={projLayout.height} width={width} bands={projBands}>
              {project.nodes.map((n) => {
                const p = projLayout.pos.get(n.id)!;
                return (
                  <NodeChip key={n.id} x={p.x} y={p.y} title={n.name}
                    sub={n.layer === "entry" ? "진입점" : n.flags.includes("orphan") ? "아무도 import 안 함" : `${n.in_degree}곳서 의존`}
                    flags={n.flags} degree={n.in_degree}
                    clickable onClick={() => openFileMap(n.id)} />
                );
              })}
            </Canvas>
          </div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 8, fontSize: 10, color: C.dim }}>
            <span style={{ display: "flex", alignItems: "center", gap: 5 }}><i style={{ width: 14, height: 2, background: C.edge, display: "inline-block" }} />import</span>
            <span style={{ display: "flex", alignItems: "center", gap: 5 }}><i style={{ width: 9, height: 9, border: `1px dashed ${C.red}`, display: "inline-block" }} />고립</span>
            <span style={{ display: "flex", alignItems: "center", gap: 5 }}><i style={{ width: 9, height: 9, background: C.amber, display: "inline-block", borderRadius: 2 }} />과부하</span>
            <span>파일 클릭 → 내부 지도</span>
          </div>
          <FindingsList findings={project.findings} onOpen={openFileMap} openLabel="열어보기" />
        </>
      )}

      {/* 파일 뷰 */}
      {!loading && view === "file" && file && fileLayout && (
        <>
          <div style={{ fontSize: 10.5, color: C.dim, marginBottom: 6 }}>
            {file.functions_scanned}개 함수 · {file.edges.length}개 호출
            <button onClick={() => postMessage("map.openFile", { id: project?.nodes.find((n) => n.name === file.name)?.id ?? file.name })}
              style={{ ...headerBtn, marginLeft: 8, fontSize: 10, padding: "2px 7px" }}>에디터에서 열기</button>
          </div>
          {file.nodes.length === 0 ? (
            <div style={{ fontSize: 11.5, color: C.dim, padding: "12px 0" }}>함수/메서드 정의가 없습니다.</div>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <Canvas pos={fileLayout.pos} edges={file.edges} height={fileLayout.height} width={width}>
                {file.nodes.map((n) => {
                  const p = fileLayout.pos.get(n.id)!;
                  return (
                    <NodeChip key={n.id} x={p.x} y={p.y}
                      title={n.cls ? `${n.cls}.${n.name}` : n.name}
                      sub={n.in_degree > 0 ? `${n.in_degree}곳서 호출` : undefined}
                      flags={n.flags} degree={n.in_degree} />
                  );
                })}
              </Canvas>
            </div>
          )}
          <FindingsList findings={file.findings} />
        </>
      )}
    </div>
  );
};

export default CodeMap;
