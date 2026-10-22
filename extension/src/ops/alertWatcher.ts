/**
 * 2.0.25 공격·장애 감지 알림 — 코어가 1분마다 판정한 알림을 VS Code 알림과 디스코드로 전한다.
 *
 * 원칙
 *  · 알림 내용은 코어가 실제 지표(방패 접속 기록·ECS 태스크 수)로 만든 것만 보여 준다.
 *  · 대응(IP 차단·방패 엄격·태스크 늘리기)은 여기서 실행하지 않는다 — [자세히] 를 누르면 운영 화면의 승인 카드로 간다.
 *  · 확장을 처음 켤 때 이미 쌓여 있던 알림은 한꺼번에 띄우지 않는다(마지막 것부터 이어 본다).
 */
import * as vscode from 'vscode';
import type { OpsAlert } from '../core/ApiClient';

export interface AlertSource {
    listOpsAlerts(since?: string): Promise<{ alerts: OpsAlert[]; cursor_reset?: boolean }>;
}
export interface AlertSink {
    /** 디스코드 등 — 설정돼 있지 않으면 조용히 넘어간다. */
    send(title: string, detail: string): Promise<void>;
}

const LAST_KEY = 'recoder.ops.alerts.last';

export function alertMessage(a: OpsAlert): { title: string; detail: string } {
    const where = `${a.service} (${a.region})`;
    const ip = a.sources?.ips?.[0];
    const path = a.sources?.paths?.[0];
    const clues = [ip ? `상위 IP ${ip.value} (${ip.count.toLocaleString()}회)` : '', path ? `많이 맞은 경로 ${path.value}` : '']
        .filter(Boolean).join(' · ');
    return {
        title: `[ReCoder] ${a.title} — ${where}`,
        detail: [a.detail, clues, a.suggestions?.length ? `대응 제안: ${a.suggestions.map(s => s.label).join(', ')} (VS Code 에서 승인)` : '']
            .filter(Boolean).join('\n'),
    };
}

export class AlertWatcher implements vscode.Disposable {
    private timer: ReturnType<typeof setInterval> | null = null;
    private busy = false;

    constructor(
        private readonly source: AlertSource,
        private readonly state: Pick<vscode.Memento, 'get' | 'update'>,
        private readonly sink: AlertSink | null,
        private readonly openDetails: (alert: OpsAlert) => void,
        private readonly intervalMs = 60_000,
        private readonly window: Pick<typeof vscode.window, 'showWarningMessage' | 'showErrorMessage'> = vscode.window,
    ) {}

    start(): void {
        if (this.timer) { return; }
        void this.tick();
        this.timer = setInterval(() => { void this.tick(); }, this.intervalMs);
    }

    /** 한 번 확인 — 새 알림 수를 돌려준다(테스트·[지금 확인]). */
    async tick(): Promise<number> {
        if (this.busy) { return 0; }
        this.busy = true;
        try {
            const last = this.state.get<string>(LAST_KEY, '');
            let alerts: OpsAlert[];
            let reset = false;
            try {
                const resp = await this.source.listOpsAlerts(last);
                alerts = resp.alerts ?? [];
                reset = resp.cursor_reset === true;
            } catch {
                return 0; // 코어가 아직 안 떴거나 재시작 중 — 다음 번에
            }
            if (!alerts.length) { return 0; }
            await this.state.update(LAST_KEY, alerts[alerts.length - 1].id);
            if (!last) { return 0; } // 처음 켤 때 쌓여 있던 것은 화면의 목록에서 본다
            // 2.0.32 — 기억한 마지막 알림을 코어가 모르면(목록이 잘림·상태 초기화) 전체가 오므로, 최근 것만 새 알림으로 띄운다.
            const recent = Date.now() - 2 * this.intervalMs - 60_000;
            const fresh = alerts.filter(a => (a.status ?? 'open') === 'open'
                && (!reset || (Date.parse(a.at) || 0) >= recent));
            for (const a of fresh) {
                const { title, detail } = alertMessage(a);
                const show = a.severity === 'critical' ? this.window.showErrorMessage : this.window.showWarningMessage;
                void Promise.resolve(show(`${title}\n${a.detail}`, '자세히', '닫기')).then(choice => {
                    if (choice === '자세히') { this.openDetails(a); }
                });
                if (this.sink) {
                    try { await this.sink.send(title, detail); } catch { /* 디스코드 실패가 알림을 막지 않는다 */ }
                }
            }
            return fresh.length;
        } finally {
            this.busy = false;
        }
    }

    dispose(): void {
        if (this.timer) { clearInterval(this.timer); }
        this.timer = null;
    }
}
