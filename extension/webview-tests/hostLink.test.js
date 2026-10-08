// 확장이 다시 시작된 뒤 남은 ReCoder 탭이 "확장이 요청을 받지 못했습니다"로 멈추던 문제.
//  1) 탭을 새 확장에 다시 붙인다(WebviewPanelSerializer + ReCoderPanel.revive).
//  2) 화면이 확장과 끊겼는지 바로 알아챈다(host.ping/host.pong → 맨 위 안내).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Module = require('node:module');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

const read = rel => fs.readFileSync(path.join(__dirname, '..', rel), 'utf8');

test('ReCoder 탭은 확장이 다시 시작돼도 되살아난다(serializer + activation event)', () => {
  const pkg = JSON.parse(read('package.json'));
  assert.ok(pkg.activationEvents.includes('onWebviewPanel:recoder.workspace'));
  const ext = read('src/extension.ts');
  assert.match(ext, /registerWebviewPanelSerializer\(ReCoderPanel\.viewType[\s\S]*?ReCoderPanel\.revive\(/);
  assert.match(read('src/sidebar/SidebarProvider.ts'), /case 'host\.ping'[\s\S]*?'host\.pong'/);
});

test('ReCoderPanel.revive: 되살린 탭을 새 확장에 붙이고, 이미 창이 있으면 중복 탭을 닫는다', () => {
  const resolve = Module._resolveFilename;
  Module._resolveFilename = function (name, ...args) {
    return name === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : resolve.call(this, name, ...args);
  };
  try {
    const vscode = require('vscode');
    const { ReCoderPanel } = require('../out/sidebar/ReCoderPanel.js');
    const attached = [], detached = [], restored = [];
    const provider = {
      setRestoredUiState: s => restored.push(s),
      getWorkspacePanelHtml: () => '<html>recoder</html>',
      attachWorkspacePanel: w => attached.push(w),
      detachWorkspacePanel: w => detached.push(w),
    };
    const fakePanel = () => {
      const p = { webview: { options: undefined, html: '' }, disposed: false, revealed: 0, onDispose: null };
      p.onDidDispose = fn => { p.onDispose = fn; return { dispose() {} }; };
      p.dispose = () => { p.disposed = true; p.onDispose?.(); };
      p.reveal = () => { p.revealed++; };
      return p;
    };
    const uri = vscode.Uri.file('/ext');
    const first = fakePanel();
    ReCoderPanel.revive(first, uri, provider, { view: 'code' });
    assert.equal(attached.length, 1, '되살린 탭이 새 확장에 붙지 않았다');
    assert.deepEqual(restored, [{ view: 'code' }], '되살린 화면 상태를 넘기지 않는다');
    assert.equal(attached[0], first.webview);
    assert.equal(first.webview.html, '<html>recoder</html>', '화면을 새로 그리지 않았다');
    assert.equal(first.webview.options.enableScripts, true);
    const dup = fakePanel();
    ReCoderPanel.revive(dup, uri, provider);
    assert.equal(dup.disposed, true, '이미 창이 있는데 탭이 둘이 됐다');
    assert.equal(attached.length, 1);
    assert.equal(first.revealed, 1);
    first.dispose();
    assert.deepEqual(detached, [first.webview]);
    const again = fakePanel();
    ReCoderPanel.revive(again, uri, provider);
    assert.equal(attached.length, 2, '창을 닫은 뒤 되살리기가 막혔다');
    again.dispose();
  } finally {
    Module._resolveFilename = resolve;
  }
});

function runHostLink() {
  const states = [], timers = new Map(), sent = [], listeners = {};
  let cursor = 0, handler, timerId = 0, value;
  const hooks = {
    ...React,
    useState(initial) { const i = cursor++; if (!(i in states)) states[i] = initial; return [states[i], v => { states[i] = typeof v === 'function' ? v(states[i]) : v; }]; },
    useRef(initial) { const i = cursor++; return states[i] ?? (states[i] = { current: initial }); },
    useCallback: fn => fn,
    useEffect(fn) { const i = cursor++; if (!states[i]) { states[i] = true; fn(); } },
  };
  const exports = {};
  const document = { visibilityState: 'visible', addEventListener: (t, fn) => { listeners[t] = fn; }, removeEventListener() {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../out/webview-test/hooks/useHostLink.js'), 'utf8'), {
    exports, document,
    window: { addEventListener: (t, fn) => { listeners['w:' + t] = fn; }, removeEventListener() {} },
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval: () => 0, clearInterval() {},
    require: id => id === 'react' ? hooks : require(id),
  });
  const render = () => { cursor = 0; value = exports.useHostLink((type, payload) => sent.push({ type, payload }), h => { handler = h; }); };
  render();
  return {
    exports, sent, listeners, get lost() { return value; },
    expire() { const fns = [...timers.values()]; timers.clear(); fns.forEach(f => f()); render(); },
    receive(type) { handler({ type, payload: {} }); render(); },
    render,
  };
}

test('useHostLink: 확장이 5초 안에 답하지 않으면 끊김, 메시지가 오면 다시 이어짐', () => {
  const h = runHostLink();
  assert.equal(h.sent[0].type, 'host.ping', '화면이 열릴 때 확인하지 않는다');
  assert.equal(h.lost, false);
  h.expire();
  assert.equal(h.lost, true);
  assert.equal(h.exports.isHostLinkLost(), true);
  h.receive('healthUpdate');
  assert.equal(h.lost, false);
  assert.equal(h.exports.isHostLinkLost(), false);
  // 답이 제때 오면 끊김으로 바뀌지 않는다.
  h.listeners.visibilitychange();
  assert.equal(h.sent.length, 2);
  h.receive('host.pong');
  h.expire();
  assert.equal(h.lost, false);
});

test('끊기면 ReCoder 창 맨 위에 다시 불러오기 안내가 뜬다', () => {
  const { WorkspaceLayout } = require('../out/webview-test/App.js');
  const render = hostLost => renderToStaticMarkup(React.createElement(WorkspaceLayout, {
    view: 'code', diagnostics: null, coreStatus: 'ok', showDiagnostics: false, isAiReady: true, isDockerReady: true, isOpsReady: true,
    costSummary: null, onSelectMode() {}, onToggleDiagnostics() {}, postMessage() {}, hostLost,
  }));
  assert.ok(!render(false).includes('host-link-lost'));
  const html = render(true);
  assert.ok(html.includes('확장과 연결이 끊겼습니다'));
  assert.ok(html.includes('Developer: Reload Window'));
});

test('되살린 ReCoder 탭은 보던 화면·쓰던 요청으로 돌아온다(실제 VS Code 웹 버전에서 확인한 경로)', () => {
  const provider = read('src/sidebar/SidebarProvider.ts');
  const panel = read('src/sidebar/ReCoderPanel.ts');
  const ext = read('src/extension.ts');
  const app = read('webview-src/App.tsx');
  const api = read('webview-src/hooks/useVSCodeApi.ts');
  // 웹뷰 → 확장: 화면 상태 사본(웹뷰 상태 저장은 늦게 디스크에 남을 수 있다)
  assert.match(api, /type: "ui\.state"/);
  assert.match(provider, /case 'ui\.state'[\s\S]*?UI_STATE_KEY/);
  // 되살림 → ready → ui.restore
  assert.match(ext, /deserializeWebviewPanel\(panel: vscode\.WebviewPanel, state: unknown\)[\s\S]*?revive\(panel, context\.extensionUri, sidebarProvider, state\)/);
  assert.match(panel, /setRestoredUiState\(state\)/);
  assert.match(provider, /case 'webview\.ready'[\s\S]*?'ui\.restore'/);
  // 화면: 저장한 보던 화면으로 시작하고, ui.restore 를 받으면 그 화면으로
  assert.match(app, /loadUiState\(\)\.view/);
  assert.match(app, /type === "ui\.restore"[\s\S]*?setView/);
  // 쓰던 요청
  assert.match(read('webview-src/components/CodeAgent.tsx'), /loadUiState\(\)\.codeDraft/);
});
