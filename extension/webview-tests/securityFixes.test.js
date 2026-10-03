/**
 * 보안 검사 → 수정안 → 적용 → 다시 검사.
 *
 * 무엇이 문제였나 (2026-10-03 사용자)
 *   "보안 게이트 돌렸을 때, 문제만 잡아내면 뭐함? 그걸로 인한 개선점 같은건 따로 수정 같은 것도
 *   안해주잖아" — 검사 화면은 "심각 3 · 높음 12" 만 보여 주고 끝났다.
 *
 * 여기서 고정하는 것
 *   1. 검사가 끝나 발견이 있으면 수정안을 받고, 위험 표시가 없는 자동 수정만 기본으로 고른다.
 *   2. 적용 뒤에는 바뀐 부분만 다시 검사한다. 이미지 취약점은 이미지를 다시 빌드한 뒤 검사한다.
 *   3. 호스트는 검사한 프로젝트와 지금 프로젝트가 다르면 적용하지 않는다.
 *   4. 확장이 부르는 경로가 코어에 있다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');
const { fixReports, defaultSelection, followUp, diffLineKind } = require('../out/webview-test/components/securityFixes.js');

const prop = (over) => ({ id: 'x', tool: 'trivy', title: 't', detail: '', files: [], diff: '', auto: true, risk: '', note: '', rebuild: false, ...over });

test('발견이 있는 정상 결과만 수정안 요청에 쓴다', () => {
  assert.strictEqual(fixReports({}), null);
  assert.strictEqual(fixReports({ trivy: { status: 'unverified', findings: [] }, hadolint: { status: 'ok', findings: [] } }), null);
  const r = fixReports({
    trivy: { status: 'ok', requestId: 'a', findings: [{ package: 'axios' }], workspace: '/w' },
    gitleaks: { status: 'error', requestId: 'b', findings: [{ file: 'x' }] },
  });
  assert.deepStrictEqual(Object.keys(r.reports), ['trivy']);
  assert.strictEqual(r.workspace, '/w');
  assert.strictEqual(r.key, 'trivy:a');
});

test('위험 표시가 있거나 안내만 하는 항목은 기본으로 고르지 않는다', () => {
  const sel = defaultSelection([prop({ id: 'a' }), prop({ id: 'b', risk: '메이저' }), prop({ id: 'c', auto: false })]);
  assert.deepStrictEqual([...sel], ['a']);
});

test('적용 뒤 다시 볼 검사 — Dockerfile·시크릿은 다시 검사, 이미지는 다시 빌드', () => {
  const results = { trivy: { status: 'ok' }, hadolint: { status: 'ok' }, gitleaks: { status: 'ok' } };
  assert.deepStrictEqual(followUp([prop({ tool: 'hadolint', files: ['Dockerfile'] })], results), { rescan: ['hadolint'], rebuild: false });
  assert.deepStrictEqual(followUp([prop({ tool: 'gitleaks', files: ['server.js', '.env'] })], results), { rescan: ['gitleaks'], rebuild: false });
  const npm = followUp([prop({ files: ['package.json'], rebuild: true })], results);
  assert.deepStrictEqual(npm, { rescan: [], rebuild: true });
  //: 이미지 검사가 돌지 않았으면(빌드된 이미지 없음) 다시 빌드하지 않는다 — 다음 배포가 빌드한다.
  assert.strictEqual(followUp([prop({ rebuild: true })], { trivy: { status: 'unverified' } }).rebuild, false);
});

test('diff 색 구분', () => {
  assert.strictEqual(diffLineKind('+++ b/Dockerfile'), 'meta');
  assert.strictEqual(diffLineKind('+RUN apk upgrade'), 'add');
  assert.strictEqual(diffLineKind('-ADD . .'), 'del');
  assert.strictEqual(diffLineKind(' WORKDIR /app'), 'ctx');
});

test('검사 패널이 수정 목록을 붙이고, 호스트는 검사한 프로젝트에만 적용한다', () => {
  const hubs = read('../webview-src/components/Hubs.tsx');
  assert.match(hubs, /<SecurityFixList results=\{results\} running=\{running !== null\} onRescan=\{run\} \/>/);
  const host = read('../src/sidebar/SidebarProvider.ts');
  assert.match(host, /postMessage\('scanResult', \{ \.\.\.scanResult, requestId, workspace: scanRoot \}\)/);
  const block = host.slice(host.indexOf("case 'security.fixPlan'"), host.indexOf("case 'createDeployPlan'"));
  assert.match(block, /!samePath\(p\.workspace, workspace\)/);
  for (const m of ['securityFixPlan', 'securityFixApply', 'securityRebuild']) assert.match(block, new RegExp(`_apiClient\\.${m}\\(`));
  const list = read('../webview-src/components/SecurityFixList.tsx');
  assert.match(list, /postMessage\("security\.rebuild"/);
  assert.match(list, /\.recoder\/backups/);
});

test('확장이 부르는 보안 수정 경로가 코어에 있다', () => {
  const api = read('../src/core/ApiClient.ts');
  const core = fs.readFileSync(path.join(__dirname, '../../core/api/routes/deploy.py'), 'utf8');
  for (const route of ['/api/deploy/security/fixes', '/api/deploy/security/fixes/apply', '/api/deploy/security/rebuild']) {
    assert.ok(api.includes(`'${route}'`), route);
    assert.ok(core.includes(`@router.post("${route}")`), route);
  }
});
