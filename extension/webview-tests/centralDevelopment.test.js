const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const React = require('react');

// Exercise the compiled component's event handlers/state without a VS Code DOM.
// The production bundle + actual HTTP host is also exercised in workspace-preview.
function mount(props = {}) {
  const states = [], effects = [], timers = new Map(), messages = [];
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
        postMessage: (type, payload) => messages.push({type, payload}), useMessage: fn => {receive = fn;},
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
    messages, render, nodes, get reviews() {return reviews;},
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
  ui.button('이 선택으로 생성 →').props.onClick(); ui.render();
  const generate = ui.messages.at(-1);
  assert.equal(generate.type, 'code.generate');
  assert.equal(generate.payload.targetFolder, 'src');
  assert.equal(generate.payload.decisions[0].impact, '데이터 보존');
  assert.equal(generate.payload.contextFiles[0].path, 'README.md');
  ui.emit('code.result', {requestId:request.requestId, summary:'게시판', model:'fixture', projectRoot:'/work/shop', ops:[{file:'app.js',content:'new code',action:'create',language:'js',rationale:'entry'}]});
  assert.equal(ui.messages.length, 2, 'generation never auto-applies');
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
  ui.button('이 선택으로 생성 →').props.onClick(); ui.render();
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
