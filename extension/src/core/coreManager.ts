import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import { ChildProcess, spawn, execSync } from 'child_process';
import { CoreHealth, RuntimeConfig } from '../types';

export class CoreManager {
    private static instance: CoreManager;
    private coreProcess: ChildProcess | null = null;
    private port: number = 17894;
    private sessionToken: string = '';
    private isSpawning: boolean = false;
    private extensionContext: vscode.ExtensionContext;

    private constructor(context: vscode.ExtensionContext) {
        this.extensionContext = context;
    }

    static getInstance(context?: vscode.ExtensionContext): CoreManager {
        if (!CoreManager.instance) {
            if (!context) {
                throw new Error('CoreManager requires ExtensionContext on first initialization');
            }
            CoreManager.instance = new CoreManager(context);
        }
        return CoreManager.instance;
    }

    async ensureRunning(): Promise<void> {
        // 1) 정상 경로 — runtime.json + healthCheck.
        const runtime = await this.readRuntime();
        if (runtime) {
            this.port = runtime.port;
            this.sessionToken = runtime.session_token;
            const health = await this.healthCheck();
            if (health && health.status !== 'down') {
                return;
            }
        }

        // 2) runtime.json 이 없거나 health 가 실패하더라도, 사용자가 `python core/main.py`
        //    같은 방식으로 수동 실행 중일 수 있다. 기본 포트 범위 (17894~17910) 에서
        //    /api/health (인증 불요) 가 응답하는지 직접 확인하고, 있다면 runtime.json
        //    이 곧 쓰여질 때까지 잠시 대기하여 토큰을 회수한다.
        const detected = await this.probeRunningCore();
        if (detected) {
            this.port = detected.port;
            // runtime.json 이 잠시 늦게 쓰여질 수 있으므로 최대 3초 polling.
            const deadline = Date.now() + 3000;
            while (Date.now() < deadline) {
                const rt = await this.readRuntime();
                if (rt && rt.port === this.port && rt.session_token) {
                    this.sessionToken = rt.session_token;
                    return;
                }
                await this.sleep(200);
            }
            // 토큰을 못 받아도 일단 connect 는 가능 — 인증 필요 호출이 401/503 일 뿐.
            // 호출 측에서 refreshToken 으로 재시도.
            return;
        }

        if (this.isSpawning) {
            await this.waitForReady();
            return;
        }

        // 3) 그래도 못 찾으면 직접 spawn 시도. 번들된 바이너리가 없는 dev 환경에선
        //    여기서 throw 한다. 그 경우 사용자가 `python core/main.py` 로 띄워야 함.
        await this.cleanupStale();
        await this.spawnCore();
    }

    /**
     * 기본 포트 범위에서 /api/health 가 응답하는 Core 가 있는지 탐색.
     * 발견하면 { port } 반환. 인증이 필요한 호출 (status/cost/diagnostics) 은
     * 토큰이 없으므로 운반되지 않는다 (그 시점에 runtime.json 에서 회수).
     */
    private async probeRunningCore(): Promise<{ port: number } | null> {
        const candidates = [17894, ...Array.from({ length: 16 }, (_, i) => 17895 + i)];
        for (const port of candidates) {
            try {
                const controller = new AbortController();
                const timerId = setTimeout(() => controller.abort(), 800);
                const res = await fetch(`http://127.0.0.1:${port}/api/health`, {
                    signal: controller.signal,
                });
                clearTimeout(timerId);
                if (res.ok) {
                    return { port };
                }
            } catch {
                // not listening on this port, try next
            }
        }
        return null;
    }

    async readRuntime(): Promise<RuntimeConfig | null> {
        const runtimePath = this.getRuntimeJsonPath();
        try {
            if (!fs.existsSync(runtimePath)) { return null; }
            const raw = fs.readFileSync(runtimePath, 'utf-8');
            return JSON.parse(raw) as RuntimeConfig;
        } catch {
            return null;
        }
    }

    private async spawnCore(): Promise<void> {
        this.isSpawning = true;
        try {
            const binaryPath = this.getCoreBinaryPath();
            if (!binaryPath) {
                throw new Error('ReCoder Core 바이너리를 찾을 수 없습니다. 설치를 확인해주세요.');
            }

            const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? os.homedir();
            const args = ['--port', String(this.port), '--workspace', workspacePath];

            this.coreProcess = spawn(binaryPath, args, {
                env: { ...process.env },
                detached: false,
                stdio: ['ignore', 'pipe', 'pipe'],
            });

            this.coreProcess.stdout?.on('data', (data: Buffer) => {
                console.log('[ReCoder Core]', data.toString().trim());
            });
            this.coreProcess.stderr?.on('data', (data: Buffer) => {
                console.error('[ReCoder Core STDERR]', data.toString().trim());
            });
            this.coreProcess.on('exit', (code, signal) => {
                console.log(`[ReCoder Core] exited code=${code} signal=${signal}`);
                this.coreProcess = null;
            });
            this.coreProcess.on('error', (err) => {
                console.error('[ReCoder Core] spawn error:', err);
                this.coreProcess = null;
            });

            await this.waitForReady(15000);
            const runtime = await this.readRuntime();
            if (runtime) {
                this.port = runtime.port;
                this.sessionToken = runtime.session_token;
            }
        } finally {
            this.isSpawning = false;
        }
    }

    private async waitForReady(timeoutMs: number = 15000): Promise<void> {
        const start = Date.now();
        while (Date.now() - start < timeoutMs) {
            const runtime = await this.readRuntime();
            if (runtime) {
                this.port = runtime.port;
                this.sessionToken = runtime.session_token;
                const health = await this.healthCheck();
                if (health && health.status !== 'down') { return; }
            }
            await this.sleep(500);
        }
        throw new Error('ReCoder Core가 시간 내에 준비되지 않았습니다.');
    }

    private async cleanupStale(): Promise<void> {
        const runtime = await this.readRuntime();
        if (!runtime) { return; }
        const pid = runtime.pid;
        try {
            if (process.platform === 'win32') {
                try { execSync(`taskkill /PID ${pid} /F`, { stdio: 'ignore' }); } catch { /* already gone */ }
            } else {
                try {
                    process.kill(pid, 'SIGTERM');
                    await this.sleep(2000);
                    process.kill(pid, 'SIGKILL');
                } catch { /* already gone */ }
            }
        } catch { /* ignore */ }
        const runtimePath = this.getRuntimeJsonPath();
        try { if (fs.existsSync(runtimePath)) { fs.unlinkSync(runtimePath); } } catch { /* ignore */ }
    }

    async healthCheck(): Promise<CoreHealth | null> {
        try {
            const url = `http://127.0.0.1:${this.port}/api/health`;
            const controller = new AbortController();
            const timerId = setTimeout(() => controller.abort(), 3000);
            const res = await fetch(url, {
                signal: controller.signal,
                headers: this.sessionToken ? { 'X-Session-Token': this.sessionToken } : {},
            });
            clearTimeout(timerId);
            if (!res.ok) { return null; }
            return await res.json() as CoreHealth;
        } catch {
            return null;
        }
    }

    async shutdown(force: boolean = false): Promise<void> {
        const runtime = await this.readRuntime();
        const pid = this.coreProcess?.pid ?? runtime?.pid;
        if (!pid) { return; }

        if (process.platform === 'win32') {
            try { execSync(`taskkill /PID ${pid} /F`, { stdio: 'ignore' }); } catch { /* ignore */ }
        } else {
            if (force) {
                try { process.kill(pid, 'SIGKILL'); } catch { /* ignore */ }
            } else {
                try { process.kill(pid, 'SIGTERM'); } catch { return; }
                const deadline = Date.now() + 5000;
                while (Date.now() < deadline) {
                    await this.sleep(300);
                    try { process.kill(pid, 0); } catch { return; }
                }
                try { process.kill(pid, 'SIGKILL'); } catch { /* ignore */ }
            }
        }

        this.coreProcess = null;
        const runtimePath = this.getRuntimeJsonPath();
        try { if (fs.existsSync(runtimePath)) { fs.unlinkSync(runtimePath); } } catch { /* ignore */ }
    }

    getPort(): number { return this.port; }
    getSessionToken(): string { return this.sessionToken; }

    async refreshToken(): Promise<boolean> {
        // runtime.json 에서 최신 토큰을 무조건 다시 읽어 in-memory 값과 동기화한다.
        // - 토큰이 없던 상태(빈 문자열)였더라도 runtime.json 의 값이 있으면 채운다.
        // - 이미 토큰이 있어도 Core 가 재시작되어 새 토큰을 발급했을 수 있으므로 갱신.
        const runtime = await this.readRuntime();
        if (runtime && runtime.session_token) {
            const changed =
                runtime.session_token !== this.sessionToken || runtime.port !== this.port;
            this.port = runtime.port;
            this.sessionToken = runtime.session_token;
            return changed;
        }
        return false;
    }

    private getCoreBinaryPath(): string {
        const platform = process.platform;
        const binaryName = platform === 'win32' ? 'recoder-core.exe' : 'recoder-core';

        const bundledPath = path.join(this.extensionContext.extensionPath, 'bin', binaryName);
        if (fs.existsSync(bundledPath)) { return bundledPath; }

        try {
            const which = platform === 'win32' ? 'where' : 'which';
            const result = execSync(`${which} recoder-core`, { encoding: 'utf-8' }).trim();
            if (result && fs.existsSync(result.split('\n')[0])) { return result.split('\n')[0]; }
        } catch { /* not in PATH */ }

        const homePath = path.join(os.homedir(), '.recoder', 'bin', binaryName);
        if (fs.existsSync(homePath)) { return homePath; }

        return '';
    }

    private getRuntimeJsonPath(): string {
        // Core writes runtime.json to ~/.recoder/runtime.json (singleton.py)
        return path.join(os.homedir(), '.recoder', 'runtime.json');
    }

    private sleep(ms: number): Promise<void> {
        return new Promise(resolve => setTimeout(resolve, ms));
    }
}
