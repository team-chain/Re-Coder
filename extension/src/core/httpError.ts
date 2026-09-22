/**
 * Core 의 오류 응답 본문 → 사용자에게 보여 줄 한 문장.
 *
 * ApiClient 와 분리한 이유: ApiClient 는 CoreManager 를 거쳐 `vscode` 모듈을
 * 끌어오므로 VS Code 밖(node --test)에서 require 할 수 없다. 이 변환은 순수
 * 함수라 따로 두면 그대로 검사할 수 있다.
 *
 * 왜 필요한가 — 데모에서 무슨 일이 있었나
 *   확장은 오류 응답 본문을 **그대로** 배너에 띄웠다. 그래서
 *     · FastAPI 오류 → `{"detail":"..."}` JSON 원문이 노출되고,
 *     · 처리되지 않은 예외 → Starlette 의 평문 `Internal Server Error` 만 떠서
 *       사용자가 원인도 다음 행동도 알 수 없었다.
 *   Core 에도 전역 예외 핸들러를 붙였지만, 구버전 Core 가 여전히 평문을
 *   돌려줄 수 있으므로 클라이언트에서도 한 번 더 사람이 읽을 문장으로 만든다.
 */
export function describeHttpError(status: number, body: string): string {
    const raw = (body ?? '').trim();

    // 1) FastAPI 표준 오류 모양 — detail 만 꺼낸다.
    if (raw.startsWith('{') || raw.startsWith('[')) {
        try {
            const parsed = JSON.parse(raw) as { detail?: unknown; message?: unknown };
            const detail = parsed?.detail ?? parsed?.message;
            if (typeof detail === 'string' && detail.trim()) { return detail.trim(); }
            // 422 검증 오류는 detail 이 배열이다. 사람이 읽게 펴 준다.
            if (Array.isArray(detail) && detail.length) {
                const parts = detail
                    .map((d) => {
                        const item = d as { loc?: unknown[]; msg?: string };
                        const where = Array.isArray(item?.loc) ? item.loc.join('.') : '';
                        return where ? `${where}: ${item?.msg ?? ''}` : (item?.msg ?? '');
                    })
                    .filter(Boolean);
                if (parts.length) { return `요청 형식이 올바르지 않습니다 — ${parts.join(', ')}`; }
            }
        } catch { /* JSON 이 아니면 아래 평문 처리로 */ }
    }

    // 2) 평문 `Internal Server Error` — 그대로 보여줘 봐야 아무 도움이 안 된다.
    if (!raw || /^internal server error$/i.test(raw)) {
        return (
            `코어에서 처리되지 않은 오류가 발생했습니다 (HTTP ${status}). `
            + '코어 로그를 확인하거나, AI 연결 상태를 점검한 뒤 다시 시도해 주세요.'
        );
    }

    // 3) 그 밖의 평문 본문은 상태 코드와 함께 그대로 전달.
    return `${raw} (HTTP ${status})`;
}
