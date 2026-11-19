/**
 * 명세 — 요구사항 · 설계 · 작업 (Kiro 의 명세 화면을 참고, 4.0.1)
 *
 * 실사용 2026-11-19: "글 너무 많아, 무조건 시각적". 상태는 점 · 막대 · 고리, 구조는 그림, 설명은 마우스를 올릴 때만.
 *   · 요구사항: 기능마다 통과 막대, 수용 기준은 아이콘 + 한 줄, API 는 방법 색 · 자물쇠/지구 아이콘
 *   · 설계: 서비스 → 서비스 → DB 흐름도. 노드를 누르면 그 서비스의 API
 *   · 작업: 심각도 색 막대 + 제목 + ▶
 * 데이터는 Core /api/project/spec. AI 검토는 ✦ 를 누를 때만.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

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
.rc-spec{--sp-line:var(--rc-line2,rgba(160,190,220,.14));--sp-soft:var(--rc-tile,rgba(148,178,210,.035));--sp-soft2:var(--rc-tile2,rgba(148,178,210,.065));
  --sp-ok:var(--rc-ok,#3dd68c);--sp-bad:var(--rc-bad,#f27272);--sp-warn:var(--rc-warn,#f2b544);--sp-mute:var(--rc-fg3,#67768a);
  border:1px solid var(--sp-line);border-radius:14px;overflow:hidden;background:var(--rc-solid,#0d131a);color:var(--rc-fg,#eef3f8);font-size:13px}
.rc-spec button{font:inherit;color:inherit}
.sp-bar{display:flex;align-items:center;gap:12px;height:48px;padding:0 12px 0 16px;border-bottom:1px solid var(--sp-line)}
.sp-bar .nm{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px}
.sp-seg{display:flex;gap:2px;padding:3px;border-radius:9px;background:var(--sp-soft2);margin:0 auto}
.sp-seg button{all:unset;cursor:pointer;display:flex;align-items:center;gap:6px;padding:5px 12px;border-radius:7px;font-size:12.5px;color:var(--sp-mute)}
.sp-seg button[aria-selected=true]{background:var(--rc-solid,#0d131a);color:var(--rc-fg,#eef3f8);box-shadow:0 0 0 1px var(--sp-line)}
.sp-seg em{font-style:normal;font-size:10.5px;min-width:16px;text-align:center;padding:0 4px;border-radius:999px;background:var(--sp-warn);color:#1a1300;font-weight:700}
.sp-ring{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:600;font-variant-numeric:tabular-nums;color:var(--rc-fg2,#9aa8b8)}
.sp-icon{all:unset;cursor:pointer;width:32px;height:32px;display:grid;place-items:center;border-radius:9px;border:1px solid var(--sp-line);color:var(--rc-fg2,#9aa8b8)}
.sp-icon:hover{background:var(--sp-soft2);color:var(--rc-fg,#eef3f8)}.sp-icon[aria-busy=true]{animation:sp-spin 1.2s linear infinite}
@keyframes sp-spin{to{transform:rotate(360deg)}}
.sp-dot{width:7px;height:7px;border-radius:50%;flex:none;display:inline-block;background:var(--sp-mute);opacity:.5}
.sp-dot.ok{background:var(--sp-ok);opacity:1}.sp-dot.fail{background:var(--sp-bad);opacity:1}.sp-dot.warn,.sp-dot.missing,.sp-dot.partial{background:var(--sp-warn);opacity:1}
.sp-main{display:grid;grid-template-columns:220px minmax(0,1fr);min-height:420px}
.sp-side{border-right:1px solid var(--sp-line);padding:8px}
.sp-item{all:unset;box-sizing:border-box;cursor:pointer;width:100%;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:5px 8px;padding:8px 10px;border-radius:8px;color:var(--rc-fg2,#9aa8b8)}
.sp-item:hover{background:var(--sp-soft)}.sp-item[aria-current=true]{background:var(--sp-soft2);color:var(--rc-fg,#eef3f8)}
.sp-item span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sp-item small{color:var(--sp-mute);font-variant-numeric:tabular-nums;font-size:11px}
.sp-meter{grid-column:1/-1;display:flex;height:3px;border-radius:2px;overflow:hidden;background:var(--sp-soft2)}
.sp-meter i{display:block;height:100%}.sp-meter .ok{background:var(--sp-ok)}.sp-meter .fail{background:var(--sp-bad)}.sp-meter .missing{background:var(--sp-warn)}
.sp-doc{padding:20px 24px;min-width:0;display:flex;flex-direction:column;gap:14px}
.sp-title{display:flex;align-items:center;gap:12px}
.sp-title h2{margin:0;font-size:17px;font-weight:650}
.sp-blocks{display:flex;gap:3px;margin-left:auto;flex-wrap:wrap;justify-content:flex-end}
.sp-blocks i{width:14px;height:6px;border-radius:2px;background:var(--sp-soft2)}
.sp-blocks i.ok{background:var(--sp-ok)}.sp-blocks i.fail{background:var(--sp-bad)}.sp-blocks i.missing{background:var(--sp-warn)}
.sp-box{border:1px solid var(--sp-line);border-radius:10px;overflow:hidden}
.sp-row{display:flex;align-items:center;gap:11px;padding:9px 14px;border-top:1px solid var(--sp-line)}
.sp-row:first-child{border-top:0}
.sp-chk{width:18px;height:18px;border-radius:50%;flex:none;display:grid;place-items:center;background:var(--sp-soft2);color:var(--sp-mute)}
.sp-chk.ok{background:color-mix(in srgb,var(--sp-ok) 18%,transparent);color:var(--sp-ok)}
.sp-chk.fail{background:color-mix(in srgb,var(--sp-bad) 18%,transparent);color:var(--sp-bad)}
.sp-chk.missing{background:color-mix(in srgb,var(--sp-warn) 18%,transparent);color:var(--sp-warn)}
.sp-txt{flex:1;min-width:0}
.sp-row.missing .sp-txt{color:var(--sp-warn)}
.sp-api{all:unset;box-sizing:border-box;cursor:pointer;display:grid;grid-template-columns:54px minmax(0,1fr) 16px 10px;gap:12px;align-items:center;width:100%;padding:8px 14px;border-top:1px solid var(--sp-line)}
.sp-api:first-child{border-top:0}.sp-api:hover{background:var(--sp-soft)}
.sp-m{font:700 10px var(--rc-mono,monospace);text-align:center;padding:2px 0;border-radius:5px}
.sp-m.GET{color:#5fb8f0;background:rgba(95,184,240,.12)}.sp-m.POST{color:#3dd68c;background:rgba(61,214,140,.12)}
.sp-m.PATCH,.sp-m.PUT{color:#f2b544;background:rgba(242,181,68,.12)}.sp-m.DELETE{color:#f27272;background:rgba(242,114,114,.12)}
.sp-p{font:12px var(--rc-mono,monospace);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sp-au{color:var(--sp-mute);display:grid;place-items:center}.sp-au.admin{color:var(--sp-warn)}
.sp-more{padding:8px 14px 10px 80px;font-size:12px;color:var(--rc-fg2,#9aa8b8);display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;border-top:1px dashed var(--sp-line)}
.sp-link{all:unset;cursor:pointer;color:var(--rc-teal,#35a9d1);font:12px var(--rc-mono,monospace)}.sp-link:hover{text-decoration:underline}
.sp-pill{display:inline-flex;align-items:center;gap:5px;padding:2px 8px 2px 3px;border-radius:999px;background:var(--sp-soft2);font-size:11.5px}
.sp-pad{padding:18px;display:flex;flex-direction:column;gap:16px}
.sp-flow{width:100%;height:auto;display:block}
.sp-flow .nd{cursor:pointer}.sp-flow .nd rect.bg{fill:var(--sp-soft);stroke:var(--sp-line);transition:stroke .15s}
.sp-flow .nd:hover rect.bg{stroke:var(--rc-fg3,#67768a)}.sp-flow .nd.sel rect.bg{stroke:var(--rc-teal,#35a9d1);stroke-width:1.6;fill:var(--sp-soft2)}
.sp-flow .nd text{fill:var(--rc-fg,#eef3f8);font-size:12.5px;font-weight:600}
.sp-flow .nd text.sub{fill:var(--sp-mute);font-size:10.5px;font-weight:500}
.sp-flow path.ed{fill:none;stroke:var(--rc-fg3,#67768a);stroke-opacity:.45;stroke-width:1.3}
.sp-flow path.ed.hl{stroke:var(--rc-teal,#35a9d1);stroke-opacity:.9;stroke-width:1.6}
.sp-task{display:flex;align-items:center;gap:12px;padding:12px 14px 12px 0;border-top:1px solid var(--sp-line)}.sp-task:first-child{border-top:0}
.sp-sev{width:3px;align-self:stretch;border-radius:0 3px 3px 0;flex:none}
.sp-sev.high{background:var(--sp-bad)}.sp-sev.medium{background:var(--sp-warn)}.sp-sev.low{background:var(--sp-mute)}
.rc-spec .sp-play{all:unset;cursor:pointer;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;background:var(--rc-fg,#eef3f8);color:var(--rc-solid,#0d131a);flex:none}
.rc-spec .sp-play:hover{opacity:.85}
.sp-chip{all:unset;cursor:pointer;align-self:flex-start;display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:9px;border:1px solid var(--sp-line);font-size:12px;color:var(--rc-fg2,#9aa8b8)}
.sp-chip:hover{background:var(--sp-soft2);color:var(--rc-fg,#eef3f8)}
.sp-empty{padding:44px 20px;display:grid;place-items:center;gap:10px;color:var(--sp-mute)}
.sp-err{margin:10px 14px 0;padding:8px 12px;border-radius:9px;font-size:12px;background:color-mix(in srgb,var(--sp-bad) 12%,transparent);color:var(--sp-bad)}
@media(max-width:760px){.sp-main{grid-template-columns:1fr}.sp-side{border-right:0;border-bottom:1px solid var(--sp-line);display:flex;overflow-x:auto;gap:4px}.sp-item{width:150px;flex:none}.sp-seg{margin:0}.sp-bar{flex-wrap:wrap;height:auto;padding:8px 12px}}
`;

// ── 아이콘 ───────────────────────────────────────────────────────────────
const I = {
  check: <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>,
  x: <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg>,
  bang: <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round"><path d="M12 5v9M12 19v.5" /></svg>,
  lock: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></svg>,
  globe: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18" /></svg>,
  crown: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round"><path d="M3 18h18L19 7l-4 4-3-6-3 6-4-4z" /></svg>,
  spark: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinejoin="round"><path d="M12 3l1.8 5.4L19 10l-5.2 1.6L12 17l-1.8-5.4L5 10l5.2-1.6z" /><path d="M19 16l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z" /></svg>,
  list: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M9 6h11M9 12h11M9 18h11" /><path d="M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2" /></svg>,
  flow: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="6" height="5" rx="1.5" /><rect x="15" y="4" width="6" height="5" rx="1.5" /><rect x="9" y="15" width="6" height="5" rx="1.5" /><path d="M9 6.5h6M18 9v3H6v3M12 12v3" /></svg>,
  todo: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><rect x="4" y="4" width="16" height="16" rx="3" /><path d="M8 12l3 3 5-6" /></svg>,
  play: <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15l13-7.5z" /></svg>,
  files: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /></svg>,
  done: <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9" /><path d="M8 12.5l2.8 2.8L16.5 9.5" /></svg>,
};

const Check: React.FC<{ s: Status }> = ({ s }) => (
  <span className={`sp-chk ${s === "ok" || s === "fail" || s === "missing" ? s : ""}`} aria-label={STATUS_LABEL[s]} title={STATUS_LABEL[s]}>
    {s === "ok" ? I.check : s === "fail" ? I.x : s === "missing" ? I.bang : <span style={{ width: 5, height: 5, borderRadius: "50%", background: "currentColor", opacity: .6 }} />}
  </span>
);

/** 테스트 통과 고리 — 숫자 하나와 원. */
const Ring: React.FC<{ passed: number; total: number }> = ({ passed, total }) => {
  const r = 8, c = 2 * Math.PI * r, p = total ? passed / total : 0;
  const color = !total ? "var(--sp-mute)" : passed === total ? "var(--sp-ok)" : "var(--sp-bad)";
  return <span className="sp-ring" aria-label={total ? `테스트 ${passed}/${total}` : "테스트 전"} title={total ? "마지막 로컬 실행의 통합 테스트" : "아직 로컬 실행 전"}>
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden><circle cx="10" cy="10" r={r} fill="none" stroke="var(--sp-soft2)" strokeWidth="3" />
      <circle cx="10" cy="10" r={r} fill="none" stroke={color} strokeWidth="3" strokeLinecap="round" strokeDasharray={`${c * p} ${c}`} transform="rotate(-90 10 10)" /></svg>
    {total ? `${passed}/${total}` : "—"}
  </span>;
};

const Meter: React.FC<{ f: SpecFeature }> = ({ f }) => {
  const n = Math.max(1, f.criteria.length);
  const miss = f.criteria.filter(c => c.status === "missing").length;
  return <span className="sp-meter" aria-hidden><i className="ok" style={{ width: `${f.counts.ok / n * 100}%` }} /><i className="fail" style={{ width: `${f.counts.fail / n * 100}%` }} /><i className="missing" style={{ width: `${miss / n * 100}%` }} /></span>;
};

const testBrief = (c: SpecCriterion): string =>
  c.text.startsWith("로그인하지") ? "로그인 없이 → 거절" : c.text.startsWith("요청을 받으면") ? "서버 오류 없음"
    : c.text.replace(/^올바른 요청에 /, "").replace(/ 로 응답한다$/, "").replace(/한다$/, "");

function apiStatus(a: SpecApi): Status {
  if (!a.implemented) return "fail";
  if (a.tests.some(t => t.status === "fail")) return "fail";
  if (a.tests.length && a.tests.every(t => t.status === "ok")) return "ok";
  return "unknown";
}

const ApiRow: React.FC<{ a: SpecApi; criteria: SpecCriterion[]; open: boolean; onToggle: () => void; onOpenFile: (f: string, l: number) => void }> = ({ a, criteria, open, onToggle, onOpenFile }) => {
  const mine = criteria.filter(c => c.source === "test" && c.api === `${a.method} ${a.path}`);
  const st = apiStatus(a);
  const who = a.admin ? "관리자" : a.auth ? "로그인 필요" : "공개";
  return <>
    <button type="button" className="sp-api" aria-expanded={open} onClick={onToggle}>
      <span className={`sp-m ${a.method}`}>{a.method}</span><span className="sp-p" title={a.path}>{a.path}</span>
      <span className={`sp-au${a.admin ? " admin" : ""}`} aria-label={who} title={who}>{a.admin ? I.crown : a.auth ? I.lock : I.globe}</span>
      <span className={`sp-dot ${st}`} aria-label={STATUS_LABEL[st]} title={STATUS_LABEL[st]} />
    </button>
    {open && <div className="sp-more">
      {a.implemented
        ? <button type="button" className="sp-link" onClick={() => onOpenFile(a.file, a.line)}>{a.file.split("/").pop()}:{a.line}</button>
        : <span className="sp-pill" style={{ color: "var(--sp-bad)" }}><Check s="fail" />코드 없음</span>}
      {mine.map(c => <span key={c.test} className="sp-pill" title={c.text}><Check s={c.status} />{testBrief(c)}{c.got !== undefined && c.status === "fail" ? ` → ${c.got}` : ""}</span>)}
    </div>}
  </>;
};

// ── 설계: 흐름도 ────────────────────────────────────────────────────────
interface FlowNode { id: string; label: string; sub: string; kind: string; col: number; x: number; y: number }
const NW = 150, NH = 46, CG = 64, RG = 14;
const KIND_COLOR: Record<string, string> = { web: "#35a9d1", api: "#5f8ff0", worker: "#a07ef0", db: "#e0a33a" };

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
      sub: s.kind === "web" ? "화면" : s.kind === "worker" ? "워커" : `API ${s.api}` })),
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

const Flow: React.FC<{ spec: Spec; sel: string; onSel: (n: string) => void }> = ({ spec, sel, onSel }) => {
  const { nodes, edges, w, h } = useMemo(() => flowLayout(spec.services ?? [], spec.databases ?? []), [spec]);
  const at = Object.fromEntries(nodes.map(n => [n.id, n]));
  const live = spec.deploy?.services ?? {};
  return <svg className="sp-flow" viewBox={`-2 -2 ${w + 4} ${h + 4}`} style={{ maxHeight: Math.max(160, h + 40) }} role="img" aria-label="서비스 흐름도">
    {edges.map(([a, b]) => { const p = at[a], q = at[b]; if (!p || !q) return null;
      const x1 = p.x + NW, y1 = p.y + NH / 2, x2 = q.x, y2 = q.y + NH / 2, mx = (x1 + x2) / 2;
      return <path key={`${a}-${b}`} className={`ed${sel === a || sel === b ? " hl" : ""}`} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} />; })}
    {nodes.map(n => { const st = live[n.id];
      return <g key={n.id} className={`nd${sel === n.id ? " sel" : ""}`} transform={`translate(${n.x},${n.y})`} onClick={() => onSel(n.id)} role="button" aria-label={n.label}>
        <rect width={NW} height={NH} rx="10" fill="var(--rc-solid,#0d131a)" />
        <rect className="bg" width={NW} height={NH} rx="10" />
        <rect x="0" y="9" width="3" height={NH - 18} rx="1.5" fill={KIND_COLOR[n.kind]} />
        <text x="14" y="20">{n.label.length > 16 ? n.label.slice(0, 15) + "…" : n.label}</text>
        <text className="sub" x="14" y="35">{n.sub}</text>
        {st && <circle cx={NW - 12} cy="12" r="3.5" fill={st === "ok" ? "var(--sp-ok)" : st === "fail" ? "var(--sp-bad)" : "var(--sp-mute)"}><title>{st === "ok" ? "실행 중" : st === "fail" ? "시작 못 함" : "확인 전"}</title></circle>}
      </g>; })}
  </svg>;
};

// ── 화면 ─────────────────────────────────────────────────────────────────
export const SpecView: React.FC<{ initial?: Spec | null; onStartTask?: (task: SpecTask) => void; onOpenFile?: (file: string, line: number) => void; onOpenStructure?: () => void }> = ({ initial = null, onStartTask, onOpenFile, onOpenStructure }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [spec, setSpec] = useState<Spec | null>(initial);
  const [tab, setTab] = useState<"req" | "design" | "tasks">("req");
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
  useMessage(useCallback(({ type, payload }) => {
    const p = (payload ?? {}) as { requestId?: string; spec?: Spec; message?: string; review?: boolean };
    if (type === "canvas.projectChanged") { load(); return; }
    if (p.requestId !== req.current) return;
    if (type === "spec.result" && p.spec) { setSpec(p.spec); setReviewing(false); }
    if (type === "spec.error") { setReviewing(false); setError(p.message || "불러오지 못했어요."); }
  }, [load]));

  const features = spec?.features ?? [];
  const current = features.find(f => f.key === sel) ?? features[0];
  const tasks = spec?.tasks ?? [];
  const openFile = useCallback((f: string, l: number) => { if (onOpenFile) onOpenFile(f, l); else postMessage("map.openFile", { id: f, line: l }); }, [onOpenFile, postMessage]);
  const busiest = useMemo(() => [...(spec?.services ?? [])].sort((a, b) => b.api - a.api)[0]?.name ?? "", [spec]);
  const shownNode = node || busiest;
  const nodeApis = useMemo(() => (spec?.endpoints ?? []).filter(a => a.service === shownNode), [spec, shownNode]);

  if (!spec) return <div className="rc-spec"><style>{SPEC_CSS}</style><div className="sp-empty">{error || "…"}</div></div>;
  if (!spec.available) return <div className="rc-spec"><style>{SPEC_CSS}</style><div className="sp-empty">{spec.reason}</div></div>;
  const t = spec.tests ?? { passed: 0, total: 0, at: "", status: "" };
  const aiRows = current?.criteria.filter(c => c.source === "ai") ?? [];
  const reviewTitle = reviewing ? "검토 중…" : spec.review?.fresh ? `AI 검토 다시 하기${spec.review.summary ? `\n\n${spec.review.summary}` : ""}` : "AI 검토 — 요구사항 정리 · 빠진 기능 찾기";

  return (
    <div className="rc-spec" data-testid="spec-view">
      <style>{SPEC_CSS}</style>
      <div className="sp-bar">
        <span className="nm" title={spec.project}>{spec.project}</span>
        <div className="sp-seg" role="tablist" aria-label="명세">
          <button role="tab" aria-selected={tab === "req"} onClick={() => setTab("req")}>{I.list}요구사항</button>
          <button role="tab" aria-selected={tab === "design"} onClick={() => setTab("design")}>{I.flow}설계</button>
          <button role="tab" aria-selected={tab === "tasks"} onClick={() => setTab("tasks")}>{I.todo}작업{tasks.length > 0 && <em>{tasks.length}</em>}</button>
        </div>
        <Ring passed={t.passed} total={t.total} />
        <button type="button" className="sp-icon" aria-label="검토" title={reviewTitle} aria-busy={reviewing} onClick={() => { if (!reviewing) load("spec.review"); }}>{I.spark}</button>
      </div>
      {error && <div className="sp-err" role="alert">{error}</div>}

      {tab === "req" && <div className="sp-main">
        <nav className="sp-side" aria-label="기능">
          {features.map(f => <button key={f.key} type="button" className="sp-item" aria-current={current?.key === f.key} title={STATUS_LABEL[f.status]} onClick={() => { setSel(f.key); setOpenApi(""); }}>
            <span>{f.label}</span><small>{f.counts.api}</small><Meter f={f} />
          </button>)}
        </nav>
        {current ? <section className="sp-doc" aria-label={current.label}>
          <div className="sp-title">
            <h2>{current.label}</h2>
            <span className="sp-blocks" aria-hidden>{current.criteria.map((c, i) => <i key={i} className={c.status} title={c.text} />)}</span>
          </div>
          {aiRows.length > 0 && <div className="sp-box">
            {aiRows.map((c, i) => <div className={`sp-row ${c.status}`} key={i}><Check s={c.status} /><span className="sp-txt">{c.text}</span></div>)}
          </div>}
          <div className="sp-box">
            {current.endpoints.map(a => { const id = `${a.service} ${a.method} ${a.path}`; return <ApiRow key={id} a={a} criteria={current.criteria} open={openApi === id} onToggle={() => setOpenApi(openApi === id ? "" : id)} onOpenFile={openFile} />; })}
          </div>
        </section> : <div className="sp-empty">—</div>}
      </div>}

      {tab === "design" && <div className="sp-pad">
        <Flow spec={spec} sel={shownNode} onSel={(n) => { setNode(n); setOpenApi(""); }} />
        {nodeApis.length > 0 && <div className="sp-box" aria-label={`${shownNode} API`}>
          {nodeApis.map(raw => {
            const id = `${raw.service} ${raw.method} ${raw.path}`;
            //: 기능 쪽 API 가 테스트 결과를 들고 있다 — 같은 API 를 찾아 그 결과로 그린다.
            const same = (x: SpecApi) => x.service === raw.service && x.method === raw.method && x.path === raw.path;
            const f = features.find(x => x.endpoints.some(same));
            const a = f?.endpoints.find(same) ?? raw;
            return <ApiRow key={id} a={a} criteria={f?.criteria ?? []} open={openApi === id} onToggle={() => setOpenApi(openApi === id ? "" : id)} onOpenFile={openFile} />;
          })}
        </div>}
        {onOpenStructure && <button type="button" className="sp-chip" onClick={onOpenStructure}>{I.files}파일 구조</button>}
      </div>}

      {tab === "tasks" && <div className="sp-pad">
        {tasks.length === 0 ? <div className="sp-empty" style={{ color: "var(--sp-ok)" }} aria-label="할 작업 없음">{I.done}</div> : <div className="sp-box">
          {tasks.map(task => <div className="sp-task" key={task.id} title={task.detail}>
            <span className={`sp-sev ${task.severity}`} aria-label={task.severity === "high" ? "높음" : task.severity === "medium" ? "보통" : "낮음"} />
            <span className="sp-txt">{task.title}</span>
            {onStartTask && task.id !== "run:local" && <button type="button" className="sp-play" aria-label="시작" title="코드 생성으로 시작" onClick={() => onStartTask(task)}>{I.play}</button>}
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
