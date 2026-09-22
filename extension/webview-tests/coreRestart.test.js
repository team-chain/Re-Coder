const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { spawn } = require('node:child_process');

const compiled = path.join(__dirname, '../out/core/CoreManager.js');
const fixture = path.join(__dirname, 'fixtures/coreLifecycle.js');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function loadManager(overrides = {}) {
  const output = { exports: {} };
  const localRequire = createRequire(compiled);
  const vscode = {
    window: { showInformationMessage() {} },
    workspace: { workspaceFolders: [] },
  };
  vm.runInNewContext(fs.readFileSync(compiled, 'utf8'), {
    module: output, exports: output.exports,
    require: id => id === 'vscode' ? vscode : localRequire(id),
    process, console, fetch, AbortController, setTimeout, clearTimeout,
    ...overrides,
  }, { filename: compiled });
  return new output.exports.CoreManager({});
}

async function waitFor(check) {
  const deadline = Date.now() + 5000;
  while (!check()) {
    if (Date.now() >= deadline) { throw new Error('Fixture did not become ready'); }
    await delay(20);
  }
}

async function isolatedCore(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-restart-'));
  const runtimeFile = path.join(dir, 'runtime.json');
  const records = [{ deployment_id: 'saved-deploy', status: 'rolled_back', rollback_proposal_status: 'completed' }];
  fs.writeFileSync(path.join(dir, 'records.json'), JSON.stringify(records));
  const original = spawn(process.execPath, [fixture, runtimeFile], { stdio: 'ignore' });
  const manager = loadManager();
  // Never read or modify the user's Core, credentials or AWS resources.
  manager._runtimePath = runtimeFile;
  manager._lockPath = path.join(dir, 'core.lock');
  manager._findWorkspaceCore = () => null;
  manager.probeRunningCore = async () => null;
  manager._gatewayEnv = async () => ({});
  manager._awsEnv = async () => ({});
  t.after(async () => {
    const child = manager.coreProcess;
    const closed = child ? new Promise(resolve => child.once('close', resolve)) : Promise.resolve();
    await manager.shutdown(true);
    await closed;
    if (original.exitCode === null && original.signalCode === null) { original.kill('SIGKILL'); }
    fs.rmSync(dir, { recursive: true, force: true });
  });
  let before;
  await waitFor(() => {
    try { before = JSON.parse(fs.readFileSync(runtimeFile, 'utf8')); return Boolean(before.pid); }
    catch { return false; }
  });
  manager._findCoreSpec = () => ({ command: process.execPath, args: [fixture, runtimeFile, String(before.port)] });
  await manager.ensureRunning();
  return { manager, before, dir, records };
}

test('attached window shutdown preserves the shared Core; explicit restart replaces it and reloads records', async t => {
  const { manager, before, records } = await isolatedCore(t);
  assert.equal(manager.coreProcess, null, 'must reproduce the attached-window case');
  await manager.shutdown();
  await manager.shutdown(true);
  assert.equal((await manager.readRuntime()).pid, before.pid);
  assert.equal((await manager.healthCheck()).status, 'ok');

  const client = await manager.restart();
  const after = await manager.readRuntime();
  assert.notEqual(after.pid, before.pid, 'reconnecting to the old PID is not a restart');
  assert.notEqual(after.started_at, before.started_at);
  assert.equal(after.port, before.port);
  assert.notEqual(after.session_token, before.session_token);
  assert.equal(await client.healthCheck(), true);
  assert.equal((await client.getStatus()).status, 'ok');
  const { ApiClient } = require('../out/core/ApiClient.js');
  const response = await new ApiClient(manager).request('GET', '/api/ecs/deployments');
  assert.equal(response.success, true);
  assert.deepEqual(response.data, records);
  assert.equal((await fetch(`http://127.0.0.1:${after.port}/api/ecs/deployments`)).status, 401);
});

test('owned Core also restarts, while concurrent restart and ensure calls wait for one new instance', async t => {
  const { manager, dir } = await isolatedCore(t);
  await manager.restart();
  const before = await manager.readRuntime();
  assert.equal(manager.coreProcess.pid, before.pid);
  const [first, second, ensured] = await Promise.all([
    manager.restart(), manager.restart(), manager.ensureRunning(),
  ]);
  const after = await manager.readRuntime();
  assert.notEqual(after.pid, before.pid);
  assert.equal(first, second);
  assert.equal(first, ensured);
  assert.equal(fs.readFileSync(path.join(dir, 'starts.log'), 'utf8').trim().split('\n').length, 3);
});

test('Core stdout, stderr and graceful or forced exits persist across restarts', async t => {
  const { manager, dir } = await isolatedCore(t);
  await manager.restart();
  const first = await manager.readRuntime();
  const file = path.join(dir, 'core.log');
  let saved = fs.readFileSync(file, 'utf8');
  assert.match(saved, /\[stdout\] fixture stdout: starting/);
  assert.match(saved, /\[stderr\] fixture stderr: test diagnostic/);
  assert.match(saved, /\[lifecycle\] ready port=/);

  await manager.restart();
  const second = await manager.readRuntime();
  saved = fs.readFileSync(file, 'utf8');
  assert.match(saved, new RegExp(`pid=${first.pid}.*restart requested`));
  assert.match(saved, new RegExp(`pid=${first.pid}.*exited code=0 signal=null`));
  assert.match(saved, /\[stdout\] fixture partial line/);
  assert.match(saved, new RegExp(`pid=${second.pid}.*spawned`));
  const child = manager.coreProcess;
  const closed = new Promise(resolve => child.once('close', resolve));
  child.kill('SIGKILL');
  await closed;
  saved = fs.readFileSync(file, 'utf8');
  assert.match(saved, new RegExp(`pid=${second.pid}.*exited code=null signal=SIGKILL`));
  assert.equal(saved.match(/fixture partial line/g).length, 2);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(dir, 'records.json'), 'utf8')), [
    { deployment_id: 'saved-deploy', status: 'rolled_back', rollback_proposal_status: 'completed' },
  ]);
});

test('failed process creation records the spawn error and startup failure', async t => {
  const { manager, dir } = await isolatedCore(t);
  manager._findCoreSpec = () => ({ command: path.join(dir, 'missing-core-executable'), args: [] });
  manager.waitForReady = async () => {
    await waitFor(() => manager.coreProcess === null);
    throw new Error('test startup failed');
  };
  await assert.rejects(manager.spawnCore(), /test startup failed/);
  const saved = fs.readFileSync(path.join(dir, 'core.log'), 'utf8');
  assert.match(saved, /spawn requested/);
  assert.match(saved, /spawn error: .*ENOENT/);
  assert.match(saved, /startup failed:.*test startup failed/);
  assert.doesNotMatch(saved, /\[lifecycle\] spawned/);
});

function simulatedManager(t, { reply = { status: 'shutting_down' }, status = 200, alive = true } = {}) {
  const calls = [];
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-restart-unit-'));
  const runtimeFile = path.join(dir, 'runtime.json');
  const runtime = { pid: 12345, port: 54321, session_token: 'fresh-token', started_at: 'before' };
  fs.writeFileSync(runtimeFile, JSON.stringify(runtime));
  const manager = loadManager({
    process: { ...process, pid: 99999, kill(pid, signal) {
      calls.push(['signal', pid, signal]);
      assert.equal(signal, 0, 'shared PID must never be terminated via OS signals');
      if (!alive) { throw Object.assign(new Error('gone'), { code: 'ESRCH' }); }
    } },
    fetch: async (url, options) => {
      calls.push(['request', url, options]);
      return { ok: status === 200, status, json: async () => reply };
    },
  });
  manager._runtimePath = runtimeFile;
  manager._lockPath = path.join(dir, 'core.lock');
  manager._ensureRunning = async () => { calls.push(['start']); return 'new-client'; };
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return { manager, calls, runtime, runtimeFile };
}

test('rejected shutdown reports failure, keeps runtime and permits a later retry', async t => {
  const { manager, calls, runtimeFile } = simulatedManager(t, { status: 403 });
  manager.sessionToken = 'old-token';
  await assert.rejects(manager.restart(), /HTTP 403/);
  assert.ok(fs.existsSync(runtimeFile));
  assert.ok(!calls.some(([kind]) => kind === 'start'));
  assert.equal(calls.find(([kind]) => kind === 'request')[2].headers['X-Session-Token'], 'fresh-token');
  assert.equal(manager.restartPromise, null);
  await assert.rejects(manager.restart(), /HTTP 403/);
  assert.equal(calls.filter(([kind]) => kind === 'request').length, 2);
});

test('a shutdown acknowledgement without process exit times out rather than reconnecting', async t => {
  const { calls, runtimeFile } = simulatedManager(t);
  let ticks = 0;
  // Use a fake process clock through a fresh module context, without waiting 15 s.
  const timed = loadManager({
    Date: { now: () => ticks++ ? 17000 : 1000 },
    process: { ...process, pid: 99999, kill() {} },
    fetch: async () => ({ ok: true, json: async () => ({ status: 'shutting_down' }) }),
  });
  timed._runtimePath = runtimeFile;
  timed._ensureRunning = async () => { calls.push(['start']); };
  await assert.rejects(timed.restart(), /종료되지 않아/);
  assert.ok(!calls.some(([kind]) => kind === 'start'));
  assert.ok(fs.existsSync(runtimeFile));
});

test('unexpected shutdown response and invalid PIDs cannot report a successful restart', async t => {
  const { manager, calls, runtime, runtimeFile } = simulatedManager(t, { reply: { status: 'ok' } });
  await assert.rejects(manager.restart(), /종료 요청을 확인하지/);
  for (const pid of [0, -1, 1, 1.5, '12345', 99999, undefined]) {
    fs.writeFileSync(runtimeFile, JSON.stringify({ ...runtime, pid }));
    await assert.rejects(manager.restart(), /PID/);
  }
  assert.equal(calls.filter(([kind]) => kind === 'request').length, 1);
  assert.ok(!calls.some(([kind]) => kind === 'start'));
});

test('an already stopped Core starts without sending a shutdown request', async t => {
  const { manager, calls } = simulatedManager(t, { alive: false });
  assert.equal(await manager.restart(), 'new-client');
  assert.ok(!calls.some(([kind]) => kind === 'request'));
});

test('restart waits for an in-flight startup before choosing the instance to stop', async t => {
  const { manager, calls } = simulatedManager(t, { alive: false });
  let finishStart;
  manager.ensurePromise = new Promise(resolve => { finishStart = resolve; });
  const restarting = manager.restart();
  await delay(0);
  assert.equal(calls.length, 0);
  finishStart('old-client');
  assert.equal(await restarting, 'new-client');
});

test('Restart Core command uses explicit restart and only signals success after completion', () => {
  const source = fs.readFileSync(path.join(__dirname, '../src/extension.ts'), 'utf8');
  const start = source.indexOf("registerCommand('recoder.restartCore'");
  const block = source.slice(start, source.indexOf('// ── Command: Start Core', start));
  assert.match(block, /await coreManager\.restart\(\)/);
  assert.doesNotMatch(block, /coreManager\.shutdown\(/);
  assert.ok(block.indexOf('await coreManager.restart()') < block.indexOf("'core.restarted'"));
  assert.match(block, /showErrorMessage/);
  assert.match(block, /'core.error'/);
});
