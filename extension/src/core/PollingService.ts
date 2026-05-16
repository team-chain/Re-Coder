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
            const health = await this.apiClient.getHealth();
            this.lastHealth = health;
            this.onUpdateCallback?.(health);
            return health;
        } catch (err: unknown) {
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
