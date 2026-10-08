const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path'), vm = require('node:vm');
const { createRequire } = require('node:module');
const { spawn } = require('node:child_process');
const compiled = path.join(__dirname, '../out/core/CoreManager.js');
const fixture = path.join(__dirname, 'fixtures/coreEnvFixture.js');

function loadManager(dir) {
  const output = { exports: {} };
  const localRequire = createRequire(compiled);
  const vscode = { ExtensionMode: { Production: 1, Development: 2, Test: 3 }, window: { showInformationMessage() {} }, workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) } };
  vm.runInNewContext(fs.readFileSync(compiled, 'utf8'), { module: output, exports: output.exports,
    require: id => id === 'vscode' ? vscode : localRequire(id), process, console, fetch, AbortController, setTimeout, clearTimeout }, { filename: compiled });
  const m = new output.exports.CoreManager({ extensionMode: 1, extensionPath: path.dirname(compiled) });
  m._findCoreSpec = () => ({ command: process.execPath, args: [fixture, path.join(dir, 'runtime.json')] });
  m._runtimePath = path.join(dir, 'runtime.json'); m._lockPath = path.join(dir, 'core.lock');
  m.probeRunningCore = async () => null; m._gatewayEnv = async () => ({}); m._awsEnv = async () => ({});
  return m;
}

function staleRuntime(t, entrypoint) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-stale-'));
  const unrelated = spawn(process.execPath, ['-e', 'setTimeout(()=>{},30000)'], { stdio: 'ignore' }); // PID 재사용 대역
  fs.writeFileSync(path.join(dir, 'runtime.json'), JSON.stringify({ port: 17999, session_token: 'x', pid: unrelated.pid, entrypoint }));
  const m = loadManager(dir);
  t.after(async () => { await m.shutdown(true); unrelated.kill('SIGKILL'); fs.rmSync(dir, { recursive: true, force: true }); });
  return { dir, m };
}

test('PID 만 살아 있고 응답 없는 runtime.json 은 남은 기록으로 보고 새 Core 를 띄운다', async t => {
  const { dir, m } = staleRuntime(t, fixture);
  await m.ensureRunning();
  const rt = JSON.parse(fs.readFileSync(path.join(dir, 'runtime.json'), 'utf8'));
  assert.notEqual(rt.port, 17999);
  assert.equal((await m.healthCheck()).status, 'ok');
});

test('예전 버전 경로의 남은 기록도 "다른 실행 경로" 오류로 막지 않는다', async t => {
  const { m } = staleRuntime(t, '/old/ext-1.1.15/bin/recoder-core');
  await m.ensureRunning();
  assert.equal((await m.healthCheck()).status, 'ok');
});

test('시작 직후 죽는 Core 는 60초를 기다리지 않고 원인과 함께 바로 알린다', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-crash-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const m = loadManager(dir);
  m._findCoreSpec = () => ({ command: process.execPath, args: ['-e', 'console.error("ImportError: No module named x"); process.exit(3)'] });
  const start = Date.now();
  await assert.rejects(m.ensureRunning(), e => /시작 직후 종료/.test(e.message) && /코드 3/.test(e.message) && /ImportError/.test(e.message));
  assert.ok(Date.now() - start < 10000, `took ${Date.now() - start}ms`);
});

test('연결 정보 없이 남은 이전 Core 는 사용자 확인 후 종료하고 새로 띄운다', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-orphan-'));
  const orphan = spawn(process.execPath, ['-e', 'setTimeout(()=>{},30000)', 'recoder-core'], { stdio: 'ignore' });
  t.after(() => { try { orphan.kill('SIGKILL'); } catch { /* gone */ } fs.rmSync(dir, { recursive: true, force: true }); });
  fs.writeFileSync(path.join(dir, 'core.lock'), JSON.stringify({ pid: orphan.pid, windows: [orphan.pid] }));
  const output = { exports: {} };
  const localRequire = createRequire(compiled);
  const asked = [];
  const vscode = { ExtensionMode: { Production: 1, Development: 2, Test: 3 },
    window: { showInformationMessage() {}, showWarningMessage: async (msg, _o, action) => { asked.push(msg); return action; } },
    workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) } };
  vm.runInNewContext(fs.readFileSync(compiled, 'utf8'), { module: output, exports: output.exports,
    require: id => id === 'vscode' ? vscode : localRequire(id), process, console, fetch, AbortController, setTimeout, clearTimeout }, { filename: compiled });
  const m = new output.exports.CoreManager({ extensionMode: 1, extensionPath: path.dirname(compiled) });
  m._findCoreSpec = () => ({ command: process.execPath, args: [fixture, path.join(dir, 'runtime.json')] });
  m._runtimePath = path.join(dir, 'runtime.json'); m._lockPath = path.join(dir, 'core.lock');
  let probes = 0;
  m.probeRunningCore = async () => (probes++ === 0 ? { port: 17998 } : null);   // 처음엔 고아가 응답하는 상황
  m._gatewayEnv = async () => ({}); m._awsEnv = async () => ({});
  t.after(async () => { await m.shutdown(true); });
  await m.ensureRunning();
  assert.equal(asked.length, 1);
  assert.equal(orphan.exitCode !== null || orphan.signalCode !== null, true, 'orphan terminated');
  assert.equal((await m.healthCheck()).status, 'ok');
});

test('이전 Core 종료를 거절하면 자동 재시도 때 다시 묻지 않고, 명시적 재시작 때는 다시 묻는다', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-orphan-no-'));
  const orphan = spawn(process.execPath, ['-e', 'setTimeout(()=>{},30000)', 'recoder-core'], { stdio: 'ignore' });
  const unrelated = spawn(process.execPath, ['-e', 'setTimeout(()=>{},30000)', 'main.py'], { stdio: 'ignore' });
  t.after(() => { for (const p of [orphan, unrelated]) { try { p.kill('SIGKILL'); } catch { /* gone */ } } fs.rmSync(dir, { recursive: true, force: true }); });
  const output = { exports: {} };
  const localRequire = createRequire(compiled);
  const asked = [];
  const vscode = { ExtensionMode: { Production: 1, Development: 2, Test: 3 },
    window: { showInformationMessage() {}, showWarningMessage: async (msg) => { asked.push(msg); return undefined; } },
    workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) } };
  vm.runInNewContext(fs.readFileSync(compiled, 'utf8'), { module: output, exports: output.exports,
    require: id => id === 'vscode' ? vscode : localRequire(id), process, console, fetch, AbortController, setTimeout, clearTimeout }, { filename: compiled });
  const m = new output.exports.CoreManager({ extensionMode: 1, extensionPath: path.dirname(compiled) });
  m._lockPath = path.join(dir, 'core.lock');
  fs.writeFileSync(m._lockPath, JSON.stringify({ pid: orphan.pid }));
  assert.equal(await m.recoverOrphanCore(), false);
  assert.equal(await m.recoverOrphanCore(), false);
  assert.equal(asked.length, 1, '거절한 뒤 다시 묻지 않는다');
  m._declinedOrphanPid = null;   // restart() 가 하는 일
  assert.equal(await m.recoverOrphanCore(), false);
  assert.equal(asked.length, 2);
  //: 이름이 ReCoder Core 가 아닌 `python main.py` 같은 프로세스는 종료하자고 묻지 않는다.
  fs.writeFileSync(m._lockPath, JSON.stringify({ pid: unrelated.pid }));
  assert.equal(m.orphanCorePid(), null);
});

test('다른 창이 같은 순간 Core 를 띄우면(잠금 충돌) 실패하지 않고 그 Core 에 붙는다', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-race-'));
  const m = loadManager(dir);
  m._findCoreSpec = () => ({ command: process.execPath, args: [fixture, path.join(dir, 'runtime.json'), 'locked'] });
  let other;
  t.after(async () => { try { other?.kill('SIGKILL'); } catch { /* gone */ } await m.shutdown(true); fs.rmSync(dir, { recursive: true, force: true }); });
  //: 다른 창의 Core 가 1.5초 뒤에 준비된다(느린 시작).
  setTimeout(() => { other = spawn(process.execPath, [fixture, path.join(dir, 'runtime.json')], { stdio: 'ignore' }); }, 1500);
  await m.ensureRunning();
  assert.equal((await m.healthCheck()).status, 'ok');
});

test('업데이트 뒤 남은 이전 버전 Core 는 새 버전이 넘겨받는다(반대는 아님)', () => {
  const { isOlderBundledCore, bundledCoreVersion } = require('../out/core/coreReuse.js');
  const old = 'C:\\Users\\a\\.vscode\\extensions\\recoder-team.recoder-1.1.18\\bin\\recoder-core.exe';
  const cur = 'c:\\Users\\a\\.vscode\\extensions\\recoder-team.recoder-1.1.21\\bin\\recoder-core.exe';
  assert.deepEqual(bundledCoreVersion(cur), [1, 1, 21]);
  assert.equal(isOlderBundledCore(old, cur), true);
  assert.equal(isOlderBundledCore(cur, old), false);
  assert.equal(isOlderBundledCore(cur, cur), false);
  assert.equal(isOlderBundledCore('/home/dev/core/main.py', cur), false);
  assert.equal(isOlderBundledCore('/x/recoder-team.recoder-1.1.9-darwin-arm64/bin/recoder-core', '/x/recoder-team.recoder-1.1.10-darwin-arm64/bin/recoder-core'), true);
});

test('이전 버전 Core 가 응답 중이면 종료를 요청하고 새 Core 를 띄운다', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-upgrade-'));
  const m = loadManager(dir);
  const http = require('node:http');
  let shutdownCalled = false;
  const oldCore = http.createServer((req, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/api/shutdown') { shutdownCalled = true; res.end(JSON.stringify({ status: 'shutting_down' })); setTimeout(() => { oldCore.close(); fs.rmSync(path.join(dir, 'runtime.json'), { force: true }); }, 50); return; }
    res.end(JSON.stringify({ status: 'ok' }));
  });
  await new Promise(r => oldCore.listen(0, '127.0.0.1', r));
  const holder = spawn(process.execPath, ['-e', 'setTimeout(()=>{},3000)'], { stdio: 'ignore' });
  t.after(async () => { try { oldCore.close(); } catch { /* closed */ } holder.kill('SIGKILL'); await m.shutdown(true); fs.rmSync(dir, { recursive: true, force: true }); });
  fs.writeFileSync(path.join(dir, 'runtime.json'), JSON.stringify({ port: oldCore.address().port, session_token: 'old', pid: holder.pid,
    entrypoint: 'C:\\x\\.vscode\\extensions\\recoder-team.recoder-1.1.18\\bin\\recoder-core.exe' }));
  m.expectedEntrypoint = () => 'C:\\x\\.vscode\\extensions\\recoder-team.recoder-1.1.21\\bin\\recoder-core.exe';
  m.isProcessRunningOriginal = m.isProcessRunning;
  m.restartCore = async function () { shutdownCalled = shutdownCalled || false; await fetch(`http://127.0.0.1:${oldCore.address().port}/api/shutdown`, { method: 'POST' }); return 'restarted'; };
  const out = await m._ensureRunning();
  assert.equal(out, 'restarted');
  assert.equal(shutdownCalled, true);
});
