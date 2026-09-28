'use strict';
// Exercise the actual VSIX contents with no Python, developer home or AWS keys.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { pipeline } = require('node:stream/promises');
const { createHash } = require('node:crypto');

async function extract(file, destination) {
  const yauzl = require('yauzl');
  const zip = await new Promise((resolve, reject) => yauzl.open(file, { lazyEntries: true }, (error, value) => error ? reject(error) : resolve(value)));
  await new Promise((resolve, reject) => {
    zip.on('error', reject);
    zip.on('end', resolve);
    zip.on('entry', async entry => {
      try {
        const target = path.resolve(destination, entry.fileName);
        assert.ok(target.startsWith(destination + path.sep), 'ZIP path escaped installation directory');
        if (entry.fileName.endsWith('/')) fs.mkdirSync(target, { recursive: true });
        else {
          fs.mkdirSync(path.dirname(target), { recursive: true });
          const stream = await new Promise((yes, no) => zip.openReadStream(entry, (error, value) => error ? no(error) : yes(value)));
          await pipeline(stream, fs.createWriteStream(target));
        }
        zip.readEntry();
      } catch (error) { zip.close(); reject(error); }
    });
    zip.readEntry();
  });
}

async function installed(ext, home) {
  assert.equal(path.resolve(os.homedir()), path.resolve(home));
  const Module = require('node:module');
  const resolve = Module._resolveFilename;
  Module._resolveFilename = function (request, ...rest) {
    return request === 'vscode' ? path.join(__dirname, 'vscode-mock.js') : resolve.call(this, request, ...rest);
  };
  const vscode = require('vscode');
  const { CoreManager } = require(path.join(ext, 'out/core/CoreManager'));
  const { AnalysisJob } = require(path.join(ext, 'out/codemap/analysisJob'));
  const workspace = path.join(home, 'sample project');
  fs.mkdirSync(workspace, { recursive: true });
  fs.writeFileSync(path.join(workspace, 'app.js'), "import { greet } from './util.js';\nfunction render() { return greet(); }\nrender();\n");
  fs.writeFileSync(path.join(workspace, 'util.js'), "export function greet() { return 'hello'; }\n");
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(workspace) }];
  const context = {
    extensionMode: vscode.ExtensionMode.Production, extensionPath: ext,
    secrets: { get: async () => undefined }, globalState: { get: (_key, fallback) => fallback },
  };
  const manager = new CoreManager(context);
  // An existing interactive user's Core uses the same loopback range; this smoke
  // must not attach to it. Runtime/health checks for our own profile remain real.
  manager.probeRunningCore = async () => null;
  const runtimeFile = path.join(home, '.recoder/runtime.json');
  let lastRuntime;
  const api = async (route, options = {}) => {
    const response = await fetch(`http://127.0.0.1:${manager.getPort()}${route}`, {
      ...options, signal: AbortSignal.timeout(20000),
      headers: { 'X-Session-Token': manager.getSessionToken(), ...(options.headers || {}) },
    });
    assert.equal(response.status, 200, `${route}: HTTP ${response.status}`);
    return response.json();
  };
  try {
    await manager.ensureRunning();
    lastRuntime = JSON.parse(fs.readFileSync(runtimeFile, 'utf8'));
    const expected = path.join(ext, 'bin', process.platform === 'win32' ? 'recoder-core.exe' : 'recoder-core');
    assert.equal(path.resolve(lastRuntime.entrypoint).toLowerCase(), expected.toLowerCase());
    const { version } = require(path.join(ext, 'package.json'));
    assert.equal((await api('/api/health')).version, version);
    assert.equal((await api('/api/status')).status, 'ok');
    const unauthenticated = await fetch(`http://127.0.0.1:${manager.getPort()}/api/status`, { signal: AbortSignal.timeout(5000) });
    assert.ok([401, 403].includes(unauthenticated.status), 'session authentication missing');
    // Clean profile has no provider credentials; this must not invoke a paid model.
    const diagnosis = api('/api/diagnostics/run', {method:'POST'});
    assert.equal((await api('/api/health')).status, 'ok');
    const readiness = await diagnosis;
    assert.equal(readiness.ai_ready, 'fail');
    assert.equal((await api('/api/status')).status, 'ok', 'AI setup failure must not stop Core');
    console.log('PASS: packaged diagnostics and live health without AI credentials');
    const canvas = await api('/api/deploy/canvas');
    assert.equal(canvas.aws.ready, false, 'clean install unexpectedly contains AWS credentials');
    const job = new AnalysisJob();
    const project = await job.run(workspace);
    assert.equal(project.files_scanned, 2);
    assert.ok(project.edges.length >= 1);
    const file = await job.run(workspace, path.join(workspace, 'app.js'));
    assert.ok(file.functions_scanned >= 1);
    assert.equal((await api('/api/github/status')).status, 'unauthenticated');
    const github = await api('/api/github/repository/connect', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repository:'fixture/repo',create:false})});
    assert.equal(github.status, 'error');assert.match(github.message, /GitHub 인증/);
    const push = await api('/api/git/push', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_path:workspace,branch:'main',auto_commit:false})});
    assert.equal(push.status,'error');assert.match(push.message,/GitHub 로그인/);
    const post = (route, body) => api(route, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    fs.writeFileSync(path.join(workspace, 'Dockerfile'), 'FROM node:22 AS builder\nEXPOSE 8000\nFROM node:22-alpine\nEXPOSE 3000\n');
    const healthDir = path.join(workspace, 'app', 'api', 'health');
    fs.mkdirSync(healthDir, {recursive:true});
    fs.writeFileSync(path.join(healthDir, 'route.ts'), 'export function GET() { return Response.json({ok: true}); }');
    const plan = await post('/api/deploy/plan', {workspace_path:workspace,host_port:41234,env:{MODE:'fixture'},skip_security_scan:true,enable_continuous_verification:false});
    assert.deepEqual(plan.ports, {'41234':'3000'});
    assert.deepEqual(plan.env, {MODE:'fixture'});
    assert.equal(plan.health_check_path, '/api/health');
    assert.equal(plan.enable_continuous_verification, false);
    const withPort = await api(`/api/deploy/canvas?workspace_path=${encodeURIComponent(workspace)}`);
    assert.equal(withPort.container_port, 3000);
    assert.equal((await post('/api/deploy/execute', {plan_id:plan.plan_id,approved:false})).status, 'cancelled');
    const streamPlan = await post('/api/deploy/plan', {workspace_path:workspace,host_port:41234,skip_security_scan:true,enable_continuous_verification:false});
    const streamResponse = await fetch(`http://127.0.0.1:${manager.getPort()}/api/deploy/execute/stream`, {
      method:'POST',signal:AbortSignal.timeout(20000),headers:{'Content-Type':'application/json','X-Session-Token':manager.getSessionToken()},
      body:JSON.stringify({plan_id:streamPlan.plan_id,approved:false}),
    });
    assert.equal(streamResponse.status,200);
    assert.match(streamResponse.headers.get('content-type'),/text\/event-stream/);
    const streamText=await streamResponse.text();
    assert.match(streamText,/"step": "done"/);assert.match(streamText,/"status": "cancelled"/);
    console.log('PASS: native SSE endpoint emits an explicit cancelled result without deploying');
    console.log('PASS: packaged deployment plan, final-stage port, real health route, environment and cancel');
    console.log(`PASS: installed Core ${version}, session auth, locked AWS canvas, project/file analysis, unauthenticated GitHub connection/push`);
    const previousPid = lastRuntime.pid;
    await manager.restart();
    lastRuntime = JSON.parse(fs.readFileSync(runtimeFile, 'utf8'));
    assert.notEqual(lastRuntime.pid, previousPid);
    assert.equal((await api('/api/status')).status, 'ok');
    console.log('PASS: installed Core restart');
    await manager.stop();
    await new Promise(resolve => setTimeout(resolve, 1200));
    assert.throws(() => process.kill(lastRuntime.pid, 0), 'packaged Core child still running after Stop');
    assert.equal(fs.existsSync(runtimeFile), false);
    console.log('PASS: Stop removes the bootloader and Core child');
  } finally {
    // Only this smoke's child is eligible for cleanup.
    if (manager.coreProcess) await manager.stop();
    if (lastRuntime) {
      try { process.kill(lastRuntime.pid, 'SIGTERM'); } catch { /* already stopped */ }
    }
  }
}

async function main() {
  if (process.argv[2] === '--installed') return installed(process.argv[3], process.argv[4]);
  const ext = path.resolve(__dirname, '..');
  const version = require('../package.json').version;
  const target = `${process.platform}-${process.arch}`;
  const vsix = path.join(ext, 'dist', `recoder-${version}-${target}.vsix`);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-release-smoke-'));
  const home = path.join(root, 'isolated user');
  fs.mkdirSync(home);
  try {
    await extract(vsix, root);
    const manifest = fs.readFileSync(path.join(root, 'extension.vsixmanifest'), 'utf8');
    assert.ok(manifest.includes(`TargetPlatform="${target}"`));
    const installedRoot = path.join(root, 'extension');
    for (const bad of ['harness', 'scripts', 'src', 'out/test', 'out/webview-test', '.env']) {
      assert.equal(fs.existsSync(path.join(installedRoot, bad)), false, bad);
    }
    const release = JSON.parse(fs.readFileSync(vsix.replace(/\.vsix$/, '.release.json'), 'utf8'));
    assert.equal(createHash('sha256').update(fs.readFileSync(vsix)).digest('hex'), release.vsixSha256);
    const binary = path.join(installedRoot, 'bin', process.platform === 'win32' ? 'recoder-core.exe' : 'recoder-core');
    assert.equal(createHash('sha256').update(fs.readFileSync(binary)).digest('hex'), release.coreSha256);
    const env = { ...process.env };
    for (const key of Object.keys(env)) {
      if (/^(AWS_|RECODER_|BEDROCK_|GEMINI_|GOOGLE_|PYTHON|SESSION_TOKEN$|ELECTRON_|NODE_OPTIONS$)/i.test(key)) delete env[key];
    }
    Object.assign(env, { HOME: home, USERPROFILE: home, AWS_EC2_METADATA_DISABLED: 'true' });
    env.PATH = process.platform === 'win32' ? path.join(process.env.SystemRoot, 'System32') : '/usr/bin:/bin';
    const result = spawnSync(process.execPath, [__filename, '--installed', installedRoot, home], {
      cwd: home, env, windowsHide: true, encoding: 'utf8', timeout: 180000,
    });
    if (result.stdout) process.stdout.write(result.stdout);
    if (result.status !== 0) {
      const log = path.join(home, '.recoder', 'core.log');
      if (fs.existsSync(log)) fs.copyFileSync(log, path.join(ext, '..', '.canvas-qa', `release-smoke-core-${version}.log`));
      throw new Error(result.stderr || String(result.error || `Smoke exited ${result.status}`));
    }
    console.log('PASS: native VSIX contents and checksums; no Python on runtime PATH');
  } finally {
    // root was created by this process beneath the OS temp directory.
    assert.ok(root.startsWith(path.join(os.tmpdir(), 'recoder-release-smoke-')));
    fs.rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 400 });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
