/**
 * 2.0.48 밤사이 요약 — VS Code 를 열면(그리고 30분마다) 감시자 기록을 읽어 "밤사이 n건 · 승인 대기 m건"을 알려 주고,
 * 위쪽 Monitor 탭의 배지 숫자를 갱신한다.
 *
 * 원칙
 *  · 기록은 사용자 AWS 의 감시자 표에서만 읽는다(코어가 사용자 자격증명으로). 여기서는 아무것도 실행하지 않는다.
 *  · "새것"의 기준은 사용자가 모니터에서 그 서비스를 마지막으로 본 기록(seen). 처음이면 최근 12시간만.
 *  · 같은 내용으로 두 번 알리지 않는다(마지막으로 알린 기록 · 대기 수를 기억).
 */
import * as vscode from 'vscode';
import type { MonitorCurrent, MonitorOverview } from '../core/ApiClient';

export const SEEN_KEY = 'recoder.monitor.seen';
const TOLD_KEY = 'recoder.monitor.told';
const FIRST_LOOK_MS = 12 * 3600 * 1000;

export interface MonitorSource { monitorOverview(key?: string, light?: boolean): Promise<MonitorOverview>; }
export type SeenMap = Record<string, string>;

/** 이 서비스의 승인 대기 — 아직 쓸 수 있는 롤백 제안 + 대응 제안이 남은 열린 알림. */
export function pendingCount(cur: Pick<MonitorCurrent, 'offers' | 'alerts'>): number {
    const open = (cur.alerts ?? []).filter(a => (a.status ?? 'open') === 'open' && (a.suggestions?.length ?? 0) > 0).length;
    return (cur.offers ?? []).length + open;
}

/** 사용자가 아직 보지 않은 감시자 기록(롤백 결과 같은 정보성 기록은 세지 않는다). */
export function unseen(cur: Pick<MonitorCurrent, 'key' | 'events'>, seen: SeenMap, now = Date.now()): number {
    const mark = seen[cur.key];
    return (cur.events ?? []).filter(e => e.severity !== 'info'
        && (mark ? e.sk > mark : now - (e.ts ?? 0) * 1000 < FIRST_LOOK_MS)).length;
}

export function digestText(items: Array<{ service: string; fresh: number; pending: number }>): string {
    const busy = items.filter(i => i.fresh || i.pending);
    const fresh = busy.reduce((n, i) => n + i.fresh, 0);
    const pending = busy.reduce((n, i) => n + i.pending, 0);
    const who = busy.map(i => i.service).slice(0, 3).join(', ') + (busy.length > 3 ? ` 외 ${busy.length - 3}개` : '');
    return `[ReCoder] ${fresh ? `밤사이 ${fresh}건` : '새 기록 없음'}${pending ? ` · 승인 대기 ${pending}건` : ''} — ${who}`;
}

export class MonitorDigest implements vscode.Disposable {
    private timer: ReturnType<typeof setInterval> | null = null;
    private first: ReturnType<typeof setTimeout> | null = null;
    private busy = false;

    constructor(
        private readonly source: MonitorSource,
        private readonly state: Pick<vscode.Memento, 'get' | 'update'>,
        private readonly openMonitor: () => void,
        private readonly badge: (counts: Record<string, number>) => void,
        private readonly window: Pick<typeof vscode.window, 'showInformationMessage' | 'showWarningMessage'> = vscode.window,
        private readonly delayMs = 20_000,
        private readonly intervalMs = 30 * 60_000,
    ) {}

    start(): void {
        if (this.timer || this.first) { return; }
        //: 코어가 뜨고 AWS 연결을 확인할 시간을 둔다.
        this.first = setTimeout(() => { this.first = null; void this.run(); }, this.delayMs);
        this.timer = setInterval(() => { void this.run(); }, this.intervalMs);
    }

    /** 한 번 확인 — {fresh, pending} 합계를 돌려준다(테스트 · 모니터가 열릴 때). */
    async run(now = Date.now()): Promise<{ fresh: number; pending: number }> {
        if (this.busy) { return { fresh: 0, pending: 0 }; }
        this.busy = true;
        try {
            let first: MonitorOverview;
            try {
                first = await this.source.monitorOverview('', true);
            } catch {
                return { fresh: 0, pending: 0 }; // 코어가 아직 안 떴다 — 다음 번에
            }
            const list = first.monitors ?? [];
            const seen = this.state.get<SeenMap>(SEEN_KEY, {}) ?? {};
            const items: Array<{ key: string; service: string; fresh: number; pending: number; last: string }> = [];
            for (const m of list.slice(0, 8)) {
                try {
                    //: 첫 응답이 이미 그 서비스(기본 = 마지막 서비스)를 담고 있으면 다시 묻지 않는다.
                    const cur = first.current?.key === m.key ? first.current : (await this.source.monitorOverview(m.key, true)).current;
                    if (!cur) { continue; }
                    items.push({ key: cur.key, service: cur.service, fresh: unseen(cur, seen, now), pending: pendingCount(cur),
                        last: cur.events?.[0]?.sk ?? '' });
                } catch { /* 한 서비스 실패가 나머지를 막지 않는다 */ }
            }
            const fresh = items.reduce((n, i) => n + i.fresh, 0);
            const pending = items.reduce((n, i) => n + i.pending, 0);
            this.badge(Object.fromEntries(items.map(i => [i.key, i.pending])));
            const told = this.state.get<string>(TOLD_KEY, '') ?? '';
            const signature = `${items.map(i => `${i.key}@${i.last}`).join('|')}#${pending}`;
            if ((fresh || pending) && signature !== told) {
                await this.state.update(TOLD_KEY, signature);
                const show = pending ? this.window.showWarningMessage : this.window.showInformationMessage;
                void Promise.resolve(show(digestText(items), '모니터 열기')).then(choice => {
                    if (choice === '모니터 열기') { this.openMonitor(); }
                });
            }
            return { fresh, pending };
        } finally {
            this.busy = false;
        }
    }

    dispose(): void {
        if (this.timer) { clearInterval(this.timer); }
        if (this.first) { clearTimeout(this.first); }
        this.timer = null; this.first = null;
    }
}
