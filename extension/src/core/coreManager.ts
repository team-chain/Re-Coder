<<<<<<< HEAD
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
=======
/**
 * Local Core 생명주기 관리 (설계서 v6.4 §6)
 * - Lazy Spawn, Singleton, 좀비 프로세스 방지
 * - runtime.json 으로 포트/토큰 공유
 *
 * 2026-05-08 갱신:
 * - dev 분기 spawn 수정: exec/args 분리, Windows python 탐색
 * - graceful shutdown: SIGTERM → 5초 대기 → SIGKILL 폴백
 */
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import * as cp from 'child_process';
import { CoreClient } from '../api/coreClient';

export interface RuntimeConfig {
    port: number;
    session_token: string;
    started_at: string;
}

interface SpawnSpec {
    command: string;
    args: string[];
    cwd?: string;
}

const SHUTDOWN_GRACE_MS = 5000;

export class CoreManager {
    private _process: cp.ChildProcess | null = null;
    private _client: CoreClient | null = null;
    private readonly _runtimePath: string;
    private readonly _lockPath: string;
    private _starting: boolean = false;

    constructor(private readonly context: vscode.ExtensionContext) {
        const home = os.homedir();
        this._runtimePath = path.join(home, '.recoder', 'runtime.json');
        this._lockPath = path.join(home, '.recoder', 'core.lock');
    }

    get client(): CoreClient {
        if (!this._client) throw new Error('Core가 실행 중이 아닙니다.');
        return this._client;
    }

    /** 이미 실행 중이면 재사용, 아니면 spawn. */
    async ensureRunning(): Promise<CoreClient> {
        const existing = this._tryLoadRuntime();
        if (existing) {
            this._client = new CoreClient(existing.port, existing.session_token);
            const alive = await this._client.healthCheck();
            if (alive) return this._client;
        }
        return this._spawn();
    }

    /** SIGTERM → 5초 대기 → SIGKILL 폴백. */
    async stop(): Promise<void> {
        const proc = this._process;
        this._client = null;
        if (!proc) return;

        proc.kill('SIGTERM');

        const exited = await new Promise<boolean>((resolve) => {
            const t = setTimeout(() => resolve(false), SHUTDOWN_GRACE_MS);
            proc.once('exit', () => {
                clearTimeout(t);
                resolve(true);
            });
        });

        if (!exited && !proc.killed) {
            try {
                proc.kill('SIGKILL');
            } catch {
                // 이미 죽었거나 권한 없음
            }
        }

        this._process = null;
    }

    // ── private ────────────────────────────────────────────────────

    private async _spawn(): Promise<CoreClient> {
        if (this._starting) {
            return this._waitForRuntime(15000);
        }
        this._starting = true;

        const spec = this._findCoreBinary();
        vscode.window.showInformationMessage('ReCoder Core를 시작합니다...');

        try {
            this._process = cp.spawn(spec.command, spec.args, {
                detached: false,
                stdio: 'pipe',
                cwd: spec.cwd,
                env: { ...process.env },
                // Windows 에서 .py / .exe 경로 모두 처리
                shell: false,
            });
        } catch (e: any) {
            this._starting = false;
            throw new Error(`ReCoder Core 실행 실패: ${e?.message ?? e}`);
        }

        this._process.stdout?.on('data', (d: Buffer) => {
            console.log('[Core]', d.toString());
        });
        this._process.stderr?.on('data', (d: Buffer) => {
            console.error('[Core]', d.toString());
        });
        this._process.on('exit', (code) => {
            console.log('[Core] 종료:', code);
            this._process = null;
            this._client = null;
            this._starting = false;
        });
        this._process.on('error', (err) => {
            console.error('[Core] spawn error:', err);
            this._process = null;
            this._starting = false;
        });

        try {
            const client = await this._waitForRuntime(15000);
            return client;
        } finally {
            this._starting = false;
        }
    }

    /**
     * Core 실행 명령을 결정한다.
     *   1) VSIX 번들 바이너리 (`extension/bin/recoder-core[.exe]`)
     *   2) 개발 모드: `extension/../core/main.py` 를 python 으로 실행
     *
     * 반환값은 spawn(command, args) 에 그대로 넘길 수 있는 형태.
     */
    private _findCoreBinary(): SpawnSpec {
        const ext = this.context.extensionPath;

        // 1) VSIX 번들 (Windows: .exe / 그 외: extension)
        const bundledExe = path.join(ext, 'bin', 'recoder-core.exe');
        if (fs.existsSync(bundledExe)) return { command: bundledExe, args: [] };

        const bundled = path.join(ext, 'bin', 'recoder-core');
        if (fs.existsSync(bundled)) return { command: bundled, args: [] };

        // 2) 개발 모드: core/main.py
        const candidates = [
            path.join(ext, '..', 'core', 'main.py'),     // monorepo: extension/ ↔ core/
            path.join(ext, '..', '..', 'core', 'main.py'),
        ];
        const mainPy = candidates.find(p => fs.existsSync(p));
        if (mainPy) {
            const py = this._findPython();
            return {
                command: py,
                args: [mainPy],
                cwd: path.dirname(mainPy),
            };
        }

        throw new Error('ReCoder Core 바이너리/소스를 찾을 수 없습니다. (bin/recoder-core 또는 core/main.py)');
    }

    /** Windows 는 py / python.exe / python, 그 외는 python3 / python 우선. */
    private _findPython(): string {
        const isWin = process.platform === 'win32';
        const candidates = isWin
            ? ['python.exe', 'python', 'py']
            : ['python3', 'python'];

        for (const cand of candidates) {
            try {
                const r = cp.spawnSync(cand, ['--version'], { stdio: 'ignore' });
                if (r.status === 0) return cand;
            } catch { /* continue */ }
        }
        // 마지막 수단
        return isWin ? 'python.exe' : 'python3';
    }

    private _tryLoadRuntime(): RuntimeConfig | null {
        try {
            if (!fs.existsSync(this._runtimePath)) return null;
            return JSON.parse(fs.readFileSync(this._runtimePath, 'utf-8')) as RuntimeConfig;
        } catch { return null; }
    }

    private async _waitForRuntime(timeoutMs: number): Promise<CoreClient> {
        const start = Date.now();
        while (Date.now() - start < timeoutMs) {
            await new Promise(r => setTimeout(r, 500));
            const cfg = this._tryLoadRuntime();
            if (cfg) {
                const client = new CoreClient(cfg.port, cfg.session_token);
                const alive = await client.healthCheck();
                if (alive) {
                    this._client = client;
                    return client;
                }
            }
        }
        throw new Error('ReCoder Core 시작 타임아웃 (15초)');
>>>>>>> 74cf4369799da45d0fa49de67d56e58e01a2cc27
    }
}
