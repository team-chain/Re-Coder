import * as vscode from 'vscode';
import { randomUUID } from 'crypto';
import * as path from 'path';
import { samePath } from '../core/coreReuse';
import { BridgeClient, OpsRequestMessage } from '../bridge/BridgeClient';
import { DiscordConnectionClient, loginProof, projectId } from './connectionClient';
import { projectRoots } from '../activeProject';

/** Discord 에서 받은, VS Code 승인을 기다리는 배포·롤백 요청. */
export interface DiscordOpsRequest {
    requestId: string;
    action: 'deploy' | 'rollback';
    requester: string;
    projectName: string;
    workspace: string;
    receivedAt: number;
    /** VS Code 에서 승인해 진행 중이다(결과만 남았다). */
    accepted: boolean;
}
export interface DiscordOpsResult { ok: boolean; state: 'accepted' | 'done' | 'failed' | 'declined' | 'expired' | 'status'; title: string; detail?: string; url?: string }
/** 승인을 기다리는 시간 · 승인한 뒤 결과를 기다리는 시간. 넘으면 Discord 에 닫았다고 알린다(시험에서 줄인다). */
export const OPS_TIMING = { approvalMs: 30 * 60_000, runningMs: 2 * 60 * 60_000, retryMs: 5000 };

/** Owns project-scoped secrets and sockets independently of the webview lifecycle. */
export class DiscordController implements vscode.Disposable {
    private bridges = new Map<string, BridgeClient>();
    private ops = new Map<string, DiscordOpsRequest & { bridgeKey: string; timer?: NodeJS.Timeout }>();
    private readonly opsChanged = new vscode.EventEmitter<DiscordOpsRequest | undefined>();
    /** 새 요청이 오면 그 요청, 목록만 바뀌면 undefined. */
    readonly onOpsChanged = this.opsChanged.event;
    /** Discord 의 상태 질문에 답할 요약(배포 화면과 같은 Core 기록). extension.ts 가 넣는다. */
    statusProvider?: (workspace: string) => Promise<DiscordOpsResult>;
    private logins = new Map<string, AbortController>();
    private disposed = false;
    constructor(private context: vscode.ExtensionContext) {}
    private client() {
        return new DiscordConnectionClient(vscode.workspace.getConfiguration('recoder.discord').get<string>('serverUrl', 'http://127.0.0.1:8765'));
    }
    private id(folder: vscode.WorkspaceFolder) { return projectId(vscode.env.machineId, folder.uri.fsPath); }
    private key(client: DiscordConnectionClient, folder: vscode.WorkspaceFolder) { return `recoder.discord.session.${client.base}.${this.id(folder)}`; }
    /** 워크스페이스 폴더 + 사용자가 고른 외부 프로젝트(워크스페이스에 넣지 않은 폴더). */
    private folders(): vscode.WorkspaceFolder[] {
        const ws = vscode.workspace.workspaceFolders || [];
        const extra = projectRoots().filter(p => !ws.some(f => samePath(f.uri.fsPath, p)))
            .map((p, i) => ({ uri: vscode.Uri.file(p), name: path.basename(p), index: ws.length + i }));
        return [...ws, ...extra];
    }
    /** 활성 프로젝트(배포 캔버스가 다루는 폴더)가 속한 워크스페이스 폴더. 없으면 첫 폴더. */
    private canvasFolder(workspace?: string): vscode.WorkspaceFolder | undefined {
        if (workspace) {
            const exact = this.folders().find(f => samePath(f.uri.fsPath, workspace));
            if (exact) return exact;
            const inside = this.folders().find(f => { const rel = path.relative(f.uri.fsPath, workspace); return !!rel && !rel.startsWith('..') && !path.isAbsolute(rel); });
            if (inside) return inside;
        }
        return this.folders()[0];
    }
    private folder(p: any): vscode.WorkspaceFolder {
        const folders = this.folders(), canvas = this.canvasFolder(p.workspace)?.uri.fsPath, first = folders[0]?.uri.fsPath;
        const folder = folders.find(f => p.project_id ? this.id(f) === p.project_id : p.workspace ? samePath(f.uri.fsPath, canvas || '') : samePath(f.uri.fsPath, first || ''));
        if (!folder) throw new Error('연결할 프로젝트 폴더를 VS Code에서 먼저 여세요.');
        return folder;
    }
    private publicProjects() { return this.folders().map(f => ({ id: this.id(f), name: f.name })); }
    private async bridge(client: DiscordConnectionClient, folder: vscode.WorkspaceFolder, token: string) {
        const key = this.key(client, folder);
        this.bridges.get(key)?.dispose();
        if (this.disposed || !vscode.workspace.isTrusted) return;
        const bridge = new BridgeClient(this.context, { root: folder.uri, url: client.websocketUrl(), token, projectId: this.id(folder),
            onOps: request => void this.receiveOps(key, folder, request).catch(console.error) });
        this.bridges.set(key, bridge);
        bridge.connect();
    }
    async restore() {
        for (const bridge of this.bridges.values()) bridge.dispose();
        this.bridges.clear();
        for (const abort of this.logins.values()) abort.abort();
        const client = this.client();
        for (const folder of this.folders()) {
            const token = await this.context.secrets.get(this.key(client, folder));
            if (token) await this.bridge(client, folder, token);
        }
    }
    /** VS Code 승인을 기다리거나 진행 중인 Discord 요청(배포 화면이 띄운다). */
    pendingOps(): DiscordOpsRequest[] {
        return [...this.ops.values()].map(({ bridgeKey: _k, timer: _t, ...request }) => request);
    }
    private async receiveOps(bridgeKey: string, folder: vscode.WorkspaceFolder, request: OpsRequestMessage) {
        const bridge = this.bridges.get(bridgeKey);
        if (!bridge || this.ops.has(request.request_id)) return;
        if (request.action === 'status') {
            let result: DiscordOpsResult;
            try {
                result = this.statusProvider ? await this.statusProvider(folder.uri.fsPath)
                    : { ok: false, state: 'status', title: `${folder.name} · 상태를 아직 읽을 수 없어요`, detail: 'VS Code에서 ReCoder를 연 뒤 다시 물어봐 주세요.' };
            } catch (error) {
                result = { ok: false, state: 'status', title: `${folder.name} · 상태를 읽지 못했어요`, detail: error instanceof Error ? error.message : String(error) };
            }
            bridge.send({ type: 'ops.result', request_id: request.request_id, ...trimResult(result) });
            return;
        }
        if ([...this.ops.values()].some(item => item.bridgeKey === bridgeKey)) {
            bridge.send({ type: 'ops.result', request_id: request.request_id, ok: false, state: 'declined',
                title: '이미 처리 중인 요청이 있어요', detail: 'VS Code에서 앞의 요청을 끝낸 뒤 다시 보내 주세요.' });
            return;
        }
        const item = { requestId: request.request_id, action: request.action, requester: request.requester || 'Discord 사용자',
            projectName: folder.name, workspace: folder.uri.fsPath, receivedAt: Date.now(), accepted: false, bridgeKey };
        this.ops.set(item.requestId, item);
        this.arm(item.requestId, OPS_TIMING.approvalMs);
        this.opsChanged.fire(this.pendingOps().find(r => r.requestId === item.requestId));
    }
    private arm(requestId: string, ms: number) {
        const item = this.ops.get(requestId);
        if (!item) return;
        if (item.timer) clearTimeout(item.timer);
        item.timer = setTimeout(() => {
            const label = item.action === 'deploy' ? '배포' : '롤백';
            this.reply(requestId, item.accepted
                ? { ok: false, state: 'expired', title: `${label} 결과를 확인하지 못했어요`, detail: 'VS Code ReCoder의 배포 화면에서 결과를 확인해 주세요.' }
                : { ok: false, state: 'expired', title: `${label} 요청을 닫았어요`, detail: '30분 안에 VS Code에서 승인하지 않았어요. 필요하면 다시 요청해 주세요.' });
        }, ms);
        item.timer.unref?.();
    }
    /** 요청에 답한다. accepted 는 중간 알림(요청은 결과가 나올 때까지 남는다). */
    reply(requestId: string, result: DiscordOpsResult): boolean {
        const item = this.ops.get(requestId);
        if (!item) return false;
        if (result.state === 'accepted') {
            if (item.accepted) return true;
            item.accepted = true;
            this.arm(requestId, OPS_TIMING.runningMs);
        } else {
            if (item.timer) clearTimeout(item.timer);
            this.ops.delete(requestId);
        }
        this.deliver(item.bridgeKey, { type: 'ops.result', request_id: requestId, ...trimResult(result) });
        this.opsChanged.fire(undefined);
        return true;
    }
    /** 소켓이 잠깐 끊겼어도 결과가 사라지지 않게 2분 동안 다시 보낸다. */
    private deliver(bridgeKey: string, message: object, attempt = 0) {
        if (this.disposed) return;
        if (this.bridges.get(bridgeKey)?.send(message)) return;
        if (attempt < 24) setTimeout(() => this.deliver(bridgeKey, message, attempt + 1), OPS_TIMING.retryMs).unref?.();
    }
    async action(type: string, p: any = {}): Promise<any> {
        if (type === 'opsPending') return { requests: this.pendingOps() };
        if (type === 'opsReply') {
            const state = String(p.state || '');
            if (!['accepted', 'done', 'failed', 'declined'].includes(state)) throw new Error('알 수 없는 응답입니다.');
            const ok = this.reply(String(p.opsId || ''), { ok: p.ok === true, state: state as DiscordOpsResult['state'],
                title: String(p.title || ''), detail: String(p.detail || ''), url: String(p.url || '') });
            return { requests: this.pendingOps(), replied: ok };
        }
        if (type === 'settings') {
            await vscode.commands.executeCommand('workbench.action.openSettings', 'recoder.discord.serverUrl');
            return { cancelled: true };
        }
        const folder = this.folder(p), client = this.client(), key = this.key(client, folder);
        const common = { mode: 'oauth', canvas_project_id: (() => { const f = this.canvasFolder(p.workspace); return f ? this.id(f) : ''; })(), project_id: this.id(folder), project_name: folder.name, projects: this.publicProjects() };
        if (type === 'connect') {
            if (!vscode.workspace.isTrusted) throw new Error('프로젝트를 신뢰한 후 Discord를 연결하세요.');
            if (this.logins.has(key)) throw new Error('이 프로젝트의 Discord 로그인이 진행 중입니다.');
            const abort = new AbortController();
            this.logins.set(key, abort);
            try {
                await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: 'Discord 연결', cancellable: true }, async (progress, cancel) => {
                    const disposable = cancel.onCancellationRequested(() => abort.abort());
                    const proof = loginProof();
                    try {
                        const started = await client.call('start', '', 'POST', { challenge: proof.challenge, project_id: this.id(folder), project_name: folder.name }, abort.signal);
                        progress.report({ message: `브라우저에서 승인하세요. 확인 코드: ${started.confirmation_code} · ${folder.name}` });
                        if (!await vscode.env.openExternal(vscode.Uri.parse(client.authorizeUrl(started.authorize_url)))) throw new Error('로그인 브라우저를 열지 못했습니다.');
                        const expires = Date.now() + 300000;
                        while (Date.now() < expires) {
                            if (abort.signal.aborted) throw new Error('Discord 연결을 취소했습니다.');
                            const result = await client.call('poll', '', 'POST', { attempt: started.attempt, verifier: proof.verifier }, abort.signal);
                            if (result.status === 'connected') {
                                if (abort.signal.aborted || !this.folders().some(f => f.uri.toString() === folder.uri.toString()) || client.base !== this.client().base) {
                                    await client.call('session', result.token, 'DELETE');
                                    throw new Error('프로젝트 또는 연결 서버가 변경되어 로그인을 취소했습니다.');
                                }
                                await this.context.secrets.store(key, result.token);
                                await this.bridge(client, folder, result.token);
                                return;
                            }
                            if (['denied', 'failed'].includes(result.status)) throw new Error('Discord 로그인이 취소되었거나 실패했습니다. 다시 연결하세요.');
                            await new Promise<void>(resolve => { const t = setTimeout(done, 1500); function done() { clearTimeout(t); abort.signal.removeEventListener('abort', done); resolve(); } abort.signal.addEventListener('abort', done, { once: true }); });
                        }
                        throw new Error('Discord 로그인이 만료되었습니다. 다시 연결하세요.');
                    } finally { disposable.dispose(); }
                });
            } finally { this.logins.delete(key); }
        }
        const token = await this.context.secrets.get(key);
        if (!token) {
            if (type !== 'status') throw new Error('먼저 Discord에 로그인하세요.');
            try { return { ...common, authenticated: false, ...await client.call('info') }; }
            catch (error: any) { return { ...common, authenticated: false, connection_error: error.message }; }
        }
        if (type === 'disconnect') {
            await client.call('session', token, 'DELETE');
            await this.context.secrets.delete(key);
            this.bridges.get(key)?.dispose(); this.bridges.delete(key);
            return { ...common, authenticated: false, active_channel_id: '' };
        }
        if (type === 'guilds') return client.call('guilds', token);
        if (type === 'channels') return client.call(`guilds/${encodeURIComponent(p.guildId)}/channels`, token);
        if (type === 'invite') {
            const data = await client.call(`invite?guild_id=${encodeURIComponent(p.guildId || '')}`, token);
            const url = new URL(data.invite_url);
            if (url.origin !== 'https://discord.com' || url.pathname !== '/oauth2/authorize') throw new Error('올바른 봇 초대 주소가 아닙니다.');
            await vscode.env.openExternal(vscode.Uri.parse(url.toString()));
            return { message: '관리자가 봇을 초대한 뒤 서버 목록을 새로고침하세요.' };
        }
        if (type === 'event' || type === 'test') {
            if (type === 'event' && p.enabled !== true) throw new Error('이벤트 알림을 먼저 켜세요.');
            return client.call('events', token, 'POST', {
                event_id: type === 'test' ? randomUUID() : String(p.eventId || ''),
                title: type === 'test' ? 'ReCoder 연결 테스트' : String(p.title || '').slice(0, 160),
                detail: type === 'test' ? `${folder.name} 프로젝트의 알림이 이 채널로 전달됩니다.` : String(p.detail || '').slice(0, 1000),
            });
        }
        if (type === 'setChannel') {
            const state = await client.call('channel', token, 'PUT', { guild_id: p.guildId, channel_id: p.channelId, development: p.development === true });
            return { ...common, ...state };
        }
        try { return { ...common, ...await client.call('status', token) }; }
        catch (error: any) { return { ...common, authenticated: false, connection_error: error.message }; }
    }
    dispose() {
        for (const item of this.ops.values()) if (item.timer) clearTimeout(item.timer);
        this.ops.clear();
        this.opsChanged.dispose();
        this.disposed = true;
        for (const abort of this.logins.values()) abort.abort();
        for (const bridge of this.bridges.values()) bridge.dispose();
        this.bridges.clear();
    }
}

function trimResult(result: DiscordOpsResult) {
    const url = /^https?:\/\/[^\s<>]{1,300}$/.test(result.url || '') ? result.url : '';
    //: 봇 소켓은 한 메시지 4KB 까지만 받는다 — 한글 600자(약 1.8KB)로 자른다.
    return { ok: result.ok === true, state: result.state, title: String(result.title || '').slice(0, 160), detail: String(result.detail || '').slice(0, 600), ...(url ? { url } : {}) };
}
