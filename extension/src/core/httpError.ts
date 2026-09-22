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
            // 정책 게이트처럼 detail 이 **객체**인 오류 — message(+수정 제안)만
            // 문장으로 만든다. 예전에는 이 모양이 3) 으로 떨어져 JSON 원문이
            // `(HTTP 403)` 과 함께 그대로 배너에 떴다(ECS 정책 거절이 그랬다).
            const structured = structuredDetail(parsed);
            if (structured) {
                const message = typeof structured.message === 'string' ? structured.message.trim() : '';
                const fix = typeof structured.fix_suggestion === 'string' ? structured.fix_suggestion.trim() : '';
                if (message) { return fix ? `${message} — ${fix}` : message; }
            }
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

/** 오류 본문의 `detail` 이 객체일 때 그 객체. 문자열·배열·없음 → null. */
function structuredDetail(parsed: unknown): Record<string, unknown> | null {
    const detail = (parsed as { detail?: unknown } | null)?.detail;
    if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
        return detail as Record<string, unknown>;
    }
    return null;
}

/** 오류 응답 본문에서 구조화된 detail 객체를 꺼낸다. 없으면 null. */
export function parseHttpErrorDetail(body: string): Record<string, unknown> | null {
    const raw = (body ?? '').trim();
    if (!raw.startsWith('{')) { return null; }
    try { return structuredDetail(JSON.parse(raw)); } catch { return null; }
}

/**
 * Core 오류를 상태 코드·구조화된 detail 과 함께 던지기 위한 예외.
 * `String(err)` 는 여전히 사람이 읽을 문장이라, 기존 catch 문은 그대로 동작한다.
 */
export class CoreHttpError extends Error {
    constructor(
        message: string,
        public readonly status: number | undefined,
        public readonly detail: Record<string, unknown> | null,
    ) {
        super(message);
        this.name = 'CoreHttpError';
    }
}

/**
 * 정책 게이트(OPA · 로컬 규칙)가 배포를 막았을 때 화면 카드가 쓰는 모양.
 * `core/api/routes/ecs.py` 의 `_gate_and_start` 가 403/503/500 으로 돌려주는
 * `{error, decision, message, fix_suggestion, deployment_id}` 를 옮긴 것.
 */
export interface PolicyDenial {
    /** policy_denied · approval_required · security_escalation_required · opa_unavailable · policy_evaluation_crashed */
    code: string;
    /** OPA 결정값 — deny · deny_with_fix_suggestion · allow_with_approval · escalate_to_security … */
    decision: string;
    message: string;
    fix: string;
    deployment_id: string | null;
}

const POLICY_ERROR_CODES = new Set([
    'policy_denied',
    'approval_required',
    'security_escalation_required',
    'opa_unavailable',
    'policy_evaluation_crashed',
]);

/**
 * 구조화된 detail 이 정책 게이트 결과면 카드용 모양으로, 아니면 null.
 *
 * 왜 따로 두나 — 예전에는 ECS 정책 거절이 `errorMessage` 배너에 JSON 원문으로
 * 떨어져서, 사용자는 "왜 막혔는지·무엇을 고치면 되는지"를 읽을 수 없었다.
 * 실기기에서는 폼에 환경 입력이 없어 이 경로를 밟아 볼 수도 없었다.
 */
export function policyDenialFromDetail(detail: Record<string, unknown> | null | undefined): PolicyDenial | null {
    if (!detail) { return null; }
    const code = typeof detail.error === 'string' ? detail.error : '';
    if (!POLICY_ERROR_CODES.has(code)) { return null; }
    const message = typeof detail.message === 'string' && detail.message.trim()
        ? detail.message.trim()
        : '정책 게이트가 배포를 막았습니다.';
    const fix = typeof detail.fix_suggestion === 'string' && detail.fix_suggestion.trim()
        ? detail.fix_suggestion.trim()
        : defaultPolicyFix(code);
    return {
        code,
        decision: typeof detail.decision === 'string' ? detail.decision : '',
        message,
        fix,
        deployment_id: typeof detail.deployment_id === 'string' ? detail.deployment_id : null,
    };
}

function defaultPolicyFix(code: string): string {
    switch (code) {
        case 'approval_required': return '승인자의 확인을 받은 뒤 다시 배포하세요.';
        case 'security_escalation_required': return '보안 담당자 확인이 필요합니다. 스캔 결과를 먼저 확인하세요.';
        case 'opa_unavailable': return 'OPA 정책 서버에 연결할 수 없습니다. 연결 상태를 확인하거나 로컬 규칙으로 다시 시도하세요.';
        case 'policy_evaluation_crashed': return '정책 평가 중 오류가 났습니다. 코어 로그를 확인하세요.';
        default: return '거절 사유를 해결한 뒤 다시 배포하세요.';
    }
}
