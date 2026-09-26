// ── 확장 통합 하네스 (자급식) ─────────────────────────────────────────────
// 실제 컴파일된 SidebarProvider + ApiClient 를 vscode 목 위에서 구동하고,
// 픽스처 LLM 을 물린 **실 HTTP 코어**를 직접 띄워 붙여, 사용자가 실기기에서
// 밟는 메시지 사슬을 처음부터 끝까지 재현한다:
//
//   webview.ready → chat.send → chat.approveAction(빈 창=확장 재시작 시나리오)
//   → code.plan → code.planResult → code.generate → code.result
//   → code.applyAll → 파일이 실제 디스크에 존재
//
// 왜 있나: 이 사슬은 단위 테스트(webview-tests)가 각 조각만 보므로, 조각은
// 다 초록인데 이어 붙이면 끊기는 회귀(예: 빈 창 승인 시 확장 재시작으로
// 요청 유실 → 무한 로딩)를 못 잡았다. 이 하네스가 사슬 전체를 잠근다.
//
// 실행: node harness/e2e-extension-flow.js   (코어 파이썬 의존성 필요)
//   - 'out/' 이 없으면 먼저 `npm run compile`
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const Module = require('node:module');

const HERE = __dirname;
const EXT = path.resolve(HERE, '..');
let PORT = Number(process.env.HARNESS_PORT || 0);
const TOKEN = 'harness-token-123';

// ── 'vscode' 를 하네스 목으로 리다이렉트 (node_modules 오염 없이) ──
const mockPath = path.join(HERE, 'vscode-mock.js');
const _origResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
    if (request === 'vscode') { return mockPath; }
    return _origResolve.call(this, request, ...rest);
};

const vscode = require('vscode');
const { ApiClient } = require(path.join(EXT, 'out/core/ApiClient'));
const { SidebarProvider } = require(path.join(EXT, 'out/sidebar/SidebarProvider'));

// ── 협력자 가짜 ──
const fakeCoreManager = {
    getSessionToken: () => TOKEN, getPort: () => PORT,
    refreshToken: async () => true, ensureRunning: async () => true,
    onCoreRestart: () => ({ dispose() {} }),
    coreInstanceKey: () => 'fixture', getStoredAwsConnection: async () => null, getAwsRoleArn: async () => '',
};
ApiClient.prototype.runDiagnostics = async () => ({ core_ready: 'ok', ai_ready: 'ok', docker_ready: 'ok', aws_deploy_ready: 'ok', ops_ready: 'ok', issues: [] });
const fakePolling = { poll: async () => {}, start() {}, stop() {}, getLastHealth: () => null };

class Memento {
    constructor(seed = {}) { this.store = { ...seed }; }
    get(k) { return this.store[k]; }
    async update(k, v) { if (v === undefined) { delete this.store[k]; } else { this.store[k] = v; } }
}
function makeWebview(captured) {
    return { postMessage: async (m) => { captured.push(m); return true; }, asWebviewUri: (u) => u, cspSource: 'harness:', onDidReceiveMessage: () => ({ dispose() {} }), options: {}, html: '' };
}
const last = (arr, type) => [...arr].reverse().find((m) => m.type === type);
async function waitFor(arr, type, ms = 90000) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
        const m = last(arr, type);
        if (m) { return m; }
        await new Promise((r) => setTimeout(r, 50));
    }
    throw new Error(`시간 초과: ${type} 미수신. 수신: ${arr.map((m) => m.type).join(', ')}`);
}

const steps = [];
const step = (name, fn) => steps.push({ name, fn });

// ═══ 시나리오 ═══
step('0. 코어 헬스', async () => {
    const res = await fetch(`http://127.0.0.1:${PORT}/api/health`);
    assert.strictEqual(res.status, 200);
});

const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'harness-proj-'));
const memento = new Memento();
let sp, captured, wv, action, decisions, acceptedPayload, ops;

step('1. 빈 창(폴더 0개)에서 기동 + webview.ready', async () => {
    vscode.workspace.workspaceFolders = undefined;
    captured = [];
    wv = makeWebview(captured);
    sp = new SidebarProvider(vscode.Uri.file('/tmp/ext'), new ApiClient(fakeCoreManager), fakeCoreManager, fakePolling, undefined, memento);
    await sp.handleMessage({ type: 'webview.ready', payload: {} }, wv);
});

step('2. chat.send → 승인 카드(action) 수신', async () => {
    await sp.handleMessage({ type: 'chat.send', payload: { id: 'c1', message: '게시판 하나 만들어줘', history: [] } }, wv);
    const resp = await waitFor(captured, 'chat.response');
    assert.ok(resp.payload.reply, '채팅 응답이 비었다');
    action = resp.payload.action;
    assert.ok(action && action.type === 'code.plan', '승인 카드(action)가 없다');
});

step('3-A. 승인 → 워크스페이스 추가 시점에 재시작 인계 메모가 이미 있다', async () => {
    let memoAtMutation = null;
    vscode.workspace.__onUpdateWorkspaceFolders = () => { memoAtMutation = memento.get('recoder.pendingChatAction'); };
    const target = path.join(ws, 'board-app');
    await sp.handleMessage({ type: 'chat.approveAction', payload: { id: 'c1', instruction: action.instruction, targetFolder: target } }, wv);
    vscode.workspace.__onUpdateWorkspaceFolders = null;
    assert.ok(memoAtMutation, '워크스페이스 추가 시점에 인계 메모가 없다 — 재시작이 먼저 오면 요청 유실');
    assert.strictEqual(memoAtMutation.instruction, action.instruction);
    assert.ok(fs.existsSync(target), '대상 폴더 미생성');
    assert.ok(memento.get('recoder.pendingChatAction'), '첫 폴더 추가 후 재시작 전 메모를 삭제하면 안 된다');
    assert.ok(!last(captured, 'chat.actionAccepted'), '재시작할 이전 화면에서 개발을 시작하면 안 된다');
    sp = new SidebarProvider(vscode.Uri.file(EXT), new ApiClient(fakeCoreManager), fakeCoreManager, fakePolling, undefined, memento);
    await sp.handleMessage({type:'webview.ready',payload:{}},wv);
    const accepted = await waitFor(captured, 'chat.actionAccepted');
    assert.ok(accepted.payload.requestId, 'actionAccepted requestId 없음');
    assert.strictEqual(memento.get('recoder.pendingChatAction'), undefined, '정상 경로에서 메모 미삭제');
});

step('3-B. 재시작 후 새 프로바이더의 ready 가 요청을 이어받고, 중복 발송하지 않는다', async () => {
    const m2 = new Memento({ 'recoder.pendingChatAction': { instruction: action.instruction, targetFolder: '', absolutePath: path.join(ws, 'board-app'), folderCreated: true, ts: Date.now() } });
    const cap2 = [];
    const w2 = makeWebview(cap2);
    const sp2 = new SidebarProvider(vscode.Uri.file('/tmp/ext'), new ApiClient(fakeCoreManager), fakeCoreManager, fakePolling, undefined, m2);
    await sp2.handleMessage({ type: 'webview.ready', payload: {} }, w2);
    const restored = await waitFor(cap2, 'chat.actionAccepted', 3000);
    assert.ok(restored.payload.restoredAfterReload, 'restoredAfterReload 표시 없음');
    assert.strictEqual(m2.get('recoder.pendingChatAction'), undefined, '복구 후 메모 미삭제');
    const before = cap2.filter((m) => m.type === 'chat.actionAccepted').length;
    await sp2.handleMessage({ type: 'webview.ready', payload: {} }, w2);
    assert.strictEqual(cap2.filter((m) => m.type === 'chat.actionAccepted').length, before, '두 번째 ready 가 중복 발송');
});

step('4. code.plan → 실 코어에서 설계 결정 수신', async () => {
    acceptedPayload = last(captured, 'chat.actionAccepted').payload;
    await sp.handleMessage({ type: 'code.plan', payload: { requestId: 777, instruction: acceptedPayload.instruction, targetFolder: acceptedPayload.targetFolder, contextFiles: [] } }, wv);
    const m = last(captured, 'code.planResult') || last(captured, 'code.error');
    assert.ok(m, 'planResult/error 모두 없음');
    assert.strictEqual(m.type, 'code.planResult', `설계 결정 실패: ${JSON.stringify(m.payload).slice(0, 300)}`);
    assert.strictEqual(m.payload.requestId, 777);
    decisions = m.payload.decisions;
    assert.ok(Array.isArray(decisions) && decisions.length >= 1, '결정이 비었다');
});

step('5. code.generate(결정 확정) → 코드 + ADR 수신', async () => {
    const choices = decisions.map((d) => ({ id: d.id, question: d.question, chosen_key: d.recommended_key || (d.options && d.options[0] && d.options[0].key), options: d.options, impact: d.impact || '' }));
    await sp.handleMessage({ type: 'code.generate', payload: { requestId: 777, instruction: acceptedPayload.instruction, targetFolder: acceptedPayload.targetFolder, contextFiles: [], decisions: choices } }, wv);
    const g = last(captured, 'code.result') || last(captured, 'code.error');
    assert.ok(g, 'code.result/error 모두 없음');
    assert.strictEqual(g.type, 'code.result', `생성 실패: ${JSON.stringify(g.payload).slice(0, 300)}`);
    ops = g.payload.ops || (g.payload.result && g.payload.result.ops);
    assert.ok(Array.isArray(ops) && ops.length >= 2, `ops 부족: ${JSON.stringify(g.payload).slice(0, 300)}`);
});

step('6. code.applyAll → 파일이 실제 디스크에 생성된다', async () => {
    sp._view = { webview: wv }; // 제품에선 사이드바/편집기 패널이 이 자리
    const applyOps = ops.map((o) => ({ file: o.path || o.file, content: o.content, ackKey: o.path || o.file }));
    await sp.handleMessage({ type: 'code.applyAll', payload: { ops: applyOps, targetFolder: acceptedPayload.targetFolder } }, wv);
    const applied = last(captured, 'code.applied');
    assert.ok(applied && applied.payload.ok, `적용 실패: ${JSON.stringify((last(captured, 'code.error') || {}).payload || {}).slice(0, 200)}`);
    const root = vscode.workspace.workspaceFolders[0].uri.fsPath;
    for (const o of applyOps) {
        const p = path.join(root, o.file);
        assert.ok(fs.existsSync(p), `디스크에 없음: ${p}`);
        assert.ok(fs.readFileSync(p, 'utf8').length > 0, `빈 파일: ${p}`);
    }
});

// ═══ 코어 스폰 + 실행 + 정리 ═══
function waitHealthy(ms = 30000) {
    const t0 = Date.now();
    return new Promise((resolve, reject) => {
        const tick = async () => {
            try { const r = await fetch(`http://127.0.0.1:${PORT}/api/health`); if (r.status === 200) { return resolve(); } } catch { /* not up yet */ }
            if (Date.now() - t0 > ms) { return reject(new Error('코어가 시간 내에 안 떴다')); }
            setTimeout(tick, 300);
        };
        tick();
    });
}

(async () => {
    if (!PORT) {
        const socket = require('node:net').createServer();
        await new Promise(r=>socket.listen(0,'127.0.0.1',r));
        PORT=socket.address().port;
        await new Promise(r=>socket.close(r));
    }
    const py = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
    const core = spawn(py, [path.join(HERE, 'fixture-core.py')], { windowsHide:true, stdio: ['ignore', 'ignore', 'inherit'], env: { ...process.env, HARNESS_PORT: String(PORT) } });
    let failed = 0;
    try {
        await waitHealthy();
        for (const { name, fn } of steps) {
            try { await fn(); console.log(`  OK   ${name}`); }
            catch (e) { failed++; console.log(`  FAIL ${name}`); console.log(`       ${e && e.message}`); }
        }
    } catch (e) {
        console.log(`하네스 기동 실패: ${e.message}`);
        failed++;
    } finally {
        if (core.exitCode === null) {
            await new Promise(resolve => {
                core.once('exit', resolve);
                core.kill();
                setTimeout(resolve, 3000).unref();
            });
        }
    }
    console.log(failed === 0 ? '\n통합 하네스 전 구간 통과' : `\n실패 ${failed}건`);
    process.exitCode = failed === 0 ? 0 : 1;
})();
