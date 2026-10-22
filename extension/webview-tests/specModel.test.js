const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const m = require('../out/webview-test/components/specModel.js');
const { SpecEditor } = require('../out/webview-test/components/SpecEditor.js');

const draft = { goal: '장바구니', in_scope: ['담기', '수량 변경'], out_of_scope: ['결제'], criteria: [
  { text: '수량 0 이면 400', kind: 'api', check: 'qty=0 → 400' },
  { text: '새로고침 후에도 유지된다', kind: 'screen', check: '' },
  { text: '', kind: 'api' },
  { text: '이상한 종류', kind: 'weird', source: 'user' },
] };

test('초안을 화면 상태로 — 빈 조건은 버리고, 모르는 종류는 기능, 처음엔 모두 포함', () => {
  const s = m.fromDraft(draft);
  assert.equal(s.criteria.length, 3);
  assert.deepEqual(s.criteria.map(c => c.kind), ['api', 'screen', 'feature']);
  assert.deepEqual(s.criteria.map(c => c.source), ['ai', 'ai', 'user']);
  assert.ok(s.criteria.every(c => c.included));
  assert.equal(s.inScope, '담기\n수량 변경');
  assert.equal(new Set(s.criteria.map(c => c.key)).size, 3);
  assert.deepEqual(m.fromDraft(null), m.emptySpec());
});

test('AI 초안을 고치면 직접 쓴 조건이 되고, 추가·삭제 키가 겹치지 않는다', () => {
  let s = m.fromDraft(draft);
  const k = s.criteria[0].key;
  s = m.updateCriterion(s, k, { included: false });
  assert.equal(s.criteria[0].source, 'ai', '포함만 바꾸면 출처는 그대로');
  s = m.updateCriterion(s, k, { text: '수량 0 이면 400 과 안내 문구' });
  assert.equal(s.criteria[0].source, 'user');
  s = m.addCriterion(m.removeCriterion(s, s.criteria[2].key));
  assert.equal(new Set(s.criteria.map(c => c.key)).size, s.criteria.length);
  for (let i = 0; i < 20; i++) s = m.addCriterion(s);
  assert.equal(s.criteria.length, m.MAX_CRITERIA);
});

test('요청에는 포함한 조건만, 범위는 한 줄에 하나(목록 기호 제거), 없으면 null', () => {
  let s = m.fromDraft(draft);
  s = { ...s, inScope: '- 담기\n\n  * 수량 변경 \n', criteria: s.criteria.map((c, i) => ({ ...c, included: i !== 1 })) };
  const p = m.specForRequest(s);
  assert.deepEqual(p.in_scope, ['담기', '수량 변경']);
  assert.deepEqual(p.criteria.map(c => c.text), ['수량 0 이면 400', '이상한 종류']);
  assert.equal(m.specForRequest({ ...s, criteria: s.criteria.map(c => ({ ...c, included: false })) }), null);
  assert.equal(m.specForRequest(null), null);
});

test('확인할 수 없는 조건·빈 확인 방법·실패 흐름 없음은 경고, 조건이 없으면 생성 잠금', () => {
  const s = m.fromDraft({ goal: '', criteria: [
    { text: '목록이 잘 동작한다', check: '보기' }, { text: '글을 쓰면 목록에 보인다', check: '' }] });
  const issues = m.specIssues(s);
  assert.ok(issues.some(i => i.key === s.criteria[0].key && i.message.includes('잘 동작')));
  assert.ok(issues.some(i => i.key === s.criteria[1].key && i.message.includes('확인 방법')));
  assert.ok(issues.some(i => !i.key && i.message.includes('실패 흐름')));
  assert.ok(issues.some(i => i.message.includes('목표')));
  assert.equal(m.hasBlocking(issues), false);
  const none = m.specIssues({ ...s, criteria: s.criteria.map(c => ({ ...c, included: false })) });
  assert.equal(m.hasBlocking(none), true);
  assert.equal(m.hasBlocking(m.specIssues(null)), true);
  const ok = m.specIssues(m.fromDraft({ goal: 'g', criteria: [{ text: '수량 0 이면 400 으로 거절', check: 'qty=0' }, { text: '담은 상품이 목록에 보인다', check: '목록' }] }));
  assert.deepEqual(ok, []);
});

test('편집기는 조건 번호·종류·AI 초안 표시·경고를 그리고 값을 이스케이프한다', () => {
  const s = m.fromDraft({ goal: '<b>목표</b>', in_scope: ['목록'], out_of_scope: ['댓글'], criteria: [{ text: '목록이 잘 동작한다', kind: 'screen', check: 'x' }, { text: '빈 확인', kind: 'api', check: '' }] });
  const html = renderToStaticMarkup(React.createElement(SpecEditor, { spec: s, issues: m.specIssues(s), onChange: () => {} }));
  for (const t of ['AC-1', 'AC-2', '화면', 'AI 초안', '확인할 수 없는 말', '+ 합격 조건 추가', '이번에 만들지 않음', '댓글', '확인 방법 없음', '확인 · x']) assert.ok(html.includes(t), t);
  assert.ok(!html.includes('<textarea'), '읽기가 먼저 — 처음부터 모든 줄이 입력칸이 아니다');
  const blank = m.addCriterion(m.fromDraft({ goal: 'g', criteria: [] }));
  assert.ok(renderToStaticMarkup(React.createElement(SpecEditor, { spec: blank, issues: [], onChange: () => {} })).includes('<textarea'), '빈 조건은 바로 쓸 수 있게 연다');
  assert.ok(!html.includes('<b>목표</b>') && html.includes('&lt;b&gt;'));
});

test('호스트·API 결선 — code.spec 은 /api/code/spec 으로, 생성 요청에는 spec 이 실린다', () => {
  const host = fs.readFileSync(path.join(__dirname, '../src/sidebar/SidebarProvider.ts'), 'utf8');
  const api = fs.readFileSync(path.join(__dirname, '../src/core/ApiClient.ts'), 'utf8');
  assert.ok(host.includes("case 'code.spec':") && host.includes("'code.specResult'") && host.includes("'code.specError'"));
  assert.ok(api.includes("'/api/code/spec'"));
  assert.equal((api.match(/spec: opts\??\.spec \?\? null/g) || []).length, 2, '두 생성 경로 모두 spec 을 보낸다');
});

test('호스트는 명세 초안을 요청한 창에 돌려주고, 실패는 specError 로 알린다', async () => {
  const Module = require('node:module');
  const originalResolve = Module._resolveFilename;
  Module._resolveFilename = function (request, ...args) {
    return request === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : originalResolve.call(this, request, ...args);
  };
  try {
    const { SidebarProvider } = require('../out/sidebar/SidebarProvider.js');
    const vscode = require('vscode');
    let got = null, generated = null;
    const api = {
      draftSpec: async (instruction, opts) => { got = { instruction, opts }; return { spec: { goal: 'g', in_scope: [], out_of_scope: [], criteria: [] } }; },
      generateCode: async (instruction, opts) => { generated = opts; return { summary: '', model: 'm', ops: [] }; },
    };
    const provider = new SidebarProvider(vscode.Uri.file('/tmp/extension'), api, { onCoreRestart: () => ({ dispose() {} }) }, {});
    provider.ensureConnection = async () => {};
    const messages = [];
    const webview = { postMessage: (message) => { messages.push(message); return true; } };
    const decisions = [{ id: '__confirm__', question: 'q', chosen_key: 'proceed', options: [], impact: '' }];
    await provider.handleMessage({ type: 'code.spec', payload: { requestId: 4, instruction: '만들어줘', targetFolder: 'web', decisions } }, webview);
    assert.equal(got.instruction, '만들어줘');
    assert.deepEqual(got.opts.decisions, decisions);
    assert.equal(got.opts.targetFolder, 'web');
    assert.deepEqual(messages.at(-1), { type: 'code.specResult', payload: { requestId: 4, spec: { goal: 'g', in_scope: [], out_of_scope: [], criteria: [] } } });

    api.draftSpec = async () => ({ spec: null, skip: 'foundation', message: '건너뜁니다', model: '', provider: '' });
    await provider.handleMessage({ type: 'code.spec', payload: { requestId: 7, instruction: '쇼핑몰' } }, webview);
    assert.deepEqual(messages.at(-1), { type: 'code.specResult', payload: { requestId: 7, spec: null, skip: 'foundation', message: '건너뜁니다' } }, '건너뛰기 이유를 화면까지 전한다');

    api.draftSpec = async () => { throw new Error('할당량'); };
    await provider.handleMessage({ type: 'code.spec', payload: { requestId: 5, instruction: 'x' } }, webview);
    assert.equal(messages.at(-1).type, 'code.specError');
    assert.ok(messages.at(-1).payload.message.includes('할당량'));
    assert.ok(!messages.some(x => x.type === 'code.error'), '명세 실패는 턴 오류가 아니다');

    const spec = { goal: 'g', in_scope: [], out_of_scope: [], criteria: [{ text: 't', kind: 'api', check: '', source: 'user' }] };
    await provider.handleMessage({ type: 'code.generate', payload: { instruction: 'x', requestId: 6, decisions, spec } }, webview);
    assert.deepEqual(generated.spec, spec);
  } finally {
    Module._resolveFilename = originalResolve;
  }
});

test('AI 조건이 외부 주소·비밀 값을 다루면 사람이 확인하게 알리고, 사람이 쓴 조건은 묻지 않는다', () => {
  const s = m.fromDraft({ goal: 'g', criteria: [
    { text: '주문이 생기면 https://hook.example.com 으로 전송한다', check: 'POST 확인' },
    { text: '응답에 API_KEY 를 담는다', check: '.env 값' },
    { text: '수량 0 이면 400 으로 거절', check: 'qty=0' },
  ] });
  const issues = m.specIssues(s);
  const risky = issues.filter(i => /외부 주소·비밀 값/.test(i.message));
  assert.equal(risky.length, 2);
  assert.ok(risky.every(i => i.level === 'warn'), '막지는 않는다(정상적인 웹훅도 있다)');
  const mine = m.updateCriterion(s, s.criteria[0].key, { text: '주문이 생기면 https://hook.example.com 으로 보낸다' });
  assert.equal(m.specIssues(mine).filter(i => i.key === s.criteria[0].key && /외부 주소/.test(i.message)).length, 0, '직접 고친 조건은 사용자의 뜻');
  for (const t of ['로그인 실패 시 401', '목록 첫 줄에 새 글']) assert.equal(m.riskyWord(t), null, t);
});

test('범위가 너무 많으면 나눠 요청하길 권하고, 번호는 포함한 조건에만 매긴다(기록 파일과 같게)', () => {
  const big = { ...m.fromDraft({ goal: 'g', criteria: [{ text: '수량 0 이면 400', check: 'x' }] }), inScope: Array.from({ length: 9 }, (_, i) => `기능 ${i}`).join('\n') };
  assert.ok(m.specIssues(big).some(i => /9줄/.test(i.message)));
  assert.ok(!m.specIssues({ ...big, inScope: 'a\nb' }).some(i => /줄입니다/.test(i.message)));
  const s = m.fromDraft({ goal: 'g', criteria: [{ text: '첫째', check: 'a' }, { text: '둘째', check: 'b' }, { text: '셋째', check: 'c' }] });
  const off = m.updateCriterion(s, s.criteria[0].key, { included: false });
  const html = renderToStaticMarkup(React.createElement(SpecEditor, { spec: off, issues: [], onChange: () => {} }));
  assert.ok(html.includes('AC-1') && html.includes('AC-2') && !html.includes('AC-3'), '뺀 줄은 번호를 건너뛴다');
  assert.ok(html.includes('이번 생성에서 뺀 조건'));
  assert.deepEqual(m.specForRequest(off).criteria.map(c => c.text), ['둘째', '셋째']);
});
