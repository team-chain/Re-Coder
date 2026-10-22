const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const React = require('react');

// Exercise the compiled component's event handlers/state without a VS Code DOM.
// The production bundle + actual HTTP host is also exercised in workspace-preview.
const STARTUP = new Set(['code.project', 'models.load']);
function mount(props = {}) {
  const states = [], effects = [], timers = new Map(), messages = [], startup = [];
  let cursor = 0, tree, receive, timerId = 0, reviews = 0;
  const exports = {};
  const hooks = {
    ...React,
    useState(initial) {
      const index = cursor++;
      if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial;
      return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value; }];
    },
    useRef(initial) { const index = cursor++; return states[index] ?? (states[index] = {current: initial}); },
    useCallback: fn => fn,
    useEffect(fn, deps) {
      const index = cursor++, previous = states[index];
      if (!previous || !deps || deps.some((value, i) => value !== previous.deps[i])) {
        states[index] = {deps}; effects.push(fn);
      }
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../out/webview-test/components/CodeAgent.js'), 'utf8'), {
    exports,
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    require(id) {
      if (id === 'react') return hooks;
      if (id === '../hooks/useVSCodeApi') return {useVSCodeApi: () => ({
        //: 처음 그릴 때 묻는 프로젝트 이름·모델 목록은 따로 모은다(요청 흐름 시험이 세는 메시지와 섞지 않는다).
        postMessage: (type, payload) => (STARTUP.has(type) ? startup : messages).push({type, payload}), useMessage: fn => {receive = fn;},
      })};
      return require(path.join(__dirname, '../out/webview-test/components', id));
    },
  });
  function render() {
    cursor = 0;
    tree = exports.CodeAgent({isActive: true, onReviewRequired: () => reviews++, ...props});
    effects.splice(0).forEach(fn => fn());
  }
  function nodes(node = tree) {
    if (!React.isValidElement(node)) return [];
    return [node, ...React.Children.toArray(node.props.children).flatMap(child => nodes(child))];
  }
  function text(node) {
    if (typeof node === 'string' || typeof node === 'number') return String(node);
    return React.isValidElement(node) ? React.Children.toArray(node.props.children).map(text).join('') : '';
  }
  render();
  return {
    messages, startup, render, nodes, get reviews() {return reviews;},
    find: predicate => nodes().find(predicate),
    button(label) { const node = nodes().find(n => n.type === 'button' && text(n) === label); assert.ok(node, label); return node; },
    input(value) { nodes().find(n => n.type === 'textarea').props.onChange({target: {value}}); render(); },
    emit(type, payload) { receive({type, payload}); render(); },
    expire() { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn()); render(); },
  };
}
const plan = {decisions: [{id:'storage', question:'저장 방식은?', impact:'데이터 보존', options:[
  {key:'sqlite', label:'SQLite', summary:'로컬 DB', pros:[], cons:[], recommended:true},
]}]};
const spec = {goal:'게시글을 쓰고 볼 수 있다', in_scope:['글 작성'], out_of_scope:['댓글'], criteria:[
  {id:'AC-1', text:'제목 없이 쓰면 400 을 돌려준다', kind:'api', check:'POST /posts {} → 400', source:'ai'},
  {id:'AC-2', text:'쓴 글이 목록 맨 위에 보인다', kind:'screen', check:'작성 후 / 첫 줄', source:'ai'},
]};
//: 결정 → 명세 → 생성까지 한 번에(명세 단계는 별도 시험이 자세히 본다).
function generateThrough(ui, requestId) {
  ui.button('다음: 합격 조건 →').props.onClick(); ui.render();
  ui.emit('code.specResult', {requestId, spec});
  ui.button('명세대로 생성 →').props.onClick(); ui.render();
}

test('central request opens design directly, then generates and applies only after user actions', () => {
  const ui = mount();
  ui.emit('code.folderPicked', {folder:'src'});
  ui.emit('code.contextAdded', {files:[{path:'README.md', content:'existing project'}]});
  ui.input('게시판을 만들어줘');
  const send = ui.button('보내기');
  send.props.onClick(); send.props.onClick(); // before a React commit
  assert.equal(ui.messages.length, 1);
  assert.equal(ui.messages[0].type, 'code.plan');
  const request = ui.messages[0].payload;
  assert.equal(request.targetFolder, 'src');
  assert.equal(request.contextFiles[0].content, 'existing project');
  ui.emit('code.planResult', {...plan, requestId:request.requestId});
  assert.ok(ui.find(n => n.props.role === 'dialog' && n.props['aria-label'] === '설계 결정'));
  assert.equal(ui.reviews, 1);
  assert.equal(ui.messages.length, 1, 'planning neither generates nor writes files');
  ui.emit('code.folderPicked', {folder:'changed-after-request'});
  ui.button('다음: 합격 조건 →').props.onClick(); ui.render();
  const specRequest = ui.messages.at(-1);
  assert.equal(specRequest.type, 'code.spec', '설계 결정을 다 고르면 바로 만들지 않고 명세 초안을 받는다');
  assert.equal(specRequest.payload.decisions[0].chosen_key, 'sqlite');
  assert.equal(specRequest.payload.targetFolder, 'src');
  ui.emit('code.specResult', {requestId:request.requestId, spec});
  assert.ok(ui.find(n => n.props.role === 'dialog' && n.props['aria-label'] === '명세 확인'));
  ui.button('명세대로 생성 →').props.onClick(); ui.render();
  const generate = ui.messages.at(-1);
  assert.equal(generate.type, 'code.generate');
  assert.equal(generate.payload.targetFolder, 'src');
  assert.equal(generate.payload.decisions[0].impact, '데이터 보존');
  assert.equal(generate.payload.contextFiles[0].path, 'README.md');
  assert.equal(generate.payload.spec.criteria.length, 2);
  ui.emit('code.result', {requestId:request.requestId, summary:'게시판', model:'fixture', projectRoot:'/work/shop', ops:[{file:'app.js',content:'new code',action:'create',language:'js',rationale:'entry'}]});
  assert.equal(ui.messages.length, 3, 'generation never auto-applies');
  ui.button('변경 보기').props.onClick();
  assert.equal(ui.messages.at(-1).type, 'code.diff');
  assert.equal(ui.messages.at(-1).payload.targetFolder, 'src');
  assert.equal(ui.messages.at(-1).payload.projectRoot, '/work/shop');
  ui.button('적용').props.onClick(); ui.render();
  const apply = ui.messages.at(-1);
  assert.equal(apply.type, 'code.apply');
  assert.equal(apply.payload.targetFolder, 'src');
  assert.equal(apply.payload.projectRoot, '/work/shop', '생성한 프로젝트에만 적용되도록 루트를 함께 보낸다');
  ui.emit('code.applied', {ackKey:apply.payload.ackKey, ok:true});
  assert.ok(ui.button('적용됨').props.disabled);
  assert.ok(!ui.messages.some(m => m.type.startsWith('chat.')));
});

test('cancelled or empty design still requires explicit confirmation and permits another request', () => {
  const ui = mount();
  ui.input('첫 요청'); ui.button('보내기').props.onClick();
  ui.emit('code.planResult', {requestId:ui.messages[0].payload.requestId, decisions:[]});
  assert.ok(ui.find(n => n.props.role === 'dialog'));
  ui.button('취소').props.onClick(); ui.render();
  assert.equal(ui.messages.at(-1).type, 'code.cancelPlan');
  assert.ok(!ui.messages.some(m => m.type === 'code.generate'));
  ui.input('다시 요청'); ui.button('보내기').props.onClick();
  assert.equal(ui.messages.at(-1).type, 'code.plan');
  assert.notEqual(ui.messages[0].payload.requestId, ui.messages.at(-1).payload.requestId);
});

test('timeout and API errors release the composer; late plans cannot replace a new request', () => {
  const ui = mount();
  ui.input('시간 초과'); ui.button('보내기').props.onClick();
  const expiredId = ui.messages[0].payload.requestId;
  ui.expire();
  assert.equal(ui.find(n => n.type === 'textarea').props.disabled, false);
  ui.input('다시'); ui.button('보내기').props.onClick();
  const newId = ui.messages.at(-1).payload.requestId;
  ui.emit('code.planResult', {...plan, requestId:expiredId});
  assert.equal(ui.find(n => n.props.role === 'dialog'), undefined);
  ui.emit('code.error', {requestId:newId, message:'AI unavailable'});
  assert.equal(ui.find(n => n.type === 'textarea').props.disabled, false);
});

test('Korean composition does not submit until Ctrl+Enter is pressed after composition', () => {
  const ui = mount(); ui.input('게시판');
  const input = ui.find(n => n.type === 'textarea');
  const event = {key:'Enter',ctrlKey:true,nativeEvent:{isComposing:true},preventDefault(){}};
  input.props.onKeyDown(event); assert.equal(ui.messages.length, 0);
  input.props.onKeyDown({...event,nativeEvent:{isComposing:false}});
  assert.equal(ui.messages[0].type, 'code.plan');
});

test('생성 결과가 여러 파일이면 목록만 먼저 보이고, 이름을 눌러야 내용이 펼쳐진다', () => {
  const ui = mount();
  ui.input('게시판'); ui.button('보내기').props.onClick();
  const request = ui.messages[0].payload;
  ui.emit('code.planResult', {...plan, requestId:request.requestId});
  generateThrough(ui, request.requestId);
  ui.emit('code.result', {requestId:request.requestId, summary:'게시판', model:'fixture', ops:[
    {file:'a.js',content:'AAA-CONTENT',action:'create',language:'js',rationale:''},
    {file:'b.js',content:'BBB-CONTENT',action:'edit',language:'js',rationale:''}]});
  const texts = () => JSON.stringify(ui.nodes().map(n => typeof n.props.children === 'string' ? n.props.children : ''));
  assert.ok(!texts().includes('AAA-CONTENT'), '내용이 처음부터 펼쳐져 있다');
  assert.ok(ui.find(n => n.props['data-testid'] === 'code-result-summary'));
  ui.find(n => n.type === 'button' && n.props['aria-expanded'] === false).props.onClick(); ui.render();
  assert.ok(texts().includes('AAA-CONTENT'));
  assert.ok(!texts().includes('BBB-CONTENT'));
});

test('요청 입력창: 위치·참고 파일·보내기가 한 덩어리 안에 있고, 보낸 요청은 말풍선과 위치·참고 파일로 남는다', () => {
  const ui = mount();
  const cls = name => ui.find(n => n.props.className === name);
  assert.ok(cls('rc-cg-hero'), '첫 화면 질문이 없다');
  // 예시는 입력창에 채우기만 하고 보내지 않는다.
  ui.button('게시판 API').props.onClick(); ui.render();
  assert.match(ui.find(n => n.type === 'textarea').props.value, /게시판 REST API/);
  assert.equal(ui.messages.length, 0);
  ui.emit('code.folderPicked', {folder:'shop'});
  ui.emit('code.contextAdded', {files:[{path:'server/db.js', content:'x'}]});
  const box = cls('rc-cg-box');
  const inBox = predicate => ui.nodes(box).some(predicate);
  assert.ok(inBox(n => n.type === 'textarea'));
  assert.ok(inBox(n => n.props['aria-label'] === '참고 파일 추가'));
  assert.ok(inBox(n => n.props['aria-label'] === 'server/db.js 참고 파일 제거'));
  assert.ok(inBox(n => n.props['aria-label'] === '위치 지우기'));
  assert.ok(inBox(n => n.props.className === 'rc-cg-send'));
  ui.input('쇼핑몰 만들어줘'); ui.button('보내기').props.onClick(); ui.render();
  assert.equal(ui.messages[0].payload.targetFolder, 'shop');
  assert.equal(cls('rc-cg-hero'), undefined, '보낸 뒤에도 첫 화면 질문이 남아 있다');
  assert.equal(cls('rc-cg-examples'), undefined);
  assert.equal(cls('rc-cg-me').props.children, '쇼핑몰 만들어줘');
  assert.equal(cls('rc-cg-meta').props.children, 'shop · server/db.js');
  assert.ok(cls('rc-cg-dock'), '이어서 수정 입력창이 아래에 고정되지 않았다');
  ui.find(n => n.props['aria-label'] === '위치 지우기').props.onClick(); ui.render();
  assert.equal(cls('rc-cg-meta').props.children, 'shop · server/db.js', '요청 시점의 위치가 바뀌었다');
});

test('확장이 요청을 받지 못하면 15초 뒤 원인·해결을 알리고, 보낸 내용을 입력창에 되돌려 둔다', () => {
  const ui = mount();
  ui.input('쇼핑몰 만들어줘'); ui.button('보내기').props.onClick(); ui.render();
  assert.equal(ui.find(n => n.type === 'textarea').props.value, '');
  ui.expire();
  assert.equal(ui.find(n => n.type === 'textarea').props.value, '쇼핑몰 만들어줘', '보낸 내용을 다시 써야 한다');
  const html = JSON.stringify(ui.nodes().map(n => typeof n.props.children === 'string' ? n.props.children : ''));
  assert.match(html, /Developer: Reload Window/);
  assert.match(html, /입력창에 다시 넣어 두었습니다/);
});

test('확장이 받았다고 답한 요청은 입력창에 되돌리지 않는다(응답이 늦은 경우)', () => {
  const ui = mount();
  ui.input('쇼핑몰'); ui.button('보내기').props.onClick(); ui.render();
  const id = ui.messages[0].payload.requestId;
  ui.emit('code.status', {requestId:id, stage:'planning', message:'AI 가 설계 결정을 준비하는 중…', waitSeconds:270});
  ui.expire();
  assert.equal(ui.find(n => n.type === 'textarea').props.value, '');
});

// ── 2.0.43 명세 · 합격 조건 ─────────────────────────────────────────────
const plan2 = {decisions: [{id:'storage', question:'저장 방식은?', impact:'', options:[
  {key:'sqlite', label:'SQLite', summary:'', pros:[], cons:[], recommended:true},
  {key:'pg', label:'PostgreSQL', summary:'', pros:[], cons:[], recommended:false},
]}]};
function toSpec(ui) {
  ui.input('게시판'); ui.button('보내기').props.onClick();
  const id = ui.messages[0].payload.requestId;
  ui.emit('code.planResult', {...plan2, requestId:id});
  ui.button('다음: 합격 조건 →').props.onClick(); ui.render();
  return id;
}
const editor = ui => ui.find(n => typeof n.type === 'function' && n.type.name === 'SpecEditor');

test('명세 초안을 받는 동안은 생성할 수 없고, 사람이 고친 조건만 생성 요청에 실린다', () => {
  const ui = mount();
  const id = toSpec(ui);
  assert.ok(ui.find(n => n.props['data-testid'] === 'spec-loading'));
  assert.ok(ui.button('명세대로 생성 →').props.disabled, '초안을 받는 중에는 잠김');
  ui.emit('code.specResult', {requestId:id, spec});
  const first = editor(ui).props.spec;
  assert.equal(first.criteria.length, 2);
  // 첫 조건은 빼고, 둘째는 고친다
  const edited = {...first, criteria:[{...first.criteria[0], included:false}, {...first.criteria[1], text:'쓴 글이 목록 첫 줄에 보인다', source:'user'}]};
  editor(ui).props.onChange(edited); ui.render();
  ui.button('명세대로 생성 →').props.onClick(); ui.render();
  const gen = ui.messages.at(-1);
  assert.equal(gen.type, 'code.generate');
  assert.deepEqual(gen.payload.spec.criteria, [{text:'쓴 글이 목록 첫 줄에 보인다', kind:'screen', check:'작성 후 / 첫 줄', source:'user'}]);
  assert.deepEqual(gen.payload.spec.out_of_scope, ['댓글']);
});

test('합격 조건을 모두 빼면 생성이 잠긴다', () => {
  const ui = mount();
  const id = toSpec(ui);
  ui.emit('code.specResult', {requestId:id, spec});
  const s = editor(ui).props.spec;
  editor(ui).props.onChange({...s, criteria:s.criteria.map(c => ({...c, included:false}))}); ui.render();
  assert.ok(ui.button('명세대로 생성 →').props.disabled);
  assert.ok(!ui.messages.some(m => m.type === 'code.generate'));
});

test('설계 결정으로 돌아갔다 와도 같은 선택이면 고친 명세를 지키고, 선택을 바꾸면 초안을 다시 받는다', () => {
  const ui = mount();
  const id = toSpec(ui);
  ui.emit('code.specResult', {requestId:id, spec});
  const s = editor(ui).props.spec;
  editor(ui).props.onChange({...s, goal:'직접 고친 목표'}); ui.render();
  const before = ui.messages.filter(m => m.type === 'code.spec').length;
  ui.button('← 설계 결정').props.onClick(); ui.render();
  assert.ok(ui.find(n => n.props['aria-label'] === '설계 결정'));
  ui.button('다음: 합격 조건 →').props.onClick(); ui.render();
  assert.equal(ui.messages.filter(m => m.type === 'code.spec').length, before, '같은 선택이면 다시 받지 않는다');
  assert.equal(editor(ui).props.spec.goal, '직접 고친 목표');
  ui.button('← 설계 결정').props.onClick(); ui.render();
  ui.find(n => n.type && n.type.name === 'DecisionOptionCards').props.onSelect('pg'); ui.render();
  ui.button('다음: 합격 조건 →').props.onClick(); ui.render();
  assert.equal(ui.messages.filter(m => m.type === 'code.spec').length, before + 1);
  assert.equal(ui.messages.at(-1).payload.decisions[0].chosen_key, 'pg');
});

test('초안을 못 받으면 다시 받거나, 직접 쓰거나, 명세 없이 만들 수 있다 — 턴은 오류로 바뀌지 않는다', () => {
  const ui = mount();
  const id = toSpec(ui);
  ui.emit('code.specError', {requestId:id, message:'AI 할당량 초과'});
  assert.ok(ui.find(n => n.props.role === 'alert' && JSON.stringify(n.props.children).includes('AI 할당량 초과')));
  assert.equal(editor(ui).props.spec.criteria.length, 1, '직접 쓸 빈 줄 하나');
  ui.button('다시 받기').props.onClick(); ui.render();
  assert.equal(ui.messages.at(-1).type, 'code.spec');
  ui.emit('code.specError', {requestId:id, message:'또 실패'});
  ui.button('명세 없이 생성').props.onClick(); ui.render();
  const gen = ui.messages.at(-1);
  assert.equal(gen.type, 'code.generate');
  assert.equal(gen.payload.spec, null);
});

test('확인 카드만 있는 작은 요청도 [진행] 뒤 합격 조건을 확인한다', () => {
  const ui = mount();
  ui.input('오타 고쳐줘'); ui.button('보내기').props.onClick();
  const id = ui.messages[0].payload.requestId;
  ui.emit('code.planResult', {requestId:id, decisions:[]});
  ui.button('진행 →').props.onClick(); ui.render();
  const req = ui.messages.at(-1);
  assert.equal(req.type, 'code.spec');
  assert.equal(req.payload.decisions[0].chosen_key, 'proceed');
});

test('명세 초안이 오지 않으면 고른 설계를 지킨 채 창 안에서 다시 받거나 명세 없이 만들 수 있다', () => {
  const ui = mount();
  const id = toSpec(ui);
  ui.expire();
  const dialog = ui.find(n => n.props.role === 'dialog' && n.props['aria-label'] === '명세 확인');
  assert.ok(dialog, '턴을 실패로 바꾸지 않고 창에 남는다');
  assert.ok(ui.find(n => n.props.role === 'alert' && JSON.stringify(n.props.children).includes('너무 오래')));
  ui.button('다시 받기').props.onClick(); ui.render();
  assert.equal(ui.messages.at(-1).type, 'code.spec');
  ui.emit('code.specError', {requestId:id, message:'연결 끊김'});
  ui.button('명세 없이 생성').props.onClick(); ui.render();
  const gen = ui.messages.at(-1);
  assert.equal(gen.type, 'code.generate');
  assert.equal(gen.payload.spec, null);
  assert.equal(gen.payload.decisions[0].chosen_key, 'sqlite', '고른 설계는 그대로');
});

test('코어가 합격 조건 단계를 건너뛰라고 하면 빈 편집기 대신 이유와 [생성 →] 만 보인다', () => {
  const ui = mount();
  const id = toSpec(ui);
  ui.emit('code.specResult', {requestId:id, spec:null, skip:'foundation', message:'검증된 쇼핑몰 기반은 정해진 코드를 그대로 씁니다.'});
  assert.ok(ui.find(n => n.props['data-testid'] === 'spec-skip'));
  assert.equal(editor(ui), undefined);
  const go = ui.button('생성 →');
  assert.equal(go.props.disabled, false);
  go.props.onClick(); ui.render();
  assert.equal(ui.messages.at(-1).type, 'code.generate');
  assert.equal(ui.messages.at(-1).payload.spec, null);
});

test('설계 선택을 바꾸면 예전 설계의 합격 조건 초안은 버리고 새로 받는다', () => {
  const ui = mount();
  const id = toSpec(ui);
  ui.emit('code.specResult', {requestId:id, spec});
  ui.button('← 설계 결정').props.onClick(); ui.render();
  ui.find(n => n.props && n.props.radioName === 'decision-storage').props.onSelect('pg'); ui.render();
  ui.button('다음: 합격 조건 →').props.onClick(); ui.render();
  assert.equal(ui.messages.at(-1).type, 'code.spec');
  assert.equal(ui.messages.at(-1).payload.decisions[0].chosen_key, 'pg');
  assert.equal(editor(ui), undefined, '새 초안이 올 때까지 예전 조건을 보여 주지 않는다');
  ui.emit('code.specError', {requestId:id, message:'x'});
  ui.button('명세 없이 생성').props.onClick(); ui.render();
  assert.equal(ui.messages.at(-1).payload.spec, null, '실패해도 다른 설계의 조건이 실리지 않는다');
});

test('결과에는 완성 기준 테스트 결과(조건·기록 파일)가 보이고, 생성 요청에는 팀 구성(모델·테스트)이 실린다', () => {
  const ui = mount();
  const id = toSpec(ui);
  ui.emit('code.specResult', {requestId:id, spec});
  ui.button('명세대로 생성 →').props.onClick(); ui.render();
  const gen = ui.messages.at(-1);
  assert.equal(gen.type, 'code.generate');
  assert.equal(gen.payload.test, true, '테스트는 기본으로 켠다');
  assert.equal(gen.payload.agents, 3);
  //: 기본(균형) — 팀 모드가 꺼져 있으면 개발(품질 = 기본이라 싣지 않음)과 테스트(빠른 모델)만.
  assert.deepEqual(gen.payload.models, {test: 'fast'});
  const acceptance = {status:'passed', passed:1, failed:0, unverifiable:1, total:2, checks:2, rounds:2, first:{passed:0, failed:1},
    criteria:[{id:'AC-1', verdict:'passed', retries:1, history:['failed','passed']}, {id:'AC-2', verdict:'unverifiable', reason:'화면'}]};
  ui.emit('code.result', {requestId:id, summary:'s', model:'m', ops:[{file:'a.js',content:'x',action:'create',language:'js',rationale:''}],
    spec:{...spec, file:'docs/spec/SPEC-1-x.md', verified:false, acceptance}});
  const panel = ui.find(n => n.props && n.props.spec && n.props.spec.acceptance);
  assert.ok(panel, '완성 기준 테스트 상자');
  assert.equal(panel.props.spec.file, 'docs/spec/SPEC-1-x.md');
  assert.equal(panel.props.spec.acceptance.passed, 1);
  assert.deepEqual(ui.startup.map(m => m.type), ['code.project', 'models.load'], '처음 한 번 프로젝트 이름·모델 목록을 묻는다');
});

test('위치 칩: 폴더를 고르지 않았으면 열린 프로젝트 이름을 보여 준다', () => {
  const ui = mount();
  const chip = () => ui.find(n => n.props && n.props['data-testid'] === 'code-folder');
  const label = () => JSON.stringify(chip().props.children);
  assert.ok(label().includes('프로젝트 루트'));
  ui.emit('code.project', {name: 'board-app'});
  assert.ok(label().includes('board-app'), label());
  ui.emit('code.setTargetFolder', {folder: 'server/src'});
  assert.ok(label().includes('server/src'));
});
