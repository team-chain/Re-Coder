/**
 * 빠른 이동 휠 (2.0.44) — 화면 쪽 규칙(순수 함수). 설정 해석은 확장(src/quickAccess.ts)이 하고,
 * 화면은 받은 상태를 그리고 바꿀 칸·기능만 돌려준다.
 */

export type QuickSlot =
  | "up" | "right" | "down" | "left"
  | "shift+up" | "shift+right" | "shift+down" | "shift+left";

export const OUTER: QuickSlot[] = ["up", "right", "down", "left"];
export const INNER: QuickSlot[] = ["shift+up", "shift+right", "shift+down", "shift+left"];
export const ALL_SLOTS: QuickSlot[] = [...OUTER, ...INNER];

export interface QuickState {
  slots: Record<QuickSlot, string>;
  problems: string[];
  targets: Array<{ id: string; label: string }>;
  keys: Record<QuickSlot, string>;
  /** 휠을 여는 키(표시용, 예: "Alt+Q" · "⌥Q"). */
  wheelKey: string;
}

//: 휠 칸에 들어가는 짧은 이름(목록에는 긴 이름).
const SHORT: Record<string, string> = {
  code: "코드 생성", build: "에러 분석", map: "아키텍처", flow: "dry run", adr: "설계 기록", models: "AI · 비용",
  ship: "로컬 배포", deploy: "배포 캔버스", replay: "배포 이력", operate: "운영 대응", scan: "취약점 스캔",
  secrets: "시크릿 검사", policy: "정책 게이트", home: "홈",
};
export const shortLabel = (id: string, targets: QuickState["targets"]): string =>
  SHORT[id] ?? targets.find((t) => t.id === id)?.label ?? id;

const ARROW: Record<string, string> = { up: "↑", right: "→", down: "↓", left: "←" };
export function defaultKey(slot: QuickSlot): string {
  const shift = slot.startsWith("shift+");
  return `Alt+${shift ? "Shift+" : ""}${ARROW[shift ? slot.slice(6) : slot]}`;
}

/** 확장이 보낸 상태를 믿을 수 있는 모양으로. 모르는 값은 비운다. */
export function toState(raw: unknown): QuickState | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Partial<QuickState>;
  const targets = Array.isArray(r.targets) ? r.targets.filter((t) => t && typeof t.id === "string" && typeof t.label === "string") : [];
  if (!targets.length) return null;
  const ids = new Set(targets.map((t) => t.id));
  const slots = {} as Record<QuickSlot, string>;
  const keys = {} as Record<QuickSlot, string>;
  for (const s of ALL_SLOTS) {
    const v = (r.slots as Record<string, unknown> | undefined)?.[s];
    slots[s] = typeof v === "string" && ids.has(v) ? v : "";
    const k = (r.keys as Record<string, unknown> | undefined)?.[s];
    keys[s] = typeof k === "string" && k ? k : defaultKey(s);
  }
  const problems = Array.isArray(r.problems) ? r.problems.filter((p): p is string => typeof p === "string").slice(0, 10) : [];
  const wheelKey = typeof r.wheelKey === "string" && r.wheelKey ? r.wheelKey.slice(0, 20) : "Alt+Q";
  return { slots, problems, targets, keys, wheelKey };
}

/** 방향 → 화면 각도(도, 오른쪽 0 · 아래 90). */
const ANGLE: Record<string, number> = { up: -90, right: 0, down: 90, left: 180 };
export function slotAngle(slot: QuickSlot): number {
  return ANGLE[slot.startsWith("shift+") ? slot.slice(6) : slot];
}

const rad = (d: number) => (d * Math.PI) / 180;
const pt = (cx: number, cy: number, r: number, a: number) => `${(cx + r * Math.cos(rad(a))).toFixed(2)} ${(cy + r * Math.sin(rad(a))).toFixed(2)}`;

/** 도넛 조각(r0 안쪽 → r1 바깥, 각도 a0→a1) SVG 경로. */
export function petalPath(cx: number, cy: number, r0: number, r1: number, a0: number, a1: number): string {
  const large = a1 - a0 > 180 ? 1 : 0;
  return `M ${pt(cx, cy, r1, a0)} A ${r1} ${r1} 0 ${large} 1 ${pt(cx, cy, r1, a1)} L ${pt(cx, cy, r0, a1)} A ${r0} ${r0} 0 ${large} 0 ${pt(cx, cy, r0, a0)} Z`;
}

export function labelPoint(cx: number, cy: number, r: number, slot: QuickSlot): { x: number; y: number } {
  const a = rad(slotAngle(slot));
  return { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
}

/** 키보드로 칸 옮기기(편집 중 방향키) — 같은 고리 안에서 그 방향 칸으로, Tab 은 안팎 고리 전환. */
export function moveSelection(current: QuickSlot | null, key: string): QuickSlot | null {
  const dir: Record<string, string> = { ArrowUp: "up", ArrowRight: "right", ArrowDown: "down", ArrowLeft: "left" };
  if (dir[key]) {
    const inner = !!current && current.startsWith("shift+");
    return (inner ? `shift+${dir[key]}` : dir[key]) as QuickSlot;
  }
  if (key === "Tab" && current) {
    return (current.startsWith("shift+") ? current.slice(6) : `shift+${current}`) as QuickSlot;
  }
  return current;
}

/** 기능 → 화면(App 의 view) · 지도 모드. 확장의 QUICK_TARGETS 와 같은 표(시험이 둘을 대조한다). */
export function viewFor(id: string): { view: string; mapMode?: "flow" | "project" } | null {
  const known = ["code", "build", "map", "adr", "models", "ship", "deploy", "replay", "operate", "scan", "secrets", "policy", "home"];
  if (id === "flow") return { view: "map", mapMode: "flow" };
  if (id === "map") return { view: "map", mapMode: "project" };
  return known.includes(id) ? { view: id } : null;
}

/** 방향키 이벤트 → 칸(Shift 면 안쪽 고리). 방향키가 아니면 null. */
export function slotForKey(key: string, shift: boolean): QuickSlot | null {
  const dir: Record<string, string> = { ArrowUp: "up", ArrowRight: "right", ArrowDown: "down", ArrowLeft: "left" };
  if (!dir[key]) return null;
  return (shift ? `shift+${dir[key]}` : dir[key]) as QuickSlot;
}
