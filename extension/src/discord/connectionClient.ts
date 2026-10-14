import { createHash, randomBytes } from 'crypto';
import { isLoopbackBridgeHost } from '../bridge/bridgeEndpoint';

export function serverUrl(raw: string): string {
    const url = new URL(raw.trim() || 'http://127.0.0.1:8765');
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && isLoopbackBridgeHost(url.hostname))) {
        throw new Error('Discord 연결 서버에는 HTTPS 주소를 사용하세요. 로컬 개발은 HTTP를 사용할 수 있습니다.');
    }
    if (url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)) {
        throw new Error('Discord 서버의 기본 주소만 입력하세요.');
    }
    return url.origin;
}

export function projectId(machine: string, workspace: string): string {
    const folder = process.platform === 'win32' ? workspace.toLowerCase() : workspace;
    return createHash('sha256').update(machine + '\0' + folder).digest('hex');
}

export function loginProof() {
    const verifier = randomBytes(48).toString('base64url');
    return { verifier, challenge: createHash('sha256').update(verifier).digest('hex') };
}

export class DiscordConnectionClient {
    readonly base: string;
    constructor(base: string, private request: typeof fetch = fetch) { this.base = serverUrl(base); }
    async call(route: string, token = '', method = 'GET', body?: unknown, signal?: AbortSignal): Promise<any> {
        const timeout = new AbortController();
        const abort = () => timeout.abort();
        if (signal?.aborted) timeout.abort();
        signal?.addEventListener('abort', abort, { once: true });
        const timer = setTimeout(abort, 15000);
        try {
            const headers: Record<string, string> = { 'Content-Type': 'application/json' };
            if (token) headers.Authorization = `Bearer ${token}`;
            const response = await this.request(`${this.base}/api/v1/connect/${route}`, {
                method, headers, body: body === undefined ? undefined : JSON.stringify(body),
                signal: timeout.signal, redirect: 'error',
            });
            const data = await response.json();
            if (!response.ok) throw new Error(`Discord 연결 (${response.status}): ${String(data.error || '요청에 실패했습니다.').slice(0, 350)}`);
            return data;
        } catch (error: any) {
            if (signal?.aborted) throw new Error('Discord 연결을 취소했습니다.');
            if (error?.message?.startsWith('Discord 연결 (')) throw error;
            throw new Error('Discord 봇 서버에 연결할 수 없습니다. 서버가 실행 중인지와 연결 서버 주소를 확인하세요.');
        } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
    }
    authorizeUrl(raw: string): string {
        const url = new URL(raw);
        if (url.origin !== this.base || !/^\/api\/v1\/connect\/authorize\/[\w-]+$/.test(url.pathname) || url.search || url.hash) {
            throw new Error('Discord 로그인 주소가 연결 서버와 일치하지 않습니다.');
        }
        return url.toString();
    }
    websocketUrl(): string { return this.base.replace(/^http/, 'ws') + '/api/v1/connect/ws'; }
}
