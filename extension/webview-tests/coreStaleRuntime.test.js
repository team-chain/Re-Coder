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
  const vscode = { ExtensionMode: { Production: 1, Development: 2, Test: 3 }, window: { showInformationMessage() {} }, workspace: { workspaceFolders: [] } };
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
    workspace: { workspaceFolders: [] } };
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
