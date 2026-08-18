/**
 * 회귀: 배포 폼 리전이 코어 리전을 따라가는가 / 어긋나면 경고하는가
 *
 * 배경 — 데모에서 무슨 일이 있었나
 *   배포 센터 ECS 폼의 AWS 리전이 ap-northeast-2 로 고정돼 있었다. 그런데
 *   실제 자격증명(AWS Academy 랩)과 .env 의 AWS_REGION 은 us-east-1 이었다.
 *   그대로 「ECS 배포 실행」을 누르면 자격증명이 유효하지 않은 리전으로
 *   요청이 나가 실패한다. **데모에서 실제 배포까지 못 간 원인 중 하나.**
 *
 * 코드리뷰에서 추가로 드러난 것
 *   (1) 「자격증명 리전」이라고 부르던 값은 사실 코어의 `AWS_REGION` 이고,
 *       그 환경변수는 AWS 연결 폼이 써넣는다. 그 폼의 기본값도
 *       ap-northeast-2 였다. 그래서 us-east-1 자격증명을 넣어도 코어 리전이
 *       ap-northeast-2 로 바뀌고, **불일치 경고까지 조용해졌다.** 원래 버그가
 *       그대로 재현되는데 UI 는 "일치한다" 고 보증하는 상태였다.
 *   (2) 경고가 아니라 우회 불가능한 **차단**이었다. 함수 docstring 은
 *       "막지는 않는다" 라고 적혀 있는데 호출부는 곧바로 return 했다.
 *       다른 리전의 ECR/클러스터로 배포하는 정상적인 사용이 불가능했고,
 *       EC2 에는 원래 없던 차단이 새로 생겼다.
 *   (3) 자격증명이 없어도(`ready:false`) 서버가 돌려주는 기본 리전을 받아
 *       두고, 없는 자격증명을 근거로 사용자를 막았다.
 *   (4) 사용자가 고른 리전이 다음 `aws.status` 에 조용히 덮어써졌다.
 *   (5) 이 테스트가 순수 함수 두 개만 호출해서, 핸들러를 통째로 지워도
 *       전부 통과했다.
 *
 * DoD 근거: 칸반 「배포 폼 기본 리전이 자격증명 리전과 불일치」(P1)
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const {
  resolveRegionDefault,
  regionMismatchWarning,
  regionBlockingError,
  regionGate,
  coreRegionFromStatus,
  FALLBACK_REGION,
} = require('../out/webview-test/components/DeploymentCenter.js');

// ---------------------------------------------------------------------------
// 기본값이 코어 리전을 따라간다
// ---------------------------------------------------------------------------

test('코어 리전을 알면 손대지 않은 기본값을 그쪽으로 맞춘다 (데모에서 어긋났던 지점)', () => {
  assert.strictEqual(
    resolveRegionDefault('us-east-1', FALLBACK_REGION, false),
    'us-east-1',
    'ap-northeast-2 로 고정된 채로 남는다 — 인증이 유효하지 않은 리전으로 배포한다'
  );
});

test('빈 값에서도 코어 리전으로 채워진다', () => {
  assert.strictEqual(resolveRegionDefault('eu-west-1', '', false), 'eu-west-1');
});

test('사용자가 건드린 값은 덮어쓰지 않는다', () => {
  // 다른 리전에 일부러 배포할 수 있다. 입력을 빼앗으면 안 된다.
  assert.strictEqual(resolveRegionDefault('us-east-1', 'eu-central-1', true), 'eu-central-1');
});

test('사용자가 코어와 **같은 값**을 일부러 골라도 덮어쓰지 않는다', () => {
  // 예전에는 "값이 기본값과 같으면 안 건드린 것" 으로 추측해서, 일부러 고른
  // 경우와 구분하지 못했다. touched 를 따로 받는 이유다.
  assert.strictEqual(
    resolveRegionDefault('us-east-1', FALLBACK_REGION, true),
    FALLBACK_REGION,
    '사용자 선택이 다음 aws.status 에 조용히 덮어써진다'
  );
});

test('코어 리전을 모르면 폼 값을 그대로 둔다 (하드코딩 기본값을 끼워 넣지 않는다)', () => {
  assert.strictEqual(resolveRegionDefault('', '', false), '');
  assert.strictEqual(resolveRegionDefault(null, 'us-west-2', false), 'us-west-2');
});

// ---------------------------------------------------------------------------
// ready 가 아닌 상태의 리전은 믿지 않는다
// ---------------------------------------------------------------------------

test('자격증명이 없으면(ready:false) 서버가 준 리전을 쓰지 않는다', () => {
  // /api/aws/status 는 자격증명이 없어도 리전을 돌려준다 — AWS_REGION 이
  // 없으면 서버 상수 ap-northeast-2 다. 그걸 근거로 사용자를 막으면,
  // 존재하지 않는 자격증명을 이유로 배포를 차단하게 된다.
  assert.strictEqual(coreRegionFromStatus({ ready: false, region: 'ap-northeast-2' }), '');
  assert.strictEqual(coreRegionFromStatus(null), '');
  assert.strictEqual(coreRegionFromStatus(undefined), '');
});

test('음성대조 — ready 면 리전을 그대로 쓴다', () => {
  assert.strictEqual(coreRegionFromStatus({ ready: true, region: ' us-east-1 ' }), 'us-east-1');
});

// ---------------------------------------------------------------------------
// 불일치 경고
// ---------------------------------------------------------------------------

test('폼 리전이 코어 리전과 다르면 경고 문구가 나온다', () => {
  const warning = regionMismatchWarning('us-east-1', 'ap-northeast-2');
  assert.ok(warning, '경고가 없으면 인증 실패의 원인을 알 수 없다');
  assert.match(warning, /us-east-1/);
  assert.match(warning, /ap-northeast-2/);
});

test('경고 문구가 이 값을 "자격증명 리전" 이라고 말하지 않는다', () => {
  // 이 값은 코어의 AWS_REGION 이다. 자격증명에서 유도한 게 아니다.
  // 사실이 아닌 라벨을 붙이면, 진짜 불일치를 못 알아채게 된다.
  const warning = regionMismatchWarning('us-east-1', 'eu-west-1');
  assert.ok(!/자격증명이 유효한 리전/.test(warning), '거짓 라벨이 남아 있다');
  assert.match(warning, /코어가 사용 중인 리전/);
});

test('음성대조 — 같으면 경고하지 않는다', () => {
  assert.strictEqual(regionMismatchWarning('us-east-1', 'us-east-1'), null);
  assert.strictEqual(regionMismatchWarning('us-east-1', 'US-EAST-1'), null);
});

test('음성대조 — 한쪽을 모르면 경고하지 않는다', () => {
  assert.strictEqual(regionMismatchWarning('', 'us-east-1'), null);
  assert.strictEqual(regionMismatchWarning('us-east-1', ''), null);
});

// ---------------------------------------------------------------------------
// 빈 리전은 막는다 (이건 취향이 아니라 필수 입력)
// ---------------------------------------------------------------------------

test('리전이 비어 있으면 배포를 막는다', () => {
  const error = regionBlockingError('');
  assert.ok(error, '빈 값이면 예전처럼 하드코딩 기본값이 대신 나간다');
  assert.match(error, /리전/);
});

test('음성대조 — 값이 있으면 막지 않는다', () => {
  assert.strictEqual(regionBlockingError('us-east-1'), null);
});

// ---------------------------------------------------------------------------
// 배선 검사 — **순수 함수만 검사하면 핸들러를 지워도 전부 통과한다**
// ---------------------------------------------------------------------------
//
// 이 저장소에는 DOM 이 없어서(jsdom 미설치) 컴포넌트를 렌더해 message
// 이벤트를 쏘는 검사는 못 한다. 대신 컴파일된 결과물에서 **실제로 호출하고
// 있는지**를 본다. 예전 테스트는 이걸 안 봐서, aws.status 핸들러를 통째로
// 지워도, setEcs 를 빼도, 페이로드 키 이름이 틀려도 초록이었다.

const compiled = fs.readFileSync(
  path.join(__dirname, '../out/webview-test/components/DeploymentCenter.js'),
  'utf8'
);

test('aws.status 핸들러가 실제로 폼 리전을 갱신한다', () => {
  assert.match(compiled, /aws\.status/, 'aws.status 를 아예 안 다룬다');
  assert.match(compiled, /coreRegionFromStatus/, 'ready 검사를 안 거치고 리전을 받는다');
  assert.match(compiled, /resolveRegionDefault/, '폼 기본값을 갱신하지 않는다');
});

test('배포 실행이 리전 검사를 거친다', () => {
  assert.match(compiled, /passesRegionCheck/, '배포 전에 리전을 검사하지 않는다');
  //: 호출부는 ECS·EC2 둘. 한쪽만 검사하면 나머지가 조용히 예전 동작으로
  //: 남는다 — 실제로 EC2 가 그런 상태였다.
  const calls = compiled.match(/passesRegionCheck\(/g) || [];
  assert.ok(
    calls.length >= 2,
    `ECS·EC2 양쪽에서 검사해야 한다. 호출 발견: ${calls.length}`
  );
  //: 두 호출이 서로 다른 폼의 리전을 본다는 것까지 확인한다.
  assert.match(compiled, /passesRegionCheck\(ecs\.aws_region\)/);
  assert.match(compiled, /passesRegionCheck\(ec2\.aws_region\)/);
});

test('불일치는 한 번 경고하고, 다시 누르면 진행한다 (차단이 아니다)', () => {
  // 예전 구현은 경고를 만들면 곧바로 return 해서 교차 리전 배포가
  // **아예 불가능**했다. 팀의 ECR/클러스터가 다른 리전에 있는 건 흔하다.
  const first = regionGate('us-east-1', 'us-west-2', '');
  assert.strictEqual(first.ok, false, '경고 없이 바로 배포하면 조용히 실패한다');
  assert.ok(first.message, '왜 막혔는지 안 알려준다');
  assert.ok(first.ack, '확인했다는 걸 기억할 방법이 없다 — 영원히 막힌다');

  const second = regionGate('us-east-1', 'us-west-2', first.ack);
  assert.strictEqual(second.ok, true, '확인해도 진행이 안 된다 — 경고가 아니라 차단이다');
  assert.strictEqual(second.message, null);
});

test('다른 조합의 확인은 재사용되지 않는다', () => {
  // eu-west-1 로 확인했다고 해서 ap-northeast-2 배포까지 통과시키면,
  // 두 번째 리전은 경고를 아예 못 보게 된다.
  const gate = regionGate('us-east-1', 'ap-northeast-2', 'us-east-1|eu-west-1');
  assert.strictEqual(gate.ok, false);
  assert.ok(gate.message);
});

test('음성대조 — 리전이 같으면 확인 없이 바로 진행한다', () => {
  const gate = regionGate('us-east-1', 'us-east-1', '');
  assert.strictEqual(gate.ok, true);
  assert.strictEqual(gate.message, null);
  assert.strictEqual(gate.ack, null);
});

test('빈 리전은 확인해도 통과시키지 않는다', () => {
  // 이건 취향이 아니라 필수 입력이다. 확인으로 넘길 수 있으면 안 된다.
  const gate = regionGate('us-east-1', '', 'us-east-1|');
  assert.strictEqual(gate.ok, false);
  assert.match(gate.message, /리전/);
});

test('컴포넌트가 그 게이트를 실제로 쓴다', () => {
  assert.match(compiled, /regionGate\(/, '순수 함수만 있고 호출하는 데가 없다');
});

test('폼 초기값에 리전이 하드코딩돼 있지 않다', () => {
  // 이게 사고의 근원이었다. 코어 리전을 모르는 동안 그럴듯한 오답이
  // 들어가 있으면, 사용자는 그대로 배포를 누른다.
  assert.ok(
    !/aws_region:\s*FALLBACK_REGION/.test(compiled),
    '폼 기본값이 다시 ap-northeast-2 로 고정됐다'
  );
});

test('AWS 연결 폼도 리전을 하드코딩하지 않는다', () => {
  // 여기가 코어의 AWS_REGION 을 써넣는 곳이다. 여기에 박아 두면 배포 센터가
  // 아무리 잘 따라가도 따라가는 대상 자체가 틀린 값이 된다.
  const connection = fs.readFileSync(
    path.join(__dirname, '../webview-src/components/AwsConnection.tsx'),
    'utf8'
  );
  assert.ok(
    !/useState\("ap-northeast-2"\)/.test(connection),
    '연결 폼이 리전을 ap-northeast-2 로 고정한다'
  );
  assert.ok(
    !/region:\s*region\.trim\(\)\s*\|\|\s*"ap-northeast-2"/.test(connection),
    '빈 리전을 하드코딩 값으로 채워 전송한다'
  );
});
