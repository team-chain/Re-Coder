/**
 * Core 에 닿지 못한 오류("fetch failed", ECONNREFUSED …)를 화면에 그대로 보내지 않는다.
 * 화면마다 따로 고치던 것을 확장 → 웹뷰 경계 한 곳에서 바꾼다. 다른 오류 문장은 건드리지 않는다(원인을 숨기지 않는다).
 */
export const CORE_UNREACHABLE = 'ReCoder Core 에 연결하지 못했습니다. Core 가 켜져 있는지 확인하고 다시 시도하세요.';
const NETWORK = /^(?:(?:Type)?Error:\s*)?(?:fetch failed|failed to fetch|socket hang up|(?:(?:connect|read|write)\s+)?(?:ECONNREFUSED|ECONNRESET)\b.*)$/i;

export function humanizeNetworkError(text: string): string {
    return NETWORK.test(text.trim()) ? CORE_UNREACHABLE : text;
}

/** payload 의 error · message · cause 문자열만 바꾼다. 바꿀 게 없으면 같은 객체를 돌려준다. */
export function humanizeNetworkErrors<T>(payload: T): T {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return payload;
    const record = payload as Record<string, unknown>;
    let out: Record<string, unknown> | undefined;
    for (const key of ['error', 'message', 'cause']) {
        const value = record[key];
        if (typeof value !== 'string') continue;
        const next = humanizeNetworkError(value);
        if (next !== value) (out ??= { ...record })[key] = next;
    }
    return (out ?? payload) as T;
}
