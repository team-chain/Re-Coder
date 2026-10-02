import * as vscode from 'vscode';
import { randomUUID } from 'crypto';
import * as path from 'path';
import { samePath } from '../core/coreReuse';
import { BridgeClient } from '../bridge/BridgeClient';
import { DiscordConnectionClient, loginProof, projectId } from './connectionClient';

/** Owns project-scoped secrets and sockets independently of the webview lifecycle. */
export class DiscordController implements vscode.Disposable {
    private bridges = new Map<string, BridgeClient>();
    private logins = new Map<string, AbortController>();
    private disposed = false;
    constructor(private context: vscode.ExtensionContext) {}
    private client() {
        return new DiscordConnectionClient(vscode.workspace.getConfiguration('recoder.discord').get<string>('serverUrl', 'http://127.0.0.1:8765'));
    }
    private id(folder: vscode.WorkspaceFolder) { return projectId(vscode.env.machineId, folder.uri.fsPath); }
    private key(client: DiscordConnectionClient, folder: vscode.WorkspaceFolder) { return `recoder.discord.session.${client.base}.${this.id(folder)}`; }
    private folders() { return vscode.workspace.workspaceFolders || []; }
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
        const folder = this.folders().find(f => p.project_id ? this.id(f) === p.project_id : p.workspace ? f === this.canvasFolder(p.workspace) : f === this.folders()[0]);
        if (!folder) throw new Error('연결할 프로젝트 폴더를 VS Code에서 먼저 여세요.');
        return folder;
    }
    private publicProjects() { return this.folders().map(f => ({ id: this.id(f), name: f.name })); }
    private async bridge(client: DiscordConnectionClient, folder: vscode.WorkspaceFolder, token: string) {
        const key = this.key(client, folder);
        this.bridges.get(key)?.dispose();
        if (this.disposed || !vscode.workspace.isTrusted) return;
        const bridge = new BridgeClient(this.context, { root: folder.uri, url: client.websocketUrl(), token, projectId: this.id(folder) });
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
    async action(type: string, p: any = {}): Promise<any> {
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
        this.disposed = true;
        for (const abort of this.logins.values()) abort.abort();
        for (const bridge of this.bridges.values()) bridge.dispose();
        this.bridges.clear();
    }
}
