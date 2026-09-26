const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const Module = require('node:module');
const resolve = Module._resolveFilename;
Module._resolveFilename = function (name, ...args) { return name === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : resolve.call(this, name, ...args); };
const vscode = require('vscode');
const { SidebarProvider } = require('../out/sidebar/SidebarProvider');
Module._resolveFilename = resolve;
const delay = ms => new Promise(r => setTimeout(r, ms));

test('첫 연결이 실패하면 "확인 중"을 풀고 원인을 보여 준 뒤 다시 시도한다', async t => {
  //: 실기기: 연결 실패 뒤 pending 이 남아 "AI 연결을 확인하고 있습니다"에 멈추고 아무도 재시도하지 않았다.
  const messages = [];
  let attempts = 0;
  const core = {
    ensureRunning: async () => { attempts++; throw new Error('실행 중인 Core의 인증 정보를 읽지 못했습니다.'); },
    refreshToken: async () => false, coreInstanceKey: () => '17894:tok',
  };
  const polling = { start() {}, stop() {}, getLastHealth: () => null, poll: async () => null };
  const p = new SidebarProvider(vscode.Uri.file(path.join(__dirname, '..')), {}, core, polling);
  p.postMessage = (type, payload) => messages.push({ type, payload });
  t.after(() => clearTimeout(p._connectRetryTimer));
  await p.handleMessage({ type: 'webview.ready', payload: { layout: 'workspace' } });
  await delay(50);
  const diag = messages.filter(m => m.type === 'diagnostics.status');
  assert.equal(diag.at(-1).payload.pending, false);
  assert.match(diag.at(-1).payload.error, /인증 정보/);
  assert.ok(messages.some(m => m.type === 'core.error'));
  assert.ok(p._connectRetryTimer, '다시 시도가 예약된다');
  //: 웹뷰를 다시 열어도 pending 으로 되돌아가지 않는다.
  messages.length = 0;
  await p.handleMessage({ type: 'webview.ready', payload: { layout: 'workspace' } });
  await delay(20);
  assert.equal(messages.find(m => m.type === 'diagnostics.status').payload.pending, false);
  assert.ok(attempts >= 2);
});
