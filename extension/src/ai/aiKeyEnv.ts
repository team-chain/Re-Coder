/**
 * Core 환경변수로 넘길 AI API 키 선택. `vscode` 모듈에 의존하지 않는다
 * (CoreManager 가 가져다 쓰고, CoreManager 테스트는 vscode 없이 돈다).
 */
export type AiKeyProvider = 'anthropic' | 'openai';
export const AI_PROVIDER_STATE = 'recoder.ai.provider';
export const AI_KEY_SECRET: Record<AiKeyProvider, string> = { anthropic: 'recoder.ai.anthropicKey', openai: 'recoder.ai.openaiKey' };

export interface AiKeyContext {
    secrets: { get(key: string): Thenable<string | undefined>; store(key: string, value: string): Thenable<void>; delete(key: string): Thenable<void> };
    globalState: { get<T>(key: string, defaultValue: T): T; update(key: string, value: unknown): Thenable<void> };
}

export function isAiKeyProvider(value: unknown): value is AiKeyProvider {
    return value === 'anthropic' || value === 'openai';
}

export async function currentAiProvider(context: AiKeyContext): Promise<AiKeyProvider | ''> {
    const chosen = context.globalState.get<string>(AI_PROVIDER_STATE, '');
    if (!isAiKeyProvider(chosen)) { return ''; }
    return (await context.secrets.get(AI_KEY_SECRET[chosen])) ? chosen : '';
}

/** Core 프로세스에 넘길 환경변수. 선택·키가 없으면 빈 객체(기존 동작). */
export async function aiKeyEnv(context: AiKeyContext, modelFor: (provider: AiKeyProvider) => string = () => ''): Promise<Record<string, string>> {
    try {
        const provider = await currentAiProvider(context);
        if (!provider) { return {}; }
        const key = (await context.secrets.get(AI_KEY_SECRET[provider])) || '';
        const upper = provider.toUpperCase();
        const env: Record<string, string> = { RECODER_AI_PROVIDER: provider, [`RECODER_${upper}_API_KEY`]: key };
        let model = '';
        try { model = (modelFor(provider) || '').trim(); } catch { model = ''; }
        if (model) { env[`RECODER_${upper}_MODEL`] = model; env[`RECODER_${upper}_FAST_MODEL`] = model; }
        return env;
    } catch {
        return {};
    }
}
