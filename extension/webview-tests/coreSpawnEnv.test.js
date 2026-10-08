const test = require('node:test'); const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path'), vm = require('node:vm');
const { createRequire } = require('node:module');
const compiled = path.join(__dirname, '../out/core/CoreManager.js');
const fixture = path.join(__dirname, 'fixtures/coreEnvFixture.js');

async function spawnWith(t, secretsInit, stateInit, extraEnv = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-env-'));
  const output = { exports: {} }; const localRequire = createRequire(compiled);
  const vscode = { ExtensionMode: { Production: 1, Development: 2, Test: 3 }, window: { showInformationMessage() {} },
    workspace: { workspaceFolders: [], getConfiguration: () => ({ get: () => '' }) } };
  const proc = Object.assign(Object.create(process), { env: { ...process.env, ...extraEnv } });
  vm.runInNewContext(fs.readFileSync(compiled, 'utf8'), { module: output, exports: output.exports,
    require: id => id === 'vscode' ? vscode : localRequire(id), process: proc, console, fetch, AbortController, setTimeout, clearTimeout }, { filename: compiled });
  const secrets = new Map(Object.entries(secretsInit)), state = new Map(Object.entries(stateInit));
  const ctx = { extensionMode: 1, extensionPath: '/x', secrets: { get: async k => secrets.get(k) }, globalState: { get: (k, d) => state.has(k) ? state.get(k) : d } };
  const m = new output.exports.CoreManager(ctx);
  m._findCoreSpec = () => ({ command: process.execPath, args: [fixture, path.join(dir, 'runtime.json')] });
  m._runtimePath = path.join(dir, 'runtime.json'); m.probeRunningCore = async () => null; m._gatewayEnv = async () => ({});
  t.after(async () => { await m.shutdown(true); fs.rmSync(dir, { recursive: true, force: true }); });
  await m.ensureRunning();
  return JSON.parse(fs.readFileSync(path.join(dir, 'env.json'), 'utf8'));
}

test('저장한 Claude API 키가 실제로 Core 환경에 들어간다 (예전엔 로그 가림용으로만 쓰였다)', async t => {
  const env = await spawnWith(t, { 'recoder.ai.anthropicKey': 'sk-ant-api03-' + 'x'.repeat(40) }, { 'recoder.ai.provider': 'anthropic' });
  assert.equal(env.provider, 'anthropic');
  assert.equal(env.key, 'set');
  assert.ok(env.parent, 'RECODER_PARENT_PID');
});

test('키로 AWS 에 연결했으면 셸의 AWS_PROFILE 을 Core 에 넘기지 않는다', async t => {
  const env = await spawnWith(t, { 'recoder.aws.accessKeyId': 'AKIAEXAMPLEEXAMPLE12', 'recoder.aws.secretAccessKey': 's'.repeat(40) }, {}, { AWS_PROFILE: 'work' });
  assert.equal(env.awsKey, 'set');
  assert.equal(env.awsProfile, null);
});
