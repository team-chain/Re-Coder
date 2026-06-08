import { CoreHealth } from '../types';
import { ApiClient } from './ApiClient';
import { CoreManager } from './CoreManager';

export class PollingService {
    /** 기본 폴링 간격 4초 (3~5초 범위 내) */
    private intervalMs: number = 4000;
    private timer: ReturnType<typeof setInterval> | null = null;
    private lastHealth: CoreHealth | null = null;
    private onUpdateCallback: ((health: CoreHealth) => void) | null = null;
    private onErrorCallback: ((err: Error) => void) | null = null;
    /** 자동 복구 진행 중 플래그 + 마지막 복구 시각(쿨다운용). */
    private recovering: boolean = false;
    private lastRecoveryMs: number = 0;

    constructor(
        private readonly _coreManager: CoreManager,
        private readonly apiClient: ApiClient,
    ) {}

    start(
        onUpdate: (health: CoreHealth) => void,
        onError: (err: Error) => void
    ): void {
        if (this.timer !== null) {
            this.stop();
        }
        this.onUpdateCallback = onUpdate;
        this.onErrorCallback = onError;

        void this.poll().then((health) => {
            if (health) { this.adjustInterval(health); }
        });

        this.timer = setInterval(async () => {
            const health = await this.poll();
            if (health) { this.adjustInterval(health); }
        }, this.intervalMs);
    }

    stop(): void {
        if (this.timer !== null) {
            clearInterval(this.timer);
            this.timer = null;
        }
        this.onUpdateCallback = null;
        this.onErrorCallback = null;
    }

    async poll(): Promise<CoreHealth | null> {
        try {
            const health = await this.fetchHealth();
            this.lastHealth = health;
            this.onUpdateCallback?.(health);
            return health;
        } catch (err: unknown) {
            // Core 가 죽었거나 포트를 잃은 경우 — 자동 복구(재spawn/재연결) 1회 시도.
            // 성공하면 곧바로 상태를 다시 읽어 "연결 안됨" 이 사라진다.
            if (await this.tryRecover()) {
                try {
                    const health = await this.fetchHealth();
                    this.lastHealth = health;
                    this.onUpdateCallback?.(health);
                    return health;
                } catch { /* 여전히 실패 → 아래 down 처리 */ }
            }
            const error = err instanceof Error ? err : new Error(String(err));
            this.onErrorCallback?.(error);
            const downHealth: CoreHealth = {
                status: 'down',
                version: this.lastHealth?.version ?? 'unknown',
                uptime: 0,
                port: this.lastHealth?.port ?? 17894,
            };
            this.lastHealth = downHealth;
            this.onUpdateCallback?.(downHealth);
            return null;
        }
    }

    /**
     * /api/status (FSM 포함) 를 우선 읽고, 구버전 Core 면 /api/health 로 폴백.
     * 실패 시 throw — 호출 측(poll)이 자동 복구를 트리거한다.
     */
    private async fetchHealth(): Promise<CoreHealth> {
        try {
            const status = await this.apiClient.getStatus();
            return {
                status: status.status as CoreHealth['status'],
                version: status.version,
                uptime: status.uptime_seconds,
                port: status.port,
                orchestrator_state: status.orchestrator_state as import('../types').OrchestratorState,
                current_proposal_id: status.current_proposal_id,
                timestamp: status.timestamp,
            };
        } catch {
            return await this.apiClient.getHealth();
        }
    }

    /**
     * Core 연결이 끊겼을 때 자동 복구.
     * CoreManager.ensureRunning() 으로 기존 Core 재탐색 → 없으면 재spawn.
     * 8초 쿨다운 + 동시 실행 방지로 재spawn 폭주를 막는다.
     */
    private async tryRecover(): Promise<boolean> {
        const now = Date.now();
        if (this.recovering) { return false; }
        if (now - this.lastRecoveryMs < 8000) { return false; }
        this.recovering = true;
        this.lastRecoveryMs = now;
        try {
            await this._coreManager.ensureRunning();
            return true;
        } catch {
            return false;
        } finally {
            this.recovering = false;
        }
    }

    setInterval(ms: number): void {
        this.intervalMs = ms;
        if (this.timer !== null && this.onUpdateCallback && this.onErrorCallback) {
            const onUpdate = this.onUpdateCallback;
            const onError = this.onErrorCallback;
            this.stop();
            this.start(onUpdate, onError);
        }
    }

    getLastHealth(): CoreHealth | null {
        return this.lastHealth;
    }

    private adjustInterval(health: CoreHealth): void {
        let targetMs: number;
        switch (health.status) {
            case 'ok':       targetMs = 5000; break;
            case 'degraded': targetMs = 3000; break;
            case 'down':     targetMs = 3000; break;
            default:         targetMs = 4000;
        }
        if (targetMs !== this.intervalMs) {
            this.setInterval(targetMs);
        }
    }
}
