// 전체 점검(2026-10)에서 재현한 확장 쪽 문제 — 고친 동작을 고정한다.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');

const resolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...args) {
  return request === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : resolve.call(this, request, ...args);
};
const vscode = require('vscode');
const { SidebarProvider } = require('../out/sidebar/SidebarProvider.js');
const { PollingService } = require('../out/core/PollingService.js');
const { isDeployableAsset } = require('../out/deploy/staticSite.js');
Module._resolveFilename = resolve;

function twoProjects() {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-audit-'));
  const a = path.join(base, 'shop'), b = path.join(base, 'board');
  fs.mkdirSync(a); fs.mkdirSync(b);
  return { base, a, b };
}

async function apply(sidebar, payload) {
  const sent = [];
  const original = sidebar.postMessage.bind(sidebar);
  sidebar.postMessage = (type, p) => { sent.push({ type, payload: p }); };
  try { await sidebar.handleMessage({ type: 'code.apply', payload }); } finally { sidebar.postMessage = original; }
  return sent;
}

test('생성한 프로젝트가 아닌 다른 프로젝트가 활성화돼도 결과는 생성한 프로젝트에 쓴다', async () => {
  const { a, b } = twoProjects();
  const before = vscode.workspace.workspaceFolders;
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(b), name: 'board' }, { uri: vscode.Uri.file(a), name: 'shop' }];
  try {
    const sidebar = new SidebarProvider(vscode.Uri.file(a), {}, {}, {});
    const sent = await apply(sidebar, { file: 'routes/cart.js', content: 'cart', targetFolder: '', projectRoot: a, ackKey: 'k1' });
    assert.ok(sent.some(m => m.type === 'code.applied' && m.payload.ok));
    assert.equal(fs.readFileSync(path.join(a, 'routes/cart.js'), 'utf8'), 'cart');
    assert.ok(!fs.existsSync(path.join(b, 'routes/cart.js')), '활성 프로젝트(board)에 쓰면 안 된다');
  } finally { vscode.workspace.workspaceFolders = before; }
});

test('생성한 프로젝트가 워크스페이스에서 빠졌으면 쓰지 않고 이유를 알린다', async () => {
  const { a, b } = twoProjects();
  const before = vscode.workspace.workspaceFolders;
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(b), name: 'board' }];
  try {
    const sidebar = new SidebarProvider(vscode.Uri.file(b), {}, {}, {});
    const sent = await apply(sidebar, { file: 'x.js', content: 'x', targetFolder: '', projectRoot: a, ackKey: 'k2' });
    assert.ok(sent.some(m => m.type === 'code.error' && /다시 생성/.test(m.payload.message)));
    assert.ok(!fs.existsSync(path.join(b, 'x.js')) && !fs.existsSync(path.join(a, 'x.js')));
  } finally { vscode.workspace.workspaceFolders = before; }
});

test('음성대조: projectRoot 가 없던 예전 결과는 활성 프로젝트에 쓴다', async () => {
  const { b } = twoProjects();
  const before = vscode.workspace.workspaceFolders;
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(b), name: 'board' }];
  try {
    const sidebar = new SidebarProvider(vscode.Uri.file(b), {}, {}, {});
    await apply(sidebar, { file: 'y.js', content: 'y', targetFolder: '', ackKey: 'k3' });
    assert.equal(fs.readFileSync(path.join(b, 'y.js'), 'utf8'), 'y');
  } finally { vscode.workspace.workspaceFolders = before; }
});

test('프로젝트 밖을 가리키는 폴더 링크 아래 새 파일은 만들지 않는다', { skip: process.platform === 'win32' }, async () => {
  const { base, b } = twoProjects();
  const outside = path.join(base, 'outside'); fs.mkdirSync(outside);
  fs.symlinkSync(outside, path.join(b, 'cfg'), 'dir');
  const before = vscode.workspace.workspaceFolders;
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(b), name: 'board' }];
  try {
    const sidebar = new SidebarProvider(vscode.Uri.file(b), {}, {}, {});
    const sent = await apply(sidebar, { file: 'cfg/autostart/x.desktop', content: 'evil', targetFolder: '', ackKey: 'k4' });
    assert.ok(sent.some(m => m.type === 'code.error'));
    assert.ok(!fs.existsSync(path.join(outside, 'autostart/x.desktop')));
  } finally { vscode.workspace.workspaceFolders = before; }
});

test('공개 버킷에 소스맵(.map)은 올리지 않는다', () => {
  assert.equal(isDeployableAsset('static/js/main.123.js.map'), false);
  assert.equal(isDeployableAsset('static/js/main.123.js'), true);
  assert.equal(isDeployableAsset('index.html'), true);
});

test('Stop Core 뒤에는 폴링이 Core 를 다시 띄우지 않는다', async () => {
  let ensured = 0;
  const core = { ensureRunning: async () => { ensured++; } };
  const api = { getStatus: async () => { throw new Error('down'); }, getHealth: async () => { throw new Error('down'); } };
  const polling = new PollingService(core, api);
  polling.suspendAutoRecovery();
  await polling.poll();
  assert.equal(ensured, 0);
  const fresh = new PollingService(core, api);
  await fresh.poll();
  assert.equal(ensured, 1, '음성대조: 멈추지 않았으면 예전처럼 복구한다');
});

test('승인 뒤 활성 프로젝트가 바뀌면 S3 배포를 멈춘다', async () => {
  const { a, b } = twoProjects();
  const before = vscode.workspace.workspaceFolders;
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(b), name: 'board' }];
  try {
    const sidebar = new SidebarProvider(vscode.Uri.file(b), {}, {}, {});
    const sent = [];
    sidebar.postMessage = (type, p) => { sent.push({ type, payload: p }); };
    await sidebar.handleMessage({ type: 'workspace.deploy.s3', payload: { dir: '', region: 'ap-northeast-2', workspace_path: a } });
    const result = sent.find(m => m.type === 'workspace.deploy.s3.result');
    assert.ok(result && result.payload.ok === false && /바뀌어/.test(result.payload.message));
  } finally { vscode.workspace.workspaceFolders = before; }
});
