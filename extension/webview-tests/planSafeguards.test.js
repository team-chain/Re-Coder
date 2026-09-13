/**
 * AI-DLC 결정 모달 안전장치 — 보드 이슈 「설계 결정이 제대로 나오지 않음」의 웹뷰 쪽.
 *
 * 검사하는 것 두 가지:
 *   1. 걸러진 결정(dropped)이 끝까지 배선돼 화면에 표시된다 — 안 보이면
 *      사용자에게는 "AI 가 설계를 안 해준다"로 보인다.
 *   2. 결정이 0개로 와도 사람 승인 없이 생성으로 직행하지 않는다 —
 *      AI-DLC 전제(항상 사람 승인)를 웹뷰에서도 보장한다.
 *
 * 순수 로직(재시도·필터)은 코어 pytest(test_code_plan_quality.py)가 검사한다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

test('SidebarProvider 가 dropped 를 웹뷰로 전달한다', () => {
  const source = read('../src/sidebar/SidebarProvider.ts');
  const anchor = source.indexOf("'code.planResult'");
  assert.notStrictEqual(anchor, -1);
  const block = source.slice(anchor, anchor + 400);
  assert.match(block, /dropped/, '코어가 보낸 걸러진-결정 사유가 여기서 버려진다');
});

test('ApiClient 플랜 결과 타입에 dropped 가 있다', () => {
  const source = read('../src/core/ApiClient.ts');
  const anchor = source.indexOf('interface CodePlanResult');
  const block = source.slice(anchor, anchor + 500);
  assert.match(block, /dropped\?/, '타입에 없으면 전달 코드가 컴파일에서 걸러질 수 있다');
});

test('결정 모달이 dropped 를 실제로 렌더한다', () => {
  const source = read('../webview-src/components/CodeAgent.tsx');
  assert.match(source, /decisionModal\.dropped\.length > 0/, '걸러진 결정이 화면에 안 보인다');
  assert.match(source, /decisionModal\.dropped\.map/, '개수만 있고 사유가 없다');
});

test('결정 0개여도 사람 승인 없이 생성으로 직행하지 않는다', () => {
  const source = read('../webview-src/components/CodeAgent.tsx');
  const anchor = source.indexOf('decisions.length === 0');
  assert.notStrictEqual(anchor, -1, '빈 결정 분기 자체가 사라졌다 — 검사 대상 확인 필요');
  const block = source.slice(anchor, anchor + 1400);
  //: 빈 목록 분기 안에서 곧장 code.generate 를 쏘면 안 된다. 확인 카드를
  //: 만들어 모달을 띄우는 코드가 그 분기에 있어야 한다.
  assert.ok(!/postMessage\("code\.generate"/.test(block.slice(0, block.indexOf('}'))),
    '빈 결정에서 승인 없이 생성으로 직행한다');
  assert.match(block, /__confirm__/, '확인 카드 대체가 없다');
  assert.match(block, /이대로 진행할까요/, '확인 카드 문구가 코어 확인 카드와 다른 형태다');
});
