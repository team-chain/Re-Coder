/**
 * Local Core 생명주기 관리 (설계서 v6.4 §6)
 * - Lazy Spawn, Singleton, 좀비 프로세스 방지
 * - runtime.json 으로 포트/토큰 공유
 * - 사용자 수동 실행(python core/main.py) 자동 감지 (probeRunningCore)
 * - graceful shutdown: SIGTERM → 5초 대기 → SIGKILL 폴백
 * - F5/확장 테스트: 해당 확장 소스의 core/main.py, 설치 실행: Core 바이너리
 */
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import * as cp from 'child_process';
import { ChildProcess, spawn, execSync } from 'child_process';
import { CoreHealth } from '../types';
import { CoreClient } from '../api/coreClient';
import { shouldReuseRunningCore } from './coreReuse';
import { CoreProcessLog } from './CoreProcessLog';

export interface RuntimeConfig {
    port: number;
    session_token: string;
    started_at?: string;
    pid?: number;
    /** 이 Core 를 띄운 실행 파일의 절대경로. 구버전 Core 는 이 값이 없다. */
    entrypoint?: string;
}

interface SpawnSpec {
    command: string;
    args: string[];
    cwd?: string;
}

const SHUTDOWN_GRACE_MS = 5000;
const RESTART_SHUTDOWN_TIMEOUT_MS = 15000;
const AWS_ACCESS_KEY_SECRET = 'recoder.aws.accessKeyId';
const AWS_SECRET_KEY_SECRET = 'recoder.aws.secretAccessKey';
const AWS_REGION_SECRET = 'recoder.aws.region';
const AWS_SESSION_TOKEN_SECRET = 'recoder.aws.sessionToken';
//: 프로필 이름은 비밀이 아니므로 globalState 에 둔다. 키(SecretStorage)와
//: **상호 배타**로 관리한다 — 둘 다 남아 있으면 재시작한 코어에서
//: AWS_ACCESS_KEY_ID 가 AWS_PROFILE 을 이겨서, 화면은 프로필 A 인데
//: 실제 요청은 옛 키 B 로 나가는 조용한 계정 뒤바뀜이 생긴다.
const AWS_PROFILE_STATE = 'recoder.aws.profile';
const AWS_PROFILE_REGION_STATE = 'recoder.aws.profileRegion';
//: 프로그램 안에서 만든 배포 전용 역할의 ARN. 비밀이 아니다 — 임시 자격증명은
//: 코어가 기반(프로필 또는 키)으로 매번 새로 빌리고, 저장하는 건 이 ARN 뿐이다.
const AWS_ROLE_ARN_STATE = 'recoder.aws.roleArn';

export interface AwsSecretCredentials {
    accessKeyId: string;
    secretAccessKey: string;
    region: string;
    sessionToken?: string;
}

export class CoreManager {
    private static instance: CoreManager;
    private coreProcess: ChildProcess | null = null;
    private port: number = 17894;
    private sessionToken: string = '';
    private isSpawning: boolean = false;
    /** 동시에 열린 Sidebar/Workspace 가 같은 Core를 중복 시작하지 않도록 직렬화. */
    private ensurePromise: Promise<CoreClient> | null = null;
    private restartPromise: Promise<CoreClient> | null = null;
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
        // 재시작 중에는 종료 대기 중인 Core를 다시 연결해 반환하지 않는다.
        if (this.restartPromise) { return this.restartPromise; }
        if (this.ensurePromise) {
            return this.ensurePromise;
        }
        this.ensurePromise = this._ensureRunning();
        try {
            return await this.ensurePromise;
        } finally {
            this.ensurePromise = null;
        }
    }

    private async _ensureRunning(): Promise<CoreClient> {
        const spec = this._findCoreSpec();
        const expected = this.expectedEntrypoint(spec);
        const runtime = await this.readRuntime();
        if (runtime) {
            if (shouldReuseRunningCore(expected, runtime.entrypoint)) {
                this.useRuntime(runtime, expected);
                const health = await this.healthCheck();
                if (health && health.status !== 'down') {
                    this._client = new CoreClient(this.port, this.sessionToken);
                    return this._client;
                }
            } else if (!Number.isSafeInteger(runtime.pid) || runtime.pid <= 1 || this.isProcessRunning(runtime.pid)) {
                this.assertCompatibleRuntime(runtime, expected);
            }
        }

        // 수동 실행 또는 runtime 파일 기록이 늦는 경우도 실행 경로와 토큰을 확인한다.
        const detected = await this.probeRunningCore();
        if (detected) {
            const deadline = Date.now() + 3000;
            while (Date.now() < deadline) {
                const rt = await this.readRuntime();
                if (rt && rt.port === detected.port) {
                    this.assertCompatibleRuntime(rt, expected);
                    if (rt.session_token) {
                        this.useRuntime(rt, expected);
                        this._client = new CoreClient(this.port, this.sessionToken);
                        return this._client;
                    }
                }
                await this.sleep(200);
            }
            throw new Error('실행 중인 Core의 인증 정보를 읽지 못했습니다. Core 실행 상태를 확인한 뒤 다시 연결하세요.');
        }

        if (this.isSpawning) {
            await this.waitForReady();
        } else {
            if (!spec) { throw this.missingCoreError(); }
            await this.cleanupStale();
            await this.spawnCore(spec);
        }
        this._client = new CoreClient(this.port, this.sessionToken);
        return this._client;
    }

    private expectedEntrypoint(spec: SpawnSpec | null = this._findCoreSpec()): string | null {
        // 개발 소스가 없을 때도 임의의 설치 Core로 연결하지 않는다.
        if (this.isDevelopment()) { return this.developmentEntrypoint(); }
        return spec ? spec.args[0] ?? spec.command : null;
    }

    private assertCompatibleRuntime(runtime: RuntimeConfig, expected: string | null): void {
        if (!shouldReuseRunningCore(expected, runtime.entrypoint)) {
            throw new Error(
                `다른 실행 경로의 Core가 실행 중입니다. 현재: ${runtime.entrypoint || '경로 정보 없음'} / ` +
                `필요: ${expected}. 다른 ReCoder 창의 자동 재연결을 멈추거나 창을 닫은 뒤 ` +
                '명령 팔레트에서 ReCoder: Restart Core를 실행해 전환하세요.',
            );
        }
    }

    private useRuntime(runtime: RuntimeConfig, expected: string | null): void {
        this.assertCompatibleRuntime(runtime, expected);
        if (!runtime.session_token) { throw new Error('Core 실행 정보에 인증 토큰이 없습니다. Core를 재시작하세요.'); }
        this.port = runtime.port;
        this.sessionToken = runtime.session_token;
    }

    private missingCoreError(): Error {
        return new Error(this.isDevelopment()
            ? '개발 Core 소스가 없습니다. 실행 중인 확장 소스 옆의 core/main.py를 확인하세요.'
            : 'ReCoder Core 바이너리가 없습니다. OS에 맞는 VSIX를 설치하거나 Core를 수동 실행한 뒤 연결하세요.');
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

    /**
     * VS Code OS 보안 저장소에서 읽은 AWS 키만 Core 프로세스에 전달한다.
     * 파일이나 workspace 설정에는 키를 쓰지 않는다.
     */
    private async _awsEnv(): Promise<Record<string, string>> {
        try {
            const [accessKeyId, secretAccessKey, storedRegion, sessionToken] = await Promise.all([
                this.extensionContext.secrets.get(AWS_ACCESS_KEY_SECRET),
                this.extensionContext.secrets.get(AWS_SECRET_KEY_SECRET),
                this.extensionContext.secrets.get(AWS_REGION_SECRET),
                this.extensionContext.secrets.get(AWS_SESSION_TOKEN_SECRET),
            ]);
            //: 역할 모드면 기반(프로필/키) 위에 역할 ARN 을 얹어 넘긴다. 코어는
            //: 뜨자마자 그 역할을 빌려 기반 자격증명 대신 임시 자격증명을 쓴다.
            const roleArn = this.extensionContext.globalState.get<string>(AWS_ROLE_ARN_STATE, '');
            const roleEnv = roleArn ? { RECODER_ASSUME_ROLE_ARN: roleArn } : {};
            if (!accessKeyId || !secretAccessKey) {
                //: 키가 없으면 프로필 연결 상태인지 본다. 프로필은 이름만
                //: 넘기면 코어의 boto3 가 ~/.aws 에서 알아서 해석한다 —
                //: 비밀 값이 확장을 거치지 않는 것이 이 경로의 장점이다.
                const profile = this.extensionContext.globalState.get<string>(AWS_PROFILE_STATE, '');
                if (!profile) { return {}; }
                const profileRegion = this.extensionContext.globalState.get<string>(AWS_PROFILE_REGION_STATE, '');
                return {
                    AWS_PROFILE: profile,
                    ...(profileRegion ? { AWS_REGION: profileRegion, AWS_DEFAULT_REGION: profileRegion } : {}),
                    ...roleEnv,
                };
            }
            const region = storedRegion || 'ap-northeast-2';
            return {
                AWS_ACCESS_KEY_ID: accessKeyId,
                AWS_SECRET_ACCESS_KEY: secretAccessKey,
                AWS_REGION: region,
                AWS_DEFAULT_REGION: region,
                ...(sessionToken ? { AWS_SESSION_TOKEN: sessionToken } : {}),
                ...roleEnv,
            };
        } catch {
            return {};
        }
    }

    /**
     * 보관된 AWS 연결 — 자가 조치(재주입)용.
     *
     * 코어를 **재사용**할 때(다른 창이 띄웠거나 사용자가 직접 실행) 는 env 주입이
     * 일어나지 않아 "재시작하면 연결 풀림" 이 됐다. 그 경우 확장이 이 값을 코어의
     * connect API 로 다시 밀어넣는다(SidebarProvider.healAwsConnection). 키는
     * 여기서도 SecretStorage 밖으로 나가지 않는다 — 코어와의 로컬 호출에만 실린다.
     */
    async getStoredAwsConnection(): Promise<
        | { kind: 'keys'; accessKeyId: string; secretAccessKey: string; region: string; sessionToken?: string }
        | { kind: 'profile'; profile: string; region: string }
        | null
    > {
        try {
            const [accessKeyId, secretAccessKey, storedRegion, sessionToken] = await Promise.all([
                this.extensionContext.secrets.get(AWS_ACCESS_KEY_SECRET),
                this.extensionContext.secrets.get(AWS_SECRET_KEY_SECRET),
                this.extensionContext.secrets.get(AWS_REGION_SECRET),
                this.extensionContext.secrets.get(AWS_SESSION_TOKEN_SECRET),
            ]);
            if (accessKeyId && secretAccessKey) {
                return {
                    kind: 'keys', accessKeyId, secretAccessKey,
                    region: storedRegion || 'ap-northeast-2',
                    ...(sessionToken ? { sessionToken } : {}),
                };
            }
            const profile = this.extensionContext.globalState.get<string>(AWS_PROFILE_STATE, '');
            if (!profile) { return null; }
            return {
                kind: 'profile', profile,
                region: this.extensionContext.globalState.get<string>(AWS_PROFILE_REGION_STATE, '') || '',
            };
        } catch {
            return null;
        }
    }

    /** 지금 붙어 있는 코어 인스턴스를 구분하는 키 — 인스턴스당 한 번만 자가 조치한다. */
    coreInstanceKey(): string {
        return `${this.port}:${this.sessionToken}`;
    }

    /** STS 검증이 끝난 자격증명만 VS Code SecretStorage에 보관한다. */
    async storeAwsCredentials(credentials: AwsSecretCredentials): Promise<void> {
        await this.extensionContext.secrets.store(AWS_ACCESS_KEY_SECRET, credentials.accessKeyId);
        await this.extensionContext.secrets.store(AWS_SECRET_KEY_SECRET, credentials.secretAccessKey);
        await this.extensionContext.secrets.store(AWS_REGION_SECRET, credentials.region || 'ap-northeast-2');
        if (credentials.sessionToken) {
            await this.extensionContext.secrets.store(AWS_SESSION_TOKEN_SECRET, credentials.sessionToken);
        } else {
            await this.extensionContext.secrets.delete(AWS_SESSION_TOKEN_SECRET);
        }
        //: 키와 프로필은 상호 배타 — 프로필 흔적을 지워야 재시작한 코어가
        //: 사용자가 마지막으로 고른 쪽(키)으로만 연결된다.
        await this.extensionContext.globalState.update(AWS_PROFILE_STATE, undefined);
        await this.extensionContext.globalState.update(AWS_PROFILE_REGION_STATE, undefined);
        //: 기반이 바뀌면 이전 기반으로 만든 역할 지시도 지운다 — 새 기반이 그
        //: 역할을 빌릴 권한이 있다는 보장이 없고, 있다면 다시 셋업하면 된다.
        await this.extensionContext.globalState.update(AWS_ROLE_ARN_STATE, undefined);
    }

    /** 프로필 연결 상태를 보관한다 — 이름뿐, 비밀 값은 저장하지 않는다. */
    async storeAwsProfile(profile: string, region: string): Promise<void> {
        await this.extensionContext.globalState.update(AWS_PROFILE_STATE, profile);
        await this.extensionContext.globalState.update(AWS_PROFILE_REGION_STATE, region || '');
        //: 반대 방향도 지운다 — 남은 키가 AWS_PROFILE 을 이기면 화면은
        //: 프로필인데 요청은 옛 키로 나가는 계정 뒤바뀜이 된다.
        await Promise.all([
            this.extensionContext.secrets.delete(AWS_ACCESS_KEY_SECRET),
            this.extensionContext.secrets.delete(AWS_SECRET_KEY_SECRET),
            this.extensionContext.secrets.delete(AWS_REGION_SECRET),
            this.extensionContext.secrets.delete(AWS_SESSION_TOKEN_SECRET),
        ]);
        await this.extensionContext.globalState.update(AWS_ROLE_ARN_STATE, undefined);
    }

    /**
     * 배포 전용 역할 ARN 을 보관한다 — 비밀 아님. 기반(프로필/키)은 그대로
     * 두고 그 위에 얹는다. 다음 코어 시작부터 RECODER_ASSUME_ROLE_ARN 으로 넘어간다.
     */
    async storeAwsRole(roleArn: string): Promise<void> {
        await this.extensionContext.globalState.update(AWS_ROLE_ARN_STATE, roleArn || undefined);
    }

    /** 보관된 역할 ARN. 없으면 빈 문자열. */
    getAwsRoleArn(): string {
        return this.extensionContext.globalState.get<string>(AWS_ROLE_ARN_STATE, '');
    }

    /** SecretStorage의 AWS 자격증명(및 프로필·역할 연결 상태)을 제거한다. */
    async clearAwsCredentials(): Promise<void> {
        await Promise.all([
            this.extensionContext.secrets.delete(AWS_ACCESS_KEY_SECRET),
            this.extensionContext.secrets.delete(AWS_SECRET_KEY_SECRET),
            this.extensionContext.secrets.delete(AWS_REGION_SECRET),
            this.extensionContext.secrets.delete(AWS_SESSION_TOKEN_SECRET),
        ]);
        await this.extensionContext.globalState.update(AWS_PROFILE_STATE, undefined);
        await this.extensionContext.globalState.update(AWS_PROFILE_REGION_STATE, undefined);
        await this.extensionContext.globalState.update(AWS_ROLE_ARN_STATE, undefined);
    }

    /** 명시적 재시작은 다른 창이 시작한 공유 Core에도 적용한다. */
    async restart(): Promise<CoreClient> {
        if (this.restartPromise) { return this.restartPromise; }
        this.restartPromise = this.restartCore(this.ensurePromise);
        try {
            return await this.restartPromise;
        } finally {
            this.restartPromise = null;
        }
    }

    private async restartCore(pendingStart: Promise<CoreClient> | null): Promise<CoreClient> {
        // 이미 시작 중이면 runtime.json이 준비된 뒤 그 인스턴스를 종료한다.
        if (pendingStart) { await pendingStart.catch(() => undefined); }
        // 수동 연결 전용 설치본은 대체 실행 파일 없이 정상 Core부터 종료하면 안 된다.
        if (!this._findCoreSpec()) { throw this.missingCoreError(); }
        const runtime = await this.readRuntime();
        if (runtime) {
            const pid = runtime.pid;
            if (!Number.isSafeInteger(pid) || pid <= 1 || pid === process.pid) {
                throw new Error('Core 실행 정보의 PID가 올바르지 않아 재시작할 수 없습니다.');
            }
            if (this.isProcessRunning(pid)) {
                this.port = runtime.port;
                this.sessionToken = runtime.session_token;
                const restartLog = this.createProcessLog();
                restartLog.setPid(pid);
                restartLog.event('restart requested');
                // 공유 runtime의 PID에 신호를 보내지 않는다. 해당 Core가 자신의
                // 종료와 파일 정리를 수행하게 하고, 실제 종료 전에는 재연결하지 않는다.
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 3000);
                try {
                    const response = await fetch(`http://127.0.0.1:${runtime.port}/api/shutdown`, {
                        method: 'POST',
                        headers: { 'X-Session-Token': runtime.session_token },
                        signal: controller.signal,
                    });
                    if (!response.ok) {
                        throw new Error(`Core 종료 요청 실패 (HTTP ${response.status})`);
                    }
                    const result = await response.json() as { status?: string };
                    if (result.status !== 'shutting_down') {
                        throw new Error('Core가 종료 요청을 확인하지 않았습니다.');
                    }
                } catch (error) {
                    restartLog.event(`shutdown request failed: ${error instanceof Error ? error.message : String(error)}`);
                    throw error;
                } finally {
                    clearTimeout(timer);
                }
                const deadline = Date.now() + RESTART_SHUTDOWN_TIMEOUT_MS;
                while (this.isProcessRunning(pid)) {
                    if (Date.now() >= deadline) {
                        restartLog.event('restart failed: shutdown timeout');
                        throw new Error('Core가 종료되지 않아 재시작을 완료하지 못했습니다. 잠시 후 다시 시도하세요.');
                    }
                    await this.sleep(100);
                }
                if (this.coreProcess?.pid === pid) { this.coreProcess = null; }
            }
        } else if (this.coreProcess || await this.healthCheck()) {
            throw new Error('실행 중인 Core의 실행 정보를 찾을 수 없어 재시작할 수 없습니다.');
        }
        this._client = null;
        // ensureRunning()은 restartPromise를 기다리므로 내부 시작 경로를 사용한다.
        return this._ensureRunning();
    }

    private isProcessRunning(pid: number): boolean {
        try {
            process.kill(pid, 0);
            return true;
        } catch (err) {
            if ((err as NodeJS.ErrnoException).code === 'ESRCH') { return false; }
            throw err;
        }
    }

    private createProcessLog(secrets: string[] = []): CoreProcessLog {
        return new CoreProcessLog(path.join(path.dirname(this._runtimePath), 'core.log'), {
            secrets: [this.sessionToken, ...secrets],
            onError: () => console.warn('[ReCoder Core] core.log 파일에 로그를 저장하지 못했습니다.'),
        });
    }

    private async spawnCore(spec: SpawnSpec | null = this._findCoreSpec()): Promise<void> {
        this.isSpawning = true;
        let processLog: CoreProcessLog | undefined;
        try {
            if (!spec) { throw this.missingCoreError(); }

            vscode.window.showInformationMessage('ReCoder Core를 시작합니다...');

            const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? os.homedir();
            // 바이너리 모드일 때만 port/workspace 인자 추가
            const args = spec.args.length === 0
                ? ['--port', String(this.port), '--workspace', workspacePath]
                : spec.args;

            // 게이트웨이 모드: 설정 URL + 저장된 학생 토큰이 있으면 Core 에 env 주입 →
            // Core 의 provider_router 가 Bedrock 직접호출 대신 운영자 게이트웨이를 사용.
            const [gatewayEnv, awsEnv] = await Promise.all([this._gatewayEnv(), this._awsEnv()]);

            //: 확장 호스트 전용 변수(ELECTRON_RUN_AS_NODE 등)는 코어에 넘기지 않는다 — 코어가
            //: 띄우는 Docker Desktop(Electron)이 그걸 물려받으면 GUI 없이 즉시 종료한다(실기기).
            const hostEnv: NodeJS.ProcessEnv = { ...process.env };
            for (const k of ['ELECTRON_RUN_AS_NODE', 'ELECTRON_NO_ATTACH_CONSOLE', 'NODE_OPTIONS']) { delete hostEnv[k]; }
            const coreEnv = { ...hostEnv, ...gatewayEnv, ...awsEnv };
            processLog = this.createProcessLog(Object.entries(coreEnv)
                .filter(([key]) => /TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY/i.test(key))
                .map(([, value]) => value ?? ''));
            processLog.event('spawn requested');
            processLog.event(`selected mode=${this.isDevelopment() ? 'development' : 'installed'} entrypoint=${this.expectedEntrypoint(spec)}`);
            this.coreProcess = spawn(spec.command, args, {
                env: { ...hostEnv, ...gatewayEnv, ...awsEnv },
                detached: false,
                stdio: ['ignore', 'pipe', 'pipe'],
                cwd: spec.cwd,
                shell: false,
            });
            const spawnedProcess = this.coreProcess;
            processLog.setPid(spawnedProcess.pid);
            this.coreProcess.once('spawn', () => processLog.event('spawned'));

            this.coreProcess.stdout?.on('data', (data: Buffer) => {
                processLog.write('stdout', data);
                console.log('[ReCoder Core]', data.toString().trim());
            });
            this.coreProcess.stderr?.on('data', (data: Buffer) => {
                processLog.write('stderr', data);
                console.error('[ReCoder Core STDERR]', data.toString().trim());
            });
            this.coreProcess.on('exit', (code, signal) => {
                processLog.event(`exited code=${code} signal=${signal}`);
                console.log(`[ReCoder Core] exited code=${code} signal=${signal}`);
                if (this.coreProcess === spawnedProcess) {
                    this.coreProcess = null;
                    this._client = null;
                }
            });
            this.coreProcess.on('error', (err) => {
                processLog.event(`spawn error: ${err.message}`);
                console.error('[ReCoder Core] spawn error:', err);
                if (this.coreProcess === spawnedProcess) { this.coreProcess = null; }
            });
            this.coreProcess.on('close', () => processLog.finish());

            await this.waitForReady(15000);
            const runtime = await this.readRuntime();
            if (runtime) {
                processLog.addSecrets([runtime.session_token]);
                this.useRuntime(runtime, this.expectedEntrypoint(spec));
            }
            processLog.event(`ready port=${this.port}`);
        } catch (error) {
            processLog?.event(`startup failed: ${error instanceof Error ? error.message : String(error)}`);
            throw error;
        } finally {
            this.isSpawning = false;
        }
    }

    private async waitForReady(timeoutMs: number = 15000): Promise<void> {
        const expected = this.expectedEntrypoint();
        const start = Date.now();
        while (Date.now() - start < timeoutMs) {
            const runtime = await this.readRuntime();
            if (runtime) {
                this.assertCompatibleRuntime(runtime, expected);
                if (runtime.session_token) {
                    this.useRuntime(runtime, expected);
                    const health = await this.healthCheck();
                    if (health && health.status !== 'down') { return; }
                }
            }
            await this.sleep(500);
        }
        throw new Error('ReCoder Core가 시간 내에 준비되지 않았습니다.');
    }

    private async cleanupStale(): Promise<void> {
        const runtime = await this.readRuntime();
        if (!runtime) { return; }
        const pid = runtime.pid;
        if (!Number.isSafeInteger(pid) || pid <= 1 || this.isProcessRunning(pid)) {
            throw new Error('기존 Core의 종료를 확인하지 못했습니다. 실행 중인 Core를 확인한 뒤 ReCoder: Restart Core로 다시 시작하세요.');
        }
        // 죽은 프로세스의 기록만 정리한다. 공유 PID를 SIGTERM/SIGKILL 하지 않는다.
        // 락은 새 Core의 singleton이 소유 PID를 확인한 뒤 회수한다.
        try { fs.unlinkSync(this.getRuntimeJsonPath()); } catch { /* already removed */ }
    }

    async healthCheck(): Promise<CoreHealth | null> {
        // 포트가 아직 확정되지 않았으면(runtime.json 미작성/부분작성 등) 요청을
        // 보내지 않는다. (이전엔 http://127.0.0.1:undefined/api/health 로 가서
        // fetch 가 throw + Node DeprecationWarning 발생 → "연결 안됨" 깜빡임)
        if (!this.port || this.port <= 0 || !Number.isFinite(this.port)) {
            return null;
        }
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
        // **이 매니저가 직접 띄운 프로세스만 죽인다.**
        //
        // runtime.json 의 PID 는 공유 자원이다 — 두 번째 VSCode 창은 기존
        // Core 에 붙기만 하고 coreProcess 가 null 인데, 여기서 runtime PID 로
        // 폴백해 죽이면 **다른 창이 소유한 Core 를 종료**한다. 아무 보조 창
        // 하나만 닫아도 열려 있는 모든 창의 연결이 끊기는 형태다.
        //
        // 붙기만 한 창의 정리는 연결 해제(_client = null)로 끝난다. Core 는
        // 소유 창이 닫힐 때 그 창의 shutdown 이 거두고, 소유 창이 비정상
        // 종료된 경우는 singleton 의 stale-lock 회수 경로가 처리한다.
        const proc = this.coreProcess;
        const pid = proc?.pid;
        this._client = null;

        if (!proc || !pid) { return; }

        const stopLog = this.createProcessLog();
        stopLog.setPid(pid);
        stopLog.event(`stop requested force=${force}`);

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
        // 같은 실행 경로의 Core가 발급한 최신 토큰만 동기화한다.
        // - 토큰이 없던 상태(빈 문자열)였더라도 runtime.json 의 값이 있으면 채운다.
        // - 이미 토큰이 있어도 Core 가 재시작되어 새 토큰을 발급했을 수 있으므로 갱신.
        const runtime = await this.readRuntime();
        if (runtime && runtime.session_token) {
            this.assertCompatibleRuntime(runtime, this.expectedEntrypoint());
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

    /** F5/확장 테스트만 소스를 사용한다. 열린 프로젝트의 이름이나 구조로 추측하지 않는다. */
    private isDevelopment(): boolean {
        return this.extensionContext.extensionMode === vscode.ExtensionMode.Development
            || this.extensionContext.extensionMode === vscode.ExtensionMode.Test;
    }

    private developmentEntrypoint(): string {
        return path.resolve(this.extensionContext.extensionPath, '..', 'core', 'main.py');
    }

    /** 개발: 해당 확장의 소스. 설치: 번들 → PATH → 사용자 bin. 소스로 자동 폴백하지 않는다. */
    private _findCoreSpec(): SpawnSpec | null {
        if (this.isDevelopment()) {
            const mainPy = this.developmentEntrypoint();
            if (!fs.existsSync(mainPy)) { return null; }
            const coreDir = path.dirname(mainPy);
            return { command: this._findPython(coreDir), args: [mainPy], cwd: coreDir };
        }

        const binaryName = process.platform === 'win32' ? 'recoder-core.exe' : 'recoder-core';
        const bundledPath = path.join(this.extensionContext.extensionPath, 'bin', binaryName);
        if (fs.existsSync(bundledPath)) { return { command: bundledPath, args: [] }; }

        try {
            const which = process.platform === 'win32' ? 'where' : 'which';
            const found = execSync(`${which} recoder-core`, { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'] })
                .split(/\r?\n/)[0].trim();
            if (found && fs.existsSync(found)) { return { command: found, args: [] }; }
        } catch { /* not in PATH */ }

        const homePath = path.join(os.homedir(), '.recoder', 'bin', binaryName);
        if (fs.existsSync(homePath)) { return { command: homePath, args: [] }; }
        return null;
    }

    /** 기존 시그니처 호환용 — 바이너리 경로만 반환 */
    private getCoreBinaryPath(): string {
        const spec = this._findCoreSpec();
        if (!spec) return '';
        // python 분기는 빈 문자열 반환 (호환)
        return spec.args.length === 0 ? spec.command : '';
    }

    /**
     * Core 소스를 실행할 python 을 고른다.
     * 개발 모드에서는 프로젝트 venv(core/.venv)를 최우선 — 글로벌 python 에
     * Core 의존성(fastapi 등)이 없어도 바로 뜨게 한다.
     */
    private _findPython(coreDir?: string): string {
        const isWin = process.platform === 'win32';
        // 0) 프로젝트 venv 우선 (의존성 포함)
        if (coreDir) {
            const venvPy = isWin
                ? path.join(coreDir, '.venv', 'Scripts', 'python.exe')
                : path.join(coreDir, '.venv', 'bin', 'python');
            if (fs.existsSync(venvPy)) { return venvPy; }
        }
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
