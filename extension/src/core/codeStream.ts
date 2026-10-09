/**
 * 대규모 코드 생성 진행 스트림(/api/code/generate/stream).
 *
 * 생성은 수십 분 걸릴 수 있다 — HTTP 응답 하나를 기다리지 않고 진행 이벤트를 받는다.
 * 전체 시간 상한은 없고, 아무 데이터(심장박동 포함)도 오지 않는 시간만 제한한다.
 * 중간에 멈추면 GenerationPausedError 로 작업 ID 를 알려 [이어서 만들기]가 가능하다.
 */

export interface CodePlanFile { file: string; layer?: number; purpose?: string; }

export interface CodeProgressEvent {
    step: string;
    message?: string;
    job_id?: string;
    agent?: string;
    file?: string;
    part?: number;
    lines?: number;
    layer?: number;
    agents?: number;
    seconds?: number;
    done_count?: number;
    total?: number;
    elapsed?: number;
    summary?: string;
    files?: CodePlanFile[];
    result?: unknown;
    resumable?: boolean;
    resume_job?: string;
    reason?: string;
}

/** 생성이 멈췄지만 만든 것은 코어에 저장됐다 — 같은 요청을 jobId 와 함께 다시 보내면 이어 만든다. */
export class GenerationPausedError extends Error {
    constructor(message: string, readonly jobId: string, readonly done = 0, readonly total = 0) {
        super(message);
        this.name = 'GenerationPausedError';
    }
}

/** 아무 데이터도 없이 이 시간이 지나면 끊긴 것으로 본다(코어는 10초마다 심장박동을 보낸다). */
export const CODE_STREAM_IDLE_MS = 120_000;

export async function readCodeStream<T>(
    response: Response,
    onEvent: (event: CodeProgressEvent) => void,
    abort: () => void,
    idleMs = CODE_STREAM_IDLE_MS,
): Promise<T> {
    if (!response.body) throw new Error('코드 생성 진행 응답이 없습니다.');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let jobId = '';
    let idle: ReturnType<typeof setTimeout> | undefined;
    let timedOut = false;
    const arm = () => {
        if (idle) clearTimeout(idle);
        idle = setTimeout(() => { timedOut = true; abort(); }, idleMs);
    };
    arm();
    try {
        for (;;) {
            let chunk: ReadableStreamReadResult<Uint8Array>;
            try {
                chunk = await reader.read();
            } catch (error) {
                if (timedOut || jobId) {
                    throw new GenerationPausedError(
                        'Core 와의 연결이 끊겼습니다. 코어는 계속 만들고 있을 수 있어요 — [이어서 만들기]로 결과를 받거나 멈춘 지점부터 계속하세요.',
                        jobId,
                    );
                }
                throw error;
            }
            const { done, value } = chunk;
            arm();
            buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
            let separator: RegExpExecArray | null;
            while ((separator = /\r?\n\r?\n/.exec(buffer))) {
                const frame = buffer.slice(0, separator.index);
                buffer = buffer.slice(separator.index + separator[0].length);
                const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
                if (!data) continue;
                let event: CodeProgressEvent;
                try { event = JSON.parse(data) as CodeProgressEvent; } catch { continue; }
                if (event.job_id) jobId = event.job_id;
                if (event.step === 'error') {
                    if (event.resumable && (event.resume_job || jobId)) {
                        throw new GenerationPausedError(event.message || '생성이 중간에 멈췄습니다.', event.resume_job || jobId,
                            Number(event.done_count) || 0, Number(event.total) || 0);
                    }
                    throw new Error(event.message || '코드 생성에 실패했습니다.');
                }
                onEvent(event);
                if (event.step === 'done' && event.result) return event.result as T;
            }
            if (done) break;
        }
        if (jobId) {
            throw new GenerationPausedError(
                'Core 와의 연결이 끊겼습니다. [이어서 만들기]로 결과를 받거나 멈춘 지점부터 계속하세요.', jobId);
        }
        throw new Error('코드 생성 진행 연결이 끊겼습니다.');
    } finally {
        if (idle) clearTimeout(idle);
        await reader.cancel().catch(() => undefined);
        try { reader.releaseLock(); } catch { /* already released */ }
    }
}

/** 웹뷰로 보낼 때는 결과 본문(파일 내용 전체)을 빼고 화면에 필요한 것만 보낸다. */
export function compactCodeEvent(event: CodeProgressEvent): CodeProgressEvent {
    const { result: _result, ...rest } = event;
    if (rest.files) rest.files = rest.files.map(f => ({ file: f.file, layer: f.layer, purpose: f.purpose }));
    return rest;
}
