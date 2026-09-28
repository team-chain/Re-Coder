const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { samePath } = require('../out/core/coreReuse.js');
const compiled = path.join(__dirname, '../out/core/CoreManager.js');

function setup(t, mode = 1) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-selection-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const ext = path.join(dir, 'active/extension');
  const source = path.join(dir, 'active/core/main.py');
  const binary = path.join(ext, 'bin', process.platform === 'win32' ? 'recoder-core.exe' : 'recoder-core');
  const write = (file, content = '') => { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, content); };
  write(source);
  write(binary);
  const otherRepo = path.join(dir, 'other-repo');
  write(path.join(otherRepo, 'core/main.py'));
  write(path.join(otherRepo, 'extension/package.json'), '{}');
  const vscode = {
    ExtensionMode: { Production: 1, Development: 2, Test: 3 },
    workspace: { workspaceFolders: [{ uri: { fsPath: otherRepo } }] },
    window: { showInformationMessage() {} },
  };
  let pathBinary = '';
  const signals = [];
  const output = { exports: {} };
  const localRequire = createRequire(compiled);
  vm.runInNewContext(fs.readFileSync(compiled, 'utf8'), {
    module: output, exports: output.exports,
    require: id => {
      if (id === 'vscode') { return vscode; }
      if (id === 'os') { return { homedir: () => dir }; }
      if (id === 'child_process') { return { execSync: () => pathBinary, spawnSync: () => ({ status: 0 }) }; }
      return localRequire(id);
    },
    process: { ...process, kill(pid, signal) { signals.push([pid, signal]); return true; } },
    console, AbortController, setTimeout, clearTimeout,
    fetch: async () => { throw new Error('Unexpected HTTP request in selection test'); },
  }, { filename: compiled });
  const manager = new output.exports.CoreManager({ extensionMode: mode, extensionPath: ext });
  manager._findPython = () => 'test-python';
  manager.probeRunningCore = async () => null;
  manager.healthCheck = async () => ({ status: 'ok' });
  manager.spawnCore = async () => assert.fail('Unexpected Core spawn');
  const runtime = (entrypoint = binary, extra = {}) => {
    const data = { pid: 54321, port: 54321, session_token: 'test-only-token', entrypoint, ...extra };
    write(manager._runtimePath, JSON.stringify(data));
    return data;
  };
  return { dir, ext, source, binary, manager, vscode, write, runtime, signals, setPath: value => { pathBinary = value; } };
}

test('installed VSIX uses its bundle even with another ReCoder repository open', t => {
  const { manager, binary } = setup(t);
  const spec = manager._findCoreSpec();
  assert.equal(spec.command, binary);
  assert.equal(spec.args.length, 0);
});

for (const mode of [2, 3]) {
  test(`development/test mode ${mode} uses its own source with any workspace or none`, t => {
    const { manager, source, vscode } = setup(t, mode);
    assert.equal(manager._findCoreSpec().args[0], source);
    vscode.workspace.workspaceFolders = [{ uri: { fsPath: '/sample-app' } }];
    assert.equal(manager._findCoreSpec().args[0], source);
    vscode.workspace.workspaceFolders = [];
    assert.equal(manager._findCoreSpec().args[0], source);
  });
}

test('missing development source does not fall back to an old bundle or another repository', t => {
  const { manager, source, runtime } = setup(t, 2);
  fs.unlinkSync(source);
  assert.equal(manager._findCoreSpec(), null);
  runtime();
  return assert.rejects(manager.ensureRunning(), /다른 실행 경로/);
});

test('installed mode falls back to PATH then user bin, never adjacent/workspace Python', t => {
  const { manager, binary, dir, write, setPath } = setup(t);
  fs.unlinkSync(binary);
  const onPath = path.join(dir, 'path/recoder-core');
  const userBin = path.join(dir, '.recoder/bin', path.basename(binary));
  write(onPath); write(userBin); setPath(onPath);
  assert.equal(manager._findCoreSpec().command, onPath);
  setPath('');
  assert.equal(manager._findCoreSpec().command, userBin);
  fs.unlinkSync(userBin);
  assert.equal(manager._findCoreSpec(), null);
});

test('same installed Core is reused with no spawn or termination', async t => {
  const { manager, runtime, signals } = setup(t);
  runtime();
  const first = await manager.ensureRunning();
  assert.ok(first);
  assert.ok(await manager.ensureRunning());
  assert.equal(manager.coreProcess, null);
  assert.equal(signals.length, 0);
});

for (const mode of [1, 2]) {
  test(`mode ${mode} rejects the other Core and token refresh leaves its connection unchanged`, async t => {
    const { manager, source, binary, runtime, signals } = setup(t, mode);
    runtime(mode === 1 ? source : binary);
    manager.port = 1234; manager.sessionToken = 'previous';
    await assert.rejects(manager.ensureRunning(), /ReCoder: Restart Core/);
    await assert.rejects(manager.refreshToken(), /다른 실행 경로/);
    assert.equal(manager.port, 1234);
    assert.equal(manager.sessionToken, 'previous');
    assert.ok(signals.every(([, signal]) => signal === 0));
    assert.ok(fs.existsSync(manager._runtimePath));
  });
}

test('probe cannot bypass entrypoint validation when runtime appears late', async t => {
  const { manager, runtime, source } = setup(t);
  manager.probeRunningCore = async () => { runtime(source); return { port: 54321 }; };
  await assert.rejects(manager.ensureRunning(), /다른 실행 경로/);
});

test('readiness polling cannot attach to a foreign Core after spawn', async t => {
  const { manager, source, runtime } = setup(t);
  runtime(source);
  await assert.rejects(manager.waitForReady(50), /다른 실행 경로/);
});

test('missing authentication cannot produce a ready client', async t => {
  const { manager, binary, runtime } = setup(t);
  runtime(binary, { session_token: '' });
  await assert.rejects(manager.ensureRunning(), /인증 토큰/);
  assert.equal(manager._client, null);
});

test('lightweight VSIX can attach to a manually started Core but cannot shut it down to restart', async t => {
  const { manager, source, binary, runtime, signals } = setup(t);
  fs.unlinkSync(binary);
  runtime(source);
  assert.ok(await manager.ensureRunning());
  await assert.rejects(manager.restart(), /바이너리가 없습니다/);
  assert.equal(signals.length, 0);
  assert.ok(fs.existsSync(manager._runtimePath));
});

test('stale cleanup preserves a live unresponsive process and only removes a dead runtime', async t => {
  const { manager, runtime, write, signals } = setup(t);
  runtime();
  write(manager._lockPath, '{"pid":54321}');
  await assert.rejects(manager.cleanupStale(), /종료를 확인하지/);
  assert.ok(fs.existsSync(manager._runtimePath));
  assert.ok(fs.existsSync(manager._lockPath));
  assert.ok(signals.every(([, signal]) => signal === 0));
  manager.isProcessRunning = () => false;
  await manager.cleanupStale();
  assert.equal(fs.existsSync(manager._runtimePath), false);
  assert.ok(fs.existsSync(manager._lockPath), 'singleton owns lock recovery');
});

test('symlinked executable matches the resolved path written by Core', t => {
  const { binary, dir } = setup(t);
  const link = path.join(dir, 'linked-core');
  try { fs.symlinkSync(binary, link); } catch (error) {
    if (process.platform === 'win32' && error.code === 'EPERM') return t.skip('Windows requires Developer Mode or symlink privilege');
    throw error;
  }
  assert.ok(samePath(link, binary));
});
