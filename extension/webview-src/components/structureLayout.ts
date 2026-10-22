/**
 * 구조 다이어그램 배치(2.0.42) — 아키텍처 맵과 dry run 이 같이 쓴다. React 없이 계산만 한다.
 *
 * 왜 바꿨나
 *   예전 배치는 "깊이마다 한 줄"이라, 흔한 요청 흐름(경로 → 처리 → 저장)이 위에서 아래로 일자로 늘어섰다.
 *   어느 파일·어느 계층을 지나가는지가 보이지 않아 구조를 파악할 수 없었다.
 *
 * 배치 규칙
 *   - 가로: 역할 레인. 화면 → 입구(API) → 처리 → 데이터 → (설정·테스트·기타) → 외부(DB·외부 API·AWS…)
 *   - 레인 안: 그룹 상자(아키텍처는 폴더, dry run 은 파일)를 위에서 아래로 쌓는다.
 *   - 그룹 안: 같은 레인 안의 호출·import 를 따라 왼쪽 → 오른쪽으로 단계(열)를 나눈다. 같은 열은 위아래로.
 *   선은 실제 import·호출(사실), 위치는 역할 추정(해석)이다.
 */

export type Role = "ui" | "entry" | "logic" | "data" | "support" | "other" | "external";
export const ROLE_ORDER: Role[] = ["ui", "entry", "logic", "data", "support", "other", "external"];
export const ROLE_LABEL: Record<Role, string> = {
  ui: "화면", entry: "입구 · API", logic: "처리", data: "데이터", support: "설정 · 테스트", other: "기타", external: "외부",
};

const SUPPORT_SEG = new Set(["test", "tests", "__tests__", "e2e", "spec", "specs", "fixtures", "__mocks__", "scripts", "config", "configs"]);
const DATA_SEG = new Set(["models", "model", "db", "database", "databases", "repositories", "repository", "repos", "schema", "schemas",
  "migrations", "migration", "prisma", "entities", "entity", "dao", "persistence", "seeds", "seed"]);
const ENTRY_SEG = new Set(["routes", "route", "router", "routers", "controllers", "controller", "api", "apis", "handlers", "endpoints",
  "resolvers", "graphql", "webhooks"]);
const UI_SEG = new Set(["frontend", "client", "web", "ui", "components", "component", "pages", "screens", "views", "hooks", "styles",
  "public", "static", "templates", "layouts", "webview-src"]);
const LOGIC_SEG = new Set(["services", "service", "lib", "libs", "utils", "util", "helpers", "helper", "core", "domain", "middleware",
  "middlewares", "workers", "jobs", "tasks", "agents", "llm", "usecases"]);
const ENTRY_FILES = /^(server|app|main|index|wsgi|asgi|manage|urls|cli|bootstrap|extension)\.(js|mjs|cjs|ts|py)$/;
const CONFIG_FILES = /(^|\.)(config|conf|rc)\.(js|cjs|mjs|ts|json)$|^(tsconfig|jsconfig|package|babel|jest|vite|webpack|rollup|eslint|prettier|tailwind|postcss)\b/;

/** 파일 경로(과 노드 종류)로 역할을 추정한다. 확실한 표시(폴더·파일 이름)가 없으면 확장자로. */
export function roleOf(path: string, hint: { kind?: string } = {}): Role {
  if (hint.kind === "route") return "entry";
  const p = (path || "").replace(/\\/g, "/").toLowerCase();
  const parts = p.split("/").filter(Boolean);
  const name = parts[parts.length - 1] ?? "";
  const dirs = parts.slice(0, -1);
  if (dirs.some((d) => SUPPORT_SEG.has(d)) || /\.(test|spec)\.[a-z]+$/.test(name) || /^test_.*\.py$/.test(name) || /_test\.(py|go)$/.test(name)
    || CONFIG_FILES.test(name)) return "support";
  if (/\.(css|scss|sass|less|html?|vue|svelte)$/.test(name)) return "ui";
  //: 프런트엔드 폴더 안은 api/·models/ 같은 이름이어도 화면 쪽 코드다(frontend/src/api/client.js 는 서버 API 가 아니다).
  if (dirs.some((d) => d === "frontend" || d === "client" || d === "web" || d === "webview-src")) return "ui";
  if (dirs.some((d) => DATA_SEG.has(d)) || /(^|[._-])(models?|schemas?|db|repository|repo|migration|entity|dao)([._-]|\.)/.test(name) || name.endsWith(".sql")) return "data";
  const pagesApi = dirs.join("/").includes("pages/api") || (dirs.includes("app") && name.startsWith("route."));
  if (pagesApi || dirs.some((d) => ENTRY_SEG.has(d)) || (ENTRY_FILES.test(name) && !dirs.some((d) => UI_SEG.has(d)))) return "entry";
  if (/\.(jsx|tsx)$/.test(name) || dirs.some((d) => UI_SEG.has(d))) return "ui";
  if (dirs.some((d) => LOGIC_SEG.has(d))) return "logic";
  if (/\.(py|js|mjs|cjs|ts|go|rb|java|kt|rs)$/.test(name)) return "logic";
  return "other";
}

export interface LNode {
  id: string;
  role: Role;
  /** 상자로 묶을 단위(폴더·파일) */
  group: string;
  groupLabel?: string;
  /** 같은 열 안의 순서·순환을 끊을 때 쓰는 순서(작을수록 먼저) */
  order: number;
  w?: number;
  h?: number;
}
export interface LEdge { from: string; to: string; }
export interface Box { x: number; y: number; w: number; h: number; }
export interface PlacedNode extends Box { id: string; role: Role; group: string; col: number; }
export interface PlacedGroup extends Box { key: string; label: string; role: Role; count: number; }
export interface PlacedLane { role: Role; label: string; x: number; w: number; }
export interface LaneLayout {
  nodes: Map<string, PlacedNode>;
  groups: PlacedGroup[];
  lanes: PlacedLane[];
  width: number;
  height: number;
}

export interface LaneOptions {
  colW?: number; nodeW?: number; nodeH?: number; rowGap?: number; laneGap?: number;
  groupPad?: number; groupHeader?: number; groupGap?: number; top?: number; maxCols?: number;
  /** 한 열에 이보다 많으면 옆 열로 넘긴다(세로로 끝없이 늘어나지 않게) */
  maxRows?: number;
  /** 노드가 하나뿐인 그룹을 이 이름의 그룹 하나로 모은다(상자가 노드마다 생기지 않게) */
  mergeSingletons?: string;
}

/**
 * 레인 안에서의 단계(열). 같은 레인 안의 선만 보고, 순환은 DFS 의 되돌아가는 선을 빼서 끊는다.
 * 가장 긴 경로로 열을 정하고 maxCols 를 넘으면 마지막 열에 쌓는다(가로로 끝없이 늘어나지 않게).
 */
function localColumns(ids: string[], edges: LEdge[], orderOf: Map<string, number>, maxCols: number): { col: Map<string, number>; topo: Map<string, number> } {
  const inLane = new Set(ids);
  const out = new Map<string, string[]>();
  for (const e of edges) {
    if (e.from === e.to || !inLane.has(e.from) || !inLane.has(e.to)) continue;
    const list = out.get(e.from) ?? [];
    list.push(e.to);
    out.set(e.from, list);
  }
  for (const list of out.values()) list.sort((a, b) => (orderOf.get(a)! - orderOf.get(b)!));
  const sorted = [...ids].sort((a, b) => orderOf.get(a)! - orderOf.get(b)!);
  //: 반복 DFS(깊은 import 사슬에서도 콜 스택이 넘치지 않게) — 후위 순서를 뒤집으면 위상 순서.
  const state = new Map<string, 1 | 2>();
  const post: string[] = [];
  for (const start of sorted) {
    if (state.has(start)) continue;
    const stack: Array<[string, number]> = [[start, 0]];
    state.set(start, 1);
    while (stack.length) {
      const top = stack[stack.length - 1];
      const next = out.get(top[0]) ?? [];
      if (top[1] < next.length) {
        const to = next[top[1]++];
        if (!state.has(to)) { state.set(to, 1); stack.push([to, 0]); }
      } else {
        state.set(top[0], 2);
        post.push(top[0]);
        stack.pop();
      }
    }
  }
  const topoList = post.reverse();
  const topo = new Map(topoList.map((id, i) => [id, i]));
  const col = new Map<string, number>(ids.map((id) => [id, 0]));
  for (const id of topoList) {
    for (const to of out.get(id) ?? []) {
      if (topo.get(to)! > topo.get(id)!) col.set(to, Math.max(col.get(to)!, Math.min(maxCols - 1, col.get(id)! + 1)));
    }
  }
  return { col, topo };
}

export function layoutLanes(nodes: LNode[], edges: LEdge[], options: LaneOptions = {}): LaneLayout {
  const colW = options.colW ?? 184;
  const nodeW = options.nodeW ?? 160;
  const nodeH = options.nodeH ?? 40;
  const rowGap = options.rowGap ?? 12;
  const laneGap = options.laneGap ?? 36;
  const pad = options.groupPad ?? 10;
  const header = options.groupHeader ?? 20;
  const groupGap = options.groupGap ?? 14;
  const top = options.top ?? 30;
  const maxCols = options.maxCols ?? 5;
  const maxRows = Math.max(1, options.maxRows ?? 8);
  const MISC = "\u0000misc";
  const orderOf = new Map(nodes.map((n) => [n.id, n.order]));
  const placed = new Map<string, PlacedNode>();
  const groups: PlacedGroup[] = [];
  const lanes: PlacedLane[] = [];
  let x = 0;
  let height = top;
  for (const role of ROLE_ORDER) {
    const members = nodes.filter((n) => n.role === role);
    if (!members.length) continue;
    const { col, topo } = localColumns(members.map((n) => n.id), edges, orderOf, maxCols);
    //: 그룹 순서 — 그 그룹에서 가장 먼저 나오는 노드 순서(dry run 은 처음 지난 순서, 아키텍처는 의존 순서)
    const byGroup = new Map<string, LNode[]>();
    for (const n of members) {
      const list = byGroup.get(n.group) ?? [];
      list.push(n);
      byGroup.set(n.group, list);
    }
    if (options.mergeSingletons && byGroup.size > 3) {
      const singles = [...byGroup.entries()].filter(([, list]) => list.length === 1);
      if (singles.length > 1) {
        for (const [key] of singles) byGroup.delete(key);
        byGroup.set(MISC, singles.map(([, list]) => list[0]));
      }
    }
    const groupKeys = [...byGroup.keys()].sort((a, b) => {
      if (a === MISC || b === MISC) return a === MISC ? 1 : -1;
      const ma = Math.min(...byGroup.get(a)!.map((n) => topo.get(n.id)!));
      const mb = Math.min(...byGroup.get(b)!.map((n) => topo.get(n.id)!));
      return ma - mb || a.localeCompare(b);
    });
    //: 그룹 × 단계(열)별 목록 — 한 열이 maxRows 를 넘으면 옆 보조 열로 넘긴다
    const cells = new Map<string, Map<number, LNode[]>>();
    const ranks = Math.max(...members.map((n) => col.get(n.id)!)) + 1;
    const subCols = new Array(ranks).fill(1);
    for (const key of groupKeys) {
      const rows = new Map<number, LNode[]>();
      for (const n of byGroup.get(key)!) {
        const c = col.get(n.id)!;
        const r = rows.get(c) ?? [];
        r.push(n);
        rows.set(c, r);
      }
      for (const [c, r] of rows) {
        r.sort((a, b) => topo.get(a.id)! - topo.get(b.id)!);
        subCols[c] = Math.max(subCols[c], Math.ceil(r.length / maxRows));
      }
      cells.set(key, rows);
    }
    const colOffset: number[] = [];
    let acc = 0;
    for (let c = 0; c < ranks; c++) { colOffset.push(acc); acc += subCols[c]; }
    const laneW = acc * colW + pad * 2;
    lanes.push({ role, label: ROLE_LABEL[role], x, w: laneW });
    let y = top;
    for (const key of groupKeys) {
      const rows = cells.get(key)!;
      const list = byGroup.get(key)!;
      const maxRowsHere = Math.min(maxRows, Math.max(...[...rows.values()].map((r) => r.length)));
      const gh = header + pad * 2 + maxRowsHere * nodeH + (maxRowsHere - 1) * rowGap;
      const label = key === MISC ? (options.mergeSingletons ?? "") : (list[0].groupLabel ?? key);
      groups.push({ key: key === MISC ? "" : key, label, role, count: list.length, x, y, w: laneW, h: gh });
      for (const [c, r] of rows) {
        r.forEach((n, i) => {
          const w = n.w ?? nodeW, h = n.h ?? nodeH;
          const cc = colOffset[c] + Math.floor(i / maxRows), row = i % maxRows;
          placed.set(n.id, {
            id: n.id, role, group: key === MISC ? "" : key, col: cc, w, h,
            x: x + pad + cc * colW + (colW - w) / 2,
            y: y + header + pad + row * (nodeH + rowGap) + (nodeH - h) / 2,
          });
        });
      }
      y += gh + groupGap;
    }
    height = Math.max(height, y);
    x += laneW + laneGap;
  }
  return { nodes: placed, groups, lanes, width: Math.max(0, x - laneGap), height: height + 6 };
}

/** 선 모양 — 오른쪽으로 가면 부드러운 S 곡선, 같은 열이면 오른쪽으로 비켜 돌고, 왼쪽(되돌아감)이면 아래로 크게 돈다. */
export function edgeRoute(a: Box, b: Box, index = 0): { d: string; mid: { x: number; y: number }; backward: boolean } {
  const ay = a.y + a.h / 2, by = b.y + b.h / 2;
  const ar = a.x + a.w, bl = b.x;
  if (bl >= ar + 8) {
    const k = Math.max(28, (bl - ar) / 2);
    return { d: `M${ar},${ay} C${ar + k},${ay} ${bl - k},${by} ${bl},${by}`, mid: { x: (ar + bl) / 2, y: (ay + by) / 2 }, backward: false };
  }
  if (Math.abs(a.x - b.x) < 8) {
    const bulge = 26 + (index % 3) * 8;
    const xr = Math.max(ar, b.x + b.w);
    return { d: `M${ar},${ay} C${xr + bulge},${ay} ${xr + bulge},${by} ${b.x + b.w},${by}`, mid: { x: xr + bulge * 0.75, y: (ay + by) / 2 }, backward: by < ay };
  }
  const drop = Math.max(a.y + a.h, b.y + b.h) + 30 + (index % 3) * 10;
  const ax = a.x + a.w / 2, bx = b.x + b.w / 2;
  return { d: `M${ax},${a.y + a.h} C${ax},${drop} ${bx},${drop} ${bx},${b.y + b.h}`, mid: { x: (ax + bx) / 2, y: drop - 8 }, backward: true };
}

/** 그룹(폴더·파일) 색 — 같은 이름은 늘 같은 색. */
const PALETTE = ["#5b95ff", "#3fd6de", "#b48cff", "#f7b955", "#3ddc97", "#ff8f6b", "#e478c4", "#8fd14f"];
export function groupColor(key: string): string {
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

/** 글자 수로 자르기(SVG 는 넘치는 글자를 자르지 않는다). 한글은 두 칸으로 센다. */
export function fit(text: string, px: number, size = 11): string {
  const unit = size * 0.58;
  let used = 0, out = "";
  for (const ch of text) {
    const w = /[ㄱ-힝]/.test(ch) ? unit * 1.75 : unit;
    if (used + w > px) return out.slice(0, Math.max(0, out.length - 1)) + "…";
    used += w;
    out += ch;
  }
  return out;
}

// ── 아키텍처: 파일 그래프 → 폴더 단위로 묶기 ─────────────────────────────────

export interface FileNodeIn { id: string; name: string; flags: string[]; in_degree: number; layer?: string; }

export function folderOf(id: string): string {
  const p = id.replace(/\\/g, "/");
  const i = p.lastIndexOf("/");
  return i < 0 ? "(최상위)" : p.slice(0, i);
}

/** 파일이 많으면 폴더를 노드로 — 선은 폴더 사이 import 수(굵기). 폴더 역할은 안의 파일 역할 중 가장 많은 것. */
export function aggregateFolders(nodes: FileNodeIn[], edges: LEdge[]): { nodes: Array<{ id: string; label: string; files: number; role: Role; flags: string[] }>; edges: Array<LEdge & { count: number }> } {
  const files = new Map<string, FileNodeIn[]>();
  //: 최상위 파일은 한 덩어리로 묶으면 모든 선이 모이는 거대한 허브가 된다 — 역할별로 나눈다.
  const keyOf = (id: string) => { const f = folderOf(id); return f === "(최상위)" ? `${f}#${roleOf(id)}` : f; };
  for (const n of nodes) {
    const f = keyOf(n.id);
    const list = files.get(f) ?? [];
    list.push(n);
    files.set(f, list);
  }
  const out = [...files.entries()].map(([folder, list]) => {
    const votes = new Map<Role, number>();
    for (const n of list) {
      const r = roleOf(n.id);
      votes.set(r, (votes.get(r) ?? 0) + 1);
    }
    const role = [...votes.entries()].sort((a, b) => b[1] - a[1] || ROLE_ORDER.indexOf(a[0]) - ROLE_ORDER.indexOf(b[0]))[0][0];
    const flags = list.some((n) => n.flags.includes("overloaded")) ? ["overloaded"] : [];
    return { id: `dir:${folder}`, label: folder.includes("#") ? `(최상위) ${ROLE_LABEL[folder.split("#")[1] as Role]}` : folder, files: list.length, role, flags };
  });
  const counts = new Map<string, number>();
  for (const e of edges) {
    const a = `dir:${keyOf(e.from)}`, b = `dir:${keyOf(e.to)}`;
    if (a === b) continue;
    counts.set(`${a}\n${b}`, (counts.get(`${a}\n${b}`) ?? 0) + 1);
  }
  return {
    nodes: out,
    edges: [...counts.entries()].map(([k, count]) => { const [from, to] = k.split("\n"); return { from, to, count }; }),
  };
}
