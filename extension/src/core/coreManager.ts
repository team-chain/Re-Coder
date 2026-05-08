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
    }
}
