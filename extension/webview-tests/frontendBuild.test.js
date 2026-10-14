const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const fb = require('../out/deploy/frontendBuild.js');
const ss = require('../out/deploy/staticSite.js');

function project(t) {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-fe-'));
  t.after(() => fs.rmSync(ws, { recursive: true, force: true }));
  const put = (rel, text) => { fs.mkdirSync(path.dirname(path.join(ws, rel)), { recursive: true }); fs.writeFileSync(path.join(ws, rel), text); };
  put('package.json', JSON.stringify({ scripts: { build: 'cd client && npm run build' }, dependencies: { express: '^4' } }));
  put('client/package.json', JSON.stringify({ scripts: { build: 'react-scripts build' }, proxy: 'http://localhost:5000' }));
  put('client/src/App.jsx', "const API = 'http://localhost:5000';\n");
  put('client/public/index.html', '<html><head><title>Shop</title></head><body><div id="root"></div></body></html>');
  put('server/index.js', 'require("express")');
  return { ws, put };
}

test('client/ 의 CRA 를 찾아 산출물 폴더를 client/build 로 정한다', t => {
  const { ws } = project(t);
  const found = fb.detectFrontendProject(ws);
  assert.equal(found.projectDir, 'client');
  assert.equal(found.outDir, 'client/build');
  assert.equal(found.callsApi, true);
  assert.equal(fb.needsBuild(ws, found), true);
});

test('산출물이 소스보다 새로우면 다시 빌드하지 않는다', t => {
  const { ws, put } = project(t);
  put('client/build/index.html', '<script src="/static/js/main.js"></script>');
  const future = new Date(Date.now() + 60_000);
  fs.utimesSync(path.join(ws, 'client/build/index.html'), future, future);
  assert.equal(fb.needsBuild(ws, fb.detectFrontendProject(ws)), false);
});

test('빌드 전 CRA 템플릿(빈 root, 스크립트 없음)은 올리지 않는다 — 흰 화면', t => {
  const { ws } = project(t);
  assert.throws(() => ss.collectStaticFiles(path.join(ws, 'client/public'), fs, path.join, 'client/public'), /빌드 전 템플릿/);
});

test('npm 이 있으면 설치 후 빌드, 소스맵은 끈다', async t => {
  const { ws } = project(t);
  const calls = [];
  const runner = async (command, cwd, env) => { calls.push({ command, cwd, sourcemap: env.GENERATE_SOURCEMAP, nodeEnv: env.NODE_ENV }); return { code: 0, output: '' }; };
  const out = await fb.buildFrontend(ws, fb.detectFrontendProject(ws), () => {}, runner);
  assert.equal(out.ok, true);
  assert.deepEqual(calls.map(c => c.command), ['npm --version', 'npm install --no-audit --no-fund', 'npm run build']);
  assert.ok(calls.every(c => c.cwd === path.join(ws, 'client') && c.sourcemap === 'false' && c.nodeEnv === undefined));
});

test('npm 이 없으면 Docker 로 빌드하고, 둘 다 없으면 이유를 알려 준다', async t => {
  const { ws } = project(t);
  const seen = [];
  const withDocker = async (command) => { seen.push(command); return { code: command.startsWith('npm') ? 1 : 0, output: '' }; };
  const out = await fb.buildFrontend(ws, fb.detectFrontendProject(ws), () => {}, withDocker);
  assert.equal(out.via, 'docker');
  assert.match(seen.at(-1), /docker run --rm -v ".*:\/app" -v \/app\/node_modules/);
  const none = await fb.buildFrontend(ws, fb.detectFrontendProject(ws), () => {}, async () => ({ code: 1, output: '' }));
  assert.equal(none.ok, false);
  assert.match(none.output, /npm 도 Docker 도/);
});

test('정적 사이트(빌드 도구 없음)는 감지하지 않는다', t => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-static-'));
  t.after(() => fs.rmSync(ws, { recursive: true, force: true }));
  fs.writeFileSync(path.join(ws, 'index.html'), '<h1>hi</h1>');
  assert.equal(fb.detectFrontendProject(ws), null);
});

test('build 가 없고 build:web 만 화면을 빌드하면 그 스크립트로 빌드한다 — 빌드되지 않은 흰 화면 방지', async t => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-fe-'));
  t.after(() => fs.rmSync(ws, { recursive: true, force: true }));
  fs.writeFileSync(path.join(ws, 'package.json'), JSON.stringify({ scripts: { dev: 'vite', 'build:web': 'vite build' }, devDependencies: { vite: '^5' } }));
  const found = fb.detectFrontendProject(ws);
  assert.equal(found.script, 'build:web');
  assert.equal(found.outDir, 'dist');
  const commands = [];
  const runner = async (command) => { commands.push(command); return { code: 0, output: '' }; };
  const outcome = await fb.buildFrontend(ws, found, () => {}, runner);
  assert.equal(outcome.ok, true);
  assert.ok(commands.includes('npm run build:web'), commands.join(' | '));
});

test('build: 로 시작해도 개발 서버(vite)만 띄우는 스크립트는 빌드로 보지 않는다', t => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-fe-'));
  t.after(() => fs.rmSync(ws, { recursive: true, force: true }));
  fs.writeFileSync(path.join(ws, 'package.json'), JSON.stringify({ scripts: { 'build:watch': 'vite' } }));
  assert.equal(fb.detectFrontendProject(ws), null);
});
