import * as http from 'http';

/**
 * 오래 걸리는 Core 호출(배포 계획·보안 검사·S3 배포 등)용 HTTP 요청.
 *
 * Node/VS Code 의 전역 fetch(undici)는 응답 헤더를 **300초** 안에 못 받으면
 * 우리 타임아웃과 상관없이 "fetch failed" 로 끊는다. 첫 Trivy DB 내려받기나
 * 큰 이미지 빌드가 5분을 넘기면 Core 는 계속 일하는데 화면만 실패로 보였다.
 * 로컬 Core(127.0.0.1) 전용이라 node:http 로 직접 보내고, 시간 제한은 호출자가 준
 * AbortSignal 로만 건다.
 */
export const FETCH_HEADERS_LIMIT_MS = 280_000;

export function longHttpFetch(
    url: string,
    init: { method: string; headers: Record<string, string>; body?: string; signal: AbortSignal },
): Promise<Response> {
    return new Promise<Response>((resolve, reject) => {
        const target = new URL(url);
        const req = http.request({
            host: target.hostname,
            port: target.port,
            path: `${target.pathname}${target.search}`,
            method: init.method,
            headers: init.body !== undefined
                ? { ...init.headers, 'Content-Length': Buffer.byteLength(init.body).toString() }
                : init.headers,
            agent: false,
        }, res => {
            const chunks: Buffer[] = [];
            res.on('data', (chunk: Buffer) => chunks.push(chunk));
            res.on('end', () => {
                const status = res.statusCode ?? 502;
                const text = Buffer.concat(chunks).toString('utf8');
                // Response 는 1xx/204/304 에 본문을 받지 않는다.
                resolve(new Response([204, 304].includes(status) ? null : text, { status }));
            });
            res.on('error', reject);
        });
        // 서버가 응답을 만드는 동안 소켓이 조용해도 끊지 않는다 — 제한은 signal 이 건다.
        req.setTimeout(0);
        const onAbort = () => req.destroy(Object.assign(new Error('aborted'), { name: 'AbortError' }));
        if (init.signal.aborted) { onAbort(); } else { init.signal.addEventListener('abort', onAbort, { once: true }); }
        req.on('error', reject);
        req.on('close', () => init.signal.removeEventListener('abort', onAbort));
        if (init.body !== undefined) { req.write(init.body); }
        req.end();
    });
}
