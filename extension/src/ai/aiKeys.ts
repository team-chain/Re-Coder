import * as vscode from 'vscode';

/**
 * AWS 없이 AI 를 쓰는 경로 — Claude(Anthropic) 또는 ChatGPT(OpenAI) API 키.
 *
 * - 키는 VS Code 보안 저장소(SecretStorage)에만 둔다. 설정 파일·로그에 쓰지 않는다.
 * - 어떤 제공자를 쓸지는 사용자가 고른다(globalState). 고르지 않으면 Core 는 기존처럼
 *   게이트웨이 또는 본인 AWS(Bedrock)를 쓴다.
 * - Core 는 RECODER_AI_PROVIDER + RECODER_<제공자>_API_KEY 가 모두 있을 때만 이 경로를 쓴다.
 */
import { AI_KEY_SECRET, AI_PROVIDER_STATE, AiKeyContext, AiKeyProvider, aiKeyEnv as baseAiKeyEnv, currentAiProvider, isAiKeyProvider } from './aiKeyEnv';
export { AI_PROVIDER_STATE, AiKeyProvider, currentAiProvider, isAiKeyProvider };

export const AI_PROVIDER_LABEL: Record<AiKeyProvider, string> = { anthropic: 'Claude (Anthropic API)', openai: 'ChatGPT (OpenAI API)' };
const KEY_PAGE: Record<AiKeyProvider, string> = {
    anthropic: 'https://platform.claude.com/',
    openai: 'https://platform.openai.com/api-keys',
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
    return undefined;
}

/** 설정 recoder.ai.<제공자>Model 을 반영한 Core 환경변수. */
export function aiKeyEnv(context: Ctx): Promise<Record<string, string>> {
    return baseAiKeyEnv(context, (p) => vscode.workspace.getConfiguration('recoder.ai').get<string>(`${p}Model`, '') || '');
}

export async function storeAiKey(context: Ctx, provider: AiKeyProvider, key: string): Promise<void> {
    await context.secrets.store(SECRET[provider], key.trim());
    await context.globalState.update(AI_PROVIDER_STATE, provider);
}

export async function clearAiKeys(context: Ctx): Promise<void> {
    await Promise.all([context.secrets.delete(SECRET.anthropic), context.secrets.delete(SECRET.openai)]);
    await context.globalState.update(AI_PROVIDER_STATE, undefined);
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
    const key = await vscode.window.showInputBox({
        title: `ReCoder: ${AI_PROVIDER_LABEL[target]} 연결`,
        prompt: `API 키를 붙여넣으세요. 키는 VS Code 보안 저장소에만 저장되고, AI 요청은 이 키로 ${target === 'anthropic' ? 'Anthropic' : 'OpenAI'}에 직접 전송되며 요금은 해당 계정에 청구됩니다.`,
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
    await storeAiKey(context, target, key);
    vscode.window.showInformationMessage(`ReCoder: ${AI_PROVIDER_LABEL[target]} 키를 저장했습니다. Core를 다시 시작해 연결을 확인합니다.`);
    return true;
}
