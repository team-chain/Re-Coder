// 4.0.1 명세 화면(요구사항 · 설계 · 작업)과 캔버스 프로젝트 요약 — Core /api/project/spec 결과를 그대로 그린다.
const test = require('node:test');
const assert = require('node:assert');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { SpecView, taskPrompt } = require('../out/webview-test/components/SpecView.js');
const { ProjectSummary } = require('../out/webview-test/components/canvas/ProjectSummary.js');

const api = (o) => ({ service: 'gateway', method: 'GET', path: '/api/x', auth: false, admin: false, public: true, implemented: true, file: 'services/gateway/src/x.ts', line: 3, tests: [], ...o });
const SPEC = {
  available: true, project: 'shop',
  features: [
    { key: 'auth', label: '회원가입 · 로그인', internal: false, services: ['gateway', 'users'], status: 'ok',
      endpoints: [api({ method: 'POST', path: '/api/auth/login', tests: [{ id: 't1', status: 'ok' }] })],
      criteria: [{ cond: 'WHEN', text: '올바른 이메일·비밀번호로 로그인하면 토큰을 준다', status: 'ok', source: 'ai' },
        { cond: 'WHEN', text: '올바른 요청에 200 로 응답한다', status: 'ok', source: 'test', api: 'POST /api/auth/login', test: 't1' }],
      counts: { api: 1, criteria: 2, ok: 2, fail: 0 } },
    { key: 'orders', label: '주문 · 결제', internal: false, services: ['orders'], status: 'fail',
      endpoints: [api({ service: 'orders', method: 'POST', path: '/api/orders', auth: true, tests: [{ id: 't2', status: 'fail' }] })],
      criteria: [], counts: { api: 1, criteria: 1, ok: 0, fail: 1 } },
  ],
  services: [{ name: 'gateway', kind: 'api', language: 'ts', port: 3000, public: true, databases: [], depends_on: ['users'], purpose: '', api: 1 },
    { name: 'web', kind: 'web', language: 'ts', port: 8080, public: true, databases: [], depends_on: ['gateway'], purpose: '', api: 0 }],
  databases: [{ name: 'usersdb', engine: 'postgres', tables: ['users'] }],
  endpoints: [],
  tasks: [{ id: 'test:t2', title: '주문 생성이 기대와 다르게 응답', detail: '실제 500', severity: 'high', feature: '주문 · 결제', source: 'rule' }],
  tests: { passed: 44, total: 45, at: '2026-11-19T00:00:00+00:00', status: 'degraded' },
  review: null,
  deploy: { id: 'd1', at: '2026-11-19T00:00:00+00:00', status: 'degraded', failures: [], url: 'http://localhost:8080',
    services: { gateway: 'ok', web: 'fail', usersdb: 'unknown' } },
};

test('요구사항 탭: 기능 목록 · 수용 기준 · API 표 — 이모티콘 · 색 배지 없이 상태는 아이콘으로', () => {
  const html = renderToStaticMarkup(React.createElement(SpecView, { initial: SPEC, onStartTask() {} }));
  for (const t of ['shop', '요구사항', '설계', '작업', '테스트 44/45', 'AI 검토', '회원가입 · 로그인', '주문 · 결제', '수용 기준', '올바른 이메일·비밀번호로 로그인하면 토큰을 준다', '/api/auth/login', '공개', '로그인', 'aria-label="실패"']) assert.ok(html.includes(t), t);
  assert.match(html, /aria-selected="true"[^>]*>요구사항/);
  assert.match(html, /작업<span class="sp-count">1<\/span>/);
  // 테스트 기준은 API 를 펼칠 때만.
  assert.ok(!html.includes('200 응답'));
  // 이모티콘 · 장식 기호가 없다.
  assert.ok(!/[\u2190-\u21FF\u2600-\u27BF\u2B00-\u2BFF]|\p{Extended_Pictographic}/u.test(html.replace(/<style>[\s\S]*?<\/style>/, '')), '이모티콘 · 화살표 기호가 있다');
});

test('AI 검토 전에는 수용 기준 목록이 없다 — 지어낸 요구사항이 없다', () => {
  const spec = { ...SPEC, features: [{ ...SPEC.features[0], criteria: SPEC.features[0].criteria.filter(c => c.source === 'test') }] };
  const html = renderToStaticMarkup(React.createElement(SpecView, { initial: spec }));
  assert.ok(!html.includes('수용 기준'));
});

test('설계 흐름도: 화면 → 게이트웨이 → DB 순서로 왼쪽에서 오른쪽', () => {
  const { flowLayout } = require('../out/webview-test/components/SpecView.js');
  const svc = (name, kind, depends_on = [], databases = []) => ({ name, kind, language: 'ts', port: 0, public: false, databases, depends_on, purpose: '', api: 1 });
  const { nodes, edges } = flowLayout([svc('web', 'web', ['gateway']), svc('gateway', 'api', ['users']), svc('users', 'api', [], ['users-db'])], [{ name: 'users-db', engine: 'postgres' }]);
  const x = Object.fromEntries(nodes.map(n => [n.id, n.x]));
  assert.ok(x.web < x.gateway && x.gateway < x.users && x.users < x['users-db']);
  assert.deepEqual(edges, [['web', 'gateway'], ['gateway', 'users'], ['users', 'users-db']]);
});

test('계약이 없는 프로젝트는 이유만 보인다', () => {
  const html = renderToStaticMarkup(React.createElement(SpecView, { initial: { available: false, reason: '아키텍처 계약(architecture.json)이 없는 프로젝트입니다.' } }));
  assert.ok(html.includes('architecture.json'));
  assert.ok(!html.includes('요구사항'));
});

test('작업 → 코드 생성 요청 문장', () => {
  assert.equal(taskPrompt(SPEC.tasks[0]), '주문 생성이 기대와 다르게 응답\n실제 500\n(기능: 주문 · 결제)');
});

test('프로젝트 요약: 상태 · 시각 · 주소, 서비스별 상태, 명세 요약, 사이트 열기 · 명세 · 다시 배포', () => {
  const html = renderToStaticMarkup(React.createElement(ProjectSummary, { active: true, branch: 'main', initial: SPEC, onSpec() {}, onDeploy() {} }));
  for (const t of ['shop', 'main', '실행 중 (일부 검사 실패)', 'localhost:8080', 'gateway', 'web', 'usersdb', '44/45', '사이트 열기', '명세', '다시 배포']) assert.ok(html.includes(t), t);
  assert.match(html, /기능<\/span><span class="v">2<\/span>/);
  assert.match(html, /class="dot fail"><\/i><span class="k">web/);
  assert.ok(!/[\u2190-\u21FF\u25B6]/.test(html.replace(/<style>[\s\S]*?<\/style>/, '')), '장식 기호가 있다');
});

test('프로젝트 요약: 아직 실행 전이면 사이트 열기 없이 로컬 실행', () => {
  const html = renderToStaticMarkup(React.createElement(ProjectSummary, { active: true, initial: { ...SPEC, deploy: null, tests: { passed: 0, total: 0, at: '', status: '' } }, onSpec() {}, onDeploy() {} }));
  assert.ok(html.includes('실행 전'));
  assert.ok(!html.includes('사이트 열기'));
  assert.ok(html.includes('로컬 실행'));
});
