/**
 * 2.0.48 모니터 화면의 계산(순수 함수) — 화면 컴포넌트와 테스트가 함께 쓴다.
 */

export interface WatchEventView {
  sk: string; at: string; ts: number; kind: string; severity: string; title: string; detail: string; source: string;
  actions?: Array<{ action: string; label?: string; target?: string; from_td?: string; status?: string; expires?: number }>;
}
export interface AlertView {
  id: string; at: string; ts?: number; kind: string; severity: string; title: string; detail: string; key?: string;
  service: string; region: string; status?: string; resolution?: string;
  suggestions?: Array<{ action: string; value: unknown; label: string; detail: string }>;
}
export interface Offer { event: string; title: string; at: string; target: string; from_td: string; label: string; expires: number }
export interface WatchStatusView {
  installed: boolean | null; error?: string; approval_link?: boolean; discord?: boolean; mail?: boolean; stable?: string;
  last_tick?: number; ticks?: number; healthy?: boolean; waiting_first_tick?: boolean; installed_at?: number;
}
export interface MonitorCurrentView {
  key: string; service: string; region: string; cluster: string; metrics: boolean;
  service_state: { status: string; running: number; desired: number; revision: string; task_definition: string } | null;
  series?: { requests: number[]; errors: number[]; blocked: number[] } | null;
  now?: { rpm: number; error_rate: number; points: number };
  watcher: WatchStatusView; events: WatchEventView[]; offers: Offer[]; alerts: AlertView[]; gaps: string[];
}
export interface MonitorData {
  monitors: Array<{ key: string; service: string; region: string; cluster: string }>;
  current: MonitorCurrentView | null; seen?: string;
}

export interface TimelineItem {
  id: string; at: string; ts: number; kind: string; tone: "bad" | "warn" | "ok" | "info"; title: string; detail: string;
  source: string; fresh: boolean;
}

const KIND_ICON: Record<string, string> = {
  spike: "pulse", blocked: "wall", errors: "alert", outage: "box", crash: "box", impaired: "box",
  deploy_failed: "upload", rollback: "history", vuln: "shield",
};
export const kindIcon = (kind: string) => KIND_ICON[kind] ?? "alert";

const SOURCE_TEXT: Record<string, string> = {
  alarm: "감시자 · 경보", ecs: "감시자 · ECS", tick: "감시자 · 5분 확인", link: "승인 링크", vscode: "VS Code", core: "VS Code 가 켜져 있을 때",
};
export const sourceText = (source: string) => SOURCE_TEXT[source] ?? source;

function tone(severity: string, kind: string): TimelineItem["tone"] {
  if (kind === "rollback") return "info";
  if (severity === "critical") return "bad";
  if (severity === "info") return "info";
  return "warn";
}

/** 감시자 기록(PC 꺼져 있을 때도) + 코어 감시 알림(VS Code 가 켜져 있을 때)을 시간순으로 합친다. 새 것 먼저. */
export function timeline(cur: Pick<MonitorCurrentView, "events" | "alerts">, seen: string): TimelineItem[] {
  const seenTs = seen ? Date.parse(seen.split("#")[0]) : NaN;
  const items: TimelineItem[] = [
    ...(cur.events ?? []).map((e) => ({
      id: `w:${e.sk}`, at: e.at, ts: (e.ts ?? 0) * 1000, kind: e.kind, tone: tone(e.severity, e.kind), title: e.title,
      detail: e.detail, source: e.source, fresh: seen ? e.sk > seen : false,
    })),
    ...(cur.alerts ?? []).map((a) => {
      const ts = (a.ts ? a.ts * 1000 : Date.parse(a.at)) || 0;
      return {
        id: `a:${a.id}`, at: a.at, ts, kind: a.kind, tone: tone(a.severity, a.kind), title: a.title, detail: a.detail, source: "core",
        fresh: Number.isFinite(seenTs) ? ts > seenTs : false,
      };
    }),
  ];
  return items.sort((x, y) => y.ts - x.ts);
}

/** 승인 대기 — 아직 쓸 수 있는 롤백 제안 + 대응 제안이 남은 열린 알림. */
export function pendingAlerts(cur: Pick<MonitorCurrentView, "alerts">): AlertView[] {
  return (cur.alerts ?? []).filter((a) => (a.status ?? "open") === "open" && (a.suggestions?.length ?? 0) > 0)
    //: 롤백 제안은 감시자 쪽 카드(이전 버전으로 되돌리기)와 같은 일 — 알림 카드에서는 뺀다.
    .map((a) => ({ ...a, suggestions: (a.suggestions ?? []).filter((s) => s.action !== "rollback") }))
    .filter((a) => (a.suggestions?.length ?? 0) > 0)
    .reverse();
}

export function hero(cur: MonitorCurrentView, items: TimelineItem[], pending: number): { title: string; sub: string; tone: "ok" | "warn" | "bad" | "off" } {
  const fresh = items.filter((i) => i.fresh && i.tone !== "info").length;
  const rev = cur.service_state?.revision ? `지금 ${cur.service_state.revision}` : "";
  const ticks = cur.watcher.ticks ? `감시자가 ${cur.watcher.ticks.toLocaleString()}번 확인했어요` : "";
  if (cur.service_state && cur.service_state.status !== "ACTIVE") {
    return { title: "서비스를 찾지 못했어요", sub: "지워졌거나 다른 리전일 수 있어요 — 정리 화면에서 확인하세요", tone: "bad" };
  }
  if (!cur.watcher.installed) {
    return { title: pending ? `승인 대기 ${pending}건` : "PC 가 꺼지면 아무도 안 봐요",
      sub: "24시간 감시자를 설치하면 밤에도 감지 · 기록 · 알림을 해요" + (rev ? ` · ${rev}` : ""), tone: pending ? "warn" : "off" };
  }
  const healthyNote = cur.watcher.healthy === false && !cur.watcher.waiting_first_tick ? "감시자 확인이 멈췄어요 — 설정을 확인하세요" : "";
  if (fresh || pending) {
    return { title: [fresh ? `새 기록 ${fresh}건` : "", pending ? `승인 대기 ${pending}건` : ""].filter(Boolean).join(" · "),
      sub: [rev, healthyNote || ticks].filter(Boolean).join(" · "), tone: items.some((i) => i.fresh && i.tone === "bad") ? "bad" : "warn" };
  }
  return { title: healthyNote ? "감시자를 확인하세요" : "이상 없음", sub: [rev, healthyNote || ticks].filter(Boolean).join(" · "),
    tone: healthyNote ? "warn" : "ok" };
}

export function minutesAgo(epochSeconds: number | undefined, now = Date.now()): string {
  if (!epochSeconds) return "아직";
  const m = Math.max(0, Math.round((now - epochSeconds * 1000) / 60000));
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h}시간 전` : `${Math.round(h / 24)}일 전`;
}

export function clock(iso: string, now = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const same = d.toDateString() === now.toDateString();
  return same ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

export const revOf = (arn: string) => (arn || "").split("/").pop() || "";
