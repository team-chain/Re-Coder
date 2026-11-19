/**
 * 명세 전체 dry run (4.0.1)
 *
 * 실사용 2026-11-19: "dry run 은 API 하나씩이 아니라, 전체 명세를 한 번에 보면서 기능별 명세에 맞는지 검토하는 것".
 * 명세의 API 마다 코드의 진입점을 찾아 호출 그래프를 따라가고(코드를 실행하지 않는다),
 * 게이트웨이가 넘기는 API 는 받는 서비스의 같은 경로까지 이어서, 명세와 맞는지 판정한다:
 *   · 코드에 그 경로가 있는가
 *   · 게이트웨이 API 라면 받는 서비스의 API 가 명세 · 코드에 있는가
 *   · 데이터를 바꾸는 API(POST · PUT · PATCH · DELETE)가 실제로 저장하는 곳에 닿는가
 * 정적 분석이라 실제 실행과 다를 수 있다 — 화면에 함께 적는다.
 */
import { dryRun, EffectKind, FlowGraph } from "./dryRunModel";
import type { Spec, SpecApi } from "./SpecView";

export type Verdict = "ok" | "warn" | "fail";
export interface Hop { service: string; api: SpecApi; entry: string | null }
export interface ApiRun {
  key: string; api: SpecApi; hops: Hop[];
  effects: Record<EffectKind, number>; functions: number; files: number;
  verdict: Verdict; reasons: string[]; truncated: boolean;
}
export interface SpecRun { runs: Record<string, ApiRun>; counts: Record<Verdict, number>; total: number }

export const apiKey = (a: Pick<SpecApi, "service" | "method" | "path">) => `${a.service} ${a.method} ${a.path}`;
/** /products/{id} · /products/<id> · /products/:productId → /products/: */
export const normPath = (p: string) => (p.replace(/\{[^}/]+\}|<[^>/]+>|:[A-Za-z_][\w]*/g, ":").replace(/\/+$/, "") || "/");
const WRITES = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const EMPTY = (): Record<EffectKind, number> => ({ db: 0, http: 0, aws: 0, secret: 0, fs: 0, exec: 0 });

export function findEntry(graph: FlowGraph, api: SpecApi): string | null {
  const method = api.method.toUpperCase();
  const path = normPath(api.path);
  const routes = graph.entries.filter(e => e.kind === "route");
  const same = (e: (typeof routes)[number]) => (e.method === method || e.method === "ANY") && normPath(e.path ?? "") === path;
  const hit = routes.find(e => same(e) && e.file === api.file)
    ?? routes.find(e => same(e) && !!api.file && e.file.split("/").pop() === api.file.split("/").pop() && Math.abs(e.line - api.line) <= 3)
    ?? routes.find(e => e.file === api.file && e.method === method && Math.abs(e.line - api.line) <= 1);
  return hit?.id ?? null;
}

/** 게이트웨이 API(/api/...)를 받는 서비스의 API. 먼저 이 서비스가 의존하는 서비스에서 찾는다. */
export function downstreamOf(spec: Spec, api: SpecApi): SpecApi | null {
  if (!/^\/api(\/|$)/.test(api.path)) return null;
  const inner = normPath(api.path.replace(/^\/api/, "") || "/");
  const deps = spec.services?.find(s => s.name === api.service)?.depends_on ?? [];
  const cands = (spec.endpoints ?? []).filter(e => e.service !== api.service && e.method === api.method && normPath(e.path) === inner);
  return cands.find(e => deps.includes(e.service)) ?? cands[0] ?? null;
}

export function runSpec(spec: Spec, graph: FlowGraph): SpecRun {
  const byId = new Map(graph.nodes.map(n => [n.id, n]));
  const runs: Record<string, ApiRun> = {};
  const counts: Record<Verdict, number> = { ok: 0, warn: 0, fail: 0 };
  const walk = (entry: string | null) => {
    const effects = EMPTY();
    const nodes = new Set<string>(), files = new Set<string>();
    if (!entry) return { effects, nodes, files, truncated: false };
    const r = dryRun(graph, entry);
    for (const s of r.steps) {
      if (nodes.has(s.node)) continue;
      nodes.add(s.node);
      const n = byId.get(s.node);
      if (!n) continue;
      files.add(n.file);
      for (const k of new Set(n.effects.map(f => f.kind))) effects[k] += 1;
    }
    return { effects, nodes, files, truncated: Boolean(r.truncated) };
  };
  for (const api of spec.endpoints ?? []) {
    const reasons: string[] = [];
    let verdict: Verdict = "ok";
    const rank: Record<Verdict, number> = { ok: 0, warn: 1, fail: 2 };
    const worse = (v: Verdict, why: string) => { reasons.push(why); if (rank[v] > rank[verdict]) verdict = v; };
    const first: Hop = { service: api.service, api, entry: findEntry(graph, api) };
    const hops: Hop[] = [first];
    const a = walk(first.entry);
    const effects = { ...a.effects };
    const nodes = new Set(a.nodes), files = new Set(a.files);
    let truncated = a.truncated;
    let lastHop = a;
    if (!first.entry) worse("fail", "코드에서 이 경로를 찾지 못했습니다");
    const down = downstreamOf(spec, api);
    if (down) {
      const hop: Hop = { service: down.service, api: down, entry: findEntry(graph, down) };
      hops.push(hop);
      const b = walk(hop.entry);
      (Object.keys(effects) as EffectKind[]).forEach(k => { effects[k] += b.effects[k]; });
      b.nodes.forEach(n => nodes.add(n)); b.files.forEach(f => files.add(f));
      truncated = truncated || b.truncated;
      lastHop = b;
      if (!hop.entry) worse("fail", `${down.service} 에서 ${down.method} ${down.path} 를 찾지 못했습니다`);
    } else if (first.entry && a.effects.http > 0 && /^\/api(\/|$)/.test(api.path) && (spec.services?.find(s => s.name === api.service)?.depends_on.length ?? 0) > 0) {
      worse("warn", "다른 서비스로 넘기지만, 받는 서비스의 API 가 명세에 없습니다");
    }
    if (first.entry && WRITES.has(api.method) && lastHop.effects.db === 0 && effects.db === 0) {
      worse("warn", "데이터를 바꾸는 API 인데 DB 에 닿는 코드가 없습니다");
    }
    if (effects.exec > 0) worse("warn", "명령 실행 코드를 지납니다");
    if (truncated) worse("warn", "호출이 길어 일부만 따라갔습니다");
    const run: ApiRun = { key: apiKey(api), api, hops, effects, functions: nodes.size, files: files.size, verdict, reasons, truncated };
    runs[run.key] = run;
    counts[verdict] += 1;
  }
  return { runs, counts, total: Object.keys(runs).length };
}

export const EFFECT_NAME: Record<EffectKind, string> = { db: "DB", http: "서비스 호출", aws: "AWS", secret: "비밀값", fs: "파일", exec: "명령 실행" };
export function effectText(e: Record<EffectKind, number>): string {
  const order: EffectKind[] = ["db", "http", "aws", "secret", "fs", "exec"];
  const parts = order.filter(k => e[k] > 0).map(k => `${EFFECT_NAME[k]} ${e[k]}`);
  return parts.length ? parts.join(" · ") : "—";
}
