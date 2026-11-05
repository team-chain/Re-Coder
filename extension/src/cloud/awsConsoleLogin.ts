import { createHash, createPublicKey, generateKeyPairSync, randomBytes, randomUUID, sign, timingSafeEqual } from 'crypto';
import { createServer, Server } from 'http';
import { awsRegion, boundedResponse, callerIdentity, CloudCredentials } from './awsTransport';

// AWS Sign-In public same-device client. Protocol: AWS Sign-In CreateOAuth2Token,
// PKCE + RFC 9449 DPoP. No AWS CLI, client secret, or ReCoder login server needed.
const CLIENT = 'arn:aws:signin:::devtools/same-device';
export const BROWSER_SESSION_SECRET = 'recoder.aws.browserSession.v1';
export const BROWSER_LOGIN_STATE = 'recoder.aws.browserLogin';
export interface SecretStore {
    get(key: string): PromiseLike<string | undefined>;
    store(key: string, value: string): PromiseLike<void>;
    delete(key: string): PromiseLike<void>;
}
interface Session {version: 1; region: string; account: string; arn: string; refreshToken: string; privateKey: string}
export interface BrowserLogin {url: string; result: Promise<{code: string; redirectUri: string}>; cancel(): void}

function equalSecret(a: string, b: string): boolean {
    const x = Buffer.from(a), y = Buffer.from(b);
    return x.length === y.length && timingSafeEqual(x, y);
}

export function dpopProof(privateKey: string, uri: string): string {
    const jwk = createPublicKey(privateKey).export({format: 'jwk'});
    const encode = (x: unknown) => Buffer.from(JSON.stringify(x)).toString('base64url');
    const message = `${encode({typ: 'dpop+jwt', alg: 'ES256', jwk})}.${encode({htm: 'POST', htu: uri, iat: Math.floor(Date.now() / 1000), jti: randomUUID()})}`;
    return message + '.' + sign('sha256', Buffer.from(message), {key: privateKey, dsaEncoding: 'ieee-p1363'}).toString('base64url');
}

/** Invalid or unrelated localhost requests never consume a pending login. */
export async function browserCallback(region: string, verifier: string, timeoutMs = 600_000): Promise<BrowserLogin> {
    awsRegion(region);
    const state = randomBytes(32).toString('base64url');
    let complete: (x: {code: string; redirectUri: string}) => void;
    let fail: (e: Error) => void;
    let settled = false;
    let timer: ReturnType<typeof setTimeout>;
    let redirectUri = '';
    const result = new Promise<{code: string; redirectUri: string}>((resolve, reject) => { complete = resolve; fail = reject; });
    // Consumers attach after the listener starts; cancellation must not produce an unhandled rejection.
    void result.catch(() => {});
    const finish = (error?: Error, code?: string) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        server.close();
        server.closeIdleConnections?.();
        if (error) fail(error); else complete({code: code!, redirectUri});
    };
    const server: Server = createServer((req, res) => {
        res.setHeader('Cache-Control', 'no-store');
        res.setHeader('Content-Type', 'text/plain; charset=utf-8');
        res.setHeader('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'");
        res.setHeader('X-Content-Type-Options', 'nosniff');
        res.setHeader('Connection', 'close');
        const expectedHost = new URL(redirectUri).host;
        if (settled || req.method !== 'GET' || req.headers.host !== expectedHost || (req.url?.length || 0) > 4096) {
            res.writeHead(400).end('Invalid callback'); return;
        }
        const url = new URL(req.url || '/', redirectUri);
        const params = url.searchParams;
        if (url.pathname !== '/oauth/callback' || params.getAll('state').length !== 1 || !equalSecret(params.get('state') || '', state)) {
            res.writeHead(400).end('Invalid callback'); return;
        }
        if (params.has('error')) {
            res.writeHead(400).end('AWS 로그인을 완료하지 못했습니다. ReCoder로 돌아가 다시 시도하세요.');
            finish(new Error('AWS 로그인이 취소되었거나 권한이 거절되었습니다.')); return;
        }
        const code = params.get('code');
        if (params.getAll('code').length !== 1 || !code || code.length > 512) { res.writeHead(400).end('Invalid callback'); return; }
        res.end('AWS 응답을 받았습니다. ReCoder에서 연결 결과를 확인하세요. 이 창은 닫아도 됩니다.');
        finish(undefined, code);
    });
    server.requestTimeout = 10_000;
    server.headersTimeout = 10_000;
    await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', () => { server.removeListener('error', reject); resolve(); }); });
    server.on('error', () => finish(new Error('AWS 로그인 응답을 받을 수 없습니다. 다시 시도하세요.')));
    const address = server.address();
    if (!address || typeof address === 'string') { server.close(); throw new Error('로그인 창을 준비하지 못했습니다.'); }
    redirectUri = `http://127.0.0.1:${address.port}/oauth/callback`;
    timer = setTimeout(() => finish(new Error('AWS 로그인 대기 시간이 지났습니다. 다시 로그인하세요.')), timeoutMs);
    timer.unref();
    const url = new URL(`https://${region}.signin.aws.amazon.com/v1/authorize`);
    url.search = new URLSearchParams({response_type: 'code', client_id: CLIENT, scope: 'openid', state,
        redirect_uri: redirectUri, code_challenge_method: 'SHA-256', code_challenge: createHash('sha256').update(verifier).digest('base64url')}).toString();
    return {url: url.toString(), result, cancel: () => finish(new Error('AWS 로그인이 취소되었습니다.'))};
}

/** Credentials stay in memory; refresh token and DPoP key stay in OS SecretStorage. */
export class AwsConsoleSession {
    private cached?: CloudCredentials;
    private current?: Session;
    private refreshing?: Promise<CloudCredentials>;
    private generation = 0;
    private loginPending = false;
    private cancelLogin?: () => void;
    private writes: Promise<void> = Promise.resolve();
    constructor(private secrets: SecretStore, private fetcher: typeof fetch = fetch,
        private identity = callerIdentity) {}

    async login(region: string, openBrowser: (url: string) => PromiseLike<boolean>, signal?: AbortSignal): Promise<{account: string; arn: string; region: string}> {
        if (this.loginPending || this.refreshing) throw new Error('진행 중인 AWS 연결 요청이 끝난 뒤 다시 시도하세요.');
        if (signal?.aborted) throw new Error('AWS 로그인이 취소되었습니다.');
        this.loginPending = true;
        const generation = ++this.generation;
        let callback: BrowserLogin | undefined;
        try {
            const verifier = randomBytes(48).toString('base64url');
            const privateKey = generateKeyPairSync('ec', {namedCurve: 'prime256v1'}).privateKey.export({type: 'pkcs8', format: 'pem'}).toString();
            callback = await browserCallback(region, verifier);
            this.cancelLogin = callback.cancel;
            signal?.addEventListener('abort', callback.cancel, {once: true});
            if (signal?.aborted) callback.cancel();
            if (!await openBrowser(callback.url)) throw new Error('브라우저를 열지 못했습니다. 기본 브라우저 설정을 확인하세요.');
            const {code, redirectUri} = await callback.result;
            const token = await this.token(region, privateKey, {grantType: 'authorization_code', code, codeVerifier: verifier, redirectUri});
            const identity = await this.identity(token.credentials, region, this.fetcher);
            if (generation !== this.generation || signal?.aborted) throw new Error('AWS 연결이 취소되었습니다.');
            const session: Session = {version: 1, region, ...identity, refreshToken: token.refreshToken, privateKey};
            await this.persist(session, generation);
            if (generation !== this.generation || signal?.aborted) throw new Error('AWS 연결이 취소되었습니다.');
            this.current = session;
            this.cached = token.credentials;
            return {...identity, region};
        } finally {
            if (callback) { signal?.removeEventListener('abort', callback.cancel); callback.cancel(); }
            this.cancelLogin = undefined;
            this.loginPending = false;
        }
    }

    async metadata(): Promise<{account: string; arn: string; region: string}> {
        const session = await this.session();
        return {account: session.account, arn: session.arn, region: session.region};
    }

    private async session(): Promise<Session> {
        if (this.current) return this.current;
        const generation = this.generation;
        try {
            const x = JSON.parse(await this.secrets.get(BROWSER_SESSION_SECRET) || 'null') as Session;
            if (!x || x.version !== 1 || !/^\d{12}$/.test(x.account) || !x.arn.startsWith(`arn:aws:`)
                || typeof x.refreshToken !== 'string' || !x.refreshToken || typeof x.privateKey !== 'string') throw new Error();
            awsRegion(x.region);
            const key = createPublicKey(x.privateKey);
            if (key.asymmetricKeyDetails?.namedCurve !== 'prime256v1') throw new Error();
            if (generation !== this.generation) throw new Error();
            this.current = x;
            return x;
        } catch { throw new Error('저장된 AWS 로그인 정보가 없거나 올바르지 않습니다. 다시 로그인하세요.'); }
    }

    async credentials(): Promise<CloudCredentials> {
        if (this.loginPending) throw new Error('AWS 로그인 중입니다. 연결을 마친 뒤 다시 시도하세요.');
        if (this.cached && Date.parse(this.cached.expiration) > Date.now() + 120_000) return this.cached;
        if (this.refreshing) return this.refreshing;
        const generation = this.generation;
        const refresh = async () => {
            const current = await this.session();
            const token = await this.token(current.region, current.privateKey, {grantType: 'refresh_token', refreshToken: current.refreshToken});
            const identity = await this.identity(token.credentials, current.region, this.fetcher);
            if (generation !== this.generation) throw new Error('AWS 연결이 변경되었습니다.');
            if (identity.account !== current.account || identity.arn !== current.arn) throw new Error('갱신된 AWS 계정이 연결된 계정과 다릅니다. 다시 로그인하세요.');
            const updated = {...current, refreshToken: token.refreshToken};
            await this.persist(updated, generation);
            if (generation !== this.generation) throw new Error('AWS 연결이 변경되었습니다.');
            this.current = updated;
            this.cached = token.credentials;
            return token.credentials;
        };
        this.refreshing = refresh();
        try { return await this.refreshing; } finally { this.refreshing = undefined; }
    }

    async logout(): Promise<void> {
        ++this.generation;
        this.cancelLogin?.();
        this.cached = undefined;
        this.current = undefined;
        // Wait out a pending SecretStorage update, then delete, preventing logout resurrection.
        await this.refreshing?.catch(() => {});
        this.writes = this.writes.catch(() => {}).then(async () => { await this.secrets.delete(BROWSER_SESSION_SECRET); });
        await this.writes;
    }

    private async persist(session: Session, generation: number): Promise<void> {
        this.writes = this.writes.catch(() => {}).then(async () => {
            if (generation !== this.generation) throw new Error('AWS 연결이 변경되었습니다.');
            await this.secrets.store(BROWSER_SESSION_SECRET, JSON.stringify(session));
        });
        await this.writes;
    }

    private async token(region: string, privateKey: string, input: Record<string, string>): Promise<{credentials: CloudCredentials; refreshToken: string}> {
        const url = `https://${awsRegion(region)}.signin.aws.amazon.com/v1/token`;
        let response: Response;
        try {
            response = await this.fetcher(url, {method: 'POST', headers: {'Content-Type': 'application/json', DPoP: dpopProof(privateKey, url)},
                body: JSON.stringify({clientId: CLIENT, ...input}), redirect: 'error', signal: AbortSignal.timeout(20_000)});
        } catch { throw new Error('AWS 로그인 서버에 연결하지 못했습니다. 네트워크를 확인하고 다시 시도하세요.'); }
        let data: any;
        try { data = JSON.parse(await boundedResponse(response)); }
        catch { throw new Error('AWS 로그인 응답을 읽지 못했습니다. 다시 로그인하세요.'); }
        if (!response.ok) {
            if (data?.error === 'INSUFFICIENT_PERMISSIONS') throw new Error('AWS 계정에 브라우저 연결 권한(SignInLocalDevelopmentAccess)이 필요합니다. 계정 관리자에게 요청하세요.');
            if (response.status === 429) throw new Error('AWS 로그인 요청이 많습니다. 잠시 후 다시 시도하세요.');
            throw new Error('AWS 로그인 세션이 만료되었거나 거절되었습니다. 다시 로그인하세요.');
        }
        const access = data?.accessToken;
        if (!access || !['accessKeyId', 'secretAccessKey', 'sessionToken'].every(k => typeof access[k] === 'string' && access[k].length > 0 && access[k].length < 32768)
            || !Number.isFinite(data.expiresIn) || data.expiresIn < 60 || data.expiresIn > 43200
            || typeof data.refreshToken !== 'string' || !data.refreshToken || data.refreshToken.length > 65536) {
            throw new Error('AWS에서 유효한 임시 자격증명을 받지 못했습니다.');
        }
        return {credentials: {accessKeyId: access.accessKeyId, secretAccessKey: access.secretAccessKey, sessionToken: access.sessionToken,
            expiration: new Date(Date.now() + data.expiresIn * 1000).toISOString()}, refreshToken: data.refreshToken};
    }
}
