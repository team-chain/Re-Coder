/**
 * 회귀: Core 의 오류가 사용자에게 읽을 수 있는 문장으로 전달되는가
 *
 * 배경
 *   데모에서 인프라 파일 생성이 실패했을 때 화면에 뜬 건 딱 이거였다.
 *
 *       Error: Internal Server Error
 *
 *   원인도 없고, 다음에 뭘 하면 되는지도 없다. 확장이 HTTP 응답 본문을
 *   **그대로** 배너에 넣었기 때문인데, Starlette 이 처리되지 않은 예외에
 *   대해 돌려주는 본문이 정확히 그 평문 한 줄이다.
 *
 *   FastAPI 가 정상적으로 낸 오류도 마찬가지로 `{"detail":"..."}` JSON 원문이
 *   그대로 노출됐다. 사용자에게 JSON 을 읽으라고 하는 셈이다.
 *
 * DoD 근거: 「인프라 파일 생성이 500 Internal Server Error」(P1)
 *   "실패하는 경우 500 대신 원인과 다음 행동이 담긴 메시지가 나온다."
 */
const test = require('node:test');
const assert = require('node:assert');

const { describeHttpError } = require('../out/core/httpError.js');

test('FastAPI 오류는 detail 만 꺼내 보여준다 (JSON 원문 노출 금지)', () => {
  const out = describeHttpError(404, '{"detail":"워크스페이스 경로가 없습니다: C:\\\\proj"}');
  assert.strictEqual(out, '워크스페이스 경로가 없습니다: C:\\proj');
  assert.ok(!out.includes('{'), 'JSON 원문이 그대로 새어 나갔다');
});

test('평문 Internal Server Error 는 원인 안내와 다음 행동으로 바꾼다', () => {
  const out = describeHttpError(500, 'Internal Server Error');
  assert.ok(!/^Internal Server Error$/i.test(out), '그대로 통과시켰다 — 사용자에게 아무 정보가 없다');
  assert.ok(out.includes('500'), '어떤 오류였는지 단서가 없다');
  assert.ok(out.includes('다시 시도'), '다음에 뭘 하면 되는지가 없다');
});

test('빈 본문도 같은 안내로 처리한다', () => {
  const out = describeHttpError(502, '');
  assert.ok(out.includes('502'));
  assert.ok(out.trim().length > 20, '빈 문자열이 그대로 나갔다');
});

test('422 검증 오류는 어느 필드가 문제인지 펴서 보여준다', () => {
  const body = JSON.stringify({
    detail: [{ loc: ['body', 'workspace_path'], msg: 'field required' }],
  });
  const out = describeHttpError(422, body);
  assert.ok(out.includes('body.workspace_path'), '문제 필드를 안 알려준다');
  assert.ok(out.includes('field required'));
});

test('[음성 대조] 일반 평문 오류는 내용을 지어내지 않고 그대로 전달한다', () => {
  // 모든 입력을 같은 안내 문구로 뭉개면 위 테스트들은 아무것도 증명하지 못한다.
  const out = describeHttpError(409, 'proposal_id 가 이미 소비되었습니다');
  assert.ok(out.includes('proposal_id 가 이미 소비되었습니다'), '원문이 사라졌다');
  assert.ok(out.includes('409'));
  assert.ok(!out.includes('다시 시도'), '평문 오류에까지 일반 안내를 덧씌웠다');
});

test('[음성 대조] JSON 이지만 detail 이 없으면 원문을 잃지 않는다', () => {
  const out = describeHttpError(500, '{"error":"weird shape"}');
  assert.ok(out.includes('weird shape'), '파싱에 실패했다고 내용을 버렸다');
});
