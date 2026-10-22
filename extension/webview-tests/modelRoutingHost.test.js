// 2.0.36 확장 호스트: 작업별 AI 공급자 메시지 — 키 값은 화면에 보내지 않고, 비교는 비용 확인을 받은 뒤에만 시작한다.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const Module = require('node:module');

const resolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...args) {
  return request === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : resolve.call(this, request, ...args);
};
const vscode = require('vscode');
const { SidebarProvider } = require('../out/sidebar/SidebarProvider.js');
Module._resolveFilename = resolve;

const KEY = 'sk-' + 'k'.repeat(40);

function fakeApi(calls) {
  return {
    getModelRouting: async () => ({ quality: { provider: 'anthropic', model: 'm' }, economy: { spec: 'quality', provider: 'anthropic', model: 'm', separate: false, scrubbed: false, note: '' }, economy_operations: [], choices: [] }),
    getModelCosts: async (days) => { calls.push(['costs', days]); return { calls: 0, total_cost_usd: 0, unknown_cost_calls: 0, economy_saved_usd: 0, groups: [], days, persisted: true }; },
    getModelCompare: async (id) => { calls.push(['get', id]); return id ? { id, status: 'running', sides: [] } : null; },
    startModelCompare: async (...args) => { calls.push(['start', ...args]); return { id: 'abcdef123456', status: 'running', sides: [] }; },
  };
}

async function send(sidebar, type, payload) {
  const sent = [];
  const original = sidebar.postMessage.bind(sidebar);
  sidebar.postMessage = (t, p) => { sent.push({ type: t, payload: p }); };
  try { await sidebar.handleMessage({ type, payload }); } finally { sidebar.postMessage = original; }
  return sent;
}

function secrets() {
  const m = new Map([['recoder.ai.compatibleKey', KEY]]);
  return { get: async (k) => m.get(k), store: async (k, v) => { m.set(k, v); }, delete: async (k) => { m.delete(k); }, onDidChange: () => ({ dispose() {} }) };
}

test('models.load sends routing, costs and which keys exist — never the key itself', async () => {
  const calls = [];
  const state = new Map();
  const sidebar = new SidebarProvider(vscode.Uri.file('/tmp'), fakeApi(calls), {}, {}, undefined,
    { get: (k, d) => (state.has(k) ? state.get(k) : d), update: async (k, v) => { state.set(k, v); }, keys: () => [] }, secrets());
  const sent = await send(sidebar, 'models.load', {});
  const msg = sent.find((m) => m.type === 'models.state');
  assert.ok(msg, JSON.stringify(sent));
  assert.deepEqual(msg.payload.keys, { anthropic: false, openai: false, compatible: true });
  assert.ok(!JSON.stringify(msg.payload).includes(KEY));
  assert.deepEqual(calls[0], ['costs', 30]);
});

test('models.compare.start needs the cost confirmation; poll ignores bad ids', async () => {
  const calls = [];
  const sidebar = new SidebarProvider(vscode.Uri.file('/tmp'), fakeApi(calls), {}, {});
  const original = vscode.window.showWarningMessage;
  try {
    let asked = '';
    vscode.window.showWarningMessage = async (text, opts) => { asked = text; assert.equal(opts.modal, true); return undefined; };
    let sent = await send(sidebar, 'models.compare.start', { instruction: '할 일 앱', providers: ['quality', 'economy'] });
    assert.match(asked, /두 번/);
    assert.ok(!calls.some((c) => c[0] === 'start'), '확인 없이 시작하지 않는다');
    assert.ok(sent.some((m) => m.type === 'models.compareError'));

    vscode.window.showWarningMessage = async () => '비교 시작';
    sent = await send(sidebar, 'models.compare.start', { instruction: '  할 일 앱  ', providers: ['quality', 'economy'] });
    const start = calls.find((c) => c[0] === 'start');
    assert.equal(start[1], '할 일 앱');
    assert.deepEqual(start[3], ['quality', 'economy']);
    assert.ok(sent.some((m) => m.type === 'models.compare' && m.payload.id === 'abcdef123456'));

    sent = await send(sidebar, 'models.compare.start', { instruction: '', providers: ['quality'] });
    assert.ok(sent.some((m) => m.type === 'models.compareError' && m.payload.message));

    await send(sidebar, 'models.compare.poll', { id: '../../etc' });
    assert.ok(!calls.some((c) => c[0] === 'get' && c[1] === '../../etc'));
    sent = await send(sidebar, 'models.compare.poll', { id: 'abcdef123456' });
    assert.ok(sent.some((m) => m.type === 'models.compare'));
  } finally { vscode.window.showWarningMessage = original; }
});
