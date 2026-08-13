/**
 * BridgeClient — Discord 봇 ↔ VSCode 확장 WebSocket 클라이언트
 *
 * 봇의 `recoder_bridge.py` (ws://127.0.0.1:7780/ws) 에 연결하고
 * /make 채널에서 생성된 코드를 받아 워크스페이스 파일에 작성한다.
 *
 * 봇이 보내는 메시지:
 *   { type: 'hello', msg: 'ReCoder bridge connected' }
 *   { type: 'start', filename, language, prompt }
 *   { type: 'chunk', text }
 *   { type: 'end',   filename, auto_run }
 *   { type: 'error', filename, error }
 *   { type: 'info',  filename, message }
 *
 * 인증: Authorization: Bearer <token>  또는  ?token=<token>
 *   token 은 VS Code 설정 `recoder.bridge.token` 또는 환경변수
 *   `RECODER_BRIDGE_TOKEN` (둘 중 우선순위는 설정 → env) 에서 읽음.
 *
 * 동작:
 *   - start  : 새 세션 생성. 워크스페이스 루트에 빈 파일 생성 후 에디터에서 연다.
 *   - chunk  : 누적 버퍼에 추가, 에디터에 실시간 반영 (200ms throttle).
 *   - end    : 디스크 저장 + (auto_run 이면) 파일 종류에 맞춰 실행.
 *
 * 자동 재연결: 1초 → 2초 → 4초 → 최대 30초 백오프.
 */

import * as vscode from 'vscode';
import WebSocket from 'ws';
import * as path from 'path';

interface BridgeMessage {
    type: string;
    filename?: string;
    language?: string;
    prompt?: string;
    text?: string;
    auto_run?: boolean;
    error?: string;
    message?: string;
    msg?: string;
}

interface Session {
    filename: string;
    language: string;
    buffer: string;
    uri: vscode.Uri;
    editor?: vscode.TextEditor;
    pendingFlush?: NodeJS.Timeout;
    statusBar: vscode.StatusBarItem;
}

const FLUSH_INTERVAL_MS = 200;
const RECONNECT_INITIAL_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const PING_INTERVAL_MS = 20_000;

export class BridgeClient implements vscode.Disposable {
    private ws: WebSocket | null = null;
    private reconnectTimer: NodeJS.Timeout | null = null;
    private pingTimer: NodeJS.Timeout | null = null;
    private reconnectDelay = RECONNECT_INITIAL_MS;
    private disposed = false;

    private session: Session | null = null;
    private readonly output: vscode.OutputChannel;

    constructor(private readonly context: vscode.ExtensionContext) {
        this.output = vscode.window.createOutputChannel('ReCoder Bridge');
        context.subscriptions.push(this.output);
    }

    /** 설정 또는 환경변수에서 브리지 endpoint + 토큰 회수 */
    /** rcdr_<student_id>_<secret> 토큰에서 student_id 추출 */
    private _parseStudentId(token: string): string {
        const t = (token || '').trim();
        if (t.startsWith('rcdr_')) {
            const parts = t.split('_');
            if (parts.length >= 3) return parts[1];
        }
        return '';
    }

    /** 브리지 접속에 쓸 학생 토큰(소유 증명). SecretStorage 우선, 없으면 env. */
    private async _getStudentToken(): Promise<string> {
        try {
            const fromSecret = await this.context.secrets.get('recoder.studentToken');
            if (fromSecret) return fromSecret;
        } catch { /* SecretStorage 접근 실패 시 env 로 폴백 */ }
        return process.env.RECODER_STUDENT_TOKEN || '';
    }

    private _resolveEndpoint(studentToken: string): { url: string; token: string } {
        const cfg = vscode.workspace.getConfiguration('recoder.bridge');
        const host = cfg.get<string>('host', '127.0.0.1');
        const port = cfg.get<number>('port', 7780);
        const token =
            cfg.get<string>('token', '') ||
            process.env.RECODER_BRIDGE_TOKEN ||
            '';
        // Phase 2 per-user 라우팅 식별자: 설정값 우선, 없으면 학생 토큰에서 추출.
        const studentId =
            cfg.get<string>('studentId', '') ||
            this._parseStudentId(studentToken);
        const params = new URLSearchParams();
        if (token) params.set('token', token);
        if (studentId) params.set('student', studentId);
        // **소유 증명 토큰을 함께 전송한다.** 이게 없으면 브리지의 student
        // 검증이 항상 실패해, per-student 라우팅이 조용히 꺼지거나(REQUIRE=0)
        // 403(REQUIRE=1) 이 된다 — 서버에만 검증을 넣고 전송을 빠뜨렸던 버그.
        // URL 쿼리는 로그에 남으므로 secret 은 **헤더로만** 보낸다.
        const qs = params.toString() ? `?${params.toString()}` : '';
        return {
            url: `ws://${host}:${port}/ws${qs}`,
            token,
        };
    }

    /** 연결 시도 — 실패하면 재시도 타이머 가동 */
    public connect(): void {
        if (this.disposed) return;
        if (this.ws && this.ws.readyState === WebSocket.OPEN) return;
        void this._connectAsync();
    }

    private async _connectAsync(): Promise<void> {
        if (this.disposed) return;
        if (this.ws && this.ws.readyState === WebSocket.OPEN) return;

        const studentToken = await this._getStudentToken();
        if (this.disposed) return;
        const { url, token } = this._resolveEndpoint(studentToken);
        this.output.appendLine(`[bridge] connecting to ${token ? url.replace(token, '***') : url}`);

        // secret 은 헤더로만 — 쿼리스트링에 실으면 액세스 로그에 평문으로 남는다.
        const headers: Record<string, string> = {};
        if (token) headers['Authorization'] = `Bearer ${token}`;
        if (studentToken) headers['X-Student-Token'] = studentToken;

        try {
            this.ws = new WebSocket(url, {
                headers,
                handshakeTimeout: 5_000,
            });
        } catch (err) {
            this.output.appendLine(`[bridge] new WebSocket failed: ${err}`);
            this._scheduleReconnect();
            return;
        }

        this.ws.on('open', () => {
            this.reconnectDelay = RECONNECT_INITIAL_MS;
            this.output.appendLine(`[bridge] ✓ connected`);
            vscode.window.setStatusBarMessage('$(plug) ReCoder Bridge 연결됨', 3_000);
            this._startPing();
        });

        this.ws.on('message', (data: WebSocket.RawData) => {
            try {
                const msg = JSON.parse(data.toString()) as BridgeMessage;
                void this._handleMessage(msg);
            } catch (err) {
                this.output.appendLine(`[bridge] bad message: ${err}`);
            }
        });

        this.ws.on('close', (code, reason) => {
            this.output.appendLine(
                `[bridge] closed code=${code} reason=${reason?.toString() || '(none)'}`,
            );
            this._stopPing();
            this.ws = null;
            this._scheduleReconnect();
        });

        this.ws.on('error', (err) => {
            this.output.appendLine(`[bridge] error: ${err.message}`);
            // close 가 곧 따라옴 — 거기서 재연결 스케줄
        });
    }

    private _scheduleReconnect(): void {
        if (this.disposed) return;
        if (this.reconnectTimer) return;
        const delay = this.reconnectDelay;
        this.output.appendLine(`[bridge] reconnect in ${delay}ms`);
        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            this.reconnectDelay = Math.min(this.reconnectDelay * 2, RECONNECT_MAX_MS);
            this.connect();
        }, delay);
    }

    private _startPing(): void {
        this._stopPing();
        this.pingTimer = setInterval(() => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                try {
                    this.ws.send(JSON.stringify({ type: 'ping' }));
                } catch { /* ignore */ }
            }
        }, PING_INTERVAL_MS);
    }

    private _stopPing(): void {
        if (this.pingTimer) {
            clearInterval(this.pingTimer);
            this.pingTimer = null;
        }
    }

    /** 봇이 보낸 메시지 → 세션 상태 업데이트 + 파일 작성 */
    private async _handleMessage(msg: BridgeMessage): Promise<void> {
        switch (msg.type) {
            case 'hello':
                this.output.appendLine(`[bridge] ${msg.msg ?? 'hello'}`);
                return;
            case 'ping':
                this._send({ type: 'pong' });
                return;
            case 'pong':
                return;

            case 'start':
                await this._handleStart(msg);
                return;

            case 'chunk':
                this._handleChunk(msg);
                return;

            case 'end':
                await this._handleEnd(msg);
                return;

            case 'info':
                if (msg.message) {
                    this.output.appendLine(`[bridge] info(${msg.filename}): ${msg.message}`);
                    vscode.window.setStatusBarMessage(
                        `ReCoder: ${msg.message}`, 4_000,
                    );
                }
                return;

            case 'error': {
                // 봇이 보내는 에러 페이로드는 'error' / 'message' 둘 다 채워보냄.
                // 어떤 필드든 채워진 걸 우선 사용.
                const detail = msg.error || msg.message || 'unknown error';
                this.output.appendLine(`[bridge] error(${msg.filename}): ${detail}`);
                vscode.window.showErrorMessage(
                    `ReCoder Bridge: ${detail}`,
                );
                return;
            }

            case 'delete':
                await this._handleDelete(msg);
                break;
            default:
                this.output.appendLine(`[bridge] unknown message type: ${msg.type}`);
        }
    }

    /** /make 새 세션 시작 — 파일 생성 + 에디터에서 열기 */
    private async _handleStart(msg: BridgeMessage): Promise<void> {
        const filename = msg.filename || 'untitled.txt';
        const language = msg.language || '';

        // 이전 세션 정리 (강제 종료)
        if (this.session) {
            this._disposeSession(this.session, /* save */ false);
            this.session = null;
        }

        const root = this._getWorkspaceRoot();
        if (!root) {
            vscode.window.showErrorMessage(
                'ReCoder Bridge: 워크스페이스가 열려있지 않습니다. 폴더를 열고 다시 시도하세요.',
            );
            return;
        }

        const safeName = this._sanitizeFilename(filename);
        const fileUri = vscode.Uri.joinPath(root, safeName);

        // 빈 파일을 디스크에 생성 후 곧바로 에디터에서 여는 패턴은
        // openTextDocument 가 fs cache 와 race 되어 "content is newer" 충돌 유발.
        // 대신 untitled 문서를 열어 메모리에서만 작업하고, _handleEnd 의
        // document.save() 가 디스크에 처음 sync 한다.
        // 단, save 시 파일 이름을 알아야 하므로 fileUri 를 그대로 사용 가능한
        // 경로 형태로 openTextDocument 호출 (없는 파일이어도 빈 문서로 열림).
        let editor: vscode.TextEditor | undefined;
        try {
            // 디스크에 없는 파일이면 자동으로 빈 untitled 처럼 열림.
            // 이미 있으면 그 내용을 무시하고 새 세션이 덮어씀 (intended).
            const existsAlready = await this._fileExists(fileUri);
            if (!existsAlready) {
                // 빈 파일 1회 생성 (openTextDocument 가 untitled scheme 대신 file scheme 사용하도록)
                await vscode.workspace.fs.writeFile(fileUri, new Uint8Array());
            }
            const doc = await vscode.workspace.openTextDocument(fileUri);
            editor = await vscode.window.showTextDocument(doc, { preview: false });
        } catch (err) {
            this.output.appendLine(`[bridge] open editor failed: ${err}`);
        }

        const statusBar = vscode.window.createStatusBarItem(
            vscode.StatusBarAlignment.Right, 1000,
        );
        statusBar.text = `$(sync~spin) /make ${safeName}`;
        statusBar.tooltip = `Discord 에서 코드 생성 중… (${language})`;
        statusBar.show();

        this.session = {
            filename: safeName,
            language,
            buffer: '',
            uri: fileUri,
            editor,
            statusBar,
        };

        this.output.appendLine(`[bridge] start → ${safeName} (${language})`);
        vscode.window.setStatusBarMessage(
            `ReCoder: /make ${safeName} 생성 시작…`, 3_000,
        );
    }

    /** 코드 청크 수신 — 버퍼에 누적 + throttle 후 에디터에 반영 */
    private _handleChunk(msg: BridgeMessage): void {
        if (!this.session) return;
        const text = msg.text ?? '';
        if (!text) return;
        this.session.buffer += text;
        this._scheduleFlush(this.session);
    }

    private _scheduleFlush(session: Session): void {
        if (session.pendingFlush) return;
        session.pendingFlush = setTimeout(() => {
            session.pendingFlush = undefined;
            void this._flushBuffer(session);
        }, FLUSH_INTERVAL_MS);
    }

    private async _flushBuffer(session: Session): Promise<void> {
        if (!session.editor) return;
        const doc = session.editor.document;
        const fullRange = new vscode.Range(
            doc.lineAt(0).range.start,
            doc.lineAt(doc.lineCount - 1).range.end,
        );
        const edit = new vscode.WorkspaceEdit();
        edit.replace(doc.uri, fullRange, session.buffer);
        try {
            await vscode.workspace.applyEdit(edit);
            // 마지막 줄로 스크롤
            const last = new vscode.Position(doc.lineCount - 1, 0);
            session.editor.revealRange(new vscode.Range(last, last));
        } catch (err) {
            this.output.appendLine(`[bridge] flush failed: ${err}`);
        }
    }

    /** 세션 완료 — 디스크 저장 + 선택 자동 실행 */
    private async _handleEnd(msg: BridgeMessage): Promise<void> {
        if (!this.session) return;
        const session = this.session;
        // **filename 대조.** end 의 filename 이 현재 세션과 다르면 다른 파일의
        // 뒤늦은 end 가 지금 세션을 종료·실행하는 것이다(연속 /make 인터리브).
        // 무시한다 — 빈 파일 저장과 원치 않은 실행을 막는다.
        if (msg.filename && session.filename && msg.filename !== session.filename) {
            this.output.appendLine(
                `[bridge] end filename 불일치: ${msg.filename} ≠ ${session.filename} — 무시`,
            );
            return;
        }
        this.session = null;

        if (session.pendingFlush) {
            clearTimeout(session.pendingFlush);
            session.pendingFlush = undefined;
        }
        await this._flushBuffer(session);

        // 저장은 에디터 document 를 통해서만 수행 — fs.writeFile 직접 호출은
        // 에디터 버퍼와 disk 가 race 되어 "content is newer" 충돌 일으킨다.
        // applyEdit 으로 버퍼에 작성 → document.save() 로 한 번에 디스크 sync.
        if (session.editor) {
            try {
                await session.editor.document.save();
            } catch (err) {
                this.output.appendLine(`[bridge] document.save failed: ${err}`);
                // 에디터 저장이 실패한 경우에만 fs 로 직접 fallback
                try {
                    await vscode.workspace.fs.writeFile(
                        session.uri, Buffer.from(session.buffer, 'utf8'),
                    );
                } catch (err2) {
                    this.output.appendLine(`[bridge] fs.writeFile fallback failed: ${err2}`);
                }
            }
        } else {
            // 에디터가 없으면 (드문 케이스) 직접 fs 로 작성
            try {
                await vscode.workspace.fs.writeFile(
                    session.uri, Buffer.from(session.buffer, 'utf8'),
                );
            } catch (err) {
                this.output.appendLine(`[bridge] fs.writeFile failed: ${err}`);
            }
        }

        session.statusBar.text = `$(check) ${session.filename}`;
        setTimeout(() => session.statusBar.dispose(), 5_000);

        const totalLines = session.buffer.split('\n').length;
        this.output.appendLine(
            `[bridge] end → ${session.filename} (${totalLines} lines)`,
        );
        vscode.window.setStatusBarMessage(
            `ReCoder: /make ${session.filename} 저장 완료 (${totalLines} 줄)`,
            5_000,
        );

        // auto_run — 파일 종류별로 실행
        if (msg.auto_run) {
            await this._autoRun(session);
        }
    }

    /** 봇이 보내는 { type: "delete", filename } — 워크스페이스에서 파일 삭제(휴지통). */
    private async _handleDelete(msg: BridgeMessage): Promise<void> {
        const filename = msg.filename;
        if (!filename) { return; }
        const root = this._getWorkspaceRoot();
        if (!root) {
            vscode.window.showErrorMessage('ReCoder Bridge: 워크스페이스가 열려있지 않습니다.');
            return;
        }
        const safeName = this._sanitizeFilename(filename);
        const fileUri = vscode.Uri.joinPath(root, safeName);
        // 그 파일을 편집 중인 세션이면 먼저 정리
        if (this.session && this.session.filename === safeName) {
            this._disposeSession(this.session, /* save */ false);
            this.session = null;
        }
        try {
            if (!(await this._fileExists(fileUri))) {
                vscode.window.setStatusBarMessage(`ReCoder: ${safeName} 없음(이미 삭제?)`, 4000);
                return;
            }
            await vscode.workspace.fs.delete(fileUri, { useTrash: true });
            this.output.appendLine(`[bridge] delete → ${safeName}`);
            vscode.window.setStatusBarMessage(`ReCoder: ${safeName} 삭제됨`, 4000);
        } catch (err) {
            this.output.appendLine(`[bridge] delete failed: ${err}`);
            vscode.window.showErrorMessage(`ReCoder: ${safeName} 삭제 실패 — ${err}`);
        }
    }

    /** /make 메시지에 "실행해줘" 같은 키워드가 있을 때 봇이 보내는 auto_run 플래그 처리 */
    private async _autoRun(session: Session): Promise<void> {
        const ext = path.extname(session.filename).toLowerCase();
        const cwdUri = this._getWorkspaceRoot();
        const cwd = cwdUri?.fsPath;

        // **실행 전 사용자 확인 필수.** auto_run 은 원격(디스코드 봇)이 보낸
        // 코드를 이 머신의 셸에서 돌리는 것이다. 브리지에 붙은 서버의 신원을
        // 완전히 보장할 수 없으므로(로컬 포트 선점·공유 토큰), 확인 없이
        // 실행하면 임의 코드 실행 통로가 된다. `.html` 미리보기처럼 셸을
        // 쓰지 않는 경로는 아래에서 계속 진행하고, 셸 실행은 여기서 막는다.
        const EXECUTES_IN_SHELL = new Set(['.py', '.js', '.mjs', '.ts', '.sh', '.go']);
        if (EXECUTES_IN_SHELL.has(ext)) {
            const allowAuto = vscode.workspace
                .getConfiguration('recoder.bridge')
                .get<boolean>('allowAutoRun', false);
            if (!allowAuto) {
                const pick = await vscode.window.showWarningMessage(
                    `ReCoder 브리지가 받은 "${session.filename}" 를 터미널에서 실행하려고 합니다. ` +
                    `원격에서 전달된 코드입니다. 실행할까요?`,
                    { modal: true },
                    '실행', '이번만 건너뛰기',
                );
                if (pick !== '실행') {
                    this.output.appendLine(`[bridge] auto_run 취소됨(사용자 거부): ${session.filename}`);
                    return;
                }
            }
        }

        // 디스크 sync 대기 — Windows 에서 write 직후 openExternal 이 0x2 (파일 없음) 뜨는 race 회피
        await new Promise<void>((resolve) => setTimeout(resolve, 250));

        // 파일이 실제로 디스크에 존재하는지 확인
        let fileReady = false;
        try {
            const stat = await vscode.workspace.fs.stat(session.uri);
            fileReady = stat.size > 0;
        } catch {
            fileReady = false;
        }
        if (!fileReady) {
            this.output.appendLine(`[bridge] auto_run skipped: file not ready on disk (${session.filename})`);
            return;
        }

        if (ext === '.html') {
            // 우선순위: VS Code 안에 미리보기 (webview panel) → 외부 브라우저 fallback
            //
            // 1) Webview panel 옆 탭에 임베드 — 새 크롬 창 안 뜨고 VS Code 안에서 바로 확인.
            //    inline script 허용 CSP 로 테트리스 같은 단일 파일 게임 그대로 동작.
            try {
                const folder = this._getWorkspaceRoot();
                const localRoots = folder ? [folder] : [];
                const panel = vscode.window.createWebviewPanel(
                    'recoderRunner',
                    `▶ ${session.filename}`,
                    { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false },
                    {
                        enableScripts: true,
                        retainContextWhenHidden: true,
                        localResourceRoots: localRoots,
                    },
                );
                const bytes = await vscode.workspace.fs.readFile(session.uri);
                let html = Buffer.from(bytes).toString('utf8');
                // Webview 의 default CSP 가 strict 라서 inline script 차단됨.
                // meta 태그로 override (unsafe-inline + unsafe-eval 허용).
                const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'self' data: blob: ${panel.webview.cspSource}; script-src 'unsafe-inline' 'unsafe-eval' data: blob:; style-src 'unsafe-inline'; img-src data: blob: ${panel.webview.cspSource} https:; connect-src 'self' data: blob:;">`;
                if (!/<meta\s+http-equiv=["']?Content-Security-Policy/i.test(html)) {
                    if (/<head[^>]*>/i.test(html)) {
                        html = html.replace(/<head[^>]*>/i, (m) => m + csp);
                    } else if (/<html[^>]*>/i.test(html)) {
                        html = html.replace(/<html[^>]*>/i, (m) => `${m}<head>${csp}</head>`);
                    } else {
                        html = `<head>${csp}</head>${html}`;
                    }
                }
                panel.webview.html = html;
                this.output.appendLine(`[bridge] auto_run: opened ${session.filename} in webview panel (옆 탭)`);
                return;
            } catch (err) {
                this.output.appendLine(`[bridge] webview panel failed: ${err} — fallback to external browser`);
            }

            // 2) Fallback: 외부 브라우저 (Windows 는 explorer.exe 우선)
            const absPath = session.uri.fsPath;
            if (process.platform === 'win32') {
                try {
                    const cp = await import('child_process');
                    cp.spawn('explorer.exe', [absPath], { detached: true, stdio: 'ignore' }).unref();
                    this.output.appendLine(`[bridge] auto_run fallback: explorer.exe ${session.filename}`);
                    return;
                } catch (err) {
                    this.output.appendLine(`[bridge] explorer.exe failed: ${err}`);
                }
            }

            // 3) openExternal
            try {
                const fileUri = vscode.Uri.file(absPath);
                const ok = await vscode.env.openExternal(fileUri);
                if (ok) {
                    this.output.appendLine(`[bridge] auto_run fallback: openExternal ${session.filename}`);
                    return;
                }
            } catch (err) {
                this.output.appendLine(`[bridge] openExternal failed: ${err}`);
            }

            // 3) 통합 터미널 fallback
            try {
                const term = vscode.window.createTerminal({
                    name: `ReCoder: open ${session.filename}`, cwd,
                });
                const cmd = process.platform === 'win32'
                    ? `start "" "${absPath}"`
                    : process.platform === 'darwin'
                    ? `open "${absPath}"`
                    : `xdg-open "${absPath}"`;
                term.sendText(cmd);
                this.output.appendLine(`[bridge] auto_run: terminal ${cmd}`);
            } catch (err) {
                this.output.appendLine(`[bridge] auto_run terminal fallback failed: ${err}`);
                vscode.window.showWarningMessage(
                    `ReCoder: ${session.filename} 자동 실행 실패 — 수동으로 열어주세요.`,
                );
            }
            return;
        }

        // 터미널에서 실행
        let cmd = '';
        if (ext === '.py') cmd = `python "${session.filename}"`;
        else if (ext === '.js' || ext === '.mjs') cmd = `node "${session.filename}"`;
        else if (ext === '.ts') cmd = `npx ts-node "${session.filename}"`;
        else if (ext === '.sh') cmd = `bash "${session.filename}"`;
        else if (ext === '.go') cmd = `go run "${session.filename}"`;
        else {
            vscode.window.setStatusBarMessage(
                `ReCoder: ${session.filename} — 자동 실행 미지원 (확장자 ${ext})`,
                4_000,
            );
            return;
        }

        const term = vscode.window.createTerminal({
            name: `ReCoder: ${session.filename}`,
            cwd,
        });
        term.sendText(cmd);
        term.show();
        this.output.appendLine(`[bridge] auto_run: ${cmd}`);
    }

    /** 워크스페이스 첫 번째 폴더의 Uri (없으면 undefined) */
    private _getWorkspaceRoot(): vscode.Uri | undefined {
        const folders = vscode.workspace.workspaceFolders;
        return folders && folders.length > 0 ? folders[0].uri : undefined;
    }

    /** 디스크에 파일이 이미 있는지 확인 */
    private async _fileExists(uri: vscode.Uri): Promise<boolean> {
        try {
            await vscode.workspace.fs.stat(uri);
            return true;
        } catch {
            return false;
        }
    }

    /** path traversal / 비정상 파일명 방지 */
    private _sanitizeFilename(name: string): string {
        const base = path.basename(name).trim();
        if (!base || base === '.' || base === '..') return 'untitled.txt';
        return base.replace(/[/\\]/g, '_');
    }

    private _disposeSession(session: Session, save: boolean): void {
        if (session.pendingFlush) {
            clearTimeout(session.pendingFlush);
            session.pendingFlush = undefined;
        }
        if (save) {
            void this._flushBuffer(session).catch(() => {});
        }
        try { session.statusBar.dispose(); } catch { /* ignore */ }
    }

    private _send(obj: object): void {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            try {
                this.ws.send(JSON.stringify(obj));
            } catch { /* ignore */ }
        }
    }

    public dispose(): void {
        this.disposed = true;
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        this._stopPing();
        if (this.session) {
            this._disposeSession(this.session, /* save */ false);
            this.session = null;
        }
        if (this.ws) {
            try { this.ws.close(); } catch { /* ignore */ }
            this.ws = null;
        }
    }
}
