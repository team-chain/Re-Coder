const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {nextHint, STEP_VIEW} = require('../out/webview-test/components/JourneyBar.js');

test('journey next step hint and screen mapping follow one flow', () => {
  assert.equal(nextHint({available: true, next: 'build'}), '무엇을 만들지 요청하세요');
  assert.match(nextHint({available: true, next: 'local', multi: true, services: 5, databases: 4}), /서비스 5개 · DB 4개를 이 PC 에서 함께 실행/);
  assert.equal(nextHint({available: true, next: ''}), '모든 단계를 마쳤습니다');
  //: 공격 테스트(레드팀)는 실행 중인 앱이 필요해서 로컬 실행 뒤, 보안 화면에서 한다
  assert.deepEqual(Object.keys(STEP_VIEW), ['build', 'verify', 'security', 'local', 'redteam', 'release']);
  assert.equal(STEP_VIEW.local, 'ship');
  assert.equal(STEP_VIEW.redteam, 'hub:security');
});
