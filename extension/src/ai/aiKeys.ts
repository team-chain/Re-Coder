import * as vscode from 'vscode';

/**
 * AWS 없이 AI 를 쓰는 경로 — Claude(Anthropic) 또는 ChatGPT(OpenAI) API 키.
 *
 * - 키는 VS Code 보안 저장소(SecretStorage)에만 둔다. 설정 파일·로그에 쓰지 않는다.
 * - 어떤 제공자를 쓸지는 사용자가 고른다(globalState). 고르지 않으면 Core 는 기존처럼
 *   게이트웨이 또는 본인 AWS(Bedrock)를 쓴다.
 * - Core 는 RECODER_AI_PROVIDER + RECODER_<제공자>_API_KEY 가 모두 있을 때만 이 경로를 쓴다.
 */
import {
    AI_KEY_SECRET, AI_PROVIDERS, AI_PROVIDER_STATE, AiKeyContext, AiKeyProvider, AiRoutingSettings, EconomyProvider,
    aiKeyEnv as baseAiKeyEnv, currentAiProvider, isAiKeyProvider, isEconomyProvider, storedAiKeys, validateCompatibleUrl,
} from './aiKeyEnv';
export { AI_PROVIDER_STATE, AiKeyProvider, currentAiProvider, isAiKeyProvider, isEconomyProvider, storedAiKeys };

export const AI_PROVIDER_LABEL: Record<AiKeyProvider, string> = {
    anthropic: 'Claude (Anthropic API)', openai: 'ChatGPT (OpenAI API)', compatible: 'OpenAI 호환 API (DeepSeek 등)',
};
const KEY_PAGE: Record<AiKeyProvider, string> = {
    anthropic: 'https://platform.claude.com/',
    openai: 'https://platform.openai.com/api-keys',
    compatible: 'https://platform.deepseek.com/api_keys',
};
const VENDOR: Record<AiKeyProvider, string> = { anthropic: 'Anthropic', openai: 'OpenAI', compatible: '입력한 API 주소' };
export const ECONOMY_LABEL: Record<EconomyProvider, string> = {
    '': '품질과 같은 공급자', fast: '품질 공급자의 빠른 모델', anthropic: 'Claude API', openai: 'OpenAI API', compatible: 'OpenAI 호환 API (DeepSeek 등)',
};
const SECRET = AI_KEY_SECRET;
type Ctx = AiKeyContext;

/** 키 형식 확인. 실제 유효성은 Core 가 1회 호출로 확인한다. */
export function validateApiKey(provider: AiKeyProvider, raw: string): string | undefined {
    const key = raw.trim();
    if (!key) { return 'API 키를 붙여넣으세요.'; }
    if (/\s/.test(key)) { return '키에 공백이나 줄바꿈이 있습니다. 키만 다시 복사하세요.'; }
    if (key.length < 20 || key.length > 400) { return '키 길이가 올바르지 않습니다. 전체 키를 복사했는지 확인하세요.'; }
    if (provider === 'anthropic' && !key.startsWith('sk-ant-')) { return 'Claude API 키는 sk-ant- 로 시작합니다.'; }
    if (provider === 'openai' && !key.startsWith('sk-')) { return 'OpenAI API 키는 sk- 로 시작합니다.'; }
    if (provider === 'compatible' && key.length < 16) { return '키 길이가 올바르지 않습니다. 전체 키를 복사했는지 확인하세요.'; }
    return undefined;
}

/** 설정 recoder.ai.* — 모델·절약 공급자·호환 주소·단가. Core 시작 때 환경변수가 된다. */
export function aiRoutingSettings(): AiRoutingSettings {
    const cfg = vscode.workspace.getConfiguration('recoder.ai');
    return {
        modelFor: (p) => cfg.get<string>(`${p}Model`, '') || '',
        economyProvider: cfg.get<string>('economyProvider', '') || '',
        economyModel: cfg.get<string>('economyModel', '') || '',
        compatibleBaseUrl: cfg.get<string>('compatibleBaseUrl', '') || '',
        prices: cfg.get<unknown>('prices', {}),
    };
}

/** 설정을 반영한 Core 환경변수. */
export function aiKeyEnv(context: Ctx): Promise<Record<string, string>> {
    return baseAiKeyEnv(context, aiRoutingSettings());
}

/** makeDefault=false 면 키만 저장한다(절약 공급자로 쓸 키 — 기본 연결은 그대로). */
export async function storeAiKey(context: Ctx, provider: AiKeyProvider, key: string, makeDefault = true): Promise<void> {
    await context.secrets.store(SECRET[provider], key.trim());
    if (makeDefault) { await context.globalState.update(AI_PROVIDER_STATE, provider); }
}

export async function clearAiKeys(context: Ctx): Promise<void> {
    await Promise.all(AI_PROVIDERS.map((p) => context.secrets.delete(SECRET[p])));
    await context.globalState.update(AI_PROVIDER_STATE, undefined);
}

let routingWriteAt = 0;
/** 이 확장이 방금 설정을 바꿨는가 — 설정 변경 감시가 Core 재시작을 한 번 더 묻지 않게. */
export function recentRoutingWrite(now = Date.now()): boolean { return now - routingWriteAt < 5000; }

/** OpenAI 호환 주소를 묻고 설정에 저장한다. 취소하면 false. */
async function askCompatibleUrl(): Promise<boolean> {
    const cfg = vscode.workspace.getConfiguration('recoder.ai');
    const url = await vscode.window.showInputBox({
        title: 'ReCoder: OpenAI 호환 API 주소',
        prompt: 'OpenAI 형식(chat/completions)을 지원하는 API 주소입니다. 예: https://api.deepseek.com',
        value: cfg.get<string>('compatibleBaseUrl', '') || 'https://api.deepseek.com',
        ignoreFocusOut: true,
        validateInput: validateCompatibleUrl,
    });
    if (url === undefined) { return false; }
    routingWriteAt = Date.now();
    await cfg.update('compatibleBaseUrl', url.trim(), vscode.ConfigurationTarget.Global);
    return true;
}

/** 키 입력 상자 — 저장했으면 true. */
async function askKey(context: Ctx, target: AiKeyProvider, makeDefault: boolean): Promise<boolean> {
    if (target === 'compatible' && !await askCompatibleUrl()) { return false; }
    const key = await vscode.window.showInputBox({
        title: `ReCoder: ${AI_PROVIDER_LABEL[target]} 연결`,
        prompt: `API 키를 붙여넣으세요. 키는 VS Code 보안 저장소에만 저장되고, AI 요청은 이 키로 ${VENDOR[target]}에 직접 전송되며 요금은 해당 계정에 청구됩니다.`,
        placeHolder: target === 'anthropic' ? 'sk-ant-…' : 'sk-…',
        password: true,
        ignoreFocusOut: true,
        validateInput: (value) => validateApiKey(target, value),
    });
    if (key === undefined) {
        const open = await vscode.window.showInformationMessage('API 키가 없으면 발급 페이지에서 만들 수 있습니다.', '키 발급 페이지 열기');
        if (open) { await vscode.env.openExternal(vscode.Uri.parse(KEY_PAGE[target])); }
        return false;
    }
    await storeAiKey(context, target, key, makeDefault);
    return true;
}

/**
 * 2.0.36 절약 등급 공급자 고르기(계획 주제·요약·오류 분류·테스트 초안). 바뀌었으면 true — 호출자가 Core 를 재시작한다.
 * 키가 필요한데 없으면 키를 받는다(기본 연결은 바꾸지 않는다).
 */
export async function runEconomyCommand(context: Ctx, provider?: EconomyProvider): Promise<boolean> {
    const cfg = vscode.workspace.getConfiguration('recoder.ai');
    const current = (cfg.get<string>('economyProvider', '') || '') as EconomyProvider;
    let chosen: EconomyProvider | undefined = provider;
    if (chosen === undefined) {
        const keys = await storedAiKeys(context);
        const pick = await vscode.window.showQuickPick((['', 'fast', 'compatible', 'anthropic', 'openai'] as EconomyProvider[]).map((value) => ({
            label: ECONOMY_LABEL[value],
            description: [value === current ? '현재 사용 중' : '', isAiKeyProvider(value) && !keys[value] ? '키 입력 필요' : ''].filter(Boolean).join(' · '),
            value,
        })), { title: 'ReCoder: 절약 등급 AI 공급자', placeHolder: '계획 주제·요약·오류 분류·테스트 초안을 보낼 곳. 코드 생성·보안 수정은 그대로 품질 공급자가 합니다.' });
        if (!pick) { return false; }
        chosen = pick.value;
    }
    if (!isEconomyProvider(chosen)) { return false; }
    let keyAdded = false;
    if (isAiKeyProvider(chosen)) {
        const has = Boolean(await context.secrets.get(SECRET[chosen]));
        const needsUrl = chosen === 'compatible' && Boolean(validateCompatibleUrl(cfg.get<string>('compatibleBaseUrl', '') || ''));
        if (!has || needsUrl) {
            if (!await askKey(context, chosen, false)) { return false; }
            keyAdded = true;
        }
    }
    if (chosen === current && !keyAdded) { return false; }
    routingWriteAt = Date.now();
    await cfg.update('economyProvider', chosen, vscode.ConfigurationTarget.Global);
    vscode.window.showInformationMessage(`ReCoder: 절약 등급 공급자를 ${ECONOMY_LABEL[chosen]}(으)로 바꿨습니다. Core를 다시 시작합니다.`);
    return true;
}

/**
 * 명령 팔레트·화면 버튼 공용. 변경이 있으면 true — 호출자가 Core 를 재시작한다.
 * `provider` 를 주면 선택 단계를 건너뛴다.
 */
export async function runAiConnectCommand(context: Ctx, provider?: AiKeyProvider): Promise<boolean> {
    let chosen: AiKeyProvider | 'aws' | undefined = provider;
    if (!chosen) {
        const current = await currentAiProvider(context);
        const pick = await vscode.window.showQuickPick([
            { label: AI_PROVIDER_LABEL.anthropic, description: current === 'anthropic' ? '현재 사용 중' : 'Claude API 키', value: 'anthropic' as const },
            { label: AI_PROVIDER_LABEL.openai, description: current === 'openai' ? '현재 사용 중' : 'OpenAI API 키', value: 'openai' as const },
            { label: AI_PROVIDER_LABEL.compatible, description: current === 'compatible' ? '현재 사용 중' : 'API 주소 + 키', value: 'compatible' as const },
            { label: '본인 AWS 계정 또는 게이트웨이 사용', description: current ? '저장한 API 키 삭제' : '현재 사용 중', value: 'aws' as const },
        ], { title: 'ReCoder: AI 연결 방법', placeHolder: 'AI 요청을 어디로 보낼지 고르세요' });
        if (!pick) { return false; }
        chosen = pick.value;
    }
    if (chosen === 'aws') {
        if (!await currentAiProvider(context)) { return false; }
        await clearAiKeys(context);
        vscode.window.showInformationMessage('ReCoder: API 키를 삭제했습니다. AWS 계정 또는 게이트웨이로 AI를 연결합니다.');
        return true;
    }
    const target = chosen;
    if (!await askKey(context, target, true)) { return false; }
    vscode.window.showInformationMessage(`ReCoder: ${AI_PROVIDER_LABEL[target]} 키를 저장했습니다. Core를 다시 시작해 연결을 확인합니다.`);
    return true;
}
