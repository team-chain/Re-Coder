/**
 * 채팅 실패 원인 표시 — "LLM 호출 실패 원인이 사용자에게 보이지 않음" 결함 수정.
 *
 * 배경
 *   코어는 chat.error 에 원인 문장을 실어 보냈지만, ChatPanel 이 그 message 를
 *   **버리고** 고정 문구("응답을 가져오지 못했어요")만 렌더했다. rate limit 인지
 *   자격증명 문제인지 사용자가 알 길이 없었다.
 *
 * 순수 로직(원인 분류)은 코어 pytest(test_ai_failure_reason.py)가 검사한다.
 * 이 파일은 끝에서 끝까지의 **배선**을 검사한다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

test('SidebarProvider 가 chat.error 에 원인 message 를 싣는다', () => {
  const source = read('../src/sidebar/SidebarProvider.ts');
  const body = source.slice(source.indexOf('private async handleChat'));
  assert.match(body, /chat\.error/, '실패를 웹뷰에 알리지 않는다');
  assert.match(body, /message:\s*error/, '원인이 chat.error 페이로드에서 빠졌다');
});

test('ChatPanel 이 chat.error 의 message 를 보관한다', () => {
  const source = read('../webview-src/components/ChatPanel.tsx');
  assert.match(source, /errorReason/, '원인을 담을 자리가 없다 — 받아도 버려진다');
  //: 핸들러가 payload.message 를 실제로 읽어야 한다.
  const anchor = source.indexOf('} else if (msg.type === "chat.error")');
  assert.notStrictEqual(anchor, -1, 'chat.error 핸들러 자체가 없다');
  const handler = source.slice(anchor, anchor + 600);
  assert.match(handler, /payload\.message/, 'chat.error 핸들러가 message 를 읽지 않는다');
  assert.match(handler, /errorReason/, '읽은 원인을 상태에 싣지 않는다');
});

test('오류 줄이 원인을 실제로 렌더한다', () => {
  const source = read('../webview-src/components/ChatPanel.tsx');
  //: 고정 문구만 있고 원인 렌더가 없으면 예전 화면 그대로다.
  assert.match(source, /message\.errorReason\s*&&/, '원인이 화면에 그려지지 않는다');
  //: 기존 고정 문구는 유지 — 원인이 비어 있어도 실패 자체는 보여야 한다.
  assert.match(source, /응답을 가져오지 못했어요/);
});
