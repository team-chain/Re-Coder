const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { CodeRemovalWarning, CodeRemovalSummary } = require('../out/webview-test/components/CodeRemovalWarning.js');

const report = { status: 'checked', removed: [
  { kind: 'function', name: 'listTodos', line: 3 },
  { kind: 'route', name: 'GET /todos', line: 6 },
  { kind: 'route', name: 'POST /todos', line: 7 },
] };
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));

test('removed names, method/path, counts and saved line numbers render before applying', () => {
  const html = render(CodeRemovalWarning, { check: report });
  for (const text of ['함수 1개', '라우트 2개', 'listTodos', 'GET /todos', 'POST /todos', '기존 6행', '이름 변경·이동']) {
    assert.ok(html.includes(text), text);
  }
});

test('apply-all summary aggregates separate files and incomplete checks', () => {
  const html = render(CodeRemovalSummary, { checks: [report, report, { status: 'unavailable', removed: [] }] });
  assert.ok(html.includes('함수 2개 · 라우트 4개'));
  assert.ok(html.includes('삭제 검사 미완료: 1개 파일'));
});

test('new files and unchanged declarations produce no warning', () => {
  for (const status of ['new_file', 'checked']) {
    const check = { status, removed: [] };
    assert.equal(render(CodeRemovalWarning, { check }), '');
    assert.equal(render(CodeRemovalSummary, { checks: [check] }), '');
  }
});

test('unsupported, failed and old-core missing checks are explicitly incomplete', () => {
  for (const check of [undefined, { status: 'unavailable', removed: [], reason: '구문 분석 실패' }]) {
    assert.ok(render(CodeRemovalWarning, { check }).includes('삭제 검사 미완료'));
    assert.ok(render(CodeRemovalSummary, { checks: [check] }).includes('미완료: 1개 파일'));
  }
});

test('names are escaped and lengthy lists remain inspectable', () => {
  const check = { status: 'checked', removed: [{ kind: 'route', name: 'GET /<script>alert(1)</script>', line: 1 }] };
  const html = render(CodeRemovalWarning, { check });
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

test('live CodeAgent wires per-file and apply-all warnings before the action controls', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const source = fs.readFileSync(path.join(__dirname, '../webview-src/components/CodeAgent.tsx'), 'utf8');
  assert.ok(source.includes('<CodeRemovalWarning check={op.removal_check} />'));
  assert.ok(source.indexOf('<CodeRemovalSummary checks={turn.result.ops.map((op) => op.removal_check)} />') < source.indexOf('onClick={() => applyAll(turn)}'));
  assert.ok(source.includes('<button onClick={() => showDiff(turn, op)} style={ghostBtn}>변경 보기</button>'));
});

test('SidebarProvider forwards real generation metadata to the requesting webview', async () => {
  const Module = require('node:module');
  const path = require('node:path');
  const originalResolve = Module._resolveFilename;
  Module._resolveFilename = function (request, ...args) {
    return request === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : originalResolve.call(this, request, ...args);
  };
  try {
    const { SidebarProvider } = require('../out/sidebar/SidebarProvider.js');
    const vscode = require('vscode');
    const op = { file: 'app.js', action: 'edit', content: '', removal_check: report };
    const api = { generateCode: async () => ({ summary: '', model: 'fixture', ops: [op] }) };
    const core = { onCoreRestart: () => ({ dispose() {} }) };
    const provider = new SidebarProvider(vscode.Uri.file('/tmp/extension'), api, core, {});
    const messages = [];
    const webview = { postMessage: (message) => { messages.push(message); return true; } };
    await provider.handleMessage({ type: 'code.generate', payload: { instruction: 'fixture', requestId: 9, decisions: [] } }, webview);
    const result = messages.find((message) => message.type === 'code.result');
    assert.ok(result, JSON.stringify(messages));
    assert.equal(result.payload.requestId, 9);
    assert.deepEqual(result.payload.ops[0].removal_check, report);
    assert.ok(render(CodeRemovalWarning, { check: result.payload.ops[0].removal_check }).includes('GET /todos'));
  } finally {
    Module._resolveFilename = originalResolve;
  }
});
