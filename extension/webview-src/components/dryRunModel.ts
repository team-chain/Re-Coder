/**
 * dry run — 실행하지 않고 호출 그래프를 따라 한 단계씩 지나간다(2.0.34).
 *
 * 그래프는 확장이 정적 분석으로 만든다(src/codemap/flow.ts — 모양이 같다). 여기서는 진입점 하나에서 시작해
 * 호출 순서(줄 순서)대로 깊이 우선으로 내려가며 단계 목록을 만든다.
 *   - 이미 지난 함수는 "이미 지남"으로 한 번 표시하고 다시 들어가지 않는다(같은 길을 반복해 그리지 않게).
 *   - 지금 호출 중인 함수를 다시 부르면 "되돌아감(재귀)"으로 표시한다.
 *   - 단계·깊이 한도를 넘으면 멈추고 그 사실을 알린다.
 */
export type EffectKind = "db" | "http" | "aws" | "secret" | "fs" | "exec";
export type BranchKind = "if" | "else" | "loop" | "catch" | "case" | "cond" | "callback";

export interface FlowEffect { kind: EffectKind; detail: string; line: number; }
export interface FlowNode {
  id: string; file: string; name: string; cls?: string | null; line: number; endLine: number;
  kind: "function" | "handler" | "route" | "main"; effects: FlowEffect[]; hash: string;
}
export interface FlowEdge { from: string; to: string; line: number; order: number; branch?: BranchKind; certainty: "direct" | "inferred"; }
export interface FlowEntry { id: string; label: string; kind: "route" | "ui" | "main"; method?: string; path?: string; file: string; line: number; }
export interface FlowGraph {
  kind: "flow"; source: "source" | "binary"; root: string; files_scanned: number;
  nodes: FlowNode[]; edges: FlowEdge[]; entries: FlowEntry[]; truncated: boolean; notes: string[];
}
export interface FlowPreview {
  graph: FlowGraph;
  changed: Record<string, "added" | "modified">;
  removed: Array<{ id: string; file: string; name: string }>;
  affected: string[];
}

export interface DryRunStep {
  index: number; node: string; depth: number;
  from?: string; line?: number; branch?: BranchKind; certainty?: "direct" | "inferred";
  /** seen: 앞에서 이미 지난 함수 · cycle: 지금 호출 중인 함수로 되돌아감 */
  revisit?: "seen" | "cycle";
}
export interface DryRunSummary {
  /** effects: 종류별로 그 일을 하는 함수 수 */
  nodes: number; files: number; effects: Record<EffectKind, number>;
  changed: number; branches: number; inferred: number;
}
export interface DryRunResult {
  entry: string; steps: DryRunStep[]; truncated: null | "steps" | "depth";
  summary: DryRunSummary;
}

export const EFFECT_LABEL: Record<EffectKind, string> = {
  db: "DB", http: "외부 API", aws: "AWS", secret: "비밀값", fs: "파일", exec: "명령 실행",
};
export const BRANCH_LABEL: Record<BranchKind, string> = {
  if: "조건이 맞을 때", else: "조건이 아닐 때", loop: "반복 안", catch: "오류가 나면",
  case: "switch 분기", cond: "조건식", callback: "콜백 안",
};

export function dryRun(
  graph: FlowGraph, entry: string,
  options: { maxSteps?: number; maxDepth?: number; changed?: Record<string, string> } = {},
): DryRunResult {
  const maxSteps = options.maxSteps ?? 300;
  const maxDepth = options.maxDepth ?? 24;
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const out = new Map<string, FlowEdge[]>();
  for (const e of graph.edges) {
    if (!byId.has(e.to)) continue;
    const list = out.get(e.from) ?? [];
    list.push(e);
    out.set(e.from, list);
  }
  for (const list of out.values()) list.sort((a, b) => a.order - b.order || a.line - b.line);

  const steps: DryRunStep[] = [];
  const seen = new Set<string>();
  const stack: string[] = [];
  let truncated: DryRunResult["truncated"] = null;

  const visit = (node: string, depth: number, via?: FlowEdge) => {
    if (steps.length >= maxSteps) { truncated = "steps"; return; }
    const base: DryRunStep = {
      index: steps.length, node, depth,
      ...(via ? { from: via.from, line: via.line, branch: via.branch, certainty: via.certainty } : {}),
    };
    if (stack.includes(node)) { steps.push({ ...base, revisit: "cycle" }); return; }
    if (seen.has(node)) { steps.push({ ...base, revisit: "seen" }); return; }
    steps.push(base);
    seen.add(node);
    if (depth >= maxDepth) { if ((out.get(node) ?? []).length) truncated = truncated ?? "depth"; return; }
    stack.push(node);
    for (const edge of out.get(node) ?? []) {
      visit(edge.to, depth + 1, edge);
      if (truncated === "steps") break;
    }
    stack.pop();
  };
  if (byId.has(entry)) visit(entry, 0);

  const effects: Record<EffectKind, number> = { db: 0, http: 0, aws: 0, secret: 0, fs: 0, exec: 0 };
  const files = new Set<string>();
  let changed = 0;
  for (const id of seen) {
    const n = byId.get(id)!;
    files.add(n.file);
    //: 종류마다 "그 일을 하는 함수 수"(한 함수의 SQL 여러 줄을 여러 번 세지 않는다)
    for (const kind of new Set(n.effects.map((f) => f.kind))) effects[kind] += 1;
    if (options.changed?.[id]) changed += 1;
  }
  return {
    entry, steps, truncated,
    summary: {
      nodes: seen.size, files: files.size, effects, changed,
      branches: steps.filter((s) => s.branch).length,
      inferred: steps.filter((s) => s.certainty === "inferred").length,
    },
  };
}

/** 미리 보기의 보안 발견(파일·줄)을 그 줄을 품은 가장 안쪽 함수에 붙인다. */
export function findingsByNode<T extends { file: string; line: number }>(graph: FlowGraph, findings: T[], prefix = ""): Map<string, T[]> {
  const out = new Map<string, T[]>();
  const norm = (p: string) => p.replace(/\\/g, "/").replace(/^\.?\/+/, "");
  const pre = norm(prefix).replace(/\/+$/, "");
  for (const f of findings) {
    const file = norm(f.file);
    const candidates = [file, pre && !file.startsWith(`${pre}/`) ? `${pre}/${file}` : file];
    const hit = graph.nodes
      .filter((n) => candidates.includes(n.file) && n.kind !== "route" && n.line <= f.line && f.line <= n.endLine)
      .sort((a, b) => (a.endLine - a.line) - (b.endLine - b.line))[0];
    if (!hit) continue;
    const list = out.get(hit.id) ?? [];
    list.push(f);
    out.set(hit.id, list);
  }
  return out;
}
