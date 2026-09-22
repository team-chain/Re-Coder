const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { EcsDeploymentProgress, initialEcsProgress, ecsProgressReducer: reduce, ecsProgressBusy, serviceLink } = require('../out/webview-test/components/EcsDeploymentProgress.js');

const stage = (key, label, status) => ({ key, label, status });
const status = (overrides = {}) => ({
  deployment_id: 'test-id', running: true, stage: 'deploying', stage_text: 'ECR 업로드 중',
  observed_at: '2026-09-23T00:00:10Z', log_tail: [], error: '',
  steps: [stage('building', '이미지 빌드', 'done'), stage('ecr_push', 'ECR 업로드', 'running'), stage('image_scan', '이미지 검사', 'pending')],
  ...overrides,
});
const render = (state) => renderToStaticMarkup(React.createElement(EcsDeploymentProgress, { state }));

test('in-progress render distinguishes completed, running and waiting steps', () => {
  const html = render({ ...initialEcsProgress, status: status() });
  for (const text of ['ECR 업로드 중', '이미지 빌드', '진행 중', '완료', '대기', 'aria-current="step"']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('서비스 URL 열기'));
});

test('successful deployment renders a safe clickable service URL', () => {
  const html = render({ ...initialEcsProgress, status: status({ running: false, stage: 'done', stage_text: '완료', service_url: 'http://example.test:3456', steps: [] }) });
  assert.ok(html.includes('href="http://example.test:3456/"'));
  assert.ok(html.includes('서비스 URL 열기'));
  assert.ok(html.includes('세부 단계 기록이 없습니다'));
  assert.ok(!html.includes('data-status="done"'), 'legacy records must not invent completed stages');
});

test('unsafe URLs and URL credentials never become a clickable service link', () => {
  for (const url of ['javascript:alert(1)', 'data:text/html,x', 'file:///tmp/test', 'https://user:pass@example.test', 'not-a-url']) {
    assert.equal(serviceLink(url), null);
    assert.ok(!render({ ...initialEcsProgress, status: status({ service_url: url }) }).includes('href='));
  }
});

test('failed deployment shows original error, detail, remedy and unreached stages', () => {
  const html = render({ ...initialEcsProgress, status: status({ running: false, stage: 'failed', stage_text: '실패', error: '업로드 실패', error_detail: 'ECR access denied', remedy: 'push 권한 확인', steps: [stage('ecr_push', 'ECR 업로드', 'failed'), stage('task_def', '태스크 정의 등록', 'pending')] }) });
  for (const text of ['업로드 실패', 'ECR access denied', 'push 권한 확인', '미실행', 'role="alert"']) assert.ok(html.includes(text), text);
});

test('warnings remain visible even on success without expanding deployment logs', () => {
  const html = render({ ...initialEcsProgress, status: status({ running: false, stage: 'done', stage_text: '완료', warnings: ['URL 접속 미확인'], steps: [stage('url_check', '공개 주소 확인', 'warning')] }) });
  assert.ok(html.includes('URL 접속 미확인'));
  assert.ok(html.includes('확인 필요'));
  assert.ok(html.includes('접속 URL이 없습니다'));
});

test('request failure survives repeated idle/success status polls', () => {
  let state = reduce(initialEcsProgress, { type: 'start' });
  state = reduce(state, { type: 'rejected', message: '새 요청의 권한이 없습니다' });
  for (let i = 0; i < 3; i++) state = reduce(state, { type: 'status', status: status({ stage: 'done', running: false, error: '' }) });
  assert.equal(state.requestError, '새 요청의 권한이 없습니다');
  assert.ok(render(state).includes('새 요청의 권한이 없습니다'));
  state = reduce(state, { type: 'start' });
  assert.equal(state.requestError, '');
});

test('poll failure retains last observed deployment and clears only when polling recovers', () => {
  let state = reduce(initialEcsProgress, { type: 'status', status: status({ error: 'health failure' }) });
  state = reduce(state, { type: 'pollError', message: 'Core 연결 끊김' });
  assert.ok(render(state).includes('마지막 확인 결과'));
  assert.ok(render(state).includes('health failure'));
  assert.ok(render(state).includes('Core 연결 끊김'));
  state = reduce(state, { type: 'status', status: status({ error: 'health failure' }) });
  assert.equal(state.pollError, '');
  assert.equal(state.status.error, 'health failure');
});

test('new deployment ignores an older deployment response and stays busy until its own status arrives', () => {
  let state = reduce(initialEcsProgress, { type: 'start' });
  assert.equal(reduce(state, { type: 'status', status: status() }).status, null);
  state = reduce(state, { type: 'accepted', deploymentId: 'new-id' });
  state = reduce(state, { type: 'status', status: status() });
  assert.equal(state.status, null);
  assert.equal(ecsProgressBusy(state), true);
  state = reduce(state, { type: 'status', status: status({ deployment_id: 'new-id', running: false, stage: 'done' }) });
  assert.equal(ecsProgressBusy(state), false);
});

test('out-of-order polling cannot turn a finished deployment back into running', () => {
  let state = reduce(initialEcsProgress, { type: 'status', status: status({ running: false, stage: 'failed', finished_at: '2026-09-23T00:00:10Z', error: 'specific failure' }) });
  state = reduce(state, { type: 'status', status: status({ observed_at: '2026-09-23T00:00:09Z' }) });
  assert.equal(state.status.error, 'specific failure');
  state = reduce(state, { type: 'status', status: status({ observed_at: '2026-09-23T00:00:11Z' }) });
  assert.equal(state.status.stage, 'failed');
});

test('empty deployment history and skipped steps are explicit', () => {
  let html = render({ ...initialEcsProgress, status: status({ running: false, deployment_id: '', stage: 'idle', steps: [] }) });
  assert.ok(html.includes('아직 ECS 배포 기록이 없습니다'));
  html = render({ ...initialEcsProgress, status: status({ steps: [stage('building', '이미지 빌드', 'skipped')] }) });
  assert.ok(html.includes('생략'));
});

test('host forwards structured status to requester without overwriting deployment messages', async () => {
  const Module = require('node:module');
  const path = require('node:path');
  const originalResolve = Module._resolveFilename;
  Module._resolveFilename = function (request, ...args) { return request === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : originalResolve.call(this, request, ...args); };
  try {
    const vscode = require('vscode');
    const { ApiClient } = require('../out/core/ApiClient.js');
    const { SidebarProvider } = require('../out/sidebar/SidebarProvider.js');
    const paths = [];
    const api = new ApiClient({});
    const data = status({ service_url: 'https://example.test', error_detail: 'reason' });
    api.request = async (method, url) => { paths.push(url); return { success: true, data }; };
    const provider = new SidebarProvider(vscode.Uri.file('/tmp/extension'), api, {}, {});
    const messages = [];
    const webview = { postMessage: (message) => { messages.push(message); return true; } };
    await provider.handleMessage({ type: 'workspace.deploy.ecs.status', payload: { deploymentId: 'test-id', reportProgress: true } }, webview);
    assert.deepEqual(paths, ['/api/deploy/ecs/status?deployment_id=test-id']);
    assert.deepEqual(messages.map(m => m.type), ['workspace.deploy.ecs.statusResult']);
    assert.deepEqual(messages[0].payload, data);
    api.request = async () => { throw new Error('poll unavailable'); };
    await provider.handleMessage({ type: 'workspace.deploy.ecs.status', payload: { deploymentId: 'test-id' } }, webview);
    assert.equal(messages.at(-1).type, 'workspace.deploy.ecs.statusError');
    assert.equal(messages.at(-1).payload.deploymentId, 'test-id');
    await provider.handleMessage({ type: 'workspace.deploy.ecs', payload: {} }, webview);
    assert.equal(messages.at(-1).type, 'workspace.deploy.ecs.error');
  } finally { Module._resolveFilename = originalResolve; }
});

test('DeploymentCenter connects the progress reducer, specific errors and rendered card', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const source = fs.readFileSync(path.join(__dirname, '../webview-src/components/DeploymentCenter.tsx'), 'utf8');
  assert.ok(source.includes('<EcsDeploymentProgress state={ecsProgress} />'));
  assert.ok(source.includes('dispatchEcsProgress({ type: "status", status })'));
  assert.ok(source.includes('workspace.deploy.ecs.error'));
  assert.ok(source.includes('workspace.deploy.ecs.statusError'));
  assert.ok(source.includes('disabled={checkingEcsPermissions || ecsBusy}'));
});
