/**
 * 회귀: 코어 연결이 끊긴 뒤 다시 붙는가 / 감지 결과가 undefined 로 새지 않는가
 *
 * 배경 — 데모에서 무슨 일이 있었나
 *   배포 센터에서 동시에 세 가지가 떴다.
 *     · 상단 상태: 연결 안 됨 (빨강)
 *     · 하단: fetch failed
 *     · 감지 결과: 감지됨: undefined
 *   「다시 검사」를 눌러도 그대로였고, 창을 리로드해야만 돌아왔다.
 *   같은 세션에서 코어를 여러 번 재시작했고 포트가 17894 → 17895 → 17894 로
 *   튀었다.
 *
 * 원인
 *   CoreManager._ensureRunning() 이 재연결 경로(runtime.json 읽기 → health →
 *   포트 probe)를 `if (!workspaceCore)` 로 감싸고 있었다. 즉 **ReCoder 저장소를
 *   워크스페이스로 연 개발 모드에서는 떠 있는 코어를 찾는 시도조차 하지 않고**
 *   곧장 재spawn 으로 떨어졌다. 그런데 개발 중에는 코어를 직접 띄워 두는 게
 *   정상 사용법이라, 이 경로가 사실상 항상 발동했다. 재spawn 은 싱글턴 락·포트와
 *   부딪히며 포트를 옮겨 다녔고, 확장은 끝내 다시 붙지 못했다.
 *
 *   원래 이 가드가 막으려던 것은 "예전 VSIX 가 남긴 **번들** 코어를 재사용하는
 *   것"이었다. 그 구분을 이제 runtime.json 의 entrypoint 로 한다.
 *
 * DoD 근거: 칸반 「코어 연결이 끊긴 뒤 복구되지 않음」(P1)
 */
const test = require('node:test');
const assert = require('node:assert');
const path = require('path');

const { shouldReuseRunningCore, samePath } = require('../out/core/coreReuse.js');
const { describeDetection } = require('../out/webview-test/components/DeploymentCenter.js');

const WS_CORE = path.resolve('/proj/Re-Coder/core/main.py');
const BUNDLED = path.resolve('/home/u/.recoder/bin/recoder-core');

// ---------------------------------------------------------------------------
// 재연결 판단
// ---------------------------------------------------------------------------

test('개발 모드에서 직접 띄운 워크스페이스 코어는 재사용한다 (데모에서 막혔던 경로)', () => {
  assert.strictEqual(
    shouldReuseRunningCore(WS_CORE, WS_CORE),
    true,
    '내가 띄운 코어인데도 재사용하지 않는다 — 재spawn 으로 떨어져 포트가 튄다'
  );
});

test('[음성 대조] 개발 모드에서 예전 VSIX 의 번들 코어는 재사용하지 않는다', () => {
  // 이게 무너지면 위 테스트는 "항상 true 를 반환한다"는 뜻이라 의미가 없다.
  // 원래 가드가 막으려던 것도 정확히 이 경우다.
  assert.strictEqual(
    shouldReuseRunningCore(WS_CORE, BUNDLED),
    false,
    '남의(번들) 코어를 재사용한다 — 옛날 API 로 조용히 동작하게 된다'
  );
});

test('개발 모드가 아니면(일반 사용자) 떠 있는 코어를 그대로 쓴다', () => {
  assert.strictEqual(shouldReuseRunningCore(null, BUNDLED), true);
  assert.strictEqual(shouldReuseRunningCore(null, null), true);
});

test('entrypoint 가 없는 구버전 코어는 개발 모드에서 재사용하지 않는다', () => {
  // 구분할 근거가 없는 코어 = 이 필드가 생기기 전에 깔린 오래된 코어.
  assert.strictEqual(shouldReuseRunningCore(WS_CORE, null), false);
  assert.strictEqual(shouldReuseRunningCore(WS_CORE, ''), false);
});

test('경로 비교는 구분자 차이를 흡수한다', () => {
  assert.ok(samePath('/proj/core/main.py', '/proj/./core/main.py'));
  assert.ok(samePath('/proj/core/', '/proj/core'));
});

test('[음성 대조] 다른 파일은 같은 경로로 판정하지 않는다', () => {
  assert.ok(!samePath('/proj/core/main.py', '/proj/core/server.py'));
  assert.ok(!samePath('/proj-a/core/main.py', '/proj-b/core/main.py'));
});

// ---------------------------------------------------------------------------
// 감지 결과 표시
// ---------------------------------------------------------------------------

test('감지 결과가 없으면 undefined 가 아니라 사람이 읽을 상태를 보여준다', () => {
  const out = describeDetection(undefined);
  assert.ok(!out.includes('undefined'), `undefined 가 그대로 노출된다: ${out}`);
  assert.ok(out.includes('확인할 수 없음'));
});

test('null / 빈 문자열 / 공백도 같은 처리', () => {
  for (const value of [null, '', '   ']) {
    const out = describeDetection(value);
    assert.ok(!out.includes('undefined'), `${JSON.stringify(value)} → ${out}`);
    assert.ok(out.includes('확인할 수 없음'));
  }
});

test('[음성 대조] 감지 결과가 있으면 그 내용을 그대로 보여준다', () => {
  // 항상 "확인할 수 없음" 을 반환하면 위 테스트들은 아무것도 증명하지 못한다.
  const out = describeDetection('Node.js 서버형 앱 (package.json · express)');
  assert.strictEqual(out, '감지됨: Node.js 서버형 앱 (package.json · express)');
  assert.ok(!out.includes('확인할 수 없음'));
});
