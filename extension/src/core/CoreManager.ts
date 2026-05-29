/**
 * Local Core 생명주기 관리 (설계서 v6.4 §6)
 * - Lazy Spawn, Singleton, 좀비 프로세스 방지
 * - runtime.json 으로 포트/토큰 공유
 * - 사용자 수동 실행(python core/main.py) 자동 감지 (probeRunningCore)
 * - graceful shutdown: SIGTERM → 5초 대기 → SIGKILL 폴백
 * - 개발 모드(분기): core/main.py 를 python 으로 실행, Windows python 탐색
 */
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import * as cp from 'child_process';
import { ChildProcess, spawn, execSync } from 'child_process';
import { CoreHealth } from '../types';
import { CoreClient } from '../api/coreClient';

export interface RuntimeConfig {
    port: number;
    session_token: string;
    started_at?: string;
    pid?: number;
}

interface SpawnSpec {
    command: string;
    args: string[];
    cwd?: string;
}

const SHUTDOWN_GRACE_MS = 5000;

export class CoreManager {
    private static instance: CoreManager;
    private coreProcess: ChildProcess | null = null;
    private port: number = 17894;
    private sessionToken: string = '';
    private isSpawning: boolean = false;
    private extensionContext: vscode.ExtensionContext;

    // CoreClient 인스턴스 (외부에서 사용)
    private _client: CoreClient | null = null;
    private readonly _runtimePath: string;
    private readonly _lockPath: string;

    constructor(context: vscode.ExtensionContext) {
        this.extensionContext = context;
        const home = os.homedir();
        this._runtimePath = path.join(home, '.recoder', 'runtime.json');
        this._lockPath = path.join(home, '.recoder', 'core.lock');
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

    /** CoreClient 접근자 — Sidebar/Workbench 가 사용 */
    get client(): CoreClient {
        if (!this._client) {
            // 가능한 경우 즉시 생성 (port/token 이 이미 있으면)
            if (this.port) {
                this._client = new CoreClient(this.port, this.sessionToken);
                return this._client;
            }
            throw new Error('Core가 실행 중이 아닙니다.');
        }
        return this._client;
    }

    async ensureRunning(): Promise<CoreClient> {
        // 1) 정상 경로 — runtime.json + healthCheck.
        const runtime = await this.readRuntime();
        if (runtime) {
            this.port = runtime.port;
            this.sessionToken = runtime.session_token;
            const health = await this.healthCheck();
            if (health && health.status !== 'down') {
                this._client = new CoreClient(this.port, this.sessionToken);
                return this._client;
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
                    this._client = new CoreClient(this.port, this.sessionToken);
                    return this._client;
                }
                await this.sleep(200);
            }
            // 토큰을 못 받아도 일단 connect 는 가능 — 인증 필요 호출이 401/503 일 뿐.
            // 호출 측에서 refreshToken 으로 재시도.
            this._client = new CoreClient(this.port, this.sessionToken);
            return this._client;
        }

        if (this.isSpawning) {
            await this.waitForReady();
            this._client = new CoreClient(this.port, this.sessionToken);
            return this._client;
        }

        // 3) 그래도 못 찾으면 직접 spawn 시도. 번들된 바이너리가 없는 dev 환경에선
        //    core/main.py 를 python 으로 실행한다.
        await this.cleanupStale();
        await this.spawnCore();
        this._client = new CoreClient(this.port, this.sessionToken);
        return this._client;
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

    private _tryLoadRuntime(): RuntimeConfig | null {
        try {
            if (!fs.existsSync(this._runtimePath)) return null;
            return JSON.parse(fs.readFileSync(this._runtimePath, 'utf-8')) as RuntimeConfig;
        } catch { return null; }
    }

    /** 게이트웨이 모드 env: recoder.gateway.url + 저장된 학생 토큰이 모두 있으면 주입. */
    private async _gatewayEnv(): Promise<Record<string, string>> {
        try {
            const url = (vscode.workspace.getConfiguration('recoder.gateway').get<string>('url', '') || '').trim();
            const token = (await this.extensionContext.secrets.get('recoder.studentToken')) || '';
            if (url && token) {
                return { RECODER_LLM_GATEWAY_URL: url, RECODER_STUDENT_TOKEN: token };
            }
        } catch { /* ignore */ }
        return {};
    }

    private async spawnCore(): Promise<void> {
        this.isSpawning = true;
        try {
            const spec = this._findCoreSpec();
            if (!spec) {
                throw new Error('ReCoder Core 바이너리/소스를 찾을 수 없습니다. (bin/recoder-core 또는 core/main.py)');
            }

            vscode.window.showInformationMessage('ReCoder Core를 시작합니다...');

            const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? os.homedir();
            // 바이너리 모드일 때만 port/workspace 인자 추가
            const args = spec.args.length === 0
                ? ['--port', String(this.port), '--workspace', workspacePath]
                : spec.args;

            // 게이트웨이 모드: 설정 URL + 저장된 학생 토큰이 있으면 Core 에 env 주입 →
            // Core 의 provider_router 가 Bedrock 직접호출 대신 운영자 게이트웨이를 사용.
            const gatewayEnv = await this._gatewayEnv();

            this.coreProcess = spawn(spec.command, args, {
                env: { ...process.env, ...gatewayEnv },
                detached: false,
                stdio: ['ignore', 'pipe', 'pipe'],
                cwd: spec.cwd,
                shell: false,
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
                this._client = null;
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
                    this.port = cfg.port;
                    this.sessionToken = cfg.session_token;
                    return client;
                }
            }
        }
        throw new Error('ReCoder Core 시작 타임아웃 (15초)');
    }

    private async cleanupStale(): Promise<void> {
        const runtime = await this.readRuntime();
        if (!runtime) { return; }
        const pid = runtime.pid;
        if (!pid) {
            // pid 없으면 runtime.json 만 제거
            try { if (fs.existsSync(this.getRuntimeJsonPath())) { fs.unlinkSync(this.getRuntimeJsonPath()); } } catch { /* ignore */ }
            return;
        }
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

    /** SIGTERM → 5초 대기 → SIGKILL 폴백. shutdown 의 별칭. */
    async stop(): Promise<void> {
        return this.shutdown(false);
    }

    async shutdown(force: boolean = false): Promise<void> {
        const runtime = await this.readRuntime();
        const pid = this.coreProcess?.pid ?? runtime?.pid;
        const proc = this.coreProcess;
        this._client = null;

        if (!pid && !proc) { return; }

        if (process.platform === 'win32') {
            if (pid) {
                try { execSync(`taskkill /PID ${pid} /F`, { stdio: 'ignore' }); } catch { /* ignore */ }
            } else if (proc) {
                try { proc.kill('SIGTERM'); } catch { /* ignore */ }
            }
        } else {
            if (force) {
                if (pid) {
                    try { process.kill(pid, 'SIGKILL'); } catch { /* ignore */ }
                } else if (proc) {
                    try { proc.kill('SIGKILL'); } catch { /* ignore */ }
                }
            } else {
                // graceful: SIGTERM 후 SHUTDOWN_GRACE_MS 대기, 안 되면 SIGKILL
                if (pid) {
                    try { process.kill(pid, 'SIGTERM'); } catch { /* might be gone */ }
                } else if (proc) {
                    try { proc.kill('SIGTERM'); } catch { /* ignore */ }
                }

                const deadline = Date.now() + SHUTDOWN_GRACE_MS;
                let exited = false;
                while (Date.now() < deadline) {
                    await this.sleep(300);
                    if (pid) {
                        try { process.kill(pid, 0); } catch { exited = true; break; }
                    } else {
                        if (!proc || proc.killed) { exited = true; break; }
                    }
                }

                if (!exited) {
                    if (pid) {
                        try { process.kill(pid, 'SIGKILL'); } catch { /* ignore */ }
                    } else if (proc) {
                        try { proc.kill('SIGKILL'); } catch { /* ignore */ }
                    }
                }
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
            if (changed) {
                this._client = new CoreClient(this.port, this.sessionToken);
            }
            return changed;
        }
        return false;
    }

    /**
     * Core 실행 명령을 결정한다.
     *   1) VSIX 번들 바이너리 (`extension/bin/recoder-core[.exe]`)
     *   2) PATH 상의 `recoder-core`
     *   3) `~/.recoder/bin/recoder-core[.exe]`
     *   4) 개발 모드: `extension/../core/main.py` 를 python 으로 실행
     *
     * 반환값은 spawn(command, args) 에 그대로 넘길 수 있는 형태.
     * 못 찾으면 null 반환.
     */
    private _findCoreSpec(): SpawnSpec | null {
        const platform = process.platform;
        const binaryName = platform === 'win32' ? 'recoder-core.exe' : 'recoder-core';
        const ext = this.extensionContext.extensionPath;

        // 1) VSIX 번들 (Windows: .exe / 그 외: extension)
        const bundledPath = path.join(ext, 'bin', binaryName);
        if (fs.existsSync(bundledPath)) { return { command: bundledPath, args: [] }; }

        // Windows 가 아닌데 .exe 가 없으면 그냥 'recoder-core' 도 확인
        const bundledAlt = path.join(ext, 'bin', 'recoder-core');
        if (fs.existsSync(bundledAlt)) { return { command: bundledAlt, args: [] }; }

        // 2) PATH 상의 바이너리
        try {
            const which = platform === 'win32' ? 'where' : 'which';
            const result = execSync(`${which} recoder-core`, { encoding: 'utf-8' }).trim();
            if (result && fs.existsSync(result.split('\n')[0])) {
                return { command: result.split('\n')[0], args: [] };
            }
        } catch { /* not in PATH */ }

        // 3) ~/.recoder/bin/...
        const homePath = path.join(os.homedir(), '.recoder', 'bin', binaryName);
        if (fs.existsSync(homePath)) { return { command: homePath, args: [] }; }

        // 4) 개발 모드: core/main.py
        const candidates = [
            path.join(ext, '..', 'core', 'main.py'),
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

        return null;
    }

    /** 기존 시그니처 호환용 — 바이너리 경로만 반환 */
    private getCoreBinaryPath(): string {
        const spec = this._findCoreSpec();
        if (!spec) return '';
        // python 분기는 빈 문자열 반환 (호환)
        return spec.args.length === 0 ? spec.command : '';
    }

    /** Windows 는 python.exe / python / py, 그 외는 python3 / python 우선. */
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

    private getRuntimeJsonPath(): string {
        // Core writes runtime.json to ~/.recoder/runtime.json (singleton.py)
        return this._runtimePath;
    }

    private sleep(ms: number): Promise<void> {
        return new Promise(resolve => setTimeout(resolve, ms));
    }
}
