/**
 * ReCoder — CodeMap component (구조 지도 / Architecture & Code Map)
 *
 * 2.0.37 — 구조 다이어그램(StructureDiagram):
 *   - 가로는 역할 레인(화면 → 입구·API → 처리 → 데이터 → 설정·테스트), 상자는 폴더, 노드는 파일, 선은 import.
 *     레인 안에서는 import 방향(부르는 쪽 → 불리는 쪽)으로 왼쪽에서 오른쪽.
 *   - 파일이 많으면(60개 초과) 폴더 단위로 먼저 보여 주고, 폴더를 누르면 그 폴더의 파일과 이웃 폴더를 펼친다.
 *   - 파일 뷰: 파일을 누르면 그 안의 함수/메서드 호출 그래프(클래스별 상자).
 *
 * 관통 원칙: 선은 사실(import/call), 색·위치는 해석(고립/과부하/계층).
 * 데이터는 확장 내부 정적 분석에서 옴 — Core·LLM 연결 불필요.
 */
import React, { useState, useCallback, useEffect, useMemo, useRef } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import FlowDryRun from "./FlowDryRun";
import type { FlowGraph } from "./dryRunModel";
import StructureDiagram, { DiagramEdge, DiagramNode } from "./StructureDiagram";
import { ROLE_LABEL, ROLE_ORDER, Role, aggregateFolders, folderOf, roleOf } from "./structureLayout";

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

/** 파일이 이보다 많으면 폴더 단위로 먼저 보여 준다. */
export const FOLDER_VIEW_THRESHOLD = 60;
const ROLE_COLOR: Record<Role, string> = {
  ui: "#3fd6de", entry: "#5b95ff", logic: "#b48cff", data: "#f7b955", support: "#7f8b99", other: "#7f8b99", external: "#ff8f6b",
};

function fileBadges(flags: string[]) {
  if (flags.includes("orphan")) return [{ text: "고립", color: C.red, solid: true }];
  if (flags.includes("overloaded")) return [{ text: "과부하", color: C.amber, solid: true }];
  return [];
}

/** 파일 단위 다이어그램 노드·선. focus 를 주면 그 폴더의 파일과, 이어진 다른 폴더(폴더 노드)만. */
export function projectDiagram(project: { nodes: ProjNode[]; edges: Edge[] }, mode: "files" | "folders", focus = "", showAll = false): { nodes: DiagramNode[]; edges: DiagramEdge[] } {
  //: 스타일·도구 설정 파일은 구조를 가린다(화면 레인이 CSS 로 가득 찬다) — 기본은 숨기고 켤 수 있게.
  if (!showAll) {
    const keep = new Set(project.nodes.filter((n) => !n.flags.includes("style") && !n.flags.includes("config")).map((n) => n.id));
    project = { nodes: project.nodes.filter((n) => keep.has(n.id)), edges: project.edges.filter((e) => keep.has(e.from) && keep.has(e.to)) };
  }
  const order = new Map([...project.nodes].sort((a, b) => a.id.localeCompare(b.id)).map((n, i) => [n.id, i]));
  if (mode === "folders" && !focus) {
    const agg = aggregateFolders(project.nodes, project.edges);
    const sorted = [...agg.nodes].sort((a, b) => a.label.localeCompare(b.label));
    return {
      nodes: sorted.map((f, i) => ({
        id: f.id, role: f.role, group: f.id.includes("#") ? "(최상위)" : f.label.split("/")[0] || f.label,
        groupLabel: f.id.includes("#") ? "(최상위)" : f.label.split("/")[0] || f.label, order: i,
        title: f.label.split("/").slice(-1)[0] || f.label, sub: `${f.label} · 파일 ${f.files}개`,
        badges: f.flags.includes("overloaded") ? [{ text: "과부하", color: C.amber, solid: true }] : [],
        attrs: { "data-map-node": f.id }, tooltip: `${f.label} — 파일 ${f.files}개 (누르면 펼침)`,
      })),
      edges: agg.edges.map((e) => ({ ...e, weight: e.count, label: e.count > 1 ? String(e.count) : undefined })),
    };
  }
  //: 최상위 폴더는 역할별로 나눠 보여 준다("(최상위)#logic").
  const [focusDir, focusRole] = focus.split("#");
  const inFocus = (id: string) => !focus || (folderOf(id) === focusDir && (!focusRole || roleOf(id) === focusRole));
  const files = project.nodes.filter((n) => inFocus(n.id));
  const nodes: DiagramNode[] = files.map((n) => ({
    id: n.id, role: roleOf(n.id), group: folderOf(n.id), order: order.get(n.id) ?? 0,
    title: n.name, sub: projSub(n), badges: fileBadges(n.flags),
    accent: n.flags.includes("orphan") ? C.red : n.flags.includes("overloaded") ? C.amber : undefined,
    attrs: { "data-map-node": n.id }, tooltip: `${n.id} — 누르면 파일 안 함수 지도`,
  }));
  const edges: DiagramEdge[] = [];
  if (!focus) {
    for (const e of project.edges) edges.push({ from: e.from, to: e.to });
    return { nodes, edges };
  }
  //: 이웃 폴더는 폴더 노드 하나로 — 선 굵기는 import 수.
  const outside = new Map<string, number>();
  const counts = new Map<string, number>();
  for (const e of project.edges) {
    const a = inFocus(e.from), b = inFocus(e.to);
    if (a && b) { edges.push({ from: e.from, to: e.to }); continue; }
    if (!a && !b) continue;
    const other = `dir:${folderOf(a ? e.to : e.from)}`;
    outside.set(other, (outside.get(other) ?? 0) + 1);
    const key = a ? `${e.from}\n${other}` : `${other}\n${e.to}`;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  let i = nodes.length;
  for (const [id, n] of outside) {
    const folder = id.slice(4);
    nodes.push({ id, role: roleOf(`${folder}/x.js`), group: "이웃 폴더", order: 100000 + i++, title: folder.split("/").slice(-1)[0] || folder,
      sub: `${folder} · import ${n}`, accent: "#7f8b99", attrs: { "data-map-node": id }, tooltip: `${folder} — 누르면 이 폴더로 이동` });
  }
  for (const [k, count] of counts) { const [from, to] = k.split("\n"); edges.push({ from, to, weight: count, color: "#7f8b99" }); }
  return { nodes, edges };
}

/** 파일 안 함수 지도 — 클래스별 상자, 호출 방향으로 왼쪽 → 오른쪽. */
export function fileDiagram(file: { nodes: FnNode[]; edges: Edge[] }): { nodes: DiagramNode[]; edges: DiagramEdge[] } {
  return {
    nodes: file.nodes.map((n, i) => ({
      id: n.id, role: "logic" as Role, group: n.cls ?? "모듈 함수", order: i,
      title: n.cls ? `${n.cls}.${n.name}` : n.name, sub: n.in_degree > 0 ? `${n.in_degree}곳서 호출` : "호출하는 곳 없음",
      badges: n.flags.includes("overloaded") ? [{ text: "과부하", color: C.amber, solid: true }] : [],
      accent: n.flags.includes("overloaded") ? C.amber : undefined, attrs: { "data-map-fn": n.id },
    })),
    edges: file.edges.map((e) => ({ from: e.from, to: e.to })),
  };
}

/** 프로젝트 노드 아래 한 줄 설명. 스타일 파일은 '아무도 import 안 함' 같은 실패 느낌의 문구를 쓰지 않는다. */
export function projSub(n: ProjNode): string {
  if (n.layer === "entry") return "진입점";
  if (n.flags.includes("style")) return n.in_degree > 0 ? `${n.in_degree}곳에서 사용하는 스타일` : "스타일 파일";
  if (n.flags.includes("config")) return "빌드·테스트 도구가 읽음";
  if (n.flags.includes("orphan")) return "아무도 import 안 함";
  return `${n.in_degree}곳서 의존`;
}

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
  const [project, setProject] = useState<ProjectGraph | null>(null);
  const [file, setFile] = useState<FileGraph | null>(null);
  const [view, setView] = useState<"project" | "file" | "flow">("project");
  const [flow, setFlow] = useState<FlowGraph | null>(null);
  const [flowLoading, setFlowLoading] = useState(false);
  const [flowError, setFlowError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
    } else if (type === "map.flowResult") {
      setFlow(payload as FlowGraph); setFlowLoading(false); setFlowError(null);
    } else if (type === "map.flowError") {
      setFlowError(String((payload as { message?: string })?.message ?? payload)); setFlowLoading(false);
    }
  }, []));

  useEffect(() => { if (isActive && !project) loadProject(); }, [isActive, project, loadProject]);

  const loadFlow = useCallback(() => {
    setView("flow"); setFlowLoading(true); setFlowError(null);
    postMessage("map.flow", {});
  }, [postMessage]);

  const openFileMap = useCallback((id: string) => {
    setLoading(true); setError(null);
    postMessage("map.file", { id });
  }, [postMessage]);

  // ── 다이어그램 데이터 ──
  const [mode, setMode] = useState<"auto" | "files" | "folders">("auto");
  const [focus, setFocus] = useState("");
  const [showAll, setShowAll] = useState(false);
  useEffect(() => { setFocus(""); }, [project]);
  const effectiveMode: "files" | "folders" = mode === "auto"
    ? ((project?.nodes.length ?? 0) > FOLDER_VIEW_THRESHOLD ? "folders" : "files") : mode;
  const projDiagram = useMemo(() => (project ? projectDiagram(project, effectiveMode, focus, showAll) : null), [project, effectiveMode, focus, showAll]);
  const hiddenCount = useMemo(() => (project && !showAll ? project.nodes.filter((n) => n.flags.includes("style") || n.flags.includes("config")).length : 0), [project, showAll]);
  const fileDiag = useMemo(() => (file ? fileDiagram(file) : null), [file]);
  const presentRoles = useMemo(() => ROLE_ORDER.filter((r) => projDiagram?.nodes.some((n) => n.role === r)), [projDiagram]);
  const onProjectNode = useCallback((id: string) => {
    if (id.startsWith("dir:")) { setFocus(id.slice(4)); return; }
    openFileMap(id);
  }, [openFileMap]);

  const headerBtn: React.CSSProperties = {
    background: "none", border: `1px solid ${C.line}`, color: C.dim,
    fontSize: 11, borderRadius: 6, padding: "4px 9px", cursor: "pointer",
  };

  return (
    <div ref={wrapRef} style={{ fontFamily: "var(--vscode-font-family)", color: C.ink }}>
      {/* 헤더 */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
        <div style={{ fontSize: 13, fontWeight: 700 }}>
          {view === "project" ? "구조 지도 — 전체 아키텍처" : view === "flow" ? "흐름 dry run — 실행하지 않고 따라가기" : `${file?.name ?? ""} — 내부 지도`}
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          {view !== "project" && (
            <button style={headerBtn} onClick={() => setView("project")}>← 아키텍처</button>
          )}
          {view !== "flow" && (
            <button style={{ ...headerBtn, color: C.cyan, borderColor: `${C.cyan}66` }} onClick={() => { if (flow) setView("flow"); else loadFlow(); }}>흐름 dry run</button>
          )}
          {view === "flow"
            ? <button style={{ ...headerBtn, opacity: flowLoading ? .5 : 1 }} disabled={flowLoading} onClick={loadFlow}>새로고침</button>
            : <button style={{ ...headerBtn, opacity: loading ? .5 : 1 }} disabled={loading} onClick={loadProject}>새로고침</button>}
        </div>
      </div>

      <div style={{ fontSize: 11, color: C.dim, lineHeight: 1.5, marginBottom: 10 }}>
        {view === "flow"
          ? <>API 경로·화면 동작·실행 파일 중 하나를 고르면, 그 요청이 <b style={{ color: C.ink }}>어느 층(화면 → 입구·API → 처리 → 데이터)·어느 파일</b>을 지나 DB·외부 API·AWS·비밀값에 닿는지 구조 위에서 한 단계씩 보여 줍니다.</>
          : view === "project"
          ? <>가로는 역할(화면 → 입구·API → 처리 → 데이터), 상자는 폴더, 선은 실제 <b style={{ color: C.ink }}>import</b>(부르는 쪽 → 불리는 쪽). 노드에 마우스를 올리면 이어진 선만 남습니다. 역할은 폴더·파일 이름으로 추정한 것이고, 정적 분석이라 동적 호출은 표시되지 않습니다.</>
          : <>파일 안 <b style={{ color: C.ink }}>함수·메서드·콜백 호출</b>의 정적 분석 결과입니다. 노랑은 호출이 몰린 과부하 지점입니다.</>}
      </div>

      {loading && view !== "flow" && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, color: C.dim, padding: "16px 0" }}>
          <div style={{ width: 13, height: 13, border: `2px solid ${C.line}`, borderTopColor: C.cyan, borderRadius: "50%", animation: "rcmspin 0.8s linear infinite" }} />
          <style>{`@keyframes rcmspin { to { transform: rotate(360deg); } }`}</style>
          정적 분석 중…
        </div>
      )}

      {error && view !== "flow" && (
        <div style={{ background: `${C.red}1a`, border: `1px solid ${C.red}`, color: C.red, borderRadius: 6, padding: "8px 10px", fontSize: 11.5 }}>
          {error}
        </div>
      )}

      {/* 흐름 dry run */}
      {view === "flow" && flowLoading && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, color: C.dim, padding: "16px 0" }}>
          <div style={{ width: 13, height: 13, border: `2px solid ${C.line}`, borderTopColor: C.cyan, borderRadius: "50%", animation: "rcmspin 0.8s linear infinite" }} />
          <style>{`@keyframes rcmspin { to { transform: rotate(360deg); } }`}</style>
          호출 흐름 분석 중…
        </div>
      )}
      {view === "flow" && flowError && (
        <div style={{ background: `${C.red}1a`, border: `1px solid ${C.red}`, color: C.red, borderRadius: 6, padding: "8px 10px", fontSize: 11.5 }}>{flowError}</div>
      )}
      {view === "flow" && !flowLoading && flow && (
        <FlowDryRun graph={flow} onOpen={(f, line) => postMessage("map.openFile", { id: f, line })} />
      )}

      {/* 프로젝트 뷰 */}
      {!loading && view === "project" && project && projDiagram && (
        <>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 10.5, color: C.dim, marginBottom: 6 }}>
            <span>{project.files_scanned}개 파일 · {project.edges.length}개 의존</span>
            {focus && (
              <span data-map-focus style={{ color: C.ink }}>
                폴더 <b>{focus.replace("#", " · ")}</b> 와 이웃 폴더
                <button style={{ ...headerBtn, marginLeft: 6, fontSize: 10, padding: "1px 7px" }} onClick={() => setFocus("")}>← 전체</button>
              </span>
            )}
            {(hiddenCount > 0 || showAll) && (
              <button style={{ ...headerBtn, fontSize: 10, padding: "1px 7px" }} onClick={() => setShowAll((v) => !v)}>
                {showAll ? "스타일·설정 파일 숨기기" : `스타일·설정 파일 ${hiddenCount}개 숨김 · 보기`}
              </button>
            )}
            {!focus && (
              <span style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
                {(["folders", "files"] as const).map((m) => (
                  <button key={m} aria-pressed={effectiveMode === m} onClick={() => setMode(m)}
                    style={{ ...headerBtn, fontSize: 10, padding: "2px 8px", color: effectiveMode === m ? C.cyan : C.dim, borderColor: effectiveMode === m ? `${C.cyan}88` : C.line }}>
                    {m === "folders" ? "폴더 단위" : "파일 단위"}
                  </button>
                ))}
              </span>
            )}
          </div>
          {projDiagram.nodes.length === 0 ? (
            <div style={{ fontSize: 11.5, color: C.dim, padding: "12px 0" }}>분석할 코드 파일을 찾지 못했습니다.</div>
          ) : (
            <StructureDiagram nodes={projDiagram.nodes} edges={projDiagram.edges} onNodeClick={onProjectNode}
              layout={{ mergeSingletons: effectiveMode === "folders" && !focus ? "폴더" : "", maxRows: 8 }}
              onNodeDoubleClick={(id) => { if (!id.startsWith("dir:")) postMessage("map.openFile", { id }); }}
              ariaLabel="아키텍처 구조 다이어그램" />
          )}
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 8, fontSize: 10, color: C.dim }}>
            {presentRoles.map((r) => (
              <span key={r} style={{ display: "flex", alignItems: "center", gap: 5 }}><i style={{ width: 9, height: 9, background: ROLE_COLOR[r], opacity: 0.7, display: "inline-block", borderRadius: 2 }} />{ROLE_LABEL[r]}</span>
            ))}
            <span style={{ display: "flex", alignItems: "center", gap: 5 }}><i style={{ width: 14, height: 2, background: C.edge, display: "inline-block" }} />import(부르는 쪽 → 불리는 쪽)</span>
            <span style={{ display: "flex", alignItems: "center", gap: 5 }}><i style={{ width: 9, height: 9, background: C.red, display: "inline-block", borderRadius: 2 }} />고립</span>
            <span style={{ display: "flex", alignItems: "center", gap: 5 }}><i style={{ width: 9, height: 9, background: C.amber, display: "inline-block", borderRadius: 2 }} />과부하</span>
            <span>{effectiveMode === "folders" && !focus ? "폴더 클릭 → 펼치기" : "파일 클릭 → 내부 지도 · 더블클릭 → 에디터"}</span>
          </div>
          <FindingsList findings={project.findings} onOpen={openFileMap} openLabel="열어보기" />
        </>
      )}

      {/* 파일 뷰 */}
      {!loading && view === "file" && file && fileDiag && (
        <>
          <div style={{ fontSize: 10.5, color: C.dim, marginBottom: 6 }}>
            {file.functions_scanned}개 함수·콜백 · {file.edges.length}개 호출
            <button onClick={() => postMessage("map.openFile", { id: project?.nodes.find((n) => n.name === file.name)?.id ?? file.name })}
              style={{ ...headerBtn, marginLeft: 8, fontSize: 10, padding: "2px 7px" }}>에디터에서 열기</button>
          </div>
          {file.nodes.length === 0 ? (
            <div style={{ fontSize: 11.5, color: C.dim, padding: "12px 0" }}>정적 분석으로 식별한 함수·콜백이 없습니다. 동적으로 만들어진 함수는 표시되지 않을 수 있습니다.</div>
          ) : (
            <StructureDiagram nodes={fileDiag.nodes} edges={fileDiag.edges} layout={{ maxCols: 6 }} ariaLabel="파일 안 함수 호출 다이어그램" />
          )}
          <FindingsList findings={file.findings} />
        </>
      )}
    </div>
  );
};

export default CodeMap;
