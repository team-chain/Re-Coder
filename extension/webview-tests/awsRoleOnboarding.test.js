/**
 * 프로그램 안 최소권한 역할 온보딩 배선 — 보드 카드 「AWS 온보딩 마찰 제거」 두 번째 절반.
 *
 * 여기서 고정하는 것
 *   1. 화면(AwsConnection)은 프로필 연결의 **기본**을 역할 경로(aws.role.setup)로
 *      보내고, 끄면 예전 프로필 연결(aws.connect.profile)로 보낸다.
 *   2. 콘솔 폴백 결과(mode=console_fallback)를 받으면 원클릭 IAM 셋업 버튼을
 *      같은 자리에 띄운다 — 권한이 모자란 사용자가 막다른 길에 서지 않게.
 *   3. 호스트(SidebarProvider)는 성공하면 역할 ARN 만 보관하고(비밀 아님),
 *      폴백이면 프로필 연결은 해 준 뒤 폴백을 알린다.
 *   4. CoreManager 는 역할 ARN 을 RECODER_ASSUME_ROLE_ARN 으로 코어에 넘기고,
 *      기반(키/프로필)이 바뀌거나 연결 해제하면 함께 지운다.
 *   5. ApiClient 가 부르는 경로가 Core 에 실제로 있다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

const UI = read('../webview-src/components/AwsConnection.tsx');
const HOST = read('../src/sidebar/SidebarProvider.ts');
const API = read('../src/core/ApiClient.ts');
const MANAGER = read('../src/core/CoreManager.ts');
const CORE_AWS = fs.readFileSync(
  path.join(__dirname, '../../core/api/routes/aws.py'), 'utf8',
);

const block = (src, marker) => {
  const start = src.indexOf(marker);
  assert.notStrictEqual(start, -1, `${marker} 가 없다`);
  const next = src.indexOf("case '", start + marker.length);
  return src.slice(start, next === -1 ? undefined : next);
};

test('화면은 프로필 연결의 기본을 역할 경로로 보내고, 끄면 예전 경로다', () => {
  assert.match(UI, /useState\(true\)/, '역할 경로가 기본(권장)이 아니다');
  assert.match(UI, /useRoleRef\.current \? "aws\.role\.setup" : "aws\.connect\.profile"/,
    '토글에 따라 역할/프로필 경로를 고르지 않는다');
  //: 연결된 뒤에도 역할로 바꿀 수 있어야 한다 — 키로 연결한 사용자용.
  assert.match(UI, /postMessage\("aws\.role\.setup", \{\}\)/, '지금 자격증명 기반 전환 버튼이 없다');
  assert.match(UI, /aws\.role\.refresh/, '임시 자격증명 갱신 버튼이 없다');
});

test('화면은 콘솔 폴백 결과를 받으면 원클릭 IAM 셋업으로 이어 준다', () => {
  assert.match(UI, /type === "aws\.role\.result"/, '역할 결과 핸들러가 없다');
  const fallback = UI.slice(UI.indexOf('roleFallbackBox ='), UI.indexOf('roleFallbackBox =') + 1200);
  assert.match(fallback, /console_fallback/);
  assert.match(fallback, /postMessage\("aws\.onboarding"\)/, '폴백에서 콘솔 경로를 열지 않는다');
  //: 역할 모드는 화면에서 구분돼야 한다 — "키 끝 ****" 로 보이면 사용자는 장기 키가 있다고 믿는다.
  assert.match(UI, /status\.storage === "assumed_role"/);
  assert.match(UI, /장기 키를 저장하지 않습니다/);
});

test('호스트는 성공하면 역할 ARN 만 보관하고, 폴백이면 프로필 연결 뒤 폴백을 알린다', () => {
  const setup = block(HOST, "case 'aws.role.setup'");
  assert.match(setup, /setupAwsRole\(\{ profile, region \}\)/);
  assert.match(setup, /storeAwsRole\(/, '역할 ARN 을 보관하지 않는다');
  assert.doesNotMatch(setup, /storeAwsCredentials/, '역할 경로에서 키를 저장하면 안 된다');
  //: 폴백 — 프로필로는 연결해 둔다(배포는 되되 권한만 안 좁혀진 상태).
  assert.match(setup, /connectAwsProfile\(\{ profile, region \}\)/);
  assert.match(setup, /mode: result\.mode, message: result\.message/);
  assert.match(setup, /denied_action: result\.denied_action/);
  const refresh = block(HOST, "case 'aws.role.refresh'");
  assert.match(refresh, /refreshAwsRole\(\)/);
});

test('CoreManager 는 역할 ARN 을 코어에 넘기고 기반이 바뀌면 지운다', () => {
  assert.match(MANAGER, /RECODER_ASSUME_ROLE_ARN: roleArn/, '코어에 역할 ARN 을 넘기지 않는다');
  assert.match(MANAGER, /async storeAwsRole\(roleArn: string\)/);
  //: 키 저장 · 프로필 저장 · 연결 해제 — 셋 다 역할 ARN 을 지워야 한다.
  const count = (MANAGER.match(/globalState\.update\(AWS_ROLE_ARN_STATE, undefined\)/g) || []).length;
  assert.ok(count >= 3, `역할 ARN 정리가 ${count}곳뿐이다 (키/프로필/해제 셋 필요)`);
  //: 코어도 같은 이름을 읽는다.
  assert.ok(CORE_AWS.includes('ENV_ASSUME_ROLE_ARN = "RECODER_ASSUME_ROLE_ARN"'),
    'Core 가 읽는 환경변수 이름이 다르다');
});

test('ApiClient 경로가 Core 에 실제로 있다', () => {
  for (const route of ['/api/aws/role/setup', '/api/aws/role/refresh']) {
    assert.ok(API.includes(`'POST', '${route}'`), `ApiClient 에 ${route} 가 없다`);
    assert.ok(CORE_AWS.includes(`"${route}"`), `Core 에 ${route} 라우트가 없다 (= 404)`);
  }
});

test('연결 해제는 보안 금고와 코어 프로세스 양쪽을 지운다 (재사용 코어에서도)', () => {
  const start = HOST.indexOf("case 'aws.clear'");
  const block = HOST.slice(start, HOST.indexOf("case '", start + 10));
  assert.match(block, /clearAwsCredentials\(\)/);
  //: 재사용 중인 코어는 restart 가 죽이지 않는다 — 코어 API 로도 지워야 해제된다.
  assert.match(block, /_apiClient\.clearAws\(\)/, '코어의 /api/aws/clear 를 부르지 않는다');
  assert.ok(block.indexOf('clearAws()') < block.indexOf('restart()'), '코어 해제가 재시작보다 먼저여야 한다');
});
