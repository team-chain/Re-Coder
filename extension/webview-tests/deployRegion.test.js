/**
 * 회귀: 배포 폼 리전이 자격증명 리전을 따라가는가 / 어긋나면 경고하는가
 *
 * 배경 — 데모에서 무슨 일이 있었나
 *   배포 센터 ECS 폼의 AWS 리전이 ap-northeast-2 로 고정돼 있었다. 그런데
 *   실제 자격증명(AWS Academy 랩)과 .env 의 AWS_REGION 은 us-east-1 이었다.
 *   그대로 「ECS 배포 실행」을 누르면 자격증명이 유효하지 않은 리전으로
 *   요청이 나가 실패한다. **데모에서 실제 배포까지 못 간 원인 중 하나.**
 *
 *   고치는 비용은 작지만, 놓치면 원인을 찾는 데 오래 걸리는 종류의 문제다.
 *   실패 메시지에는 "리전" 이야기가 나오지 않고 인증 오류만 뜬다.
 *
 * DoD 근거: 칸반 「배포 폼 기본 리전이 자격증명 리전과 불일치」(P1)
 *   "코어가 사용 중인 리전(AWS_REGION)이 폼 기본값으로 채워진다. 사용자가
 *    다른 리전을 입력하면 배포 실행 전에 불일치 경고가 뜬다."
 */
const test = require('node:test');
const assert = require('node:assert');

const {
  resolveRegionDefault,
  regionMismatchWarning,
  FALLBACK_REGION,
} = require('../out/webview-test/components/DeploymentCenter.js');

// ---------------------------------------------------------------------------
// 기본값이 자격증명 리전을 따라간다
// ---------------------------------------------------------------------------

test('자격증명 리전을 알면 손대지 않은 기본값을 그쪽으로 맞춘다 (데모에서 어긋났던 지점)', () => {
  assert.strictEqual(
    resolveRegionDefault('us-east-1', FALLBACK_REGION),
    'us-east-1',
    'ap-northeast-2 로 고정된 채로 남는다 — 인증이 유효하지 않은 리전으로 배포한다'
  );
});

test('빈 값에서도 자격증명 리전으로 채워진다', () => {
  assert.strictEqual(resolveRegionDefault('eu-west-1', ''), 'eu-west-1');
});

test('사용자가 직접 고친 값은 덮어쓰지 않는다', () => {
  // 다른 리전에 일부러 배포할 수 있다. 입력을 빼앗으면 안 된다.
  assert.strictEqual(resolveRegionDefault('us-east-1', 'eu-central-1'), 'eu-central-1');
});

test('[음성 대조] 자격증명 리전을 모르면 기존 값을 그대로 둔다', () => {
  // 항상 자격증명 리전을 반환한다면 위 테스트들은 아무것도 증명하지 못한다.
  assert.strictEqual(resolveRegionDefault('', 'eu-central-1'), 'eu-central-1');
  assert.strictEqual(resolveRegionDefault(null, ''), FALLBACK_REGION);
  assert.strictEqual(resolveRegionDefault(undefined, ''), FALLBACK_REGION);
});

// ---------------------------------------------------------------------------
// 불일치 경고
// ---------------------------------------------------------------------------

test('리전이 어긋나면 실행 전에 경고한다 — 양쪽 리전과 조치가 함께', () => {
  const warning = regionMismatchWarning('us-east-1', 'ap-northeast-2');

  assert.ok(warning, '경고가 없다 — 그대로 실행되어 인증 오류로만 끝난다');
  assert.ok(warning.includes('us-east-1'), '자격증명 리전이 안 적혀 있다');
  assert.ok(warning.includes('ap-northeast-2'), '입력한 리전이 안 적혀 있다');
  assert.ok(warning.includes('자격증명'), '무엇을 하면 되는지가 없다');
});

test('[음성 대조] 리전이 같으면 경고하지 않는다', () => {
  // 항상 경고하면 사용자는 경고를 무시하게 되고, 위 테스트도 무의미해진다.
  assert.strictEqual(regionMismatchWarning('us-east-1', 'us-east-1'), null);
});

test('대소문자·공백 차이는 불일치로 보지 않는다', () => {
  assert.strictEqual(regionMismatchWarning('us-east-1', ' US-East-1 '), null);
});

test('한쪽을 모르면 경고하지 않는다 (근거 없는 경고 금지)', () => {
  assert.strictEqual(regionMismatchWarning('', 'us-east-1'), null);
  assert.strictEqual(regionMismatchWarning('us-east-1', ''), null);
  assert.strictEqual(regionMismatchWarning(null, null), null);
});
