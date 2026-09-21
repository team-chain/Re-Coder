/**
 * 승인 Level 3 "Double Confirm" 은 정말 두 단계여야 한다 (실기기 검증 B3).
 *
 * 코어는 미검증 배포(Trivy 못 돌림 등)를 ApprovalLevel.DOUBLE_CONFIRM(3) 으로 올리는데,
 * 화면은 라벨만 "Double Confirm" 이고 Approve 가 한 번에 눌렸다. 사유를 읽었다는
 * 체크 없이는 승인 버튼이 열리면 안 된다. Level 4 의 CONFIRM 타이핑은 그대로.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const MODAL = fs.readFileSync(path.join(__dirname, '../webview-src/components/ApprovalModal.tsx'), 'utf8');
const DEPLOY = fs.readFileSync(path.join(__dirname, '../../core/api/routes/deploy.py'), 'utf8');

test('Level 3 은 체크(acknowledged) 없이는 승인이 닫혀 있다', () => {
  assert.match(MODAL, /const isLevel3Confirmed = level < 3 \|\| level >= 4 \|\| acknowledged;/);
  assert.match(MODAL, /const canApprove = isLevel4Confirmed && isLevel3Confirmed;/);
  assert.match(MODAL, /disabled=\{!canApprove\}/, '승인 버튼이 Level 3 체크를 보지 않는다');
  assert.match(MODAL, /if \(canApprove\) \{\s*onApprove\(\);/, 'handleApprove 가 체크를 우회한다');
});

test('Level 3 화면에 체크박스와 "확인하지 못한 상태" 안내가 있다', () => {
  assert.match(MODAL, /level === 3 && \(/);
  assert.match(MODAL, /type="checkbox"[\s\S]*?checked=\{acknowledged\}/);
  assert.match(MODAL, /검사가 통과된 것이 아니라 확인하지 못한 상태입니다/);
});

test('코어는 미검증 스캔을 DOUBLE_CONFIRM 으로 올린다 (화면 단계와 맞물림)', () => {
  const site = DEPLOY.slice(DEPLOY.indexOf('if gate.get("unverified"):'), DEPLOY.indexOf('if gate.get("unverified"):') + 400);
  assert.match(site, /plan\.approval_level = ApprovalLevel\.DOUBLE_CONFIRM/);
});
