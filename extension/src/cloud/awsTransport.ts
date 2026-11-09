import { createHash, createHmac } from 'crypto';

export interface CloudCredentials {
    accessKeyId: string;
    secretAccessKey: string;
    sessionToken: string;
    expiration: string;
}

// Endpoint hosts are constructed here, never taken from workspace settings.
export function awsRegion(value: string): string {
    if (!/^(?:af|ap|ca|eu|il|me|mx|sa|us)-(?!gov|iso)[a-z]+-\d$/.test(value)) {
        throw new Error('지원하는 AWS 상용 리전을 선택하세요. 예: ap-northeast-2');
    }
    return value;
}

export async function boundedResponse(response: Response, limit = 256 * 1024): Promise<string> {
    if (!response.body) return '';
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let length = 0;
    try {
        while (true) {
            const next = await reader.read();
            if (next.done) break;
            length += next.value.length;
            if (length > limit) throw new Error('AWS 응답이 허용 크기를 초과했습니다.');
            chunks.push(next.value);
        }
        return Buffer.concat(chunks).toString('utf8');
    } finally { await reader.cancel().catch(() => {}); }
}

const hash = (value: string) => createHash('sha256').update(value).digest('hex');
const hmac = (key: string | Buffer, value: string) => createHmac('sha256', key).update(value).digest();

/** SigV4 for the fixed AWS JSON/Query endpoints used by onboarding. */
export function signAwsRequest(service: string, region: string, body: string, credentials: CloudCredentials,
    contentType: string, target?: string, now = new Date()): {url: string; headers: Record<string, string>} {
    awsRegion(region);
    if (!['sts', 'cloudformation', 'codebuild'].includes(service)) throw new Error('지원하지 않는 AWS 요청입니다.');
    const host = `${service}.${region}.amazonaws.com`;
    const timestamp = now.toISOString().replace(/[:-]|\.\d{3}/g, '');
    const day = timestamp.slice(0, 8);
    const headers: Record<string, string> = {'content-type': contentType, host, 'x-amz-date': timestamp,
        'x-amz-security-token': credentials.sessionToken};
    if (target) headers['x-amz-target'] = target;
    const keys = Object.keys(headers).sort();
    const canonical = `POST\n/\n\n${keys.map(k => `${k}:${headers[k].trim()}\n`).join('')}\n${keys.join(';')}\n${hash(body)}`;
    const scope = `${day}/${region}/${service}/aws4_request`;
    const signingKey = hmac(hmac(hmac(hmac('AWS4' + credentials.secretAccessKey, day), region), service), 'aws4_request');
    const signature = createHmac('sha256', signingKey).update(`AWS4-HMAC-SHA256\n${timestamp}\n${scope}\n${hash(canonical)}`).digest('hex');
    headers.Authorization = `AWS4-HMAC-SHA256 Credential=${credentials.accessKeyId}/${scope}, SignedHeaders=${keys.join(';')}, Signature=${signature}`;
    return {url: `https://${host}/`, headers};
}

export async function callerIdentity(credentials: CloudCredentials, region: string, fetcher: typeof fetch = fetch): Promise<{account: string; arn: string}> {
    const body = new URLSearchParams({Action: 'GetCallerIdentity', Version: '2011-06-15'}).toString();
    const signed = signAwsRequest('sts', region, body, credentials, 'application/x-www-form-urlencoded');
    const response = await fetcher(signed.url, {method: 'POST', headers: signed.headers, body,
        redirect: 'error', signal: AbortSignal.timeout(20_000)});
    const xml = await boundedResponse(response);
    const account = /<Account>(\d{12})<\/Account>/.exec(xml)?.[1];
    const arn = /<Arn>(arn:aws:(?:sts|iam)::\d{12}:[A-Za-z0-9_+=,.@/:-]+)<\/Arn>/.exec(xml)?.[1];
    if (!response.ok || !account || !arn || arn.split(':')[4] !== account) throw new Error('AWS 로그인 자격증명을 확인하지 못했습니다. 다시 로그인하세요.');
    return {account, arn};
}
