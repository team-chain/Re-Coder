export interface DeploymentProgressEvent {
    step: string;
    message?: string;
    plan_id?: string;
    result?: unknown;
}

/** Parses split UTF-8/CRLF frames. A closed connection is never completion. */
export async function readDeploymentStream<T>(response: Response, onEvent: (event: DeploymentProgressEvent) => void): Promise<T> {
    if (!response.body) throw new Error('배포 진행 응답이 없습니다.');
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = '';
    try {
        for (;;) {
            const { done, value } = await reader.read();
            buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
            let separator: RegExpExecArray | null;
            while ((separator = /\r?\n\r?\n/.exec(buffer))) {
                const frame = buffer.slice(0, separator.index);
                buffer = buffer.slice(separator.index + separator[0].length);
                const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
                if (!data) continue;
                const event = JSON.parse(data) as DeploymentProgressEvent;
                onEvent(event);
                if (event.step === 'error') throw new Error(event.message || '배포에 실패했습니다.');
                if (event.step === 'done' && event.result) return event.result as T;
            }
            if (done) break;
        }
        throw new Error('배포 진행 연결이 끊겼습니다. 배포는 계속될 수 있으므로 실행 상태를 확인하세요.');
    } finally {
        await reader.cancel().catch(() => undefined);
        reader.releaseLock();
    }
}
