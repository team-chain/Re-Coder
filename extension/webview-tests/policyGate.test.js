/**
 * 회귀: ECS 정책 게이트(rego / 로컬 규칙) 거절이 화면에서 밟히고, 사유가 카드로 보이는가
 *
 * 배경 — 실기기 검증(2026-09-22)에서 무슨 일이 있었나
 *   MVP 수용 기준 C4 「정책 게이트 차단 + 사유」를 실기기에서 확인할 수 없었다.
 *   · 배포 센터 ECS 폼에 환경(production/staging) 입력이 없어서 요청은 항상
 *     environment="" 로 나갔고, production 규칙은 한 번도 평가되지 않았다.
 *   · 코어가 403 `{"detail":{"error":"policy_denied",…}}` 을 돌려줘도 확장은
 *     `describeHttpError` 가 객체 detail 을 모르니 JSON 원문을 `(HTTP 403)` 과
 *     함께 배너에 띄웠다 — 왜 막혔는지, 무엇을 고치면 되는지가 없었다.
 *   · ApiClient 는 403 을 전부 "세션 토큰 문제"로 보고 한 번 더 요청했다.
 *     정책 거절 한 번에 배포 요청이 두 번 나가 실패 기록이 둘씩 쌓였다.
 *
 * DoD 근거: 칸반 「정책 게이트(rego) 차단 경로를 화면에서 밟을 수 없음」
 *   "폼에 환경 선택 추가 + 차단 사유 카드"
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { describeHttpError, parseHttpErrorDetail, policyDenialFromDetail, CoreHttpError } = require('../out/core/httpError.js');
const { ECS_ENVIRONMENTS, policyDenialTitle } = require('../out/webview-test/components/DeploymentCenter.js');

const DENY_BODY = JSON.stringify({
  detail: {
    error: 'policy_denied',
    decision: 'deny',
    message: '정책 통과 실패(deny) — 프로덕션 배포는 main 브랜치에서만 가능합니다. 현재 브랜치: develop',
    fix_suggestion: 'main 브랜치로 전환한 뒤 다시 배포하세요.',
    deployment_id: 'ecs-20260922-0001',
  },
});

// ---------------------------------------------------------------------------
// 오류 본문 → 사람이 읽는 문장 / 구조화된 detail
// ---------------------------------------------------------------------------

test('객체 detail 도 JSON 원문 대신 message(+수정 제안) 문장으로 바꾼다', () => {
  const out = describeHttpError(403, DENY_BODY);
  assert.ok(!out.includes('{'), `JSON 원문이 새어 나갔다: ${out}`);
  assert.ok(out.includes('main 브랜치에서만'), '거절 사유가 없다');
  assert.ok(out.includes('다시 배포하세요'), '수정 제안이 없다');
});

test('parseHttpErrorDetail 은 객체 detail 만 돌려준다 (문자열·배열·평문은 null)', () => {
  assert.deepStrictEqual(parseHttpErrorDetail(DENY_BODY).error, 'policy_denied');
  assert.strictEqual(parseHttpErrorDetail('{"detail":"Invalid session token."}'), null);
  assert.strictEqual(parseHttpErrorDetail('{"detail":[{"loc":["body"],"msg":"x"}]}'), null);
  assert.strictEqual(parseHttpErrorDetail('Internal Server Error'), null);
  assert.strictEqual(parseHttpErrorDetail(''), null);
});

// ---------------------------------------------------------------------------
// 정책 거절 → 카드 모양
// ---------------------------------------------------------------------------

test('policy_denied detail 은 카드용 모양(code·decision·message·fix)으로 바뀐다', () => {
  const denial = policyDenialFromDetail(parseHttpErrorDetail(DENY_BODY));
  assert.ok(denial, '정책 거절을 알아보지 못했다');
  assert.strictEqual(denial.code, 'policy_denied');
  assert.strictEqual(denial.decision, 'deny');
  assert.ok(denial.message.includes('main 브랜치'));
  assert.strictEqual(denial.fix, 'main 브랜치로 전환한 뒤 다시 배포하세요.');
  assert.strictEqual(denial.deployment_id, 'ecs-20260922-0001');
});

test('코어가 fix_suggestion 을 비워 보내도 카드에는 다음 행동이 있다', () => {
  for (const code of ['policy_denied', 'approval_required', 'security_escalation_required', 'opa_unavailable', 'policy_evaluation_crashed']) {
    const denial = policyDenialFromDetail({ error: code, message: 'x' });
    assert.ok(denial && denial.fix.trim().length > 5, `${code}: 수정 방법이 비었다`);
    assert.ok(policyDenialTitle(denial).length > 5, `${code}: 카드 제목이 비었다`);
  }
  // 승인 필요와 규칙 위반은 같은 말로 시작하면 안 된다 — 사용자의 다음 행동이 다르다.
  assert.notStrictEqual(
    policyDenialTitle({ code: 'approval_required', decision: '', message: '', fix: '', deployment_id: null }),
    policyDenialTitle({ code: 'policy_denied', decision: '', message: '', fix: '', deployment_id: null }),
  );
});

test('정책 오류가 아닌 detail 은 카드로 만들지 않는다 (일반 배너 경로 유지)', () => {
  assert.strictEqual(policyDenialFromDetail({ error: 'concurrent_deployment', message: 'x' }), null);
  assert.strictEqual(policyDenialFromDetail({ message: 'x' }), null);
  assert.strictEqual(policyDenialFromDetail(null), null);
  assert.strictEqual(policyDenialFromDetail(undefined), null);
});

test('CoreHttpError 는 String(err) 로도 읽을 수 있고 detail 을 잃지 않는다', () => {
  const err = new CoreHttpError('정책 통과 실패', 403, parseHttpErrorDetail(DENY_BODY));
  assert.ok(err instanceof Error);
  assert.ok(String(err).includes('정책 통과 실패'));
  assert.strictEqual(err.status, 403);
  assert.strictEqual(err.detail.error, 'policy_denied');
});

// ---------------------------------------------------------------------------
// 배선 — 테스트가 순수 함수만 보고 통과하는 일이 없도록 소스도 본다
// ---------------------------------------------------------------------------

const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');

test('ECS 폼에 배포 환경 선택이 있고 staging/production 을 제공한다', () => {
  assert.deepStrictEqual(ECS_ENVIRONMENTS.map((o) => o.value), ['staging', 'production']);
  const src = read('webview-src/components/DeploymentCenter.tsx');
  assert.ok(/environment:\s*"staging"/.test(src), '폼 상태에 environment 기본값(staging)이 없다');
  assert.ok(src.includes('data-testid="ecs-environment"'), '환경 <select> 가 렌더되지 않는다');
  assert.ok(src.includes('"workspace.deploy.ecs.policyDenied"'), '웹뷰가 정책 거절 메시지를 받지 않는다');
  assert.ok(src.includes('<PolicyDenialCard'), '거절 카드가 렌더되지 않는다');
});

test('호스트는 정책 거절을 errorMessage 배너가 아니라 policyDenied 로 보낸다', () => {
  const src = read('src/sidebar/SidebarProvider.ts');
  const handler = src.slice(src.indexOf("case 'workspace.deploy.ecs':"), src.indexOf("case 'workspace.deploy.ecs.status':"));
  assert.ok(handler.includes('policyDenialFromDetail'), '정책 거절을 판별하지 않는다');
  assert.ok(handler.includes("'workspace.deploy.ecs.policyDenied'"), '정책 거절 메시지를 보내지 않는다');
});

test('ApiClient 는 구조화된 403 (정책 거절) 을 토큰 오류로 재시도하지 않는다', () => {
  const src = read('src/core/ApiClient.ts');
  assert.ok(src.includes('parseHttpErrorDetail(errorText)'), '본문을 보지 않고 403 을 재시도한다 — 배포 요청이 두 번 나간다');
  assert.ok(/res\.status === 403 && !detail/.test(src), '403 재시도 조건이 detail 유무를 보지 않는다');
  assert.ok(src.includes('new CoreHttpError('), 'deployEcs 가 detail 을 잃고 평문 Error 만 던진다');
});
