const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { ReplayView, historyReducer: reduce, initialHistory, filterHistory } = require('../out/webview-test/components/Replay.js');
const entry = (override = {}) => ({ key: 'ecs:one', source: 'ecs', deployment_id: 'one', project_id: 'sample', target: 'cluster / service', region: 'ap-northeast-2', status: 'failed', status_text: '실패', started_at: '2026-09-23T00:00:00Z', image: 'registry/sample:v2', service_url: 'https://example.test', error: 'Health Check 실패', remedy: '이전 버전을 확인하세요', warnings: ['태스크 1개가 실행 중입니다'], events: [{ at: '', title: '롤백 승인 대기', detail: 'task:1' }], rollback: { available: true, target: 'task:1', reason: '', approval_level: 3 }, ...override });
const data = (...entries) => ({ entries, total: entries.length, warnings: [] });
const loaded = (entries = [entry()]) => reduce(reduce(initialHistory, { type: 'load', requestId: 'one' }), { type: 'loaded', requestId: 'one', data: data(...entries) });
const render = (state, overrides = {}) => renderToStaticMarkup(React.createElement(ReplayView, { state, source: 'all', search: '', onRefresh() {}, onSource() {}, onSearch() {}, onSelect() {}, onReview() {}, onConfirm() {}, ...overrides }));

test('history renders recorded facts without manual deploy ID or imagined timestamps', () => {
  const html = render(loaded());
  for (const text of ['cluster / service', 'Health Check 실패', '이전 버전을 확인하세요', '기록 당시의 안내', '시각 미기록', '롤백 승인 대기', '롤백 대상 확인']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('Deploy ID 입력'));
  assert.ok(!html.includes('승인하고 롤백'));
  assert.ok(html.includes('href="https://example.test/"'));
});

test('review step names affected service and target before offering explicit approval', () => {
  const state = reduce(loaded(), { type: 'review', key: 'ecs:one' });
  const html = render(state);
  for (const text of ['롤백 실행 확인', 'ECS 롤백 승인 · Level 3', '승인하고 롤백', '취소', 'task:1', '서비스 안정화 상태']) assert.ok(html.includes(text));
  assert.equal(state.busyKey, '');
  assert.equal(reduce(state, { type: 'review', key: '' }).review, '');
});

test('without review there is no transition to executing rollback', () => {
  assert.equal(reduce(loaded(), { type: 'rollback', key: 'ecs:one', requestId: 'rollback' }).busyKey, '');
  const unavailable = loaded([entry({ rollback: { available: false, reason: '이미 롤백했습니다', target: 'task:1', approval_level: 3 } })]);
  const html = render(unavailable);
  assert.ok(html.includes('이미 롤백했습니다'));
  assert.ok(!html.includes('롤백 대상 확인'));
});

test('duplicate rollback is blocked and only matching response can unlock it', () => {
  let state = reduce(loaded(), { type: 'review', key: 'ecs:one' });
  state = reduce(state, { type: 'rollback', key: 'ecs:one', requestId: 'rollback' });
  assert.ok(render(state).includes('롤백 처리 중…'));
  assert.equal(reduce(state, { type: 'rollback', key: 'ecs:one', requestId: 'second' }), state);
  assert.equal(reduce(state, { type: 'result', requestId: 'older', message: 'old', ok: true }), state);
  state = reduce(state, { type: 'result', requestId: 'rollback', message: '이미지가 변경되어 차단했습니다', ok: false });
  assert.equal(state.busyKey, '');
  state = reduce(state, { type: 'load', requestId: 'refresh' });
  state = reduce(state, { type: 'loaded', requestId: 'refresh', data: data(entry()) });
  assert.ok(render(state).includes('이미지가 변경되어 차단했습니다'));
});

test('late and failed refreshes do not remove recorded entries or selection', () => {
  const second = entry({ key: 'ecs:two', deployment_id: 'two' });
  let state = reduce(loaded([entry(), second]), { type: 'select', key: second.key });
  state = reduce(state, { type: 'load', requestId: 'fresh' });
  assert.equal(reduce(state, { type: 'loaded', requestId: 'old', data: data() }), state);
  state = reduce(state, { type: 'error', requestId: 'fresh', message: 'Core 연결 끊김' });
  assert.equal(state.selected, second.key);
  assert.ok(render(state).includes('마지막 조회 결과를 유지합니다'));
  assert.ok(render(state).includes('Core 연결 끊김'));
});

test('empty, filtered and invalid URL states remain truthful', () => {
  assert.ok(render(loaded([])).includes('저장된 배포 기록이 없습니다'));
  assert.ok(render(loaded(), { source: 'local' }).includes('조건에 맞는 배포 기록이 없습니다'));
  assert.equal(filterHistory([entry(), entry({ source: 'local', key: 'local:2', target: 'other' })], 'ecs', 'SERVICE').length, 1);
  assert.ok(!render(loaded([entry({ service_url: 'javascript:alert(1)' })])).includes('href='));
});

test('host requests real history, preserves errors, and refuses unapproved rollback', async () => {
  const Module = require('node:module');
  const path = require('node:path');
  const originalResolve = Module._resolveFilename;
  Module._resolveFilename = function(request, ...args) { return request === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : originalResolve.call(this, request, ...args); };
  try {
    const vscode = require('vscode');
    const { ApiClient } = require('../out/core/ApiClient.js');
    const { SidebarProvider } = require('../out/sidebar/SidebarProvider.js');
    const api = new ApiClient({});
    const calls = [];
    api.request = async (...args) => { calls.push(args); return { success: true, data: data(entry()) }; };
    const provider = new SidebarProvider(vscode.Uri.file('/tmp/extension'), api, {}, {});
    const messages = [];
    const webview = { postMessage(message) { messages.push(message); return true; } };
    await provider.handleMessage({ type: 'replay.history', payload: { requestId: 'one' } }, webview);
    assert.equal(calls[0][1], '/api/deploy/history');
    assert.equal(messages.at(-1).type, 'replay.historyResult');
    assert.equal(messages.at(-1).payload.requestId, 'one');
    await provider.handleMessage({ type: 'replay.rollback', payload: { source: 'ecs', deploymentId: 'one', approved: false } }, webview);
    assert.equal(calls.length, 1);
    assert.equal(messages.at(-1).type, 'replay.rollbackError');
    api.request = async (...args) => { calls.push(args); return { success: true, data: { status: 'completed', message: '롤백 요청 완료', adr: { file: 'docs/adr/test.md', content: 'test' } } }; };
    provider.writeWorkspaceFile = async () => { throw new Error('disk full'); };
    await provider.handleMessage({ type: 'replay.rollback', payload: { source: 'ecs', deploymentId: 'id/with slash', approved: true, requestId: 'two' } }, webview);
    assert.equal(calls.at(-1)[1], '/api/deploy/history/ecs/id%2Fwith%20slash/rollback');
    assert.deepEqual(calls.at(-1)[2], { approved: true });
    assert.equal(messages.at(-1).type, 'replay.rollbackResult');
    assert.equal(messages.at(-1).payload.status, 'completed');
    assert.ok(messages.at(-1).payload.auditWarning.includes('결정 기록'));
    api.request = async () => ({ success: false, error: 'Core 연결 끊김' });
    await provider.handleMessage({ type: 'replay.history', payload: { requestId: 'three' } }, webview);
    assert.equal(messages.at(-1).type, 'replay.historyError');
    assert.equal(messages.at(-1).payload.message, 'Core 연결 끊김');
  } finally { Module._resolveFilename = originalResolve; }
});
