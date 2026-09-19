/**
 * 원클릭 IAM 셋업 배선 — 보드 카드 「AWS 온보딩 마찰 제거」.
 *
 * 여기서 고정하는 것
 *   1. 화면(AwsConnection)이 aws.onboarding 을 보내고, 결과를 받아 다음
 *      단계를 표시한다.
 *   2. 호스트(SidebarProvider)가 그 메시지를 받아 링크를 열고, 템플릿이
 *      호스팅 안 됐으면 본문을 클립보드에 복사한다 — 폴백에서도 사용자가
 *      붙여넣기 한 번이면 되게.
 *   3. ApiClient 가 부르는 경로가 Core 에 실제로 있다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

const UI = read('../webview-src/components/AwsConnection.tsx');
const HOST = read('../src/sidebar/SidebarProvider.ts');
const API = read('../src/core/ApiClient.ts');
const CORE_AWS = fs.readFileSync(
  path.join(__dirname, '../../core/api/routes/aws.py'), 'utf8',
);

test('화면이 aws.onboarding 을 보내고 결과를 받는다', () => {
  assert.match(UI, /postMessage\("aws\.onboarding"\)/, '버튼이 메시지를 안 보낸다');
  assert.match(UI, /aws\.onboarding\.result/, '결과를 받는 핸들러가 없다');
  //: 링크만 열고 끝나면 사용자는 브라우저에서 뭘 할지 모른다.
  assert.match(UI, /onboarding\?\.steps/, '다음 단계 안내를 표시하지 않는다');
});

test('호스트가 링크를 열고, 폴백에서는 템플릿을 클립보드에 복사한다', () => {
  const start = HOST.indexOf("case 'aws.onboarding'");
  assert.notStrictEqual(start, -1, '호스트에 aws.onboarding 핸들러가 없다');
  const block = HOST.slice(start, HOST.indexOf("case '", start + 10));
  assert.match(block, /getAwsOnboardingLink\(\)/);
  assert.match(block, /openExternal/, '브라우저를 열지 않는다');
  //: 폴백 플로우의 핵심 — 템플릿 본문이 클립보드에 있어야 업로드가 한 번에 된다.
  assert.match(block, /clipboard\.writeText\(link\.template_body\)/, '폴백에서 템플릿을 복사하지 않는다');
  //: quick-create 가 없으면 콘솔 업로드 화면으로.
  assert.match(block, /quick_create_url \|\| link\.console_upload_url/);
});

test('ApiClient 경로가 Core 에 실제로 있다', () => {
  const m = API.match(/'GET', '(\/api\/aws\/onboarding-link)'/);
  assert.ok(m, 'ApiClient 에 온보딩 경로가 없다');
  assert.ok(CORE_AWS.includes(`"${m[1]}"`), `Core 에 ${m[1]} 라우트가 없다 (= 404)`);
});
