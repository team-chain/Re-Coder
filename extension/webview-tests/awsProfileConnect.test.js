/**
 * AWS 프로필 원클릭 연결 — 온보딩 마찰 제거
 *
 * 배경
 *   연결 수단이 "키 붙여넣기"뿐이라, AWS CLI 를 이미 쓰는 사용자도 콘솔에서
 *   키를 다시 찾아 복사해야 했다. 자격증명이 이미 이 컴퓨터(~/.aws)에 있는데
 *   재입력을 강요하는 것은 마찰일 뿐 보안 이득이 없다.
 *
 * 여기서 검사하는 것
 *   순수 로직은 코어 pytest(test_aws_profile_connect.py)가 검사한다. 이
 *   파일은 **배선**을 검사한다 — 권한표(P0) 때 배운 그대로, 끝에서 끝까지
 *   이어져 있지 않으면 기능은 없는 것과 같다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

// ---------------------------------------------------------------------------
// 확장 → 코어
// ---------------------------------------------------------------------------

test('ApiClient 가 /api/aws/connect-profile 을 호출한다', () => {
  const source = read('../src/core/ApiClient.ts');
  assert.match(source, /\/api\/aws\/connect-profile/, '코어 엔드포인트를 부르는 코드가 없다');
  assert.match(source, /connectAwsProfile/);
  //: 프로필 연결 요청에 키 필드가 실리면 이 경로의 존재 이유가 사라진다.
  const method = source.slice(source.indexOf('connectAwsProfile'), source.indexOf('connectAwsProfile') + 600);
  assert.ok(!/access_key_id|secret_access_key/.test(method), '프로필 연결 요청에 키가 실린다');
});

test('SidebarProvider 가 프로필 연결 요청을 받아 넘긴다', () => {
  const source = read('../src/sidebar/SidebarProvider.ts');
  assert.match(source, /case 'aws\.connect\.profile':/, '웹뷰가 요청해도 받는 곳이 없다');
  assert.match(source, /connectAwsProfile/);
  //: 키 연결과 같은 결과 채널을 써야 화면이 두 경로를 따로 처리하지 않는다.
  assert.match(source, /aws\.configure\.result/);
  //: 코어 재시작 후에도 선택이 살아남아야 한다.
  assert.match(source, /storeAwsProfile/, '프로필 선택을 보관하지 않아 재시작하면 풀린다');
});

// ---------------------------------------------------------------------------
// 재시작 생존 + 키/프로필 상호 배타
// ---------------------------------------------------------------------------
//
// AWS_ACCESS_KEY_ID 는 AWS_PROFILE 보다 우선한다. 키와 프로필이 동시에
// 남으면, 재시작한 코어는 화면 표시(프로필 A)와 다른 계정(옛 키 B)으로
// 연결된다. 저장은 항상 반대쪽을 지워야 한다.

test('CoreManager 가 프로필 상태를 보관하고 재시작 시 주입한다', () => {
  const source = read('../src/core/CoreManager.ts');
  assert.match(source, /AWS_PROFILE_STATE/);
  assert.match(source, /storeAwsProfile/);
  assert.match(source, /AWS_PROFILE:/, '_awsEnv 가 프로필을 코어에 주입하지 않는다');
});

test('프로필 저장은 남은 키를 지운다 (상호 배타)', () => {
  const source = read('../src/core/CoreManager.ts');
  const body = source.slice(
    source.indexOf('async storeAwsProfile'),
    source.indexOf('clearAwsCredentials'),
  );
  assert.match(body, /secrets\.delete\(AWS_ACCESS_KEY_SECRET\)/,
    '옛 키가 남아 프로필을 이긴다 — 화면과 다른 계정으로 배포된다');
  assert.match(body, /secrets\.delete\(AWS_SECRET_KEY_SECRET\)/);
});

test('키 저장은 남은 프로필 상태를 지운다 (반대 방향)', () => {
  const source = read('../src/core/CoreManager.ts');
  const body = source.slice(
    source.indexOf('async storeAwsCredentials'),
    source.indexOf('storeAwsProfile'),
  );
  assert.match(body, /AWS_PROFILE_STATE, undefined/,
    '프로필 상태가 남는다 — 마지막 선택이 무엇인지 애매해진다');
});

test('연결 해제는 프로필 상태까지 지운다', () => {
  const source = read('../src/core/CoreManager.ts');
  const body = source.slice(source.indexOf('async clearAwsCredentials'));
  assert.match(body, /AWS_PROFILE_STATE, undefined/,
    '해제했는데 재시작하면 프로필로 다시 연결된다');
});

// ---------------------------------------------------------------------------
// 화면
// ---------------------------------------------------------------------------

test('연결 화면이 프로필 목록을 요청하고 원클릭 연결을 제공한다', () => {
  const source = read('../webview-src/components/AwsConnection.tsx');
  assert.match(source, /aws\.listProfiles/, '목록을 요청하지 않으면 섹션이 영영 안 뜬다');
  assert.match(source, /aws\.connect\.profile/, '버튼이 연결 요청을 안 보낸다');
  //: 프로필이 없는 사용자(CLI 미사용)는 지금과 완전히 같은 화면을 봐야 한다.
  assert.match(source, /profiles\.length > 0/, '빈 목록에도 섹션이 떠서 소음이 된다');
});

test('연결된 화면이 프로필 연결임을 표시한다', () => {
  //: "키 끝 ××××" 자리에 아무것도 없으면 사용자는 무엇으로 연결됐는지 모른다.
  const source = read('../webview-src/components/AwsConnection.tsx');
  assert.match(source, /aws_profile/, '프로필 연결과 키 연결이 화면에서 구분되지 않는다');
});
