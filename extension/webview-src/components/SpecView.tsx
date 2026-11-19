/**
 * 명세 — 요구사항 · 설계 · 작업 (4.0.1)
 *
 * 실사용 2026-11-19: 글이 많고, 이모티콘 · 반짝이 아이콘 · 색 배지가 "AI 가 만든 화면" 처럼 보인다는 의견.
 * GitHub(Checks · Issues) · Vercel(Deployments) · Linear 의 표현을 따른다:
 *   무채색 바탕, 상태 색은 아이콘에만, 밑줄 탭 + 회색 숫자, 표 형태의 목록, 텍스트 버튼.
 * 데이터는 Core /api/project/spec. AI 검토는 버튼을 누를 때만.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import FlowDryRun from "./FlowDryRun";
import type { FlowGraph } from "./dryRunModel";
import { apiKey, effectText, runSpec, SpecRun, Verdict } from "./specDryRun";

export type Status = "ok" | "fail" | "missing" | "warn" | "partial" | "unknown";
export interface SpecCriterion { cond: string; text: string; status: Status; source: "test" | "ai"; api?: string; test?: string; got?: number; note?: string }
export interface SpecApi {
  service: string; method: string; path: string; auth: boolean; admin: boolean; public: boolean;
  implemented: boolean; file: string; line: number; tests: { id: string; status: Status }[];
}
export interface SpecFeature {
  key: string; label: string; internal: boolean; services: string[]; status: Status;
  endpoints: SpecApi[]; criteria: SpecCriterion[]; counts: { api: number; criteria: number; ok: number; fail: number };
}
export interface SpecTask { id: string; title: string; detail: string; severity: "high" | "medium" | "low"; feature: string; source: "rule" | "ai" }
export interface SpecService { name: string; kind: string; language: string; port: number; public: boolean; databases: string[]; depends_on: string[]; purpose: string; api: number }
export interface Spec {
  available: boolean; reason?: string; project?: string;
  features?: SpecFeature[]; services?: SpecService[]; databases?: { name: string; engine: string; tables: string[] }[];
  endpoints?: SpecApi[]; tasks?: SpecTask[];
  tests?: { passed: number; total: number; at: string; status: string };
  review?: { at: string; summary: string; fresh: boolean; missing: string[] } | null;
  deploy?: null | { id: string; at: string; status: string; failures: string[]; url: string; services: Record<string, "ok" | "fail" | "unknown"> };
}
export const STATUS_LABEL: Record<Status, string> = { ok: "통과", fail: "실패", missing: "없음", warn: "확인 필요", partial: "일부 확인", unknown: "확인 전" };

export const SPEC_CSS = `
.rc-spec{--sp-line:var(--rc-line2,rgba(160,190,220,.12));--sp-hover:rgba(148,178,210,.045);--sp-sel:rgba(148,178,210,.08);
  --sp-ok:#3fb950;--sp-bad:#f85149;--sp-warn:#d29922;--sp-fg:var(--rc-fg,#e6edf3);--sp-fg2:var(--rc-fg2,#9aa8b8);--sp-mute:var(--rc-fg3,#6e7b8b);
  --sp-mono:var(--vscode-editor-font-family,var(--rc-mono,ui-monospace,Menlo,Consolas,monospace));
  border:1px solid var(--sp-line);border-radius:8px;background:var(--rc-solid,#0d1117);color:var(--sp-fg);font-size:13px;line-height:1.45;overflow:hidden}
.rc-spec *{box-sizing:border-box}
.rc-spec button{font:inherit;color:inherit}
.sp-head{display:flex;align-items:center;gap:16px;padding:0 16px;border-bottom:1px solid var(--sp-line)}
.sp-name{font-weight:600;font-size:14px;padding:14px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px}
.sp-tabs{display:flex;gap:4px;align-self:stretch;margin-left:8px}
.sp-tabs button{all:unset;cursor:pointer;display:flex;align-items:center;gap:6px;padding:0 10px;font-size:13px;color:var(--sp-fg2);border-bottom:2px solid transparent;margin-bottom:-1px}
.sp-tabs button:hover{color:var(--sp-fg)}
.sp-tabs button[aria-selected=true]{color:var(--sp-fg);font-weight:500;border-bottom-color:var(--rc-teal,#2f81f7)}
.sp-count{display:inline-block;min-width:18px;padding:0 6px;border-radius:999px;background:rgba(148,178,210,.14);color:var(--sp-fg2);font-size:11px;font-weight:500;line-height:18px;text-align:center}
.sp-right{margin-left:auto;display:flex;align-items:center;gap:12px}
.sp-checks{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--sp-fg2);white-space:nowrap}
.sp-btn{all:unset;box-sizing:border-box;cursor:pointer;height:28px;padding:0 12px;display:inline-flex;align-items:center;gap:6px;border-radius:6px;border:1px solid var(--sp-line);background:rgba(148,178,210,.05);font-size:12px;font-weight:500;color:var(--sp-fg);white-space:nowrap}
.sp-btn:hover{background:rgba(148,178,210,.1)}.sp-btn[aria-disabled=true]{opacity:.55;cursor:default}
.sp-ic{flex:none;display:inline-block;vertical-align:middle}
.sp-ic.ok{color:var(--sp-ok)}.sp-ic.fail{color:var(--sp-bad)}.sp-ic.missing,.sp-ic.warn,.sp-ic.partial{color:var(--sp-warn)}.sp-ic.unknown{color:var(--sp-mute)}
.sp-main{display:grid;grid-template-columns:232px minmax(0,1fr);min-height:440px}
.sp-side{border-right:1px solid var(--sp-line);padding:8px}
.sp-item{all:unset;box-sizing:border-box;cursor:pointer;display:flex;align-items:center;gap:8px;width:100%;padding:6px 8px;border-radius:6px;color:var(--sp-fg2)}
.sp-item:hover{background:var(--sp-hover);color:var(--sp-fg)}
.sp-item[aria-current=true]{background:var(--sp-sel);color:var(--sp-fg);font-weight:500}
.sp-item .lb{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sp-item .n{color:var(--sp-mute);font-size:12px;font-variant-numeric:tabular-nums;font-weight:400}
.sp-doc{padding:20px 24px 24px;min-width:0}
.sp-doc h2{margin:0 0 2px;font-size:15px;font-weight:600}
.sp-meta{margin:0 0 16px;font-size:12px;color:var(--sp-mute)}
.sp-sec{font-size:12px;font-weight:600;color:var(--sp-fg2);margin:20px 0 8px}
.sp-sec:first-of-type{margin-top:0}
.sp-list{border:1px solid var(--sp-line);border-radius:6px}
.sp-li{display:flex;align-items:center;gap:10px;padding:8px 12px;border-top:1px solid var(--sp-line)}
.sp-li:first-child{border-top:0}
.sp-li .tx{flex:1;min-width:0}
.sp-api{all:unset;box-sizing:border-box;cursor:pointer;display:grid;grid-template-columns:52px minmax(0,1fr) 56px 16px;gap:12px;align-items:center;width:100%;padding:7px 12px;border-top:1px solid var(--sp-line)}
.sp-api:first-child{border-top:0}.sp-api:hover{background:var(--sp-hover)}
.sp-m{font:600 11px var(--sp-mono);letter-spacing:.02em;color:var(--sp-fg2)}
.sp-p{font:12.5px var(--sp-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sp-au{font-size:12px;color:var(--sp-mute);text-align:right}
.sp-more{padding:8px 12px 10px 76px;font-size:12px;color:var(--sp-fg2);border-top:1px solid var(--sp-line);background:rgba(148,178,210,.025);display:grid;gap:6px}
.sp-more .r{display:flex;align-items:center;gap:8px}
.sp-link{all:unset;cursor:pointer;color:var(--rc-teal,#2f81f7);font:12px var(--sp-mono)}.sp-link:hover{text-decoration:underline}
.sp-note{font-size:12px;color:var(--sp-mute)}
.sp-pad{padding:20px 24px 24px}
.sp-flow{width:100%;height:auto;display:block;margin-bottom:8px}
.sp-flow .nd{cursor:pointer}
.sp-flow .nd rect.bg{fill:var(--rc-solid,#0d1117);stroke:var(--sp-line)}
.sp-flow .nd:hover rect.bg{stroke:rgba(148,178,210,.35)}
.sp-flow .nd.sel rect.bg{stroke:var(--sp-fg);stroke-width:1.2}
.sp-flow .nd text{fill:var(--sp-fg);font-size:12.5px;font-weight:500}
.sp-flow .nd text.sub{fill:var(--sp-mute);font-size:11px;font-weight:400}
.sp-flow path.ed{fill:none;stroke:rgba(148,178,210,.28);stroke-width:1}
.sp-flow path.ed.hl{stroke:rgba(148,178,210,.75)}
.sp-sev{font-size:11px;font-weight:500;padding:0 7px;border-radius:999px;border:1px solid var(--sp-line);color:var(--sp-fg2);line-height:18px;white-space:nowrap}
.sp-sev.high{color:var(--sp-bad);border-color:rgba(248,81,73,.4)}
.sp-sev.medium{color:var(--sp-warn);border-color:rgba(210,153,34,.4)}
.sp-sub{display:block;font-size:12px;color:var(--sp-mute);margin-top:1px}
.sp-dry-bar{display:flex;align-items:center;gap:16px;padding:12px 24px;border-bottom:1px solid var(--sp-line);font-size:12.5px;color:var(--sp-fg2)}
.sp-dry-bar b{color:var(--sp-fg);font-weight:600;font-variant-numeric:tabular-nums}
.sp-dry-bar .st{display:flex;align-items:center;gap:6px}
.sp-dry-bar .sp-btn{margin-left:auto}
.sp-dry{width:100%;border-collapse:collapse;font-size:12.5px;table-layout:fixed}
.sp-dry .wrap{overflow:auto;max-width:100%}
.sp-dry th{text-align:left;font-weight:500;font-size:12px;color:var(--sp-mute);padding:8px 12px;border-bottom:1px solid var(--sp-line);white-space:nowrap}
.sp-dry td{padding:7px 12px;border-bottom:1px solid var(--sp-line);vertical-align:middle}
.sp-dry tr.grp td{padding:14px 12px 6px 24px;font-weight:600;font-size:12px;color:var(--sp-fg2);border-bottom:1px solid var(--sp-line)}
.sp-dry tr.r{cursor:pointer}.sp-dry tr.r:hover td{background:var(--sp-hover)}
.sp-dry tr.r[aria-expanded=true] td{background:var(--sp-sel)}
.sp-dry td.ic{width:28px;padding-left:24px}
.sp-dry td.m{width:64px;font:600 11px var(--sp-mono);color:var(--sp-fg2)}
.sp-dry td.p{font:12.5px var(--sp-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sp-dry td.c,.sp-dry td.e{overflow:hidden;text-overflow:ellipsis}
.sp-dry td.c{color:var(--sp-fg2);white-space:nowrap}
.sp-dry td.e{color:var(--sp-fg2);white-space:nowrap}
.sp-dry td.n{color:var(--sp-mute);text-align:right;padding-right:24px;font-variant-numeric:tabular-nums;white-space:nowrap}
.sp-dry tr.x td{padding:12px 24px 16px;background:rgba(148,178,210,.025)}
.sp-why{margin:0 0 10px;padding:0;list-style:none;display:grid;gap:4px;font-size:12.5px}
.sp-why li{display:flex;align-items:center;gap:8px}
.sp-hops{display:flex;gap:4px;margin:0 0 10px}
.sp-hops button{all:unset;cursor:pointer;padding:3px 10px;border-radius:6px;border:1px solid var(--sp-line);font-size:12px;color:var(--sp-fg2)}
.sp-hops button[aria-pressed=true]{color:var(--sp-fg);border-color:rgba(148,178,210,.4);background:var(--sp-sel)}
.sp-empty{padding:48px 20px;text-align:center;color:var(--sp-mute);font-size:13px}
.sp-err{margin:12px 16px 0;padding:8px 12px;border-radius:6px;font-size:12px;border:1px solid rgba(248,81,73,.4);color:var(--sp-bad)}
@media(max-width:760px){.sp-main{grid-template-columns:1fr}.sp-side{border-right:0;border-bottom:1px solid var(--sp-line);display:flex;overflow-x:auto;gap:2px}.sp-item{width:auto;white-space:nowrap}
  .sp-head{flex-wrap:wrap;gap:8px}.sp-tabs{margin-left:0;min-height:36px}.sp-right{margin-left:0;padding-bottom:8px}.sp-api{grid-template-columns:48px minmax(0,1fr) 16px}.sp-au{display:none}}
`;

// ── 아이콘 (Octicons 와 같은 16px 선 굵기 · 모양) ───────────────────────
const Ic: React.FC<{ s: Status; size?: number }> = ({ s, size = 14 }) => {
  const common = { className: `sp-ic ${s}`, width: size, height: size, viewBox: "0 0 16 16", "aria-label": STATUS_LABEL[s], role: "img" } as const;
  if (s === "ok") return <svg {...common} fill="currentColor"><title>{STATUS_LABEL[s]}</title><path d="M8 16A8 8 0 1 1 8 0a8 8 0 0 1 0 16Zm3.78-9.72a.75.75 0 0 0-1.06-1.06L6.75 9.19 5.28 7.72a.75.75 0 0 0-1.06 1.06l2 2a.75.75 0 0 0 1.06 0Z" /></svg>;
  if (s === "fail") return <svg {...common} fill="currentColor"><title>{STATUS_LABEL[s]}</title><path d="M2.343 13.657A8 8 0 1 1 13.658 2.343 8 8 0 0 1 2.343 13.657ZM6.03 4.97a.75.75 0 0 0-1.06 1.06L6.94 8 4.97 9.97a.75.75 0 1 0 1.06 1.06L8 9.06l1.97 1.97a.75.75 0 1 0 1.06-1.06L9.06 8l1.97-1.97a.75.75 0 1 0-1.06-1.06L8 6.94Z" /></svg>;
  if (s === "missing" || s === "warn" || s === "partial") return <svg {...common} fill="currentColor"><title>{STATUS_LABEL[s]}</title><path d="M6.457 1.047c.659-1.234 2.427-1.234 3.086 0l6.082 11.378A1.75 1.75 0 0 1 14.082 15H1.918a1.75 1.75 0 0 1-1.543-2.575Zm1.763.707a.25.25 0 0 0-.44 0L1.698 13.132a.25.25 0 0 0 .22.368h12.164a.25.25 0 0 0 .22-.368Zm.53 3.996v2.5a.75.75 0 0 1-1.5 0v-2.5a.75.75 0 0 1 1.5 0ZM9 11a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z" /></svg>;
  return <svg {...common} fill="none" stroke="currentColor" strokeWidth="1.5"><title>{STATUS_LABEL[s]}</title><circle cx="8" cy="8" r="6.25" strokeDasharray="2.5 2.2" /></svg>;
};

const testBrief = (c: SpecCriterion): string =>
  c.text.startsWith("로그인하지") ? "로그인 없이 호출하면 거절" : c.text.startsWith("요청을 받으면") ? "서버 오류 없이 응답"
    : c.text.replace(/^올바른 요청에 /, "").replace(/ 로 응답한다$/, " 응답").replace(/한다$/, "");

function apiStatus(a: SpecApi): Status {
  if (!a.implemented) return "fail";
  if (a.tests.some(t => t.status === "fail")) return "fail";
  if (a.tests.length && a.tests.every(t => t.status === "ok")) return "ok";
  return "unknown";
}
const authText = (a: SpecApi) => a.admin ? "관리자" : a.auth ? "로그인" : "공개";

const ApiRow: React.FC<{ a: SpecApi; criteria: SpecCriterion[]; open: boolean; onToggle: () => void; onOpenFile: (f: string, l: number) => void }> = ({ a, criteria, open, onToggle, onOpenFile }) => {
  const mine = criteria.filter(c => c.source === "test" && c.api === `${a.method} ${a.path}`);
  return <>
    <button type="button" className="sp-api" aria-expanded={open} onClick={onToggle}>
      <span className="sp-m">{a.method}</span><span className="sp-p" title={a.path}>{a.path}</span>
      <span className="sp-au">{authText(a)}</span><Ic s={apiStatus(a)} />
    </button>
    {open && <div className="sp-more">
      <div className="r">{a.implemented
        ? <button type="button" className="sp-link" onClick={() => onOpenFile(a.file, a.line)}>{a.file}:{a.line}</button>
        : <><Ic s="fail" size={12} />코드에서 찾지 못함</>}</div>
      {mine.map(c => <div className="r" key={c.test}><Ic s={c.status} size={12} />{testBrief(c)}{c.got !== undefined && c.status === "fail" ? ` (실제 ${c.got})` : ""}</div>)}
    </div>}
  </>;
};

// ── 설계: 흐름도 ────────────────────────────────────────────────────────
interface FlowNode { id: string; label: string; sub: string; kind: string; col: number; x: number; y: number }
const NW = 156, NH = 48, CG = 56, RG = 12;

/** 왼쪽(사용자 쪽)에서 오른쪽(DB)으로 — 서비스는 자기를 부르는 쪽보다 한 칸 오른쪽. */
export function flowLayout(services: SpecService[], dbs: { name: string; engine: string }[]): { nodes: FlowNode[]; edges: [string, string][]; w: number; h: number } {
  const names = new Set(services.map(s => s.name));
  const depth: Record<string, number> = {};
  const dep = (n: string, seen: Set<string>): number => {
    if (depth[n] !== undefined) return depth[n];
    if (seen.has(n)) return 0;
    seen.add(n);
    const parents = services.filter(s => s.depends_on.includes(n));
    depth[n] = parents.length ? Math.max(...parents.map(p => dep(p.name, seen))) + 1 : 0;
    return depth[n];
  };
  services.forEach(s => dep(s.name, new Set()));
  const dbCol = services.length ? Math.max(...services.map(s => depth[s.name])) + 1 : 0;
  const raw: Omit<FlowNode, "x" | "y">[] = [
    ...services.map(s => ({ id: s.name, label: s.name, kind: s.kind === "web" ? "web" : s.kind === "worker" ? "worker" : "api", col: depth[s.name],
      sub: s.kind === "web" ? "Web" : s.kind === "worker" ? "Worker" : `API ${s.api}${s.port ? ` · :${s.port}` : ""}` })),
    ...dbs.map(d => ({ id: d.name, label: d.name, kind: "db", col: dbCol, sub: d.engine })),
  ];
  const cols: Record<number, typeof raw> = {};
  raw.forEach(n => (cols[n.col] ??= []).push(n));
  const maxRows = Math.max(1, ...Object.values(cols).map(c => c.length));
  const h = maxRows * (NH + RG) - RG;
  const nodes: FlowNode[] = [];
  Object.entries(cols).forEach(([c, list]) => {
    const off = (h - (list.length * (NH + RG) - RG)) / 2;
    list.forEach((n, i) => nodes.push({ ...n, x: Number(c) * (NW + CG), y: off + i * (NH + RG) }));
  });
  const edges: [string, string][] = [];
  services.forEach(s => {
    s.depends_on.filter(d => names.has(d)).forEach(d => edges.push([s.name, d]));
    s.databases.forEach(d => edges.push([s.name, d]));
  });
  return { nodes, edges, w: (dbCol + 1) * (NW + CG) - CG, h };
}

const KIND_GLYPH: Record<string, string> = {
  web: "M2 3.75C2 2.784 2.784 2 3.75 2h8.5c.966 0 1.75.784 1.75 1.75v8.5A1.75 1.75 0 0 1 12.25 14h-8.5A1.75 1.75 0 0 1 2 12.25Zm1.5 2.75v5.75c0 .138.112.25.25.25h8.5a.25.25 0 0 0 .25-.25V6.5Zm9-1.5V3.75a.25.25 0 0 0-.25-.25h-8.5a.25.25 0 0 0-.25.25V5Z",
  api: "M4.72 3.22a.75.75 0 0 1 1.06 1.06L2.06 8l3.72 3.72a.75.75 0 1 1-1.06 1.06L.47 8.53a.75.75 0 0 1 0-1.06Zm6.56 0a.75.75 0 1 0-1.06 1.06L13.94 8l-3.72 3.72a.75.75 0 1 0 1.06 1.06l4.25-4.25a.75.75 0 0 0 0-1.06Z",
  worker: "M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0ZM1.5 8a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Zm7-3.25v2.992l2.028.812a.75.75 0 0 1-.557 1.392l-2.5-1A.751.751 0 0 1 7 8.25v-3.5a.75.75 0 0 1 1.5 0Z",
  db: "M1 3.5c0-.626.292-1.165.7-1.59.406-.422.956-.767 1.579-1.041C4.525.32 6.195 0 8 0c1.805 0 3.475.32 4.722.869.622.274 1.172.62 1.578 1.04.408.426.7.965.7 1.591v9c0 .626-.292 1.165-.7 1.59-.406.422-.956.767-1.579 1.041C11.476 15.68 9.806 16 8 16c-1.805 0-3.475-.32-4.721-.869-.623-.274-1.173-.62-1.579-1.04-.408-.426-.7-.965-.7-1.591Zm1.5 0c0 .133.058.318.282.551.227.237.591.483 1.101.707C4.898 5.205 6.353 5.5 8 5.5c1.646 0 3.101-.295 4.118-.742.508-.224.873-.471 1.1-.708.224-.232.282-.417.282-.55 0-.133-.058-.318-.282-.551-.227-.237-.591-.483-1.101-.707C11.102 1.795 9.647 1.5 8 1.5c-1.646 0-3.101.295-4.118.742-.508.224-.873.471-1.1.708-.224.232-.282.417-.282.55Zm0 4.5c0 .133.058.318.282.551.227.237.591.483 1.101.707C4.898 9.705 6.353 10 8 10c1.646 0 3.101-.295 4.118-.742.508-.224.873-.471 1.1-.708.224-.232.282-.417.282-.55V5.724c-.241.15-.503.286-.778.407C11.475 6.68 9.805 7 8 7c-1.805 0-3.475-.32-4.721-.869a6.15 6.15 0 0 1-.779-.407Zm0 2.225V12.5c0 .133.058.318.282.55.227.237.592.484 1.1.708 1.016.447 2.471.742 4.118.742 1.647 0 3.102-.295 4.117-.742.51-.224.874-.47 1.101-.707.224-.233.282-.418.282-.551v-2.275c-.241.15-.503.285-.778.406-1.247.549-2.917.869-4.722.869-1.805 0-3.475-.32-4.721-.869a6.327 6.327 0 0 1-.779-.406Z",
};

const Flow: React.FC<{ spec: Spec; sel: string; onSel: (n: string) => void }> = ({ spec, sel, onSel }) => {
  const { nodes, edges, w, h } = useMemo(() => flowLayout(spec.services ?? [], spec.databases ?? []), [spec]);
  const at = Object.fromEntries(nodes.map(n => [n.id, n]));
  const live = spec.deploy?.services ?? {};
  return <svg className="sp-flow" viewBox={`-2 -2 ${w + 4} ${h + 4}`} style={{ maxHeight: Math.max(160, h + 40) }} role="img" aria-label="서비스 구성도">
    {edges.map(([a, b]) => { const p = at[a], q = at[b]; if (!p || !q) return null;
      const x1 = p.x + NW, y1 = p.y + NH / 2, x2 = q.x, y2 = q.y + NH / 2, mx = (x1 + x2) / 2;
      return <path key={`${a}-${b}`} className={`ed${sel === a || sel === b ? " hl" : ""}`} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} />; })}
    {nodes.map(n => { const st = live[n.id];
      return <g key={n.id} className={`nd${sel === n.id ? " sel" : ""}`} transform={`translate(${n.x},${n.y})`} onClick={() => onSel(n.id)} role="button" aria-label={n.label}>
        <rect className="bg" width={NW} height={NH} rx="6" />
        <path d={KIND_GLYPH[n.kind]} transform="translate(12,16)" fill="var(--sp-mute)" />
        <text x="36" y="21">{n.label.length > 15 ? n.label.slice(0, 14) + "…" : n.label}</text>
        <text className="sub" x="36" y="37">{n.sub}</text>
        {st && <circle cx={NW - 13} cy={NH / 2} r="3.5" fill={st === "ok" ? "var(--sp-ok)" : st === "fail" ? "var(--sp-bad)" : "var(--sp-mute)"}><title>{st === "ok" ? "실행 중" : st === "fail" ? "시작 못 함" : "확인 전"}</title></circle>}
      </g>; })}
  </svg>;
};

// ── 화면 ─────────────────────────────────────────────────────────────────
export const SpecView: React.FC<{ initial?: Spec | null; onStartTask?: (task: SpecTask) => void; onOpenFile?: (file: string, line: number) => void; onOpenStructure?: () => void }> = ({ initial = null, onStartTask, onOpenFile, onOpenStructure }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [spec, setSpec] = useState<Spec | null>(initial);
  const [tab, setTab] = useState<"req" | "dry" | "design" | "tasks">("req");
  const [flow, setFlow] = useState<FlowGraph | null>(null);
  const [flowState, setFlowState] = useState<"idle" | "loading" | "error">("idle");
  const [flowError, setFlowError] = useState("");
  const [openRun, setOpenRun] = useState("");
  const [hop, setHop] = useState(0);
  const [sel, setSel] = useState<string>("");
  const [node, setNode] = useState<string>("");
  const [openApi, setOpenApi] = useState<string>("");
  const [reviewing, setReviewing] = useState(false);
  const [error, setError] = useState("");
  const req = useRef("");

  const load = useCallback((kind: "spec.load" | "spec.review" = "spec.load") => {
    req.current = `${kind}-${Date.now()}`;
    if (kind === "spec.review") setReviewing(true);
    setError("");
    postMessage(kind, { requestId: req.current });
  }, [postMessage]);
  useEffect(() => { if (!initial) load(); }, [initial, load]);
  const runDry = useCallback(() => { setFlowState("loading"); setFlowError(""); postMessage("map.flow", {}); }, [postMessage]);
  useEffect(() => { if (tab === "dry" && !flow && flowState === "idle") runDry(); }, [tab, flow, flowState, runDry]);
  useMessage(useCallback(({ type, payload }) => {
    const p = (payload ?? {}) as { requestId?: string; spec?: Spec; message?: string; review?: boolean };
    if (type === "canvas.projectChanged") { load(); setFlow(null); setFlowState("idle"); return; }
    if (type === "map.flowResult") { setFlow(payload as FlowGraph); setFlowState("idle"); return; }
    if (type === "map.flowError") { setFlowState("error"); setFlowError((payload as { message?: string })?.message || "분석하지 못했습니다."); return; }
    if (p.requestId !== req.current) return;
    if (type === "spec.result" && p.spec) { setSpec(p.spec); setReviewing(false); }
    if (type === "spec.error") { setReviewing(false); setError(p.message || "불러오지 못했습니다."); }
  }, [load]));

  const features = spec?.features ?? [];
  const current = features.find(f => f.key === sel) ?? features[0];
  const tasks = spec?.tasks ?? [];
  const openFile = useCallback((f: string, l: number) => { if (onOpenFile) onOpenFile(f, l); else postMessage("map.openFile", { id: f, line: l }); }, [onOpenFile, postMessage]);
  const busiest = useMemo(() => [...(spec?.services ?? [])].sort((a, b) => b.api - a.api)[0]?.name ?? "", [spec]);
  const shownNode = node || busiest;
  const nodeApis = useMemo(() => (spec?.endpoints ?? []).filter(a => a.service === shownNode), [spec, shownNode]);
  const dry: SpecRun | null = useMemo(() => (spec?.available && flow ? runSpec(spec, flow) : null), [spec, flow]);

  if (!spec) return <div className="rc-spec"><style>{SPEC_CSS}</style><div className="sp-empty">{error || "불러오는 중"}</div></div>;
  if (!spec.available) return <div className="rc-spec"><style>{SPEC_CSS}</style><div className="sp-empty">{spec.reason}</div></div>;
  const t = spec.tests ?? { passed: 0, total: 0, at: "", status: "" };
  const testsTone: Status = !t.total ? "unknown" : t.passed === t.total ? "ok" : "fail";
  const aiRows = current?.criteria.filter(c => c.source === "ai") ?? [];

  return (
    <div className="rc-spec" data-testid="spec-view">
      <style>{SPEC_CSS}</style>
      <div className="sp-head">
        <span className="sp-name" title={spec.project}>{spec.project}</span>
        <div className="sp-tabs" role="tablist" aria-label="명세">
          <button role="tab" aria-selected={tab === "req"} onClick={() => setTab("req")}>요구사항</button>
          <button role="tab" aria-selected={tab === "dry"} onClick={() => setTab("dry")}>Dry run{dry && dry.counts.fail + dry.counts.warn > 0 && <span className="sp-count">{dry.counts.fail + dry.counts.warn}</span>}</button>
          <button role="tab" aria-selected={tab === "design"} onClick={() => setTab("design")}>설계</button>
          <button role="tab" aria-selected={tab === "tasks"} onClick={() => setTab("tasks")}>작업{tasks.length > 0 && <span className="sp-count">{tasks.length}</span>}</button>
        </div>
        <div className="sp-right">
          <span className="sp-checks" title={t.at ? "마지막 로컬 실행의 통합 테스트" : "아직 로컬에서 실행하지 않음"}><Ic s={testsTone} />{t.total ? `테스트 ${t.passed}/${t.total}` : "테스트 전"}</span>
          <button type="button" className="sp-btn" aria-disabled={reviewing} title={spec.review?.fresh ? spec.review.summary : "요구사항을 문장으로 정리하고 빠진 기능을 찾습니다"} onClick={() => { if (!reviewing) load("spec.review"); }}>
            {reviewing ? "검토 중" : spec.review?.fresh ? "다시 검토" : "AI 검토"}
          </button>
        </div>
      </div>
      {error && <div className="sp-err" role="alert">{error}</div>}

      {tab === "req" && <div className="sp-main">
        <nav className="sp-side" aria-label="기능">
          {features.map(f => <button key={f.key} type="button" className="sp-item" aria-current={current?.key === f.key} onClick={() => { setSel(f.key); setOpenApi(""); }}>
            <Ic s={f.status} /><span className="lb">{f.label}</span><span className="n">{f.counts.api}</span>
          </button>)}
        </nav>
        {current ? <section className="sp-doc" aria-label={current.label}>
          <h2>{current.label}</h2>
          <p className="sp-meta">{current.services.join(", ")} · API {current.counts.api}개{current.counts.criteria ? ` · 기준 ${current.counts.ok}/${current.counts.criteria} 충족` : ""}</p>
          {aiRows.length > 0 && <>
            <div className="sp-sec">수용 기준</div>
            <div className="sp-list">
              {aiRows.map((c, i) => <div className="sp-li" key={i}><Ic s={c.status} /><span className="tx">{c.text}</span>{c.status === "missing" && <span className="sp-note">구현 없음</span>}</div>)}
            </div>
          </>}
          <div className="sp-sec">API</div>
          <div className="sp-list">
            {current.endpoints.map(a => { const id = `${a.service} ${a.method} ${a.path}`; return <ApiRow key={id} a={a} criteria={current.criteria} open={openApi === id} onToggle={() => setOpenApi(openApi === id ? "" : id)} onOpenFile={openFile} />; })}
          </div>
        </section> : <div className="sp-empty">API가 없습니다.</div>}
      </div>}

      {tab === "dry" && <div>
        <div className="sp-dry-bar">
          {dry ? <>
            <span className="st"><b>API {dry.total}</b></span>
            <span className="st"><Ic s="ok" />적합 <b>{dry.counts.ok}</b></span>
            <span className="st"><Ic s="warn" />확인 필요 <b>{dry.counts.warn}</b></span>
            <span className="st"><Ic s="fail" />불일치 <b>{dry.counts.fail}</b></span>
          </> : <span>{flowState === "loading" ? "호출 경로를 따라가는 중" : flowState === "error" ? flowError : "명세의 모든 API 를 코드에서 따라갑니다"}</span>}
          <button type="button" className="sp-btn" aria-disabled={flowState === "loading"} title="코드를 실행하지 않고 호출 그래프만 따라갑니다" onClick={() => { if (flowState !== "loading") runDry(); }}>{flowState === "loading" ? "분석 중" : dry ? "다시 실행" : "Dry run 실행"}</button>
        </div>
        {dry && <table className="sp-dry">
          <colgroup><col style={{ width: 52 }} /><col style={{ width: 76 }} /><col /><col style={{ width: "22%" }} /><col style={{ width: "24%" }} /><col style={{ width: 76 }} /></colgroup>
          <thead><tr><th /><th>Method</th><th>경로</th><th>지나는 서비스</th><th>닿는 곳</th><th style={{ textAlign: "right", paddingRight: 24 }}>함수</th></tr></thead>
          <tbody>
            {features.map(f => <React.Fragment key={f.key}>
              <tr className="grp"><td colSpan={6}>{f.label}</td></tr>
              {f.endpoints.map(a => {
                const run = dry.runs[apiKey(a)];
                if (!run) return null;
                const open = openRun === run.key;
                const shown = run.hops[Math.min(hop, run.hops.length - 1)];
                const v: Record<Verdict, Status> = { ok: "ok", warn: "warn", fail: "fail" };
                return <React.Fragment key={run.key}>
                  <tr className="r" aria-expanded={open} onClick={() => { setOpenRun(open ? "" : run.key); setHop(run.hops.length > 1 ? 1 : 0); }}>
                    <td className="ic"><Ic s={v[run.verdict]} /></td>
                    <td className="m">{a.method}</td>
                    <td className="p" title={a.path}>{a.path}</td>
                    <td className="c">{run.hops.map(h => h.service).join(" → ")}</td>
                    <td className="e">{effectText(run.effects)}</td>
                    <td className="n">{run.functions || "—"}</td>
                  </tr>
                  {open && <tr className="x"><td colSpan={6}>
                    {run.reasons.length > 0 && <ul className="sp-why">{run.reasons.map((r, i) => <li key={i}><Ic s={run.verdict === "fail" ? "fail" : "warn"} size={12} />{r}</li>)}</ul>}
                    {run.hops.length > 1 && <div className="sp-hops">{run.hops.map((h, i) => <button key={i} type="button" aria-pressed={i === Math.min(hop, run.hops.length - 1)} onClick={() => setHop(i)}>{h.service} · {h.api.method} {h.api.path}</button>)}</div>}
                    {flow && shown.entry ? <div className="wrap"><FlowDryRun graph={flow} initialEntry={shown.entry} onOpen={openFile} /></div> : <p className="sp-note">이 단계의 코드를 찾지 못해 흐름을 그릴 수 없습니다.</p>}
                  </td></tr>}
                </React.Fragment>;
              })}
            </React.Fragment>)}
          </tbody>
        </table>}
        {dry && <p className="sp-note" style={{ padding: "10px 24px 14px", margin: 0 }}>코드를 실행하지 않고 호출 그래프를 따라간 결과입니다. 실제 실행 순서 · 값과 다를 수 있습니다.</p>}
      </div>}

      {tab === "design" && <div className="sp-pad">
        <Flow spec={spec} sel={shownNode} onSel={(n) => { setNode(n); setOpenApi(""); }} />
        {nodeApis.length > 0 && <>
          <div className="sp-sec">{shownNode} · API {nodeApis.length}개</div>
          <div className="sp-list" aria-label={`${shownNode} API`}>
            {nodeApis.map(raw => {
              const id = `${raw.service} ${raw.method} ${raw.path}`;
              //: 기능 쪽 API 가 테스트 결과를 들고 있다 — 같은 API 를 찾아 그 결과로 그린다.
              const same = (x: SpecApi) => x.service === raw.service && x.method === raw.method && x.path === raw.path;
              const f = features.find(x => x.endpoints.some(same));
              const a = f?.endpoints.find(same) ?? raw;
              return <ApiRow key={id} a={a} criteria={f?.criteria ?? []} open={openApi === id} onToggle={() => setOpenApi(openApi === id ? "" : id)} onOpenFile={openFile} />;
            })}
          </div>
        </>}
        {onOpenStructure && <p style={{ margin: "16px 0 0" }}><button type="button" className="sp-btn" onClick={onOpenStructure}>파일 구조 보기</button></p>}
      </div>}

      {tab === "tasks" && <div className="sp-pad">
        {tasks.length === 0 ? <div className="sp-empty">남은 작업이 없습니다.</div> : <div className="sp-list">
          {tasks.map(task => <div className="sp-li" key={task.id}>
            <Ic s={task.severity === "high" ? "fail" : task.severity === "medium" ? "warn" : "unknown"} />
            <span className="tx">{task.title}{task.detail && <span className="sp-sub">{task.detail}</span>}</span>
            <span className={`sp-sev ${task.severity}`}>{task.severity === "high" ? "높음" : task.severity === "medium" ? "보통" : "낮음"}</span>
            {onStartTask && task.id !== "run:local" && <button type="button" className="sp-btn" onClick={() => onStartTask(task)}>시작</button>}
          </div>)}
        </div>}
      </div>}
    </div>
  );
};

/** 작업을 코드 생성 화면에 넘길 요청 문장. */
export function taskPrompt(task: SpecTask): string {
  return `${task.title}${task.detail ? `\n${task.detail}` : ""}${task.feature ? `\n(기능: ${task.feature})` : ""}`;
}

export default SpecView;
