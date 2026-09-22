/**
 * 죽은 UI 경로 제거/복구 — 보드 이슈 「죽은 버튼 3종 + S3 탭 진입 경로 없음」.
 *
 * 원칙: 반응 없는 버튼은 없는 버튼보다 나쁘다. 여기서는 (1) 죽었던 경로가
 * 되살아나지 않았는지, (2) 유일하게 복구한 경로(S3 탭)가 실제로 있는지 고정한다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

test('BuildMode 에 핸들러 없는 build.scan 버튼이 없다', () => {
  const source = read('../webview-src/components/BuildMode.tsx');
  //: 핸들러(SidebarProvider case 'build.scan')를 추가하기 전에는 이 메시지를
  //: 보내는 버튼이 있으면 안 된다. 되살릴 때는 이 테스트를 핸들러 검사로 바꿀 것.
  assert.ok(!/postMessage\("build\.scan"/.test(source),
    'build.scan 을 보내는 버튼이 돌아왔는데 받는 핸들러가 없다');
});

test('ShipMode 에 빈 onClick 버튼이 없다', () => {
  const source = read('../webview-src/components/ShipMode.tsx');
  assert.ok(!/onClick=\{\(\)\s*=>\s*\{\s*(\/\*[^*]*\*\/)?\s*\}\}/.test(source),
    '아무 일도 하지 않는 onClick 버튼이 있다');
});

test('Home 에 라벨-동작이 어긋난 Discord 행이 없다', () => {
  const source = read('../webview-src/App.tsx');
  //: 주석 속 언급은 허용 — 실제 렌더되는 라벨(actionLabel)만 검사한다.
  assert.ok(!/actionLabel="봇 초대"/.test(source),
    '"봇 초대" 라벨이 돌아왔다 — 실제 Discord 초대 동작과 함께가 아니면 안 된다');
});

test('배포 센터 탭에서 S3 에 언제든 도달할 수 있다', () => {
  const source = read('../webview-src/components/DeploymentCenter.tsx');
  const anchor = source.indexOf('["decision", "배포 결정"]');
  assert.notStrictEqual(anchor, -1, '탭 배열을 찾지 못했다');
  const tabArray = source.slice(anchor, source.indexOf('].map', anchor));
  assert.match(tabArray, /\["s3"/,
    'S3 탭이 없다 — 배포 결정에서 고른 순간에만 도달 가능하고 되돌아올 수 없다');
  //: 패널 자체도 남아 있어야 탭이 의미가 있다.
  assert.match(source, /target === "s3"/, 'S3 패널이 사라졌다');
});
