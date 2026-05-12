/**
 * Local Core 생명주기 관리 (설계서 v6.4 §6)
 * - Lazy Spawn, Singleton, 좀비 프로세스 방지
 * - runtime.json 으로 포트/토큰 공유
 *
 * 2026-05-08: dev 분기 spawn 수정, graceful shutdown SIGTERM->SIGKILL
 * 2026-05-10: venv Python 우선 탐색 추가
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
    private _spawnPromise: Promise<CoreClient> | null = null;
    private readonly _channel: vscode.OutputChannel;

    constructor(private readonly context: vscode.ExtensionContext) {
        const home = os.homedir();
        this._runtimePath = path.join(home, '.recoder', 'runtime.json');
        this._lockPath = path.join(home, '.recoder', 'core.lock');
        this._channel = vscode.window.createOutputChannel('ReCoder Core');
    }

    get client(): CoreClient {
        if (!this._client) { throw new Error('Core가 실행 중이 아닙니다.'); }
        return this._client;
    }

    /**
     * Notion 스타일 라이브 대시보드 URL.
     * runtime.json 의 port + session_token 을 사용해 토큰이 포함된 외부 링크를 만든다.
     */
    getDashboardUrl(): string | null {
        const config = vscode.workspace.getConfiguration('recoder');
        const remoteUrl = config.get<string>('remoteServer.url', '').trim();
        if (remoteUrl) {
            return `${remoteUrl.replace(/\/$/, '')}/dashboard`;
        }
        const cfg = this._tryLoadRuntime();
        if (!cfg) { return null; }
        return `http://127.0.0.1:${cfg.port}/dashboard?token=${cfg.session_token}`;
    }

    async ensureRunning(): Promise<CoreClient> {
        // 원격 서버 URL이 설정되어 있으면 로컬 실행 없이 바로 연결
        const config = vscode.workspace.getConfiguration('recoder');
        const remoteUrl = config.get<string>('remoteServer.url', '').trim();
        const remoteToken = config.get<string>('remoteServer.token', '').trim();

        if (remoteUrl) {
            if (this._client) {
                const alive = await this._client.healthCheck();
                if (alive) { return this._client; }
                this._client = null;
            }
            const candidate = new CoreClient(remoteUrl, remoteToken);
            const alive = await candidate.healthCheck();
            if (alive) {
                this._client = candidate;
                return this._client;
            }
            throw new Error(`원격 ReCoder Core 서버에 연결할 수 없습니다: ${remoteUrl}`);
        }

        if (this._client) {
            const alive = await this._client.healthCheck();
            if (alive) { return this._client; }
            this._client = null;
        }
        const existing = this._tryLoadRuntime();
        if (existing) {
            const candidate = new CoreClient(existing.port, existing.session_token);
            const alive = await candidate.healthCheck();
            if (alive) {
                this._client = candidate;
                return this._client;
            }
        }
        if (!this._spawnPromise) {
            this._spawnPromise = this._doSpawn().finally(() => {
                this._spawnPromise = null;
            });
        }
        return this._spawnPromise;
    }

    async stop(): Promise<void> {
        const proc = this._process;
        this._client = null;
        if (!proc) { return; }

        proc.kill('SIGTERM');

        const exited = await new Promise<boolean>((resolve) => {
            const t = setTimeout(() => resolve(false), SHUTDOWN_GRACE_MS);
            proc.once('exit', () => {
                clearTimeout(t);
                resolve(true);
            });
        });

        if (!exited && !proc.killed) {
            try { proc.kill('SIGKILL'); } catch { /* already dead */ }
        }

        this._process = null;
    }

    private async _doSpawn(): Promise<CoreClient> {
        const spec = this._findCoreBinary();
        vscode.window.showInformationMessage('ReCoder Core를 시작합니다...');

        try {
            this._process = cp.spawn(spec.command, spec.args, {
                detached: false,
                stdio: 'pipe',
                cwd: spec.cwd,
                env: { ...process.env },
                shell: false,
            });
        } catch (e: any) {
            throw new Error(`ReCoder Core 실행 실패: ${e?.message ?? e}`);
        }

        this._process.stdout?.on('data', (d: Buffer) => {
            const text = d.toString();
            console.log('[Core]', text);
            this._channel.append(text);
        });
        this._process.stderr?.on('data', (d: Buffer) => {
            const text = d.toString();
            console.error('[Core]', text);
            this._channel.append(text);
        });
        this._process.on('exit', (code) => {
            console.log('[Core] 종료:', code);
            this._process = null;
            this._client = null;
        });
        this._process.on('error', (err) => {
            console.error('[Core] spawn error:', err);
            this._process = null;
        });

        return this._waitForRuntime(15000);
    }

    private _findCoreBinary(): SpawnSpec {
        const ext = this.context.extensionPath;

        const bundledExe = path.join(ext, 'bin', 'recoder-core.exe');
        if (fs.existsSync(bundledExe)) { return { command: bundledExe, args: [] }; }

        const bundled = path.join(ext, 'bin', 'recoder-core');
        if (fs.existsSync(bundled)) { return { command: bundled, args: [] }; }

        const candidates = [
            path.join(ext, '..', 'core', 'main.py'),
            path.join(ext, '..', '..', 'core', 'main.py'),
        ];
        const mainPy = candidates.find(p => fs.existsSync(p));
        if (mainPy) {
            const coreDir = path.dirname(mainPy);
            const py = this._findPython(coreDir);
            return { command: py, args: [mainPy], cwd: coreDir };
        }

        throw new Error('ReCoder Core 바이너리/소스를 찾을 수 없습니다.');
    }

    private _findPython(coreDir?: string): string {
        const isWin = process.platform === 'win32';

        if (coreDir) {
            const venvCandidates = isWin
                ? [
                    path.join(coreDir, 'venv', 'Scripts', 'python.exe'),
                    path.join(coreDir, '.venv', 'Scripts', 'python.exe'),
                ]
                : [
                    path.join(coreDir, 'venv', 'bin', 'python3'),
                    path.join(coreDir, 'venv', 'bin', 'python'),
                    path.join(coreDir, '.venv', 'bin', 'python3'),
                    path.join(coreDir, '.venv', 'bin', 'python'),
                ];
            for (const vp of venvCandidates) {
                if (fs.existsSync(vp)) { return vp; }
            }
        }

        const systemCandidates = isWin
            ? ['python.exe', 'python', 'py']
            : ['python3', 'python'];

        for (const cand of systemCandidates) {
            try {
                const r = cp.spawnSync(cand, ['--version'], { stdio: 'ignore' });
                if (r.status === 0) { return cand; }
            } catch { /* continue */ }
        }

        return isWin ? 'python.exe' : 'python3';
    }

    private _tryLoadRuntime(): RuntimeConfig | null {
        try {
            if (!fs.existsSync(this._runtimePath)) { return null; }
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
    }
}
