/**
 * Core 환경변수로 넘길 AI API 키 선택. `vscode` 모듈에 의존하지 않는다
 * (CoreManager 가 가져다 쓰고, CoreManager 테스트는 vscode 없이 돈다).
 *
 * 2.0.36 작업별 AI 공급자
 * - 'compatible' — OpenAI 호환 API(DeepSeek 등). 키 + 주소(https, 내 PC 의 localhost 만 http).
 * - 절약 등급 공급자(설정 recoder.ai.economyProvider). 기본 공급자가 아닌 키는 **절약 공급자로 고른 경우에만**
 *   Core 에 넘긴다 — 쓰지 않는 키를 프로세스 환경에 올리지 않는다.
 * - 모델 단가(recoder.ai.prices, 달러/백만 토큰)는 비용 원장 계산용으로 넘긴다.
 */
export type AiKeyProvider = 'anthropic' | 'openai' | 'compatible';
export const AI_PROVIDERS: AiKeyProvider[] = ['anthropic', 'openai', 'compatible'];
export const AI_PROVIDER_STATE = 'recoder.ai.provider';
export const AI_KEY_SECRET: Record<AiKeyProvider, string> = {
    anthropic: 'recoder.ai.anthropicKey', openai: 'recoder.ai.openaiKey', compatible: 'recoder.ai.compatibleKey',
};
export const ECONOMY_PROVIDERS = ['', 'fast', 'anthropic', 'openai', 'compatible'] as const;
export type EconomyProvider = typeof ECONOMY_PROVIDERS[number];

export interface AiKeyContext {
    secrets: { get(key: string): Thenable<string | undefined>; store(key: string, value: string): Thenable<void>; delete(key: string): Thenable<void> };
    globalState: { get<T>(key: string, defaultValue: T): T; update(key: string, value: unknown): Thenable<void> };
}

export interface AiRoutingSettings {
    /** 제공자별 모델 ID(recoder.ai.<제공자>Model). */
    modelFor?: (provider: AiKeyProvider) => string;
    economyProvider?: string;
    economyModel?: string;
    compatibleBaseUrl?: string;
    prices?: unknown;
}

export function isAiKeyProvider(value: unknown): value is AiKeyProvider {
    return value === 'anthropic' || value === 'openai' || value === 'compatible';
}

export function isEconomyProvider(value: unknown): value is EconomyProvider {
    return typeof value === 'string' && (ECONOMY_PROVIDERS as readonly string[]).includes(value);
}

/** OpenAI 호환 주소 확인 — Core(api_key_provider.compatible_endpoint)와 같은 규칙. 문제가 없으면 undefined. */
export function validateCompatibleUrl(raw: string): string | undefined {
    const value = (raw || '').trim();
    if (!value) { return 'API 주소를 입력하세요. 예: https://api.deepseek.com'; }
    let url: URL;
    try { url = new URL(value); } catch { return '주소 형식이 올바르지 않습니다. https:// 로 시작하는 전체 주소를 입력하세요.'; }
    const host = url.hostname.replace(/^\[|\]$/g, '').toLowerCase();
    const local = ['localhost', '127.0.0.1', '::1'].includes(host);
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && local)) {
        return 'https 주소만 쓸 수 있습니다(http 는 내 PC 의 localhost 만 허용).';
    }
    if (url.username || url.password || url.search || url.hash) { return '주소에 사용자 정보·쿼리를 넣지 마세요. 키는 따로 입력합니다.'; }
    return undefined;
}

/** 단가 설정 → Core 가 읽는 JSON. 숫자가 아니거나 음수인 항목은 버린다. */
export function pricesEnv(raw: unknown): string {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) { return ''; }
    const out: Record<string, { input: number; output: number }> = {};
    for (const [model, rate] of Object.entries(raw as Record<string, unknown>)) {
        if (!model.trim() || !rate || typeof rate !== 'object') { continue; }
        const r = rate as Record<string, unknown>;
        if (typeof r.input !== 'number' && typeof r.input !== 'string') { continue; }
        if (typeof r.output !== 'number' && typeof r.output !== 'string') { continue; }
        const input = Number(r.input), output = Number(r.output);
        if (String(r.input).trim() === '' || String(r.output).trim() === '') { continue; }
        if (Number.isFinite(input) && Number.isFinite(output) && input >= 0 && output >= 0 && input < 10000 && output < 10000) {
            out[model.trim()] = { input, output };
        }
    }
    return Object.keys(out).length ? JSON.stringify(out) : '';
}

export async function currentAiProvider(context: AiKeyContext): Promise<AiKeyProvider | ''> {
    const chosen = context.globalState.get<string>(AI_PROVIDER_STATE, '');
    if (!isAiKeyProvider(chosen)) { return ''; }
    return (await context.secrets.get(AI_KEY_SECRET[chosen])) ? chosen : '';
}

/** 저장된 키가 있는 제공자들(값은 돌려주지 않는다). */
export async function storedAiKeys(context: AiKeyContext): Promise<Record<AiKeyProvider, boolean>> {
    const out: Record<AiKeyProvider, boolean> = { anthropic: false, openai: false, compatible: false };
    await Promise.all(AI_PROVIDERS.map(async (p) => {
        try { out[p] = Boolean(await context.secrets.get(AI_KEY_SECRET[p])); } catch { out[p] = false; }
    }));
    return out;
}

function settingsOf(arg: AiRoutingSettings | ((provider: AiKeyProvider) => string) | undefined): AiRoutingSettings {
    return typeof arg === 'function' ? { modelFor: arg } : (arg ?? {});
}

/** Core 프로세스에 넘길 환경변수. 선택·키가 없으면 빈 객체(기존 동작). */
export async function aiKeyEnv(context: AiKeyContext, settingsArg?: AiRoutingSettings | ((provider: AiKeyProvider) => string)): Promise<Record<string, string>> {
    const settings = settingsOf(settingsArg);
    const env: Record<string, string> = {};
    const modelOf = (p: AiKeyProvider): string => { try { return (settings.modelFor?.(p) || '').trim(); } catch { return ''; } };
    const baseUrl = (settings.compatibleBaseUrl || '').trim();
    const addKey = async (p: AiKeyProvider): Promise<boolean> => {
        const key = ((await context.secrets.get(AI_KEY_SECRET[p])) || '').trim();
        if (!key) { return false; }
        if (p === 'compatible') {
            if (validateCompatibleUrl(baseUrl)) { return false; }  // 주소가 없거나 안전하지 않으면 키도 넘기지 않는다
            env.RECODER_COMPATIBLE_BASE_URL = baseUrl;
        }
        const upper = p.toUpperCase();
        env[`RECODER_${upper}_API_KEY`] = key;
        const model = modelOf(p);
        if (model) { env[`RECODER_${upper}_MODEL`] = model; env[`RECODER_${upper}_FAST_MODEL`] = model; }
        return true;
    };
    try {
        const provider = await currentAiProvider(context);
        if (provider && await addKey(provider)) { env.RECODER_AI_PROVIDER = provider; }
        const economy = (settings.economyProvider || '').trim();
        if (economy && isEconomyProvider(economy)) {
            const usable = economy === 'fast' || economy === env.RECODER_AI_PROVIDER
                || (isAiKeyProvider(economy) && await addKey(economy));
            if (usable) {
                env.RECODER_ECONOMY_PROVIDER = economy;
                const model = (settings.economyModel || '').trim();
                if (model) { env.RECODER_ECONOMY_MODEL = model; }
            }
        }
        const prices = pricesEnv(settings.prices);
        if (prices) { env.RECODER_MODEL_PRICES = prices; }
        return env;
    } catch {
        return {};
    }
}
