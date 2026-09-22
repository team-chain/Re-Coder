/**
 * 회귀: 채팅에서 정한 설계 결정이 ADR 의 「영향」까지 채워서 기록되는가
 *
 * 배경 — 이 테스트가 왜 있는가
 *   결정 모달은 각 결정의 `impact`(이 선택이 프로젝트에 주는 영향)를 화면에
 *   보여준다. 그런데 "이 선택으로 생성" 을 눌러 서버로 보낼 때 만드는
 *   `DecisionChoice` 에는 그 필드가 빠져 있었다. 코어의
 *   `adr.normalize_decisions` 는 `d.get("impact")` 로 읽으므로 항상 빈
 *   문자열이 됐고, 생성된 모든 ADR 의 「## 영향」이 `(영향 미기재)` 로 남았다.
 *
 *   화면에는 보이는데 파일에는 안 남는 종류의 버그라서, UI 를 눈으로 보는
 *   것만으로는 절대 안 잡힌다.
 *
 * 검사 방법
 *   변환을 컴포넌트 밖 순수 함수(`buildDecisionChoices`)로 꺼내 직접 검사한다.
 *   예전에는 모달을 클릭해야만 도달하는 코드라서 필드가 하나 빠져도 깨지는
 *   테스트가 하나도 없었다.
 */
const test = require('node:test');
const assert = require('node:assert');

const { buildDecisionChoices } = require('../out/webview-test/components/CodeAgent.js');

const DECISIONS = [
  {
    id: 'storage',
    question: '업로드 파일을 어디에 저장할까요?',
    impact: '저장 위치가 배포 대상과 비용 구조를 함께 결정합니다.',
    options: [
      { key: 's3', label: 'S3', summary: '외부 오브젝트 스토리지', pros: ['확장'], cons: ['비용'], recommended: true },
      { key: 'local', label: '로컬 디스크', summary: '서버 파일시스템', pros: ['간단'], cons: ['확장 불가'], recommended: false },
    ],
  },
  {
    id: 'sdk',
    question: '업로드 처리는 무엇으로 할까요?',
    impact: '의존성과 미들웨어 구성이 달라집니다.',
    options: [
      { key: 'multer', label: 'multer', summary: '미들웨어', pros: [], cons: [], recommended: true },
      { key: 'busboy', label: 'busboy', summary: '저수준', pros: [], cons: [], recommended: false },
    ],
  },
];

test('impact 가 서버로 보낼 결정에 그대로 실린다', () => {
  const choices = buildDecisionChoices(DECISIONS, { storage: 'local', sdk: 'multer' });

  assert.strictEqual(choices.length, 2);
  assert.strictEqual(
    choices[0].impact,
    '저장 위치가 배포 대상과 비용 구조를 함께 결정합니다.',
    'impact 가 누락됐다 — ADR 의 「## 영향」이 (영향 미기재) 로 남는다'
  );
  assert.strictEqual(choices[1].impact, '의존성과 미들웨어 구성이 달라집니다.');
});

test('[음성 대조] impact 가 원문 그대로여야 한다 — 아무 문자열이나 통과하지 않는다', () => {
  const choices = buildDecisionChoices(
    [{ ...DECISIONS[0], impact: '완전히 다른 영향 설명' }],
    { storage: 's3' }
  );
  assert.notStrictEqual(
    choices[0].impact,
    '저장 위치가 배포 대상과 비용 구조를 함께 결정합니다.',
    '검사식이 입력과 무관하게 통과한다 — 위 테스트는 아무것도 증명하지 못한다'
  );
  assert.strictEqual(choices[0].impact, '완전히 다른 영향 설명');
});

test('사용자가 고른 선택지가 chosen_key 로 실린다 (권장값이 아니라)', () => {
  const choices = buildDecisionChoices(DECISIONS, { storage: 'local', sdk: 'busboy' });
  assert.strictEqual(choices[0].chosen_key, 'local');
  assert.strictEqual(choices[1].chosen_key, 'busboy');
});

test('impact 가 없는 결정은 빈 문자열로 정규화된다 (undefined 로 새지 않는다)', () => {
  // undefined 가 그대로 나가면 JSON 직렬화에서 키 자체가 사라져, 코어가
  // 필드 유무로 분기할 때 "안 보낸 것"과 "비워서 보낸 것"을 구분 못 한다.
  const choices = buildDecisionChoices(
    [{ id: 'x', question: 'q', options: DECISIONS[0].options }],
    { x: 's3' }
  );
  assert.strictEqual(choices[0].impact, '');
});

test('질문과 선택지 목록도 함께 전달된다 (ADR 의 검토한 대안 절)', () => {
  const choices = buildDecisionChoices(DECISIONS, { storage: 's3', sdk: 'multer' });
  assert.strictEqual(choices[0].question, '업로드 파일을 어디에 저장할까요?');
  assert.deepStrictEqual(choices[0].options.map((o) => o.key), ['s3', 'local']);
});
