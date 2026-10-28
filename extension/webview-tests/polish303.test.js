// 3.0.3 화면 다듬기 — 예전 인라인 색을 실제 브라우저 표기(rgb)로 잡기 · 오류 문장 · 설계 기록 본문 · 배포 기록 도구 막대.
const test = require('node:test'), assert = require('node:assert/strict');
const React = require('react'); const { renderToStaticMarkup } = require('react-dom/server');
const { THEME_CSS, LEGACY_CSS, LEGACY_PRIMARY } = require('../out/webview-test/components/shell/theme');
const { friendlyError, CORE_UNREACHABLE } = require('../out/webview-test/components/ui/friendlyError');
const { AdrBody } = require('../out/webview-test/components/Hubs');
const { ReplayView, initialHistory } = require('../out/webview-test/components/Replay.js');

test('3.0.3 예전 색 규칙은 브라우저가 style 에 적는 rgb 표기로 찾는다(#hex 로는 실제 화면에서 안 걸린다)', () => {
  assert.ok(THEME_CSS.includes(LEGACY_CSS));
  assert.ok(LEGACY_CSS.includes('[style*="background: rgb(22, 163, 74)"]'), '초록 버튼 #16a34a');
  assert.ok(LEGACY_CSS.includes('[style*="background: rgb(37, 99, 235)"]'), '파란 버튼 #2563eb');
  assert.ok(LEGACY_CSS.includes('[style*="background: rgb(37, 37, 38)"]'), '회색 패널 #252526');
  assert.ok(LEGACY_CSS.includes('[style*="solid rgb(51, 51, 51)"]'), '테두리 #333');
  assert.ok(!/\[style\*="[^"]*#[0-9a-f]{3,6}/i.test(THEME_CSS), 'style 속성을 #hex 로 찾는 규칙이 남아 있으면 안 된다');
  assert.ok(LEGACY_PRIMARY.length >= 6);
  // 고대비 테마는 건드리지 않는다.
  assert.ok(LEGACY_CSS.split('\n').every(rule => rule.startsWith('body:not(.vscode-high-contrast):not(.vscode-high-contrast-light) .rc-app')));
});

test('3.0.3 네트워크 오류는 사람이 읽는 문장으로, 나머지는 그대로', () => {
  assert.equal(friendlyError('Error: fetch failed'), CORE_UNREACHABLE);
  assert.equal(friendlyError('TypeError: Failed to fetch'), CORE_UNREACHABLE);
  assert.equal(friendlyError('connect ECONNREFUSED 127.0.0.1:8000'), CORE_UNREACHABLE);
  assert.equal(friendlyError('Error: 권한이 없습니다 (403)'), '권한이 없습니다 (403)');
  assert.equal(friendlyError(undefined), '');
});

test('3.0.3 설계 기록 본문은 마크다운 기호 없이 제목 · 목록 · 문단으로', () => {
  const html = renderToStaticMarkup(React.createElement(AdrBody, { text: '# ADR-001: 게시판\n\n- 상태: 승인됨\n- 날짜: 오늘\n\n## 결정\nHTML/CSS/JS <script>x</script>' }));
  assert.ok(html.includes('ADR-001: 게시판') && !html.includes('# ADR'));
  assert.equal((html.match(/<li>/g) || []).length, 2);
  assert.ok(html.includes('결정') && !html.includes('## '));
  assert.ok(html.includes('&lt;script&gt;'), 'HTML 은 글자로만 보인다');
});

test('3.0.3 배포 기록: 유형은 고르기 버튼, 새로고침은 작은 보조 버튼, 오류는 읽히는 문장', () => {
  const state = { ...initialHistory, error: 'fetch failed' };
  const html = renderToStaticMarkup(React.createElement(ReplayView, { state, source: 'ecs', search: '', onRefresh() {}, onSource() {}, onSearch() {}, onSelect() {}, onReview() {}, onConfirm() {} }));
  assert.ok(html.includes('class="rc-seg" role="group" aria-label="배포 유형"'));
  assert.ok(/aria-pressed="true"[^>]*>ECS</.test(html));
  assert.ok(/class="rc-btn sm"[^>]*>새로고침</.test(html));
  assert.ok(html.includes(CORE_UNREACHABLE) && !html.includes('fetch failed'));
});
