/**
 * 기다림에 상한을 둔다.
 *
 * 왜 필요한가 (2026-09-30 실기기)
 *   작업 폴더를 바꾸면 VS Code 가 확장 호스트를 다시 띄운다. 새 호스트가 Core 를 띄우는 길의 어느
 *   한 곳(보안 저장소 읽기 등)이 끝나지 않으면, 연결 시도는 하나의 Promise 로 공유되므로 그 뒤의
 *   **모든** 요청이 같은 자리에서 영원히 기다렸다. core.log 에는 아무것도 남지 않고 화면에는
 *   "개발 요청 응답이 없습니다"만 떴다. 기다림마다 상한을 두면 멈춘 곳 대신 원인이 보인다.
 */
export class TimeoutError extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'TimeoutError';
    }
}

/** ms 안에 끝나지 않으면 TimeoutError. 원래 작업은 취소되지 않지만 기다리는 쪽은 풀려난다. */
export function withTimeout<T>(work: PromiseLike<T>, ms: number, message: string): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const limit = new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new TimeoutError(message)), ms);
    });
    return Promise.race([Promise.resolve(work), limit]).finally(() => { if (timer) { clearTimeout(timer); } });
}

/** ms 안에 끝나지 않거나 실패하면 fallback. 없어도 되는 값(보안 저장소의 선택 항목 등)에 쓴다. */
export async function withFallback<T>(work: PromiseLike<T>, ms: number, fallback: T, onFallback?: (reason: string) => void): Promise<T> {
    try {
        return await withTimeout(work, ms, `${ms}ms 안에 끝나지 않음`);
    } catch (error) {
        onFallback?.(error instanceof Error ? error.message : String(error));
        return fallback;
    }
}
