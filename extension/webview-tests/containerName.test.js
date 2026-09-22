/**
 * 로컬 배포 컨테이너 이름 400 — 보드 이슈
 * 「로컬 배포 컨테이너 이름에 이미지 태그가 들어가 400 — workbenchHost 수정」.
 *
 * 무엇이 사고였나
 *   wb.local.deploy 가 이미지 입력값을 **그대로 컨테이너 이름으로** 넘겼다.
 *   `recoder-app:v1` 을 입력하면 이름에 `:` 가 들어가 코어의 이름 검증
 *   (`^[a-zA-Z0-9][a-zA-Z0-9_.\-]+$`)에 걸려 플랜 생성이 400 으로 죽었다.
 *
 * 여기서 고정하는 것 (DoD)
 *   1. 이미지 `recoder-app:v1` → 컨테이너 이름 `recoder-app`.
 *   2. 이미지 인자에는 태그가 **남는다** — v1→v2 롤백이 태그로 구분된다.
 *   3. 핸들러가 파생 함수를 실제로 쓴다 (이미지 원문을 이름에 다시 넣는 회귀 방지).
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const HOST = fs.readFileSync(
  path.join(__dirname, '../src/sidebar/workbenchHost.ts'), 'utf8',
);

/** TS 소스에서 containerNameFromImage 를 꺼내 JS 로 실행한다.
 *  (webview-tests 는 빌드 산출물이 없어도 돌아야 한다 — 타입 표기만 벗긴다.) */
function loadDeriveFn() {
  const m = HOST.match(/export function containerNameFromImage\([^)]*\)[^{]*\{([\s\S]*?)\n\}/);
  assert.ok(m, 'containerNameFromImage 가 workbenchHost.ts 에 없다');
  // eslint-disable-next-line no-new-func
  return new Function('image', m[1]);
}

const derive = loadDeriveFn();

test('태그가 붙은 이미지에서 태그를 뗀 이름이 나온다', () => {
  assert.strictEqual(derive('recoder-app:v1'), 'recoder-app');
  assert.strictEqual(derive('recoder-app:latest'), 'recoder-app');
});

test('레지스트리 경로·다이제스트도 이름에 들어가지 않는다', () => {
  //: `/` 와 `@` 도 컨테이너 이름 규칙 위반이다.
  assert.strictEqual(derive('ghcr.io/team/recoder-app:v2'), 'recoder-app');
  assert.strictEqual(derive('recoder-app@sha256:abcdef'), 'recoder-app');
});

test('빈 값은 기본 이름으로 떨어진다', () => {
  assert.strictEqual(derive(''), 'recoder-app');
});

test('[음성 대조] 태그 없는 이름은 그대로 유지된다', () => {
  assert.strictEqual(derive('recoder-app'), 'recoder-app');
});

test('wb.local.deploy 가 파생 함수를 쓰고, 이미지 원문을 이름 자리에 다시 넣지 않는다', () => {
  const start = HOST.indexOf("case 'wb.local.deploy'");
  assert.notStrictEqual(start, -1);
  //: 다음 case 까지가 이 핸들러다 (핸들러 안에 이른 break 가 있어 break 로 못 자른다).
  const block = HOST.slice(start, HOST.indexOf("case '", start + 10));
  //: 이름 자리에는 파생 함수.
  assert.match(block, /containerNameFromImage\(/, '컨테이너 이름을 이미지에서 파생하지 않는다');
  //: 이미지 원문(String(p.image ...))이 두 번 들어가던 회귀 — 한 번(이미지 인자)만 허용.
  const rawUses = (block.match(/String\(p\.image/g) || []).length;
  assert.ok(rawUses <= 1, `이미지 원문이 ${rawUses}번 들어간다 — 이름 자리에 원문이 되살아났다`);
});
