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
        const runtime = await this.readRuntime();
        if (runtime) {
            this.port = runtime.port;
            this.sessionToken = runtime.session_token;
            const health = await this.healthCheck();
            if (health && health.status !== 'down') {
                return;
            }
        }

        if (this.isSpawning) {
            await this.waitForReady();
            return;
        }

        await this.cleanupStale();
        await this.spawnCore();
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
        return path.join(os.tmpdir(), 'recoder-runtime.json');
    }

    private sleep(ms: number): Promise<void> {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }
}
