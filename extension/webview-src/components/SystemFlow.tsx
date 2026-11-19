/**
 * 전체 흐름 (4.0.1) — 프로젝트 코드 전체가 어떻게 흐르는지 한 장으로, 명세의 API 마다 그 위를 따라간다.
 *
 * 실사용 2026-11-19: "API 표 말고, 전체 코드의 흐름을 알 수 있게. 각각의 명세 · API 도 확인하고".
 *   · 그림: 서비스 상자(왼쪽 화면 → 게이트웨이 → 서비스 → 오른쪽 DB) 안에 실제로 지나가는 파일, 파일 사이 호출 선,
 *     게이트웨이 → 받는 서비스 선, DB 에 닿는 파일 → DB 선. 명세의 모든 API dry run 을 합친 것이다.
 *   · 왼쪽: 기능 → API(판정 아이콘). 누르면 그 API 가 지나는 길만 밝게, 아래에 호출 순서(함수 · 파일 · 하는 일)와 단계 재생.
 * 코드를 실행하지 않는 정적 분석이다.
 */
import React, { useEffect, useMemo, useState } from "react";
import type { EffectKind, FlowGraph, FlowNode } from "./dryRunModel";
import type { Spec, SpecFeature, SpecService } from "./SpecView";
import { ApiRun, SpecRun } from "./specDryRun";

export const SYSTEM_FLOW_CSS = `
.sf{display:grid;grid-template-columns:260px minmax(0,1fr);min-height:560px}
.sf-side{border-right:1px solid var(--sp-line);overflow:auto;max-height:820px;padding:6px 0}
.sf-grp{padding:12px 16px 4px;font-size:11.5px;font-weight:600;color:var(--sp-mute)}
.sf-api{all:unset;box-sizing:border-box;cursor:pointer;display:grid;grid-template-columns:16px 46px minmax(0,1fr);gap:8px;align-items:center;width:100%;padding:5px 16px;font-size:12px}
.sf-api:hover{background:var(--sp-hover)}.sf-api[aria-current=true]{background:var(--sp-sel)}
.sf-api .m{font:600 10.5px var(--sp-mono);color:var(--sp-fg2)}
.sf-api .p{font:12px var(--sp-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sf-main{min-width:0;display:flex;flex-direction:column}
.sf-canvas{overflow:auto;max-height:62vh;padding:16px 20px;border-bottom:1px solid var(--sp-line)}
.sf-canvas svg{display:block}
.sf-svc rect.box{fill:rgba(148,178,210,.03);stroke:var(--sp-line)}
.sf-svc text.t{fill:var(--sp-fg);font-size:12.5px;font-weight:600}
.sf-svc text.s{fill:var(--sp-mute);font-size:11px}
.sf-file rect{fill:var(--rc-solid,#0d1117);stroke:var(--sp-line)}
.sf-file text{fill:var(--sp-fg2);font-size:11.5px}
.sf-file text.k{fill:var(--sp-mute);font-size:10px;font-weight:600}
.sf-file.on rect{stroke:rgba(148,178,210,.55)}.sf-file.on text{fill:var(--sp-fg)}
.sf-file.cur rect{stroke:var(--rc-teal,#2f81f7);stroke-width:1.6}
.sf-dim{opacity:.28}
.sf-ed{fill:none;stroke:rgba(148,178,210,.22);stroke-width:1}
.sf-ed.x{stroke-dasharray:4 3}
.sf-ed.on{stroke:rgba(148,178,210,.8);stroke-width:1.4}
.sf-ed.cur{stroke:var(--rc-teal,#2f81f7);stroke-width:1.8}
.sf-db rect{fill:var(--rc-solid,#0d1117);stroke:var(--sp-line)}
.sf-db text{fill:var(--sp-fg);font-size:12px;font-weight:500}.sf-db text.s{fill:var(--sp-mute);font-size:10.5px;font-weight:400}
.sf-db.on rect{stroke:rgba(148,178,210,.55)}
.sf-detail{padding:14px 20px 18px;display:grid;gap:10px;min-height:180px}
.sf-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sf-head b{font:600 13px var(--sp-mono)}
.sf-head .route{color:var(--sp-fg2);font-size:12px}
.sf-ctrl{margin-left:auto;display:flex;align-items:center;gap:6px;font-size:12px;color:var(--sp-fg2);font-variant-numeric:tabular-nums}
.sf-ctrl button{all:unset;cursor:pointer;width:26px;height:26px;display:grid;place-items:center;border-radius:6px;border:1px solid var(--sp-line);color:var(--sp-fg2)}
.sf-ctrl button:hover{background:var(--sp-hover);color:var(--sp-fg)}
.sf-why{display:grid;gap:4px;font-size:12.5px}.sf-why div{display:flex;align-items:center;gap:8px}
.sf-steps{border:1px solid var(--sp-line);border-radius:6px;max-height:260px;overflow:auto;font-size:12px}
.sf-step{all:unset;box-sizing:border-box;cursor:pointer;display:grid;grid-template-columns:30px minmax(0,1fr) auto;gap:10px;align-items:center;width:100%;padding:4px 12px;border-top:1px solid var(--sp-line)}
.sf-step:first-child{border-top:0}.sf-step:hover{background:var(--sp-hover)}
.sf-step[aria-current=true]{background:var(--sp-sel)}
.sf-step .n{color:var(--sp-mute);font-variant-numeric:tabular-nums;text-align:right}
.sf-step .f{font:12px var(--sp-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sf-step .f small{color:var(--sp-mute);margin-left:8px;font-family:inherit}
.sf-step .e{display:flex;gap:4px}
.sf-tag{font-size:10.5px;padding:0 6px;border-radius:999px;border:1px solid var(--sp-line);color:var(--sp-fg2);line-height:17px;white-space:nowrap}
.sf-tag.db{color:#a371f7;border-color:rgba(163,113,247,.4)}.sf-tag.http{color:#58a6ff;border-color:rgba(88,166,255,.4)}
.sf-tag.secret,.sf-tag.exec{color:#d29922;border-color:rgba(210,153,34,.4)}
.sf-hop{font-size:11px;font-weight:600;color:var(--sp-mute);padding:6px 12px 2px;border-top:1px solid var(--sp-line)}
.sf-hop:first-child{border-top:0}
.sf-hint{color:var(--sp-mute);font-size:12.5px}
@media(max-width:860px){.sf{grid-template-columns:1fr}.sf-side{max-height:220px;border-right:0;border-bottom:1px solid var(--sp-line)}}
`;

/** 서비스 열 — 서비스는 자기를 부르는 쪽보다 한 칸 오른쪽(화면 → 게이트웨이 → 서비스), DB 는 맨 오른쪽 열. */
export function serviceColumns(services: SpecService[]): { col: Record<string, number>; dbCol: number } {
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
  return { col: depth, dbCol: services.length ? Math.max(...services.map(s => depth[s.name])) + 1 : 0 };
}

const EFFECT_TAG: Record<EffectKind, string> = { db: "DB", http: "호출", aws: "AWS", secret: "비밀값", fs: "파일", exec: "명령" };
const CW = 236, CG = 72, FH = 30, FG = 6, HEAD = 40, PAD = 10, SG = 18, DBH = 44;

/** 파일 역할 순서 — 들어오는 곳(컨트롤러) → 검사 → 처리 → 밖으로 나가는 곳 → 데이터 */
function roleRank(file: string): number {
  const f = file.toLowerCase().split("/").pop() ?? "";
  if (/controller|route|router|handler|main\./.test(f)) return 0;
  if (/guard|auth|middleware|identity|interceptor|pipe/.test(f)) return 1;
  if (/service|usecase|logic/.test(f)) return 2;
  if (/client|proxy|producer|gateway|http|api\./.test(f)) return 3;
  if (/repo|prisma|db|schema|model|entity|sql/.test(f)) return 5;
  return 4;
}
function roleTag(file: string): string {
  return ["입구", "검사", "처리", "외부", "기타", "데이터"][roleRank(file)];
}

interface FileBox { file: string; service: string; x: number; y: number; fns: Set<string>; effects: Set<EffectKind> }
interface SvcBox { name: string; kind: string; x: number; y: number; h: number; sub: string; aux: number }
interface DbBox { name: string; engine: string; x: number; y: number }
interface Edge { id: string; from: [number, number]; to: [number, number]; kind: "call" | "hop" | "db" | "svc"; same?: boolean }

export function systemLayout(spec: Spec, graph: FlowGraph, dry: SpecRun) {
  const byId = new Map<string, FlowNode>(graph.nodes.map(n => [n.id, n]));
  const services = spec.services ?? [];
  const dbs = spec.databases ?? [];
  const prefix = (s: (typeof services)[number]) => (s.path || `services/${s.name}`).replace(/\/+$/, "") + "/";
  const serviceOf = (file: string) => services.filter(s => file.startsWith(prefix(s))).sort((a, b) => prefix(b).length - prefix(a).length)[0]?.name ?? null;

  // 모든 API 의 dry run 이 지난 파일
  const files = new Map<string, FileBox>();
  const aux = new Map<string, Set<string>>();
  const touch = (n: FlowNode) => {
    const svc = serviceOf(n.file);
    if (!svc) return null;
    //: 보조 파일(dto · config · 유틸)은 그림을 어지럽힌다 — 상자에 개수만 적고, 호출 순서 목록에는 그대로 나온다.
    if (roleRank(n.file) === 4) { (aux.get(svc) ?? aux.set(svc, new Set()).get(svc)!).add(n.file); return null; }
    let box = files.get(n.file);
    if (!box) { box = { file: n.file, service: svc, x: 0, y: 0, fns: new Set(), effects: new Set() }; files.set(n.file, box); }
    if (n.kind !== "route") box.fns.add(n.id);
    n.effects.forEach(e => box!.effects.add(e.kind));
    return box;
  };
  for (const run of Object.values(dry.runs)) for (const st of run.steps) { const n = byId.get(st.node); if (n) touch(n); }

  // 열: 서비스 의존 깊이(왼쪽 → 오른쪽), DB 는 맨 오른쪽
  const { col, dbCol } = serviceColumns(services);
  const svcBoxes: SvcBox[] = [];
  const colH: Record<number, number> = {};
  const byCol: Record<number, typeof services> = {};
  services.forEach(s => (byCol[col[s.name] ?? 0] ??= []).push(s));
  for (const [c, list] of Object.entries(byCol)) {
    let y = 0;
    for (const s of list) {
      const mine = [...files.values()].filter(f => f.service === s.name).sort((a, b) => roleRank(a.file) - roleRank(b.file) || a.file.localeCompare(b.file));
      const auxN = aux.get(s.name)?.size ?? 0;
      const h = mine.length ? HEAD + mine.length * (FH + FG) - FG + PAD + (auxN ? 18 : 0) : HEAD + 4;
      const x = Number(c) * (CW + CG);
      svcBoxes.push({ name: s.name, kind: s.kind, x, y, h, aux: auxN, sub: s.kind === "web" ? "화면" : s.kind === "worker" ? "워커" : `API ${s.api}${s.port ? ` · :${s.port}` : ""}` });
      mine.forEach((f, i) => { f.x = x + PAD; f.y = y + HEAD + i * (FH + FG); });
      y += h + SG;
    }
    colH[Number(c)] = y - SG;
  }
  const dbBoxes: DbBox[] = dbs.map((d, i) => ({ name: d.name, engine: d.engine, x: dbCol * (CW + CG), y: i * (DBH + SG) }));
  colH[dbCol] = Math.max(colH[dbCol] ?? 0, dbs.length * (DBH + SG) - SG);
  const height = Math.max(120, ...Object.values(colH));
  const width = (dbCol + 1) * (CW + CG) - CG;
  // 열마다 세로 가운데 맞춤
  for (const [c, h] of Object.entries(colH)) {
    const off = (height - h) / 2;
    svcBoxes.filter(b => b.x === Number(c) * (CW + CG)).forEach(b => {
      b.y += off;
      files.forEach(f => { if (f.service === b.name) f.y += off; });
    });
    if (Number(c) === dbCol) dbBoxes.forEach(d => { d.y += off; });
  }

  const right = (f: FileBox): [number, number] => [f.x + CW - 2 * PAD, f.y + FH / 2];
  const left = (f: FileBox): [number, number] => [f.x, f.y + FH / 2];
  const edges = new Map<string, Edge>();
  const edgeKeysByRun = new Map<string, Set<string>>();
  const filesByRun = new Map<string, Set<string>>();
  const dbsByRun = new Map<string, Set<string>>();
  const dbOf = (svcName: string, detail: string) => {
    const own = services.find(s => s.name === svcName)?.databases ?? [];
    const cache = /캐시|redis/i.test(detail);
    return own.find(d => cache === /redis/i.test(dbs.find(x => x.name === d)?.engine ?? d)) ?? own[0];
  };
  const add = (run: string, e: Edge) => { if (!edges.has(e.id)) edges.set(e.id, e); edgeKeysByRun.get(run)!.add(e.id); };

  for (const run of Object.values(dry.runs)) {
    edgeKeysByRun.set(run.key, new Set()); filesByRun.set(run.key, new Set()); dbsByRun.set(run.key, new Set());
    for (const st of run.steps) {
      const n = byId.get(st.node);
      const fb = n && files.get(n.file);
      if (!n || !fb) continue;
      filesByRun.get(run.key)!.add(fb.file);
      if (st.from) {
        const p = byId.get(st.from);
        const pb = p && files.get(p.file);
        if (pb && pb.file !== fb.file) {
          const same = pb.service === fb.service && pb.x === fb.x;
          add(run.key, { id: `c ${pb.file} ${fb.file}`, from: same ? right(pb) : right(pb), to: same ? right(fb) : left(fb), kind: "call", same });
        }
      }
      for (const eff of n.effects) {
        if (eff.kind !== "db") continue;
        const d = dbOf(fb.service, eff.detail);
        const db = d && dbBoxes.find(x => x.name === d);
        if (db) { add(run.key, { id: `d ${fb.file} ${db.name}`, from: right(fb), to: [db.x, db.y + DBH / 2], kind: "db" }); dbsByRun.get(run.key)!.add(db.name); }
      }
    }
    // 게이트웨이 → 받는 서비스: 첫 서비스에서 HTTP 를 보내는 파일 → 받는 서비스의 입구 파일
    if (run.hops.length > 1 && run.hops[1].entry) {
      const sender = run.steps.filter(s => s.hop === 0).map(s => byId.get(s.node)).find(n => n?.effects.some(e => e.kind === "http"));
      const recvNode = byId.get(run.hops[1].entry);
      const sb = sender && files.get(sender.file), rb = recvNode && files.get(recvNode.file);
      if (sb && rb) add(run.key, { id: `h ${sb.file} ${rb.file}`, from: right(sb), to: left(rb), kind: "hop" });
    }
  }
  //: 서비스가 다른 서비스를 부르는 파일(products.client.ts → products) — 파일 이름으로 받는 서비스를 짐작해 점선으로 잇는다.
  const tokens = (x: string) => x.toLowerCase().split(/[^a-z0-9]+/).filter(t => t.length > 2 && !["client", "service", "api", "mock", "http", "ts", "js"].includes(t));
  for (const f of files.values()) {
    if (!f.effects.has("http")) continue;
    const s = services.find(x => x.name === f.service);
    const mine = new Set(tokens(base(f.file)));
    for (const d of s?.depends_on ?? []) {
      const to = svcBoxes.find(b => b.name === d);
      if (!to || !tokens(d).some(t => mine.has(t))) continue;
      const id = `s ${f.file} ${d}`;
      edges.set(id, { id, from: right(f), to: [to.x, to.y + HEAD / 2 + 4], kind: "svc" });
      for (const [run, set] of filesByRun) if (set.has(f.file)) edgeKeysByRun.get(run)!.add(id);
    }
  }
  // 화면 → 게이트웨이(서비스 의존)
  for (const s of services.filter(x => x.kind === "web")) {
    const from = svcBoxes.find(b => b.name === s.name);
    for (const d of s.depends_on) {
      const to = svcBoxes.find(b => b.name === d);
      if (from && to) edges.set(`s ${s.name} ${d}`, { id: `s ${s.name} ${d}`, from: [from.x + CW, from.y + HEAD / 2 + 4], to: [to.x, to.y + HEAD / 2 + 4], kind: "svc" });
    }
  }
  return { files: [...files.values()], svcBoxes, dbBoxes, edges: [...edges.values()], edgeKeysByRun, filesByRun, dbsByRun, width, height, byId };
}

const path = (e: Edge) => {
  const [x1, y1] = e.from, [x2, y2] = e.to;
  if (e.same) { const bulge = 18 + Math.min(40, Math.abs(y2 - y1) / 6); return `M${x1},${y1} C${x1 + bulge},${y1} ${x2 + bulge},${y2} ${x2},${y2}`; }
  const mx = (x1 + x2) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
};
const base = (f: string) => f.split("/").pop() ?? f;

export const SystemFlow: React.FC<{
  spec: Spec; graph: FlowGraph; dry: SpecRun; features: SpecFeature[];
  statusIcon: (v: "ok" | "warn" | "fail" | "unknown", size?: number) => React.ReactNode;
  onOpenFile: (file: string, line: number) => void;
}> = ({ spec, graph, dry, features, statusIcon, onOpenFile }) => {
  const L = useMemo(() => systemLayout(spec, graph, dry), [spec, graph, dry]);
  const [sel, setSel] = useState("");
  const [step, setStep] = useState(0);
  const [playing, setPlaying] = useState(false);
  const run: ApiRun | undefined = sel ? dry.runs[sel] : undefined;
  const steps = useMemo(() => (run?.steps ?? []).filter(s => !s.revisit), [run]);
  useEffect(() => { setStep(0); setPlaying(false); }, [sel]);
  useEffect(() => {
    if (!playing) return undefined;
    if (step >= steps.length - 1) { setPlaying(false); return undefined; }
    const t = setTimeout(() => setStep(s => s + 1), 700);
    return () => clearTimeout(t);
  }, [playing, step, steps.length]);

  const onFiles = run ? L.filesByRun.get(run.key) : undefined;
  const onEdges = run ? L.edgeKeysByRun.get(run.key) : undefined;
  const onDbs = run ? L.dbsByRun.get(run.key) : undefined;
  const cur = steps[step] ? L.byId.get(steps[step].node) : undefined;
  const prev = steps[step]?.from ? L.byId.get(steps[step].from!) : undefined;
  const curEdge = cur && prev && prev.file !== cur.file ? `c ${prev.file} ${cur.file}` : "";

  return <div className="sf">
    <style>{SYSTEM_FLOW_CSS}</style>
    <nav className="sf-side" aria-label="명세 API">
      {features.map(f => <React.Fragment key={f.key}>
        <div className="sf-grp">{f.label}</div>
        {f.endpoints.map(a => {
          const r = dry.runs[`${a.service} ${a.method} ${a.path}`];
          const key = r?.key ?? `${a.service} ${a.method} ${a.path}`;
          return <button key={key} type="button" className="sf-api" aria-current={sel === key} onClick={() => setSel(sel === key ? "" : key)} title={r?.reasons.join("\n") || undefined}>
            {statusIcon(r?.verdict ?? "unknown", 13)}<span className="m">{a.method}</span><span className="p">{a.path}</span>
          </button>;
        })}
      </React.Fragment>)}
    </nav>
    <div className="sf-main">
      <div className="sf-canvas">
        <svg width={L.width + 8} height={L.height + 8} viewBox={`-4 -4 ${L.width + 8} ${L.height + 8}`} role="img" aria-label="전체 코드 흐름">
          {L.svcBoxes.map(b => <g key={b.name} className="sf-svc">
            <rect className="box" x={b.x} y={b.y} width={CW} height={b.h} rx="8" />
            <text className="t" x={b.x + 12} y={b.y + 18}>{b.name}</text>
            <text className="s" x={b.x + 12} y={b.y + 32}>{b.sub}</text>
            {b.aux > 0 && <text className="s" x={b.x + 12} y={b.y + b.h - 8}>보조 파일 {b.aux}개</text>}
          </g>)}
          {L.edges.map(e => {
            const on = !onEdges || onEdges.has(e.id) || (e.kind === "svc" && !!run && run.hops[0].service === e.id.split(" ")[2]);
            //: 같은 서비스 안의 호출 선은 API 를 골랐을 때만 — 전체 그림은 서비스 사이 · DB 로 가는 선만 보인다.
            if (e.same && (!run || !on)) return null;
            return <path key={e.id} d={path(e)} className={`sf-ed${e.kind === "hop" || e.kind === "svc" ? " x" : ""}${run && on ? " on" : ""}${e.id === curEdge ? " cur" : ""}${run && !on ? " sf-dim" : ""}`} />;
          })}
          {L.files.map(f => {
            const on = !onFiles || onFiles.has(f.file);
            const isCur = cur?.file === f.file;
            const tags = (["db", "http", "secret"] as EffectKind[]).filter(k => f.effects.has(k));
            return <g key={f.file} className={`sf-file${run && on ? " on" : ""}${isCur ? " cur" : ""}${run && !on ? " sf-dim" : ""}`} transform={`translate(${f.x},${f.y})`} style={{ cursor: "pointer" }}
              onClick={() => { const n = [...f.fns].map(id => L.byId.get(id)).find(Boolean); if (n) onOpenFile(n.file, n.line); }}>
              <title>{f.file}</title>
              <rect width={CW - 2 * PAD} height={FH} rx="5" />
              <text className="k" x="10" y="19">{roleTag(f.file)}</text>
              <text x="44" y="19">{base(f.file).length > 22 ? base(f.file).slice(0, 21) + "…" : base(f.file)}</text>
              {tags.map((k, i) => <text key={k} className="k" x={CW - 2 * PAD - 10 - (tags.length - 1 - i) * 30} y="19" textAnchor="end">{EFFECT_TAG[k]}</text>)}
            </g>;
          })}
          {L.dbBoxes.map(d => <g key={d.name} className={`sf-db${run && onDbs?.has(d.name) ? " on" : ""}${run && !onDbs?.has(d.name) ? " sf-dim" : ""}`} transform={`translate(${d.x},${d.y})`}>
            <rect width={CW} height={DBH} rx="8" />
            <text x="12" y="19">{d.name}</text>
            <text className="s" x="12" y="34">{d.engine}</text>
          </g>)}
        </svg>
      </div>
      <div className="sf-detail">
        {!run ? <div className="sf-hint">왼쪽에서 API 를 고르면 그 요청이 지나는 길과 호출 순서를 보여 줍니다. 그림은 명세의 API {dry.total}개가 지나는 코드를 모두 합친 것입니다.</div> : <>
          <div className="sf-head">
            {statusIcon(run.verdict)}<b>{run.api.method} {run.api.path}</b>
            <span className="route">{run.hops.map(h => h.service).join(" → ")} · 함수 {run.functions} · 파일 {run.files}</span>
            <span className="sf-ctrl">
              <button type="button" aria-label="처음" onClick={() => { setStep(0); setPlaying(false); }}><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M3 2h1.5v12H3zM14 2.5v11L6 8z" /></svg></button>
              <button type="button" aria-label="이전" onClick={() => setStep(s => Math.max(0, s - 1))}><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M11 2.5v11L3 8z" /></svg></button>
              <button type="button" aria-label={playing ? "멈춤" : "재생"} onClick={() => { if (step >= steps.length - 1) setStep(0); setPlaying(p => !p); }}>{playing
                ? <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M4 2h3v12H4zM9 2h3v12H9z" /></svg>
                : <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M4 2.5v11L13 8z" /></svg>}</button>
              <button type="button" aria-label="다음" onClick={() => setStep(s => Math.min(steps.length - 1, s + 1))}><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M5 2.5v11L13 8z" /></svg></button>
              <span>{steps.length ? step + 1 : 0} / {steps.length}</span>
            </span>
          </div>
          {run.reasons.length > 0 && <div className="sf-why">{run.reasons.map((r, i) => <div key={i}>{statusIcon(run.verdict === "fail" ? "fail" : "warn", 12)}{r}</div>)}</div>}
          <div className="sf-steps" role="list" aria-label="호출 순서">
            {steps.map((s, i) => {
              const n = L.byId.get(s.node);
              if (!n) return null;
              const hopHead = i === 0 || steps[i - 1].hop !== s.hop;
              const kinds = [...new Set(n.effects.map(e => e.kind))];
              return <React.Fragment key={i}>
                {hopHead && <div className="sf-hop">{run.hops[s.hop]?.service} · {run.hops[s.hop]?.api.method} {run.hops[s.hop]?.api.path}</div>}
                <button type="button" role="listitem" className="sf-step" aria-current={i === step} onClick={() => { setStep(i); setPlaying(false); }} onDoubleClick={() => onOpenFile(n.file, n.line)}>
                  <span className="n">{i + 1}</span>
                  <span className="f" style={{ paddingLeft: Math.min(s.depth, 8) * 12 }}>{n.kind === "route" ? `${n.name}` : n.cls ? `${n.cls}.${n.name}` : n.name}<small>{base(n.file)}:{n.line}</small></span>
                  <span className="e">{kinds.map(k => <span key={k} className={`sf-tag ${k}`} title={n.effects.filter(e => e.kind === k).map(e => e.detail).join("\n")}>{EFFECT_TAG[k]}</span>)}</span>
                </button>
              </React.Fragment>;
            })}
          </div>
        </>}
      </div>
    </div>
  </div>;
};

export default SystemFlow;
