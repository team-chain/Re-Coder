/**
 * 구조 지도 — JS/TS import 가 전부 사라지던 회귀 (실기기 검증 3-A4).
 *
 * stripJs 가 문자열 내용을 공백으로 바꾸는 바람에 `from "./x"` 가 `from "   "` 가 되어
 * 381 파일짜리 저장소에서 TS/TSX 파일 전부가 "아무도 import 안 함(고립)" 으로 표시됐다.
 * import 경로 추출은 문자열을 보존한 소스에서 해야 한다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const SRC = fs.readFileSync(path.join(__dirname, '../src/codemap/analyzer.ts'), 'utf8');

const fn = (name) => {
  const a = SRC.indexOf(`function ${name}(`);
  assert.notStrictEqual(a, -1, `${name} 가 없다`);
  const b = SRC.indexOf('\nfunction ', a + 1);
  return SRC.slice(a, b === -1 ? undefined : b);
};

test('import 경로는 문자열을 보존한 소스에서 읽는다', () => {
  assert.match(fn('jsImportRefs'), /stripJs\(src, true\)/, 'jsImportRefs 가 문자열을 지운 소스를 쓴다 → import 전부 소실');
  assert.match(fn('stripJs'), /keepStrings/, 'stripJs 에 문자열 보존 모드가 없다');
});

test('함수 정의 탐지는 여전히 문자열을 지운 소스를 쓴다(문자열 속 function 오탐 방지)', () => {
  // defs 를 뽑는 곳은 stripJs(src) 기본(문자열 제거)이어야 한다.
  const defsSite = SRC.slice(SRC.indexOf('const s = stripJs(src);'), SRC.indexOf('const s = stripJs(src);') + 40);
  assert.ok(defsSite.startsWith('const s = stripJs(src);'), '정의 탐지가 문자열 제거 모드를 쓰지 않는다');
});

test('stripJs 는 keepStrings=true 일 때 문자열 내용을 그대로 둔다 (동작 검증)', () => {
  // TS 함수 본문을 JS 로 그대로 평가해 실제 동작을 본다(타입 표기 제거).
  const body = fn('stripJs')
    .replace(/function stripJs\(src: string, keepStrings = false\): string/, 'function stripJs(src, keepStrings = false)')
    .replace(/const out: string\[\] = \[\]/, 'const out = []');
  const stripJs = new Function(`${body}; return stripJs;`)();
  const src = 'import { A } from "./components/A"; // 주석\nconst s = "function fake() {}";';
  const kept = stripJs(src, true);
  assert.ok(kept.includes('"./components/A"'), '문자열이 지워졌다');
  assert.ok(!kept.includes('주석'), '주석이 남았다');
  const blanked = stripJs(src);
  assert.ok(!blanked.includes('fake'), '기본 모드에서 문자열이 남았다');
});
