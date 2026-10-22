// 2.0.35 배포 전 신뢰 점검(제로 트러스트) — 승인 카드 표시와 확장이 보내는 설정(값 없음).
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const Module = require('node:module');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { TrustCheck } = require('../out/webview-test/components/TrustCheck.js');
const { ApprovalCard } = require('../out/webview-test/components/canvas/ApprovalCard.js');
const { ApprovalModal } = require('../out/webview-test/components/ApprovalModal.js');

const resolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...args) {
  return request === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : resolve.call(this, request, ...args);
};
const { trustConfig } = require('../out/sidebar/canvasHost.js');
Module._resolveFilename = resolve;

const report = {
  target: 'ecs', scanned_files: 12, truncated: false, counts: { pass: 2, warn: 1, block: 1, info: 0 },
  principles: [
    { key: 'ai', label: 'AI 결과를 그대로 믿지 않는다', summary: '', status: 'block', items: [
      { key: 'dockerfile', principle: 'ai', status: 'block', title: 'Dockerfile 안전 검사 — 위험한 명령', detail: 'Dockerfile 2행', fix: '이 줄을 지우세요' }] },
    { key: 'network', label: '네트워크를 믿지 않는다', summary: '', status: 'warn', items: [
      { key: 'https', principle: 'network', status: 'pass', title: '사용자 → 서비스 HTTPS', detail: '', fix: '숨겨야 함' },
      { key: 'origin_tls', principle: 'network', status: 'warn', title: 'CloudFront → 로드밸런서 구간은 HTTP', detail: '평문', fix: '내 도메인을 쓰세요' }] },
    { key: 'audit', label: '기록하고 되돌릴 수 있게', summary: '', status: 'pass', items: [
      { key: 'approval', principle: 'audit', status: 'pass', title: '승인한 것만 실행', detail: '', fix: '' }] },
  ],
};

test('점검표: 원칙·항목 상태, 고치는 법은 통과가 아닌 항목에만, 배포를 새로 막지 않는다는 안내', () => {
  const html = renderToStaticMarkup(React.createElement(TrustCheck, { report }));
  assert.match(html, /제로 트러스트 점검/);
  assert.match(html, /통과 2/); assert.match(html, /주의 1/); assert.match(html, /막힘 1/);
  assert.match(html, /data-trust-principle="ai" data-status="block"/);
  assert.match(html, /data-trust-item="origin_tls" data-status="warn"/);
  assert.match(html, /내 도메인을 쓰세요/);
  assert.doesNotMatch(html, /숨겨야 함/, '통과 항목에는 고치는 법을 보이지 않는다');
  assert.match(html, /배포를 새로 막지 않습니다/);
  assert.match(html, /<details open=""/, '주의·막힘이 있으면 펼친다');
  const quiet = renderToStaticMarkup(React.createElement(TrustCheck, { report: { ...report, counts: { pass: 3, warn: 0, block: 0, info: 0 } } }));
  assert.doesNotMatch(quiet, /<details open=""/);
  assert.equal(renderToStaticMarkup(React.createElement(TrustCheck, { report: null })), '');
});

test('클라우드 승인 카드·로컬 승인 창에 점검표가 붙는다(없으면 빠진다)', () => {
  const plan = { id: 'p', config: { target: 'ecs', image_name: 'shop', tag: '1', aws_region: 'ap-northeast-2', ecs_cluster: 'c', ecs_service: 's', task_family: 't', container_port: 3000, cpu: '256', memory: '512', environment: 'staging', dir: '' },
    projectName: 'shop', repository: '', branch: '', commit: '', dirty: false, account: '1', coreRegion: 'ap-northeast-2', preflight: { blocked: false, reasons: [], warnings: [] } };
  const withTrust = renderToStaticMarkup(React.createElement(ApprovalCard, { plan: { ...plan, trust: report }, onApprove() {}, onCancel() {}, onFix() {} }));
  assert.match(withTrust, /data-testid="trust-check"/);
  const without = renderToStaticMarkup(React.createElement(ApprovalCard, { plan, onApprove() {}, onCancel() {}, onFix() {} }));
  assert.doesNotMatch(without, /data-testid="trust-check"/);
  const modal = renderToStaticMarkup(React.createElement(ApprovalModal, { level: 2, title: 't', summary: 's', riskLevel: 'low', riskReasons: [], trust: report, onApprove() {}, onReject() {} }));
  assert.match(modal, /data-testid="trust-check"/);
});

test('확장이 코어에 보내는 점검 설정에는 환경변수·비밀값의 값이 없다', () => {
  const out = trustConfig({ target: 'ecs', https: true, edge: 'alb_waf', custom_domain: 'shop.example.com', environment: 'production',
    env_vars: { JWT_SECRET: 'super-secret' }, secret_refs: { DB: 'arn:aws:secretsmanager:x' }, image_name: 'shop' });
  assert.deepEqual(out.env_vars, ['JWT_SECRET']);
  assert.deepEqual(out.secret_refs, ['DB']);
  assert.equal(out.https, true); assert.equal(out.custom_domain, 'shop.example.com');
  assert.ok(!JSON.stringify(out).includes('super-secret'));
  assert.ok(!('image_name' in out));
});
