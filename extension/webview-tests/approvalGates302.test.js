// 3.0.2 — AWS·GitHub·운영 서비스를 바꾸는 요청은 화면의 확인 단계를 거친 것만 approved:true 로 코어에 간다.
// 실제 SidebarProvider / WorkbenchHost → 실제 ApiClient → 루프백 HTTP 로 확인한다.
const test = require('node:test'), assert = require('node:assert/strict');
const path = require('node:path'), http = require('node:http'), fs = require('node:fs'), os = require('node:os'), Module = require('node:module');
const resolve = Module._resolveFilename;
Module._resolveFilename = function (name, ...args) { return name === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : resolve.call(this, name, ...args); };
const vscode = require('vscode');
const { SidebarProvider } = require('../out/sidebar/SidebarProvider.js');
const { ApiClient } = require('../out/core/ApiClient.js');
const { WorkbenchHost, pushConfirmText, describeLocalPlan } = require('../out/sidebar/workbenchHost.js');
Module._resolveFilename = resolve;
const React = require('react'); const { renderToStaticMarkup } = require('react-dom/server');
const { EcsDeployConfirm, S3DeployConfirm, ActionsSaveNotice } = require('../out/webview-test/components/DeploymentCenter');
const { CleanupGroup } = require('../out/webview-test/components/AwsCleanupPanel');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');
const NO_EMOJI = /[\u{1F300}-\u{1FAFF}☀-⛿✅]/u;

/** 루프백 코어. routes[path] 가 함수면 (body, query) → [status, json]. */
async function fakeCore(routes = {}) {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    let raw = ''; for await (const ch of req) raw += ch;
    const url = new URL(req.url, 'http://x');
    const body = raw ? JSON.parse(raw) : null;
    const query = Object.fromEntries(url.searchParams);
    requests.push({ method: req.method, path: url.pathname, query, body });
    res.setHeader('Content-Type', 'application/json');
    const route = routes[url.pathname];
    const [status, json] = typeof route === 'function' ? route(body, query) : route ? [200, route] : [404, { detail: 'not found' }];
    res.statusCode = status; res.end(JSON.stringify(json));
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { server, requests, close: () => new Promise((r) => server.close(r)) };
}

function withWindow(answers, fn) {
  //: answers: 확인 창마다 돌려줄 버튼(문자열이면 그 버튼, null 이면 닫음). 물어본 문구·옵션을 남긴다.
  const original = vscode.window.showWarningMessage;
  const asked = [];
  vscode.window.showWarningMessage = async (text, opts, ...items) => {
    asked.push({ text, modal: !!(opts && opts.modal), items });
    const answer = answers.length ? answers.shift() : null;
    return answer === true ? items[0] : answer ?? undefined;
  };
  return Promise.resolve(fn(asked)).finally(() => { vscode.window.showWarningMessage = original; });
}

async function sidebar(routes, extraCore = {}) {
  const core = await fakeCore(routes);
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-302-'));
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(workspace), name: 'app', index: 0 }];
  const manager = { getPort: () => core.server.address().port, getSessionToken: () => 't', refreshToken: async () => true, ...extraCore };
  const provider = new SidebarProvider(vscode.Uri.file(workspace), new ApiClient(manager), manager, {}, undefined, undefined);
  const msgs = []; const webview = { postMessage: async (m) => { msgs.push(m); return true; } };
  provider._view = { webview };
  const send = (type, payload = {}) => provider.handleMessage({ type, payload }, webview);
  const last = (t) => msgs.findLast((m) => m.type === t)?.payload;
  return { core, provider, msgs, webview, send, last, workspace, manager };
}

// ── 7. 상세 배포 · ECS / S3 ─────────────────────────────────────────────

test('3.0.2 상세 배포 ECS: 승인 없는 요청은 코어로 가지 않고, 승인하면 approved:true 로 간다', async () => {
  const s = await sidebar({ '/api/deploy/ecs': { status: 'ok', message: '시작', deployment_id: 'd1' } });
  try {
    await s.send('workspace.deploy.ecs', { ecs_cluster: 'c', ecs_service: 'svc', aws_region: 'us-east-1' });
    assert.equal(s.core.requests.length, 0, '승인 없는 배포가 코어로 갔다');
    assert.match(s.last('workspace.deploy.ecs.error').message, /승인/);
    await s.send('workspace.deploy.ecs', { ecs_cluster: 'c', ecs_service: 'svc', aws_region: 'us-east-1', approved: 'true' });
    assert.equal(s.core.requests.length, 0, '문자열 "true" 를 승인으로 받았다');
    await s.send('workspace.deploy.ecs', { ecs_cluster: 'c', ecs_service: 'svc', aws_region: 'us-east-1', approved: true });
    const sent = s.core.requests.find((r) => r.path === '/api/deploy/ecs');
    assert.equal(sent.body.approved, true);
    assert.equal(s.last('workspace.deploy.result').deployment_id, 'd1');
  } finally { await s.core.close(); }
});

test('3.0.2 상세 배포 S3: 승인 없는 요청은 파일을 읽지도 코어로 보내지도 않는다', async () => {
  const s = await sidebar({});
  try {
    fs.writeFileSync(path.join(s.workspace, 'index.html'), '<h1>x</h1>');
    await s.send('workspace.deploy.s3', { dir: '', region: 'us-east-1' });
    assert.equal(s.core.requests.length, 0);
    const result = s.last('workspace.deploy.s3.result');
    assert.equal(result.ok, false); assert.match(result.message, /승인/);
  } finally { await s.core.close(); }
});

test('3.0.2 상세 배포 화면: 권한 확인 뒤 바로 배포하지 않고 확인 단계를 거쳐 approved:true 를 싣는다', () => {
  const src = read('../webview-src/components/DeploymentCenter.tsx');
  const permission = src.slice(src.indexOf('type === "aws.permissions.result"'), src.indexOf('type === "workspace.deploy.remediationResult"'));
  assert.doesNotMatch(permission, /postMessage\("workspace\.deploy\.ecs"/, '권한 확인만으로 배포를 보낸다');
  assert.match(permission, /setEcsConfirm\(deploymentRequest\)/);
  assert.match(src, /postMessage\("workspace\.deploy\.ecs", \{ \.\.\.request, approved: true \}\)/);
  assert.match(src, /postMessage\("workspace\.deploy\.s3", \{ dir: s3Dir\.trim\(\), region: coreRegion\.trim\(\), approved: true \}\)/);
  //: S3 버튼은 확인 단계를 연다(직접 배포하지 않는다).
  assert.match(src, /onClick=\{\(\) => \{ setS3Result\(null\); setMessage\(""\); setS3Confirm\(true\); \}\}/);
});

test('3.0.2 확인 단계 화면: 무엇이 어디로 가는지 · 요금 · 공개 범위를 보여 준다', () => {
  let approved = 0;
  const ecs = renderToStaticMarkup(React.createElement(EcsDeployConfirm, {
    request: { aws_region: 'us-east-1', ecs_cluster: 'shop', ecs_service: 'web', image_name: 'app', tag: 'v3', cpu: '512', memory: '1024', environment: 'production' },
    onApprove: () => approved++, onCancel: () => {},
  }));
  assert.match(ecs, /data-testid="ecs-deploy-confirm"/);
  assert.match(ecs, /us-east-1/); assert.match(ecs, /shop\/web/); assert.match(ecs, /app:v3/); assert.match(ecs, /요금/);
  assert.match(ecs, /승인하고 배포/); assert.match(ecs, /취소/);
  const s3 = renderToStaticMarkup(React.createElement(S3DeployConfirm, { dir: 'dist', region: 'ap-northeast-2', onApprove() {}, onCancel() {} }));
  assert.match(s3, /dist/); assert.match(s3, /ap-northeast-2/); assert.match(s3, /누구나 볼 수 있습니다/);
  for (const html of [ecs, s3]) { assert.doesNotMatch(html, NO_EMOJI); assert.doesNotMatch(html, /어요|해요/); }
});

// ── 9. 정리 「중지」 ───────────────────────────────────────────────────

test('3.0.2 정리 중지: 확인 창(modal)을 거치고, 취소하면 코어로 보내지 않는다', async () => {
  const s = await sidebar({
    '/api/aws/cleanup/stop': { ok: true, message: 'web 의 태스크 수를 0 으로 내렸습니다' },
    '/api/aws/cleanup/plan': { region: 'us-east-1', groups: [], untagged: [], summary: {}, gaps: [] },
  });
  try {
    await withWindow([null, true], async (asked) => {
      await s.send('aws.cleanup.stop', { region: 'us-east-1', cluster: 'recoder-cluster', service: 'web' });
      assert.equal(asked.length, 1); assert.equal(asked[0].modal, true); assert.match(asked[0].text, /recoder-cluster\/web/);
      assert.equal(s.core.requests.length, 0, '취소했는데 중지 요청이 갔다');
      assert.equal(s.last('aws.cleanup.stopResult').cancelled, true);
      await s.send('aws.cleanup.stop', { region: 'us-east-1', cluster: 'recoder-cluster', service: 'web' });
      const stop = s.core.requests.find((r) => r.path === '/api/aws/cleanup/stop');
      assert.deepEqual(stop.body, { region: 'us-east-1', cluster: 'recoder-cluster', service: 'web', approved: true });
      assert.match(s.last('aws.cleanup.stopResult').message, /0 으로/);
    });
  } finally { await s.core.close(); }
});

test('3.0.2 정리 중지 버튼: 요청이 도는 동안 막힌다', () => {
  const group = { service: 'web', items: [{ id: 'ecs_service:c/web', kind: 'ecs_service', label: 'ECS 서비스', name: 'web', service: 'web', cluster: 'c', tagged: true, deletable: true, detail: '', cost: '', note: '' }] };
  const idle = renderToStaticMarkup(React.createElement(CleanupGroup, { group, picked: {}, onPick() {}, onStop() {}, disabled: false }));
  assert.doesNotMatch(idle.match(/<button[^>]*data-testid="cleanup-stop"[^>]*>/)[0], /disabled/);
  const busy = renderToStaticMarkup(React.createElement(CleanupGroup, { group, picked: {}, onPick() {}, onStop() {}, disabled: false, stopping: 'ecs_service:c/web' }));
  assert.match(busy, /<button[^>]*disabled=""[^>]*data-testid="cleanup-stop"[^>]*>중지 중…/);
});

// ── 2. IAM 역할 · 감시 설정 ─────────────────────────────────────────────

test('3.0.2 배포 전용 역할: 확인 창에서 승인한 것만 approved:true 로 IAM 을 쓴다', async () => {
  const s = await sidebar({ '/api/aws/role/setup': { ok: false, mode: 'console_fallback', message: '권한 부족' } });
  try {
    await withWindow([null, true], async (asked) => {
      await s.send('aws.role.setup', {});
      assert.equal(asked[0].modal, true); assert.match(asked[0].text, /IAM 역할/);
      assert.equal(s.core.requests.length, 0);
      assert.equal(s.last('aws.role.result').mode, 'cancelled');
      await s.send('aws.role.setup', {});
      assert.equal(s.core.requests.find((r) => r.path === '/api/aws/role/setup').body.approved, true);
    });
  } finally { await s.core.close(); }
});

test('3.0.2 감시 알림 설정: 화면의 [저장] 이 확인이고 approved:true 로 보낸다', async () => {
  const s = await sidebar({
    '/api/ops/watch/settings': { installed: true, message: '저장했습니다' },
    '/api/ops/watch/overview': { monitors: [], current: null },
  });
  try {
    await s.send('monitor.watch.settings', { key: 'r/c/app', discord: '', email: 'a@b.co' });
    const sent = s.core.requests.find((r) => r.path === '/api/ops/watch/settings');
    assert.deepEqual(sent.body, { key: 'r/c/app', discord: '', email: 'a@b.co', approved: true });
  } finally { await s.core.close(); }
});

// ── 10. 배포 준비 점검 ─────────────────────────────────────────────────

test('3.0.2 deploy.readiness.check 는 DB 선택이 아니라 준비 점검으로 간다', async () => {
  const s = await sidebar({ '/api/deploy/readiness': { ready: true, issues: [] } });
  try {
    await s.send('deploy.readiness.check', { requestId: 'r1' });
    assert.deepEqual(s.core.requests.map((r) => r.path), ['/api/deploy/readiness']);
    assert.equal(s.last('deploy.db.error'), undefined, '점검 요청이 DB 선택 오류가 됐다');
    assert.deepEqual(s.last('deploy.readiness.result'), { requestId: 'r1', readiness: { ready: true, issues: [] } });
  } finally { await s.core.close(); }
});

// ── 12. GitHub Actions 저장 ───────────────────────────────────────────

test('3.0.2 Actions 저장: 결과(이미 있음)를 화면에 돌려주고, 덮어쓰기를 확인하면 overwrite 로 다시 저장한다', async () => {
  let calls = 0;
  const s = await sidebar({
    '/api/deploy/github-actions/approve': (_b, q) => (++calls, q.overwrite === 'true'
      ? [200, { status: 'saved', path: '/w/.github/workflows/deploy.yml', overwritten: true, backup_path: '/w/.github/workflows/deploy.yml.recoder-prev' }]
      : [200, { status: 'exists', path: '/w/.github/workflows/deploy.yml', diff: '-old\n+new' }]),
  });
  try {
    await s.send('approveGithubActions', { proposalId: 'p1', approved: true });
    assert.equal(s.core.requests[0].query.overwrite, 'false');
    assert.equal(s.last('githubActions.saveResult').status, 'exists');
    assert.match(s.last('githubActions.saveResult').diff, /\+new/);
    await s.send('approveGithubActions', { proposalId: 'p1', approved: true, overwrite: true });
    assert.equal(s.core.requests[1].query.overwrite, 'true');
    assert.equal(s.last('githubActions.saveResult').status, 'saved');
  } finally { await s.core.close(); }
  const exists = renderToStaticMarkup(React.createElement(ActionsSaveNotice, { result: { status: 'exists', path: 'deploy.yml', diff: '+new' }, onOverwrite() {}, onKeep() {} }));
  assert.match(exists, /data-testid="actions-exists"/); assert.match(exists, /기존 파일 덮어쓰기/); assert.match(exists, /\.recoder-prev/);
  const saved = renderToStaticMarkup(React.createElement(ActionsSaveNotice, { result: { status: 'saved', path: 'deploy.yml' }, onOverwrite() {}, onKeep() {} }));
  assert.match(saved, /data-testid="actions-saved"/); assert.match(saved, /저장했습니다/);
  const failed = renderToStaticMarkup(React.createElement(ActionsSaveNotice, { result: { status: 'error' }, onOverwrite() {}, onKeep() {} }));
  assert.match(failed, /저장하지 못했습니다/);
  for (const html of [exists, saved, failed]) { assert.doesNotMatch(html, NO_EMOJI); }
  const src = read('../webview-src/components/DeploymentCenter.tsx');
  assert.match(src, /type === "githubActions\.saveResult"/);
  assert.match(src, /postMessage\("approveGithubActions", \{ proposalId: proposal\.proposal_id, approved: true, overwrite \}\)/);
});

// ── 14. AWS 연결 해제 ─────────────────────────────────────────────────

test('3.0.2 AWS 연결 해제: Core 에서 해제되면 공유 Core 를 재시작하지 않는다', async () => {
  let restarts = 0, cleared = 0;
  const extra = { clearAwsCredentials: async () => { cleared++; }, restart: async () => { restarts++; } };
  const ok = await sidebar({ '/api/aws/clear': { status: 'cleared' }, '/api/aws/status': { ready: false } }, extra);
  try {
    await ok.send('aws.clear');
    assert.equal(cleared, 1); assert.equal(restarts, 0, 'Core 해제가 됐는데 재시작했다');
    assert.equal(ok.last('aws.clear.result').ok, true); assert.equal(ok.last('aws.clear.result').restarted, false);
  } finally { await ok.core.close(); }
  const down = await sidebar({ '/api/aws/clear': () => [500, { detail: 'boom' }] }, extra);
  try {
    await down.send('aws.clear');
    assert.equal(restarts, 1, 'Core 가 해제를 못 했으면 재시작으로 처리해야 한다');
    assert.match(down.last('aws.clear.result').message, /다시 시작했습니다/);
  } finally { await down.core.close(); }
  const ext = read('../src/extension.ts');
  const cmd = ext.slice(ext.indexOf("'recoder.awsClear'"), ext.indexOf("'recoder.setupWizard'"));
  assert.match(cmd, /try \{[\s\S]*clearAwsCredentials[\s\S]*\} catch \(err\)/, 'awsClear 명령이 실패를 잡지 않는다');
  assert.match(cmd, /if \(coreCleared\)[\s\S]*else \{\s*await coreManager\.restart\(\)/);
});

// ── 13. 채팅 「모두 적용」 ───────────────────────────────────────────

test('3.0.2 채팅 모두 적용: 생성 결과의 프로젝트 루트를 함께 보내고, 빠뜨려도 호스트가 그 루트에 쓴다', async () => {
  const chat = read('../webview-src/components/ChatPanel.tsx');
  assert.match(chat, /postMessage\("code\.applyAll", \{ ops, targetFolder: st\.targetFolder \?\? "", projectRoot: st\.projectRoot \?\? "" \}\)/);
  assert.match(chat, /projectRoot: payload\.projectRoot \?\? st\.projectRoot/);
  const a = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-302-a-')), b = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-302-b-'));
  const before = vscode.workspace.workspaceFolders;
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(b), name: 'b', index: 0 }, { uri: vscode.Uri.file(a), name: 'a', index: 1 }];
  try {
    const provider = new SidebarProvider(vscode.Uri.file(b), {}, {}, {});
    provider.postMessage = () => {};
    provider._generatedRoots.set('77', a);       // 77번 생성 요청은 a 에서 만들었다
    assert.equal(provider._generatedRootFor('77:src/x.js'), a);
    assert.equal(provider._generatedRootFor('78:src/x.js'), '');
    await provider.handleMessage({ type: 'code.applyAll', payload: { ops: [{ file: 'hello.txt', content: 'hi', ackKey: '77:hello.txt' }], targetFolder: '' } });
    assert.equal(fs.existsSync(path.join(b, 'hello.txt')), false, '다른(활성) 프로젝트에 썼다');
    assert.equal(fs.readFileSync(path.join(a, 'hello.txt'), 'utf8'), 'hi', '생성한 프로젝트에 쓰지 않았다');
  } finally { vscode.workspace.workspaceFolders = before; }
});

// ── 11. 명령 결과를 ReCoder 창으로 ────────────────────────────────────

test('3.0.2 명령 결과: ReCoder 창이 준비되기 전 결과는 쌓아 두었다가 ready 뒤 그 창으로 보낸다', async () => {
  const provider = new SidebarProvider(vscode.Uri.file(os.tmpdir()), { getAwsStatus: async () => ({ ready: false }) }, {}, { getLastHealth: () => null, start() {}, stop() {} });
  provider.connectAndDiagnose = () => {};
  const sidebarMsgs = [], panelMsgs = [];
  provider._view = { webview: { postMessage: async (m) => { sidebarMsgs.push(m); return true; } } };
  const panel = { postMessage: async (m) => { panelMsgs.push(m); return true; }, onDidReceiveMessage() {} };
  const delay = SidebarProvider.PANEL_FLUSH_DELAY_MS; SidebarProvider.PANEL_FLUSH_DELAY_MS = 0;
  try {
    provider.expectPanelResults(['build.analysis.result']);
    provider.postMessage('build.analysis.result', { id: 1 });          // 창이 아직 없다
    provider.attachWorkspacePanel(panel);                               // 창을 만들었지만 ready 전
    provider.postMessage('build.analysis.result', { id: 2 });
    provider.postMessage('stateUpdate', {});                            // 다른 메시지는 그대로 흘려보낸다
    assert.equal(panelMsgs.filter((m) => m.type === 'build.analysis.result').length, 0);
    await provider.handleMessage({ type: 'webview.ready', payload: {} }, panel);
    await new Promise((r) => setTimeout(r, 10));
    assert.deepEqual(panelMsgs.filter((m) => m.type === 'build.analysis.result').map((m) => m.payload.id), [1, 2]);
    provider.postMessage('build.analysis.result', { id: 3 });           // 준비된 뒤에는 바로
    assert.equal(panelMsgs.filter((m) => m.type === 'build.analysis.result').length, 3);
  } finally { SidebarProvider.PANEL_FLUSH_DELAY_MS = delay; }
  const ext = read('../src/extension.ts');
  for (const cmd of ["'recoder.analyzeError'", "'recoder.generateDockerfile'", "'recoder.generateGithubActions'", "'recoder.runWithRecoder'"]) {
    const start = ext.indexOf(cmd);
    const body = ext.slice(start, ext.indexOf('registerCommand(', start + cmd.length));
    assert.match(body, /openPanelResult\('(build|ship)'/, `${cmd} 결과가 ReCoder 창으로 가지 않는다`);
    assert.doesNotMatch(body, /recoder\.sidebarView\.focus/, `${cmd} 가 결과를 보여 주지 않는 사이드바로 간다`);
  }
  assert.match(ext, /const openPanelResult = \(view: 'build' \| 'ship', start: \(\) => void\): void => \{\s*ReCoderPanel\.showView\(context\.extensionUri, sidebarProvider, view\);/);
  //: 터미널 오류는 화면을 빼앗지 않는다 — 알림의 [열기] 를 눌렀을 때만 연다.
  const auto = ext.slice(ext.indexOf('registerShellIntegrationListeners('), ext.indexOf('onDidChangeTerminalShellIntegration'));
  assert.doesNotMatch(auto, /sidebarView\.focus|showView\(|triggerAnalysis\(request\)/);
  assert.match(auto, /showWarningMessage\([\s\S]*'열기'/);
  assert.match(auto, /choice === '열기'[\s\S]*openPanelResult\('build'/);
});

// ── 8. Workbench ──────────────────────────────────────────────────────

class TestHost extends WorkbenchHost {
  constructor(api) { super(vscode.Uri.file('/x'), api, {}, {}); this.out = []; }
  _post(m) { this.out.push(m); }
  _getWorkspacePath() { return '/ws'; }
  send(type, payload) { return this._handleMessage({ type, payload }); }
  last(t) { return this.out.findLast((m) => m.type === t)?.payload; }
}

test('3.0.2 Workbench push: 확인 창 · 자동 커밋 없음 · 강제 push 는 두 번째 확인', async () => {
  const pushes = [];
  const host = new TestHost({ gitPush: async (r) => { pushes.push(r); return { status: 'ok', branch: 'main' }; } });
  await withWindow([null, true, null, '강제 push 계속', '원격 기록 덮어쓰기'], async (asked) => {
    await host.send('wb.gh.push', { branch: 'main' });
    assert.equal(pushes.length, 0, '취소했는데 push 했다'); assert.equal(asked[0].modal, true);
    await host.send('wb.gh.push', { branch: 'main' });
    assert.deepEqual(pushes[0], { workspace_path: '/ws', branch: 'main', force: false, auto_commit: false, approved: true });
    assert.equal(asked.length, 2, '일반 push 에 강제 확인을 물었다');
    await host.send('wb.gh.push', { branch: 'main', force: true });   // 첫 확인은 닫음
    assert.equal(pushes.length, 1);
    await host.send('wb.gh.push', { branch: 'main', force: true });   // 두 번 확인
    assert.equal(asked.length, 5); assert.match(asked[4].text, /다른 사람이 올린 커밋이 사라질 수 있습니다/);
    assert.deepEqual(pushes[1], { workspace_path: '/ws', branch: 'main', force: true, auto_commit: false, approved: true });
  });
  assert.match(pushConfirmText('/ws', '', false), /커밋하지 않은 변경은 올라가지 않습니다/);
  const src = read('../src/sidebar/workbenchHost.ts');
  const push = src.slice(src.indexOf("case 'wb.gh.push'"), src.indexOf("case 'wb.gh.listRuns'"));
  assert.match(push, /auto_commit: false/);
});

test('3.0.2 Workbench 로컬 배포: 플랜 요약을 보여 주고, 승인하지 않으면 실행하지 않고 플랜을 버린다', async () => {
  const executed = [];
  const plan = { plan_id: 'p9', image: 'shop:v2', container_name: 'shop', ports: { 8080: '8000' }, health_check_path: '/health', risk_level: 'medium', risk_reasons: ['포트 노출'] };
  const host = new TestHost({ createDeploymentPlan: async () => plan, executeDeployment: async (id, ok) => { executed.push([id, ok]); return { status: 'ok', health_ok: true, deployment_id: 'd' }; } });
  await withWindow([null, true], async (asked) => {
    await host.send('wb.local.deploy', { image: 'shop:v2' });
    assert.equal(asked[0].modal, true);
    assert.match(asked[0].text, /shop:v2/); assert.match(asked[0].text, /8080→8000/); assert.match(asked[0].text, /\/health/); assert.match(asked[0].text, /포트 노출/);
    assert.deepEqual(executed, [['p9', false]], '승인하지 않은 플랜을 실행했다');
    await host.send('wb.local.deploy', { image: 'shop:v2' });
    assert.deepEqual(executed.at(-1), ['p9', true]);
  });
  assert.match(describeLocalPlan({}), /실행하시겠습니까\?/);
});

test('3.0.2 Workbench 저장소 · Secret · ECS: 확인 창을 거친 것만 approved:true', async () => {
  const calls = [];
  const host = new TestHost({
    githubCreateRepo: async (r) => { calls.push(['repo', r]); return { status: 'ok' }; },
    githubSetSecret: async (r) => { calls.push(['secret', r]); return { status: 'ok' }; },
    ecsDeployReady: async () => ({ ready: true, issues: [] }),
    deployEcs: async (r) => { calls.push(['ecs', r]); return { status: 'ok', message: '시작' }; },
  });
  host._startEcsStatusPolling = () => {};
  await withWindow([null, null, null, true, true, true], async () => {
    await host.send('wb.gh.createRepo', { name: 'shop', private: true });
    await host.send('wb.gh.setSecret', { repo: 'me/shop', name: 'K', value: 'v' });
    await host.send('wb.deploy.ecs', { ecs_cluster: 'c', ecs_service: 's', aws_region: 'us-east-1' });
    assert.equal(calls.length, 0, '취소했는데 GitHub·AWS 를 바꿨다');
    await host.send('wb.gh.createRepo', { name: 'shop', private: true });
    await host.send('wb.gh.setSecret', { repo: 'me/shop', name: 'K', value: 'v' });
    await host.send('wb.deploy.ecs', { ecs_cluster: 'c', ecs_service: 's', aws_region: 'us-east-1' });
  });
  assert.deepEqual(calls.map(([k, r]) => [k, r.approved]), [['repo', true], ['secret', true], ['ecs', true]]);
});

// ── 핵심 계약: ApiClient 는 approved 를 정확히 true 일 때만 싣는다 ──────────

test('3.0.2 ApiClient: approved 는 정확히 true 일 때만 true 로 나간다', async () => {
  const core = await fakeCore({ '/api/git/push': { status: 'ok' }, '/api/aws/cleanup/stop': { ok: true, message: '' }, '/api/deploy/ecs/stop': { stopped: true, desired_count: 0, message: '' } });
  try {
    const api = new ApiClient({ getPort: () => core.server.address().port, getSessionToken: () => 't', refreshToken: async () => true });
    await api.gitPush({ workspace_path: '/w', force: 'yes' });
    await api.cleanupStop('us-east-1', 'c', 's');
    await api.stopEcsService({ ecs_cluster: 'c', ecs_service: 's', aws_region: 'us-east-1' });
    assert.deepEqual(core.requests.map((r) => [r.path, r.body.approved]), [['/api/git/push', false], ['/api/aws/cleanup/stop', false], ['/api/deploy/ecs/stop', false]]);
    assert.equal(core.requests[0].body.force, false, '참 같은 값으로 강제 push 를 보냈다');
  } finally { await core.close(); }
});
