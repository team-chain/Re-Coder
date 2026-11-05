import { createServer, Server } from 'http';
import { randomBytes, timingSafeEqual } from 'crypto';
import { CloudCredentials } from './awsTransport';
import { withTimeout } from '../core/timeouts';

/** Botocore's refreshable container provider; never writes credentials to files. */
export class CredentialBridge {
    private server?: Server;
    private starting?: Promise<Record<string, string>>;
    private environment?: Record<string, string>;
    private disposed = false;
    constructor(private getCredentials: () => Promise<CloudCredentials>) {}

    async start(): Promise<Record<string, string>> {
        if (this.disposed) throw new Error('AWS 연결이 종료되었습니다.');
        if (this.environment) return this.environment;
        if (this.starting) return this.starting;
        this.starting = this.listen();
        try { return await this.starting; } finally { this.starting = undefined; }
    }

    private async listen(): Promise<Record<string, string>> {
        const token = randomBytes(48).toString('base64url');
        const expected = Buffer.from(token);
        const server = createServer(async (req, res) => {
            res.setHeader('Content-Type', 'application/json');
            res.setHeader('Cache-Control', 'no-store');
            res.setHeader('Connection', 'close');
            const received = Buffer.from(req.headers.authorization || '');
            const address = server.address();
            const host = address && typeof address !== 'string' ? `127.0.0.1:${address.port}` : '';
            if (this.disposed || req.method !== 'GET' || req.url !== '/credentials' || req.headers.host !== host
                || req.headers.origin || received.length !== expected.length || !timingSafeEqual(received, expected)) {
                res.writeHead(403).end(JSON.stringify({Code: 'AccessDenied'})); return;
            }
            try {
                const credentials = await withTimeout(this.getCredentials(), 25_000, 'AWS 로그인 갱신 시간 초과');
                if (this.disposed || Date.parse(credentials.expiration) < Date.now() + 15_000) throw new Error();
                res.end(JSON.stringify({AccessKeyId: credentials.accessKeyId, SecretAccessKey: credentials.secretAccessKey,
                    Token: credentials.sessionToken, Expiration: credentials.expiration}));
            } catch {
                // Botocore logs the response on failure; never include provider errors or secrets.
                res.writeHead(503).end(JSON.stringify({Code: 'CredentialsUnavailable', Message: 'AWS browser login required'}));
            }
        });
        this.server = server;
        server.headersTimeout = 10_000;
        server.requestTimeout = 30_000;
        await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', () => { server.removeListener('error', reject); resolve(); }); });
        server.on('error', () => this.dispose());
        if (this.disposed) { server.close(); throw new Error('AWS 연결이 종료되었습니다.'); }
        const address = server.address();
        if (!address || typeof address === 'string') throw new Error('AWS 연결을 준비하지 못했습니다.');
        server.unref();
        this.environment = {AWS_CONTAINER_CREDENTIALS_FULL_URI: `http://127.0.0.1:${address.port}/credentials`,
            AWS_CONTAINER_AUTHORIZATION_TOKEN: token, AWS_EC2_METADATA_DISABLED: 'true'};
        return this.environment;
    }

    dispose(): void {
        this.disposed = true;
        this.environment = undefined;
        this.server?.close();
        this.server?.closeAllConnections?.();
        this.server = undefined;
    }
}

/** A selected account must never silently fall back to ambient keys or profiles. */
export function removeAmbientAwsCredentials(env: NodeJS.ProcessEnv): void {
    for (const key of Object.keys(env)) {
        if (key.startsWith('AWS_') && (/(?:KEY|TOKEN|PROFILE|CREDENTIAL|ROLE_ARN|ENDPOINT_URL|CONFIG_FILE)/.test(key))) delete env[key];
    }
    delete env.RECODER_ASSUME_ROLE_ARN;
}
