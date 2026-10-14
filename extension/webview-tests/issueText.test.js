// 중간 단계 화면의 글자를 줄인 규칙 — 문제는 첫 문장 한 줄, 원문·해결은 접는다.
const test = require('node:test'), assert = require('node:assert/strict');
const React = require('react'); const { renderToStaticMarkup } = require('react-dom/server');
const { shortIssue, issueKind } = require('../out/webview-test/components/issueText.js');
const { ReadinessPanel } = require('../out/webview-test/components/ReadinessPanel.js');

const BLOCKER = 'BLOCKER: 빌드·실행 실패 예상 — 코드가 불러오는 패키지가 package.json 에 없습니다: `pg`(server/index.js). 컨테이너에는 선언된 패키지만 설치합니다. 해결: `npm install pg` 로 의존성에 추가하세요.';

test('Core 사유는 접두어·둘째 문장·해결을 빼고 첫 문장만 남긴다', () => {
  assert.equal(shortIssue(BLOCKER), '코드가 불러오는 패키지가 package.json 에 없습니다: `pg`(server/index.js).');
  assert.equal(shortIssue('확인 필요 — PostgreSQL(pg)은 값을 문자열로 돌려줍니다. 해결: 파서를 넣으세요.'), 'PostgreSQL(pg)은 값을 문자열로 돌려줍니다.');
  assert.equal(shortIssue('배포 전 자동 수정 — Vite 는 .js 파일 안의 JSX 를 해석하지 않습니다: client/src/index.js. 설명 (수정: 확장자)'), 'Vite 는 .js 파일 안의 JSX 를 해석하지 않습니다: client/src/index.js.');
  assert.ok(shortIssue('가'.repeat(300)).length <= 96);
  assert.equal(shortIssue('Local Docker run — container will be exposed on localhost'), 'Local Docker run — container will be exposed on localhost');
});

test('사유 종류를 구분한다', () => {
  assert.equal(issueKind(BLOCKER), 'blocker');
  assert.equal(issueKind('확인 필요 — x'), 'check');
  assert.equal(issueKind('배포 전 자동 수정 — x'), 'autofix');
  assert.equal(issueKind('롤백 대상 없음'), 'other');
});

test('배포 준비 점검: 자동 수정되는 항목은 해결 문장을 접고 버튼만 보인다', () => {
  const html = renderToStaticMarkup(React.createElement(ReadinessPanel, { issues: [
    { code: 'A', severity: 'error', message: BLOCKER.replace(/^BLOCKER: 빌드·실행 실패 예상 — /, ''), fix: '자동으로 고칩니다', auto_fix: true },
  ], onFix() {}, fixing: null }));
  assert.match(html, /<details/);
  assert.match(html, /자동 수정<\/button>/);
  assert.ok(!/컨테이너에는 선언된 패키지만 설치합니다[^<]*<\/div><button/.test(html));
});
