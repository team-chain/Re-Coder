/**
 * 3.0.3 — 네트워크 수준 오류("fetch failed", ECONNREFUSED …)를 사람이 읽는 문장으로.
 * 그 밖의 메시지는 앞의 "Error: " 만 떼고 그대로 둔다(원인 정보를 숨기지 않는다).
 */
export const CORE_UNREACHABLE = "ReCoder Core 에 연결하지 못했습니다. Core 가 켜져 있는지 확인하고 다시 시도하세요.";

export function friendlyError(message: unknown): string {
  const text = String(message ?? "").replace(/^(Type)?Error:\s*/i, "").trim();
  if (/^(fetch failed|failed to fetch)$|ECONNREFUSED|ECONNRESET|socket hang up|NetworkError/i.test(text)) return CORE_UNREACHABLE;
  return text;
}
