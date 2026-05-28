/**
 * ReCoder Bridge Client — Discord 봇 (WebSocket) ↔ VSCode 에디터.
 *
 * 흐름:
 *   핸드폰 Discord 채팅 → 봇 → Bedrock 스트리밍 → 이 클라이언트(WS) →
 *   활성 워크스페이스에 파일을 만들고 토큰 청크를 실시간으로 삽입.
 *
 * 서버 → 클라이언트 이벤트:
 *   { type: "hello" }
 *   { type: "start", filename, language, prompt }
 *   { type: "chunk", text }
 *   { type: "end",   filename }
 *   { type: "error", message }
 *
 * ── 신뢰성 설계 (이전 버그 회귀 방지) ──────────────────────────────────
 *
 *  1. 이벤트는 도착 순서 그대로 단일 Promise 큐 (`processQueue`) 에서
 *     직렬로 처리한다 — 'message' 핸들러는 enqueue 만 한다.
 *
 *  2. `start` 가 끝나기 전 chunk 가 도착해도 큐에 보존되므로 손실 없음.
 *
 *  3. drain 시 마지막 부분 라인은 `pendingBuffer` 에 그대로 남기고,
 *     `end` 시 무조건 한 번 더 drain 한다. 종료시 잔여 텍스트도
 *     디스크에 안전하게 flush.
 *
 *  4. 코드 펜스 제거: 응답 *처음에 단독 펜스 라인* 1개와 *마지막 단독
 *     펜스 라인* 1개만 제거한다. 본문 안의 ``` 는 절대 건드리지 않음
 *     — 이전 버그(`</script></body></html>` 가 통째로 사라지던 케이스) 회귀 방지.
 *
 *  5. 청크 삽입은 `editor.edit()` 대신 `WorkspaceEdit + applyEdit` 를
 *     사용해 에디터가 닫혀도 디스크 파일에 직접 반영. 더 atomic.
 *
 *  6. 누적 텍스트를 메모리에도 보관(`receivedText`) → end 시 디스크 파일과
 *     크기를 검증해 손실이 감지되면 강제 재기록한다 (최후의 안전망).
 */

import * as vscode from 'vscode';
import * as path from 'path';
import WebSocket from 'ws';

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const PING_INTERVAL_MS = 25000;

interface BridgeEvent {
    type: 'hello' | 'start' | 'chunk' | 'end' | 'error' | 'pong' | 'info';
    filename?: string;
    language?: string;
    text?: string;
    message?: string;
    prompt?: string;
    /** true 면 endSession 후 파일 종류에 맞춰 자동 실행. */
    auto_run?: boolean;
}

interface Session {
    filename: string;
    uri: vscode.Uri;
    editor?: vscode.TextEditor;
    /** 라인 단위 처리를 위한 청크 누적 버퍼 (개행 없이 끝난 꼬리). */
    pendingBuffer: string;
    /** 펜스 필터를 위한 누적 출력 라인 수 (펜스는 stream 의 첫 라인/마지막 라인일 때만 의미). */
    emittedLineCount: number;
    /** 메모리에 보관한 전체 텍스트 — end 시점에 파일 무결성 검증용. */
    receivedText: string;
    /** receivedText 중 디스크에 flush 된 길이 — 이걸 넘어선 텍스트만 추가 flush. */
    flushedLength: number;
}

export class BridgeClient implements vscode.Disposable {
    private ws: WebSocket | null = null;
    private statusBar: vscode.StatusBarItem;
    private reconnectMs = RECONNECT_BASE_MS;
    private disposed = false;
    private currentSession: Session | null = null;
    private pingTimer: NodeJS.Timeout | null = null;
    private output: vscode.OutputChannel;

    /**
     * 단일 직렬 처리 큐 — message 이벤트 핸들러가 yield 해도
     * 다음 메시지의 처리가 이전 메시지 완료 후에만 시작되도록 보장.
     * 이게 이전 race condition 의 핵심 수정점.
     */
    private processQueue: Promise<void> = Promise.resolve();

    constructor(
        private readonly url: string,
        private readonly token: string,
    ) {
        this.statusBar = vscode.window.createStatusBarItem(
            vscode.StatusBarAlignment.Left,
            100,
        );
        this.statusBar.text = '$(plug) ReCoder Bridge: idle';
        this.statusBar.tooltip = `ReCoder Bridge\nURL: ${this.url}`;
        this.statusBar.show();

        this.output = vscode.window.createOutputChannel('ReCoder Bridge');
    }

    start(): void {
        this.output.appendLine(`[start] connecting → ${this.url}`);
        this.connect();
    }

    dispose(): void {
        this.disposed = true;
        this.clearPing();
        try {
            this.ws?.close();
        } catch {
            // ignore
        }
        this.statusBar.dispose();
        this.output.dispose();
    }

    // ── connection lifecycle ────────────────────────────────────────────────
    private connect(): void {
        if (this.disposed) return;

        const headers: Record<string, string> = {};
        if (this.token) headers['Authorization'] = `Bearer ${this.token}`;

        this.statusBar.text = '$(sync~spin) ReCoder Bridge: 연결 중…';

        try {
            this.ws = new WebSocket(this.url, { headers });
        } catch (err) {
            this.output.appendLine(`[connect] new WebSocket 실패: ${err}`);
            this.scheduleReconnect();
            return;
        }

        this.ws.on('open', () => {
            this.reconnectMs = RECONNECT_BASE_MS;
            this.statusBar.text = '$(check) ReCoder Bridge';
            this.statusBar.tooltip = `연결됨: ${this.url}`;
            this.output.appendLine('[open] connected');
            this.schedulePing();
        });

        this.ws.on('message', (data: WebSocket.RawData) => {
            // 파싱은 이벤트 도착 순서 유지를 위해 동기적으로 수행한 다음,
            // 실제 처리는 큐에 enqueue 한다. parse 실패는 더 자세히 로깅.
            let event: BridgeEvent;
            try {
                event = JSON.parse(data.toString());
            } catch (err) {
                this.output.appendLine(
                    `[message] JSON parse error: ${err} | payload=${data
                        .toString()
                        .slice(0, 200)}`,
                );
                return;
            }
            this.enqueue(() => this.handleEvent(event));
        });

        this.ws.on('close', (code: number, reason: Buffer) => {
            this.clearPing();
            this.statusBar.text = '$(debug-disconnect) ReCoder Bridge: 끊김';
            this.output.appendLine(
                `[close] code=${code} reason=${reason?.toString() || '-'}`,
            );
            if (!this.disposed) this.scheduleReconnect();
        });

        this.ws.on('error', (err) => {
            this.output.appendLine(`[error] ${err.message}`);
            // 'close' 이벤트가 곧이어 발생하므로 거기서 재연결 처리
        });
    }

    private scheduleReconnect(): void {
        const delay = this.reconnectMs;
        this.reconnectMs = Math.min(this.reconnectMs * 2, RECONNECT_MAX_MS);
        this.output.appendLine(`[reconnect] in ${delay}ms`);
        setTimeout(() => this.connect(), delay);
    }

    private schedulePing(): void {
        this.clearPing();
        this.pingTimer = setInterval(() => {
            if (this.ws?.readyState === WebSocket.OPEN) {
                try {
                    this.ws.send(JSON.stringify({ type: 'ping' }));
                } catch {
                    // ignore
                }
            }
        }, PING_INTERVAL_MS);
    }

    private clearPing(): void {
        if (this.pingTimer) {
            clearInterval(this.pingTimer);
            this.pingTimer = null;
        }
    }

    // ── 단일 처리 큐 ─────────────────────────────────────────────────────────
    /**
     * 다음 작업을 큐에 추가하고, 이전 작업이 끝난 뒤에만 실행되도록 체이닝.
     * 한 작업의 실패는 다음 작업까지 막지 않도록 .catch 로 흡수.
     */
    private enqueue(work: () => Promise<void>): void {
        this.processQueue = this.processQueue
            .then(work)
            .catch((err) => {
                this.output.appendLine(`[queue] uncaught: ${err?.stack || err}`);
            });
    }

    // ── event handling ──────────────────────────────────────────────────────
    private async handleEvent(event: BridgeEvent): Promise<void> {
        switch (event.type) {
            case 'hello':
                this.output.appendLine('[hello] bridge ready');
                return;
            case 'pong':
                return;
            case 'start':
                await this.startSession(
                    event.filename || 'untitled.txt',
                    event.language || '',
                    event.prompt || '',
                );
                return;
            case 'chunk':
                await this.appendChunk(event.text || '');
                return;
            case 'end':
                await this.endSession(!!event.auto_run);
                return;
            case 'error':
                vscode.window.showErrorMessage(
                    `ReCoder Bridge 오류: ${event.message}`,
                );
                this.output.appendLine(`[server-error] ${event.message}`);
                return;
            case 'info':
                this.output.appendLine(`[info] ${event.message}`);
                vscode.window.setStatusBarMessage(
                    `ReCoder: ${event.message}`,
                    5000,
                );
                return;
        }
    }

    // ── editor session ──────────────────────────────────────────────────────
    private async startSession(
        filename: string,
        language: string,
        prompt: string,
    ): Promise<void> {
        // 이전 세션이 완전히 종료되지 않았으면 강제 flush 후 종료
        if (this.currentSession) {
            this.output.appendLine(
                `[start] 이전 세션(${this.currentSession.filename}) 미종료 — 강제 종료 처리`,
            );
            await this.endSession().catch(() => {});
        }

        const folders = vscode.workspace.workspaceFolders;
        if (!folders || folders.length === 0) {
            vscode.window.showErrorMessage(
                'ReCoder Bridge: 워크스페이스 폴더가 열려있지 않습니다.',
            );
            return;
        }

        const rootUri = folders[0].uri;
        let target = vscode.Uri.joinPath(rootUri, this.sanitize(filename));

        // 이미 존재하면 타임스탬프 부여
        try {
            await vscode.workspace.fs.stat(target);
            const ts = new Date().toISOString().replace(/[:.]/g, '-');
            const parsed = path.parse(this.sanitize(filename));
            target = vscode.Uri.joinPath(
                rootUri,
                `${parsed.name}.${ts}${parsed.ext}`,
            );
        } catch {
            /* 파일 없음 — 그대로 사용 */
        }

        // 빈 파일 작성 후 에디터에서 연다.
        // — 이 await 들은 큐 안에서 직렬로 실행되므로, 완료될 때까지
        //   다음 chunk 처리가 절대 시작되지 않는다 (← 이전 race 해결)
        await vscode.workspace.fs.writeFile(target, new Uint8Array());
        const doc = await vscode.workspace.openTextDocument(target);
        const editor = await vscode.window.showTextDocument(doc, {
            preview: false,
            preserveFocus: false,
        });

        this.currentSession = {
            filename: path.basename(target.fsPath),
            uri: target,
            editor,
            pendingBuffer: '',
            emittedLineCount: 0,
            receivedText: '',
            flushedLength: 0,
        };

        this.output.appendLine(
            `[start] file=${target.fsPath} language=${language} prompt="${prompt.slice(0, 80)}"`,
        );
        vscode.window.setStatusBarMessage(
            `ReCoder: ${this.currentSession.filename} 생성 중…`,
            5000,
        );
    }

    private async appendChunk(text: string): Promise<void> {
        const session = this.currentSession;
        if (!session || !text) return;

        // 1) 메모리에 누적 — end 시 무결성 검증 기준이 됨
        session.receivedText += text;
        session.pendingBuffer += text;

        // 2) 라인 단위로 drain — 펜스 라인만 필터링
        const ready = this.drainCleanLines(session, /*atEnd*/ false);
        if (!ready) return;

        // 3) 에디터/디스크 양쪽에 반영
        await this.flushText(session, ready);
    }

    private async endSession(autoRun: boolean = false): Promise<void> {
        const session = this.currentSession;
        if (!session) return;

        // 1) 남은 버퍼를 atEnd=true 로 한 번 더 drain
        //    — 마지막에 개행 없이 끝나도 손실되지 않도록.
        const tail = this.drainCleanLines(session, /*atEnd*/ true);
        if (tail) {
            await this.flushText(session, tail).catch((err) => {
                this.output.appendLine(`[end] tail flush 실패: ${err}`);
            });
        }

        // 2) 무결성 검증 — 누적 텍스트와 디스크 파일 크기 비교.
        //    드물게 race 잔존이나 펜스 필터 false positive 가 있다면
        //    여기서 디스크에 직접 한 번 더 강제 기록한다.
        try {
            const onDisk = await vscode.workspace.fs.readFile(session.uri);
            const expected = this.applyFenceFilter(session.receivedText);
            const decoder = new TextDecoder('utf-8');
            const actual = decoder.decode(onDisk);

            if (actual !== expected) {
                this.output.appendLine(
                    `[end] 무결성 불일치 (actual=${actual.length}B, expected=${expected.length}B) — 강제 재기록`,
                );
                const enc = new TextEncoder();
                await vscode.workspace.fs.writeFile(
                    session.uri,
                    enc.encode(expected),
                );
            }
        } catch (err) {
            this.output.appendLine(`[end] 무결성 검증 실패: ${err}`);
        }

        // 3) 에디터 저장 (디스크는 이미 최신 상태)
        if (session.editor) {
            try {
                await session.editor.document.save();
            } catch (err) {
                this.output.appendLine(`[end] save 실패: ${err}`);
            }
        }

        vscode.window.showInformationMessage(
            `ReCoder: ${session.filename} 생성 완료`,
        );
        this.output.appendLine(
            `[end] ${session.filename} (총 ${session.receivedText.length}자)`,
        );

        // 4) auto_run 이면 파일 종류에 맞춰 자동 실행
        if (autoRun) {
            await this.autoRunFile(session.uri).catch((err) => {
                this.output.appendLine(`[autorun] 실패: ${err}`);
            });
        }

        this.currentSession = null;
    }

    /**
     * 생성된 파일을 적절한 방식으로 실행한다.
     *  - .html → 외부 기본 브라우저로 열기 (env.openExternal)
     *  - .py   → 통합 터미널에서 `python3 file.py`
     *  - .sh   → 통합 터미널에서 `bash file.sh`
     *  - .js   → 통합 터미널에서 `node file.js`
     *  - 기타  → 정보 메시지만
     *
     * 모든 실패는 사용자에게 알리되, 다음 세션 흐름을 막지 않는다.
     */
    private async autoRunFile(uri: vscode.Uri): Promise<void> {
        const fileName = path.basename(uri.fsPath);
        const ext = path.extname(fileName).toLowerCase();
        this.output.appendLine(`[autorun] ${fileName} (ext=${ext})`);

        switch (ext) {
            case '.html':
            case '.htm': {
                // file:// URI 로 외부 브라우저에서 열기.
                // openExternal 은 OS 의 기본 핸들러(브라우저)를 사용한다.
                const ok = await vscode.env.openExternal(uri);
                if (!ok) {
                    vscode.window.showWarningMessage(
                        `브라우저를 열 수 없습니다: ${fileName}`,
                    );
                }
                return;
            }
            case '.py': {
                await this.runInTerminal(
                    'ReCoder Run',
                    `python3 ${this.shellQuote(uri.fsPath)}`,
                );
                return;
            }
            case '.sh':
            case '.bash': {
                await this.runInTerminal(
                    'ReCoder Run',
                    `bash ${this.shellQuote(uri.fsPath)}`,
                );
                return;
            }
            case '.js':
            case '.mjs': {
                await this.runInTerminal(
                    'ReCoder Run',
                    `node ${this.shellQuote(uri.fsPath)}`,
                );
                return;
            }
            case '.ts': {
                await this.runInTerminal(
                    'ReCoder Run',
                    `npx ts-node ${this.shellQuote(uri.fsPath)}`,
                );
                return;
            }
            default: {
                vscode.window.showInformationMessage(
                    `ReCoder: ${fileName} 은(는) 자동 실행을 지원하지 않는 형식입니다.`,
                );
            }
        }
    }

    /** 같은 이름의 터미널을 재사용해서 명령을 보낸다. */
    private async runInTerminal(name: string, command: string): Promise<void> {
        let term = vscode.window.terminals.find((t) => t.name === name);
        if (!term) {
            term = vscode.window.createTerminal({ name });
        }
        term.show(false);
        term.sendText(command, true);
    }

    /**
     * 셸 인자로 안전하게 인용. POSIX 와 Windows PowerShell 양쪽에서
     * 동작하도록 큰따옴표로 감싸고 내부 큰따옴표만 이스케이프.
     */
    private shellQuote(p: string): string {
        return `"${p.replace(/"/g, '\\"')}"`;
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /**
     * pendingBuffer에서 라인을 꺼낸다.
     *  - atEnd=false: 마지막의 미완성 라인(개행 없음) 은 버퍼에 남긴다.
     *  - atEnd=true:  남은 모든 텍스트를 강제로 출력 (마지막 호출).
     *
     * 펜스 필터: 응답 *최초 라인* 과 *최종 라인* 이 ``` 또는 ```lang 단독이면
     * 한 번씩만 제거. 본문 안의 ``` 는 절대 건드리지 않는다.
     */
    private drainCleanLines(session: Session, atEnd: boolean): string {
        const out: string[] = [];
        while (true) {
            const idx = session.pendingBuffer.indexOf('\n');
            if (idx === -1) {
                if (!atEnd) break;
                // atEnd: 남은 텍스트를 마지막 라인으로 처리 (개행 없이)
                const lastLine = session.pendingBuffer;
                session.pendingBuffer = '';
                if (lastLine === '') break;

                // 마지막 라인이 단독 펜스면 한 번만 제거
                if (this.isFenceLine(lastLine) && session.emittedLineCount > 0) {
                    break;
                }
                out.push(lastLine);
                session.emittedLineCount++;
                break;
            }

            const line = session.pendingBuffer.slice(0, idx);
            session.pendingBuffer = session.pendingBuffer.slice(idx + 1);

            // 첫 라인이 단독 펜스면 제거 (예: ```html)
            if (session.emittedLineCount === 0 && this.isFenceLine(line)) {
                continue;
            }
            out.push(line);
            session.emittedLineCount++;
        }
        // 마지막 라인 뒤 개행 보존 — atEnd 가 아니면 모든 출력 라인이 완전 라인.
        if (out.length === 0) return '';
        return out.join('\n') + (atEnd ? '' : '\n');
    }

    /** 줄 전체가 ``` 또는 ```lang 형태인지 */
    private isFenceLine(line: string): boolean {
        const trimmed = line.trim();
        return /^```[a-zA-Z0-9_+\-]*$/.test(trimmed);
    }

    /**
     * 무결성 검증용: receivedText 전체에서 첫/마지막 단독 펜스 1개씩만 제거.
     * 본문 중간의 ``` 는 보존한다.
     */
    private applyFenceFilter(full: string): string {
        const lines = full.split('\n');
        // 첫 줄 펜스 제거 (있을 때만)
        if (lines.length > 0 && this.isFenceLine(lines[0])) {
            lines.shift();
        }
        // 마지막 줄 펜스 제거 — 단, 빈 줄로 끝나는 경우(트레일링 개행) 고려
        // 끝에서부터 비어있지 않은 첫 줄을 찾는다
        let lastNonEmpty = lines.length - 1;
        while (lastNonEmpty >= 0 && lines[lastNonEmpty] === '') {
            lastNonEmpty--;
        }
        if (lastNonEmpty >= 0 && this.isFenceLine(lines[lastNonEmpty])) {
            lines.splice(lastNonEmpty, 1);
        }
        return lines.join('\n');
    }

    /**
     * 텍스트 한 덩어리를 에디터 + 디스크 양쪽에 반영.
     *
     *  - WorkspaceEdit + applyEdit 로 에디터가 닫혀도 디스크에 직접 기록되도록.
     *  - 에디터가 살아있으면 자동으로 갱신되고, 추가로 revealRange 로 따라가게.
     *  - flushedLength 를 갱신해 end 시 무결성 검증 기준으로 사용.
     */
    private async flushText(session: Session, text: string): Promise<void> {
        if (!text) return;

        // 1) WorkspaceEdit 로 파일 끝에 insert — atomic
        const edit = new vscode.WorkspaceEdit();
        // 현재 파일 길이를 line/character 로 계산. 에디터 doc 우선, 없으면 디스크.
        let endPos: vscode.Position;
        if (session.editor && !session.editor.document.isClosed) {
            const doc = session.editor.document;
            const lastLineIdx = Math.max(0, doc.lineCount - 1);
            endPos = doc.lineAt(lastLineIdx).range.end;
        } else {
            // 에디터가 닫혔으면 디스크에서 직접 읽어 위치 계산
            const bytes = await vscode.workspace.fs.readFile(session.uri);
            const current = new TextDecoder('utf-8').decode(bytes);
            const linesNow = current.split('\n');
            endPos = new vscode.Position(
                linesNow.length - 1,
                linesNow[linesNow.length - 1].length,
            );
        }
        edit.insert(session.uri, endPos, text);
        const ok = await vscode.workspace.applyEdit(edit);
        if (!ok) {
            // applyEdit 가 실패하면 직접 디스크에 append
            await this.appendToDisk(session.uri, text);
        }

        session.flushedLength += text.length;

        // 2) UI 스크롤 따라가기 — 에디터가 살아있을 때만
        if (session.editor && !session.editor.document.isClosed) {
            const doc = session.editor.document;
            const newEnd = doc.lineAt(doc.lineCount - 1).range.end;
            session.editor.selection = new vscode.Selection(newEnd, newEnd);
            session.editor.revealRange(
                new vscode.Range(newEnd, newEnd),
                vscode.TextEditorRevealType.Default,
            );
        }
    }

    /** WorkspaceEdit 실패 시 fallback — 디스크에 직접 append. */
    private async appendToDisk(uri: vscode.Uri, text: string): Promise<void> {
        // 명시적 ArrayBuffer 기반 Uint8Array — 최신 lib.es5 타입에서 readFile 이
        // Uint8Array<ArrayBufferLike> 를 반환해도 writeFile 시 호환되도록 변환.
        let currentBytes: Uint8Array;
        try {
            const read = await vscode.workspace.fs.readFile(uri);
            currentBytes = new Uint8Array(
                read.buffer as ArrayBuffer,
                read.byteOffset,
                read.byteLength,
            );
        } catch {
            currentBytes = new Uint8Array(0);
        }
        const addition = new TextEncoder().encode(text);
        const merged = new Uint8Array(currentBytes.length + addition.length);
        merged.set(currentBytes, 0);
        merged.set(addition, currentBytes.length);
        await vscode.workspace.fs.writeFile(uri, merged);
    }

    /** 경로 탈출 차단 — 파일명에서 슬래시/역슬래시 제거. */
    private sanitize(filename: string): string {
        const base = filename.replace(/[\\/]+/g, '_').replace(/^\.+/, '');
        return base || 'untitled.txt';
    }
}
