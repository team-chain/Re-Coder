/**
 * FR — 「권한표(정책 JSON) 제공 → 복붙 가이드」 (P0)
 *
 * 배경
 *   코어에는 `GET /api/aws/policy` 가 오래전부터 있었다. 계정 ID·리전·클러스터
 *   이름까지 채운, 그대로 붙여넣을 수 있는 최소권한 정책을 만들어 준다.
 *   그런데 **확장이 그걸 한 번도 부르지 않았다** — 저장소 전체에서 호출부가
 *   0건이었다.
 *
 *   사용자가 겪는 건 이렇다. 배포를 누르면 "권한이 없습니다" 라고만 나오고,
 *   무엇을 허용해야 하는지는 어디에도 없다. 답은 제품 안에 이미 있는데
 *   화면에 붙어 있지 않아서, 사용자는 AWS 문서를 뒤지거나
 *   AdministratorAccess 를 붙인다. 후자가 훨씬 흔하다.
 *
 *   이번 세션 내내 잡아온 「도달 불가능한 코드」와 같은 모양이다. 다만 여기서는
 *   죽은 코드를 지우는 게 아니라 **화면에 이어 붙이는 것**이 고침이다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const {
  describePolicyFill,
  describePolicyScope,
} = require('../out/webview-test/components/AwsPolicyGuide.js');

// ---------------------------------------------------------------------------
// 자리표시자가 남은 정책을 그대로 붙이게 두지 않는다
// ---------------------------------------------------------------------------

test('자리표시자가 남아 있으면 붙여 넣기 전에 막아 세운다', () => {
  const { blocking, message } = describePolicyFill({
    policy_json: '{}', needs_manual_fill: true,
  });
  assert.strictEqual(blocking, true);
  // 조용히 넘기면 사용자는 정책을 붙이고도 권한이 안 생기는 상태에 빠진다.
  // 그 시점에 원인이 자리표시자라는 걸 알아채기는 매우 어렵다.
  assert.match(message, /자리표시자/);
  assert.match(message, /채우/);
});

test('음성대조 — 다 채워졌으면 그대로 붙이라고 말한다', () => {
  const { blocking, message } = describePolicyFill({
    policy_json: '{}', needs_manual_fill: false,
    account_id: '413113423592', region: 'us-east-1',
  });
  assert.strictEqual(blocking, false);
  assert.match(message, /413113423592/);
  assert.match(message, /us-east-1/);
  assert.ok(!/자리표시자/.test(message));
});

test('계정·리전을 모르면 그 값을 지어내지 않는다', () => {
  const { blocking, message } = describePolicyFill({ policy_json: '{}' });
  assert.strictEqual(blocking, false);
  assert.ok(!/계정 /.test(message), '없는 계정 ID 를 문구에 넣었다');
  assert.ok(!/리전 /.test(message), '없는 리전을 문구에 넣었다');
});

test('정책이 없으면 아무 말도 하지 않는다', () => {
  assert.deepStrictEqual(describePolicyFill(null), { blocking: false, message: '' });
});

// ---------------------------------------------------------------------------
// 무엇에 대한 권한인지 보여준다
// ---------------------------------------------------------------------------

test('정책 범위를 요약한다 — 무엇에 대한 권한인지 안 보이면 검토가 불가능하다', () => {
  const scope = describePolicyScope({
    policy_json: '{}', action_count: 23, targets: ['ecs', 's3'],
    cluster: 'recoder-cluster', service: 'recoder-svc', ecr_repo: 'recoder-app',
  });
  assert.match(scope, /23/);
  assert.match(scope, /ecs/);
  assert.match(scope, /recoder-cluster/);
  assert.match(scope, /recoder-app/);
});

test('음성대조 — 모르는 값은 요약에 넣지 않는다', () => {
  const scope = describePolicyScope({ policy_json: '{}', action_count: 5 });
  assert.match(scope, /5/);
  assert.ok(!/클러스터/.test(scope));
  assert.ok(!/ECR/.test(scope));
});

// ---------------------------------------------------------------------------
// 배선 — **이게 없어서 이 기능이 여태 없는 것과 같았다**
// ---------------------------------------------------------------------------
//
// 순수 함수만 검사하면, 컴포넌트를 아무 데도 안 붙여도 전부 통과한다.
// 이 카드의 결함이 정확히 그거였다: 코어 엔드포인트는 멀쩡했고 호출부만
// 없었다. 그러니 "호출하고 있는가" 를 검사하지 않으면 의미가 없다.

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

test('ApiClient 가 /api/aws/policy 를 호출한다', () => {
  const source = read('../src/core/ApiClient.ts');
  assert.match(source, /\/api\/aws\/policy/, '코어 엔드포인트를 부르는 코드가 없다');
  assert.match(source, /getAwsPolicy/);
});

test('SidebarProvider 가 웹뷰 요청을 받아 넘긴다', () => {
  const source = read('../src/sidebar/SidebarProvider.ts');
  assert.match(source, /case 'aws\.policy':/, '웹뷰가 요청해도 받는 곳이 없다');
  assert.match(source, /getAwsPolicy/);
  assert.match(source, /aws\.policy\.result/);
});

test('복사는 웹뷰가 아니라 확장이 한다', () => {
  // 웹뷰의 navigator.clipboard 는 VS Code 웹뷰 샌드박스에서 막히는 경우가
  // 있고, 그러면 버튼을 눌러도 조용히 아무 일도 안 일어난다.
  const source = read('../src/sidebar/SidebarProvider.ts');
  assert.match(source, /case 'aws\.policy\.copy':/);
  assert.match(source, /vscode\.env\.clipboard\.writeText/);

  const guide = read('../webview-src/components/AwsPolicyGuide.tsx');
  //: 언급이 아니라 **호출**을 본다. 주석에서 이유를 설명하는 건 정상이다.
  assert.ok(
    !/navigator\.clipboard\.\w+\s*\(/.test(guide),
    '웹뷰에서 직접 복사한다 — 샌드박스에서 조용히 실패한다'
  );
  assert.match(guide, /postMessage\("aws\.policy\.copy"/, '복사 요청을 확장에 안 보낸다');
});

test('권한표가 화면에 실제로 붙어 있다', () => {
  // 컴포넌트만 만들고 아무 데도 안 쓰면, 고치기 전과 정확히 같은 상태다.
  const connection = read('../webview-src/components/AwsConnection.tsx');
  assert.match(connection, /AwsPolicyGuide/, '컴포넌트를 어디에도 붙이지 않았다');

  // 연결 전(키를 만들 때)과 연결 후(권한 점검 실패 시) 둘 다 필요하다.
  const uses = connection.match(/<AwsPolicyGuide/g) || [];
  assert.ok(
    uses.length >= 2,
    `연결 전·후 양쪽에 있어야 한다. 발견: ${uses.length}`
  );
});
