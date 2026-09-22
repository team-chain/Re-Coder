/**
 * 게이트웨이 자가발급(enroll) — 학생이 "반 코드" 한 번으로 본인 토큰을 받아
 * VSCode SecretStorage 에 저장한다.
 *
 * "확장만 설치 → 반 코드 1회 입력 → AWS 키 없이 AI" 흐름의 핵심.
 * 토큰은 CoreManager 가 Local Core spawn 시 환경변수로 주입한다.
 */
import * as vscode from 'vscode';
import * as https from 'https';
import * as http from 'http';
import { URL } from 'url';
import { buildDiscordLinkCommand } from './discordLink';

export const SECRET_TOKEN = 'recoder.studentToken';
export const SECRET_STUDENT_ID = 'recoder.studentId';

export function getGatewayUrl(): string {
    const raw = vscode.workspace.getConfiguration('recoder.gateway').get<string>('url', '') || '';
    return raw.trim().replace(/\/+$/, '');
}

export async function getStudentToken(context: vscode.ExtensionContext): Promise<string> {
    return (await context.secrets.get(SECRET_TOKEN)) || '';
}

function postJson(
    urlStr: string,
    body: unknown,
    headers: Record<string, string> = {},
): Promise<{ status: number; json: any }> {
    return new Promise((resolve, reject) => {
        const u = new URL(urlStr);
        const data = Buffer.from(JSON.stringify(body), 'utf-8');
        const lib = u.protocol === 'http:' ? http : https;
        const req = lib.request(
            {
                hostname: u.hostname,
                port: u.port || (u.protocol === 'http:' ? 80 : 443),
                path: u.pathname + u.search,
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Content-Length': data.length, ...headers },
            },
            (res) => {
                const chunks: Buffer[] = [];
                res.on('data', (c: Buffer) => chunks.push(c));
                res.on('end', () => {
                    const text = Buffer.concat(chunks).toString('utf-8');
                    let json: any = {};
                    try { json = text ? JSON.parse(text) : {}; } catch { /* ignore */ }
                    resolve({ status: res.statusCode || 0, json });
                });
            },
        );
        req.on('error', reject);
        req.write(data);
        req.end();
    });
}

/** 반 코드로 발급받아 SecretStorage 에 저장. 성공 시 true. */
export async function enrollWithCode(context: vscode.ExtensionContext, code: string): Promise<boolean> {
    const base = getGatewayUrl();
    if (!base) {
        vscode.window.showErrorMessage('ReCoder: 게이트웨이 URL이 설정되지 않았습니다 (recoder.gateway.url).');
        return false;
    }
    try {
        const name = (vscode.env.machineId || 'student').slice(0, 8);
        const { status, json } = await postJson(`${base}/enroll`, { code, name });
        if (status === 200 && json.token) {
            await context.secrets.store(SECRET_TOKEN, json.token);
            if (json.student_id) {
                await context.secrets.store(SECRET_STUDENT_ID, json.student_id);
                await vscode.workspace
                    .getConfiguration('recoder.bridge')
                    .update('studentId', json.student_id, vscode.ConfigurationTarget.Global);
                // 봇은 공개 student_id가 아니라 전체 발급 토큰으로 소유권을
                // 검증한다. 토큰은 알림 본문에 노출하지 않고 사용자가 명시적으로
                // 선택했을 때만 클립보드에 복사한다.
                const linkCmd = buildDiscordLinkCommand(String(json.token));
                const copyAction = '보안 명령 복사';
                void vscode.window
                    .showInformationMessage(
                        'ReCoder 연결 완료 — Discord 연동 명령에는 비밀 토큰이 포함됩니다.',
                        {
                            modal: true,
                            detail: '버튼으로 명령을 복사한 뒤 Discord의 /recoder link 입력란에 붙여넣으세요. 복사한 명령을 다른 사람에게 공개하지 마세요.',
                        },
                        copyAction,
                    )
                    .then((sel) => {
                        if (sel === copyAction) {
                            void vscode.env.clipboard.writeText(linkCmd);
                        }
                    });
            } else {
                vscode.window.showInformationMessage(
                    'ReCoder: 연결 완료 — 이제 AWS 키 없이 AI를 사용할 수 있습니다. (Core 재시작 시 적용)',
                );
            }
            return true;
        }
        vscode.window.showErrorMessage(`ReCoder 발급 실패: ${json.message || json.error || `HTTP ${status}`}`);
        return false;
    } catch (err) {
        vscode.window.showErrorMessage(`ReCoder 게이트웨이 연결 실패: ${err}`);
        return false;
    }
}

/** 최초 실행: 토큰이 없고 게이트웨이 URL 이 설정돼 있으면 반 코드를 물어 자가발급. */
export async function ensureEnrolled(context: vscode.ExtensionContext): Promise<void> {
    if (!getGatewayUrl()) { return; }
    if (await getStudentToken(context)) { return; }
    const code = await vscode.window.showInputBox({
        title: 'ReCoder 연결',
        prompt: '반 코드를 입력하세요 (강사에게 받은 enroll 코드). 입력하면 AWS 설정 없이 AI를 쓸 수 있습니다.',
        ignoreFocusOut: true,
    });
    if (!code) { return; }
    await enrollWithCode(context, code.trim());
}

/** 명령 핸들러: 수동 (재)발급. */
export async function runEnrollCommand(context: vscode.ExtensionContext): Promise<void> {
    if (!getGatewayUrl()) {
        vscode.window.showErrorMessage('ReCoder: 먼저 설정 recoder.gateway.url 을 지정하세요.');
        return;
    }
    if (await getStudentToken(context)) {
        const choice = await vscode.window.showWarningMessage(
            '이미 게이트웨이에 연결돼 있습니다. 다시 발급할까요?', '다시 발급', '취소',
        );
        if (choice !== '다시 발급') { return; }
    }
    const code = await vscode.window.showInputBox({
        title: 'ReCoder 연결', prompt: '반 코드를 입력하세요', ignoreFocusOut: true,
    });
    if (code) { await enrollWithCode(context, code.trim()); }
}
