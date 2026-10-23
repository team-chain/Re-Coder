// 2.0.44 빠른 이동 — Alt+Q 휠 · 방향키 4칸(기본) + Shift+방향키 4칸(사용자 추가) · 기능별 열기 명령.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

const host = require('../out/quickAccess.js');
const model = require('../out/webview-test/components/quickAccessModel.js');
const { QuickWheelView } = require('../out/webview-test/components/QuickWheel.js');
const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '../package.json'), 'utf8'));

// ── 설정 해석 ────────────────────────────────────────────────────────────

test('기본 4칸 + 비어 있는 Shift 4칸, 설정은 기본값과 합친다', () => {
  const none = host.resolveQuickAccess(undefined);
  assert.deepEqual(none.slots, { up: 'code', right: 'map', down: 'deploy', left: 'build', 'shift+up': '', 'shift+right': '', 'shift+down': '', 'shift+left': '' });
  assert.deepEqual(none.problems, []);
  // 한 칸만 적어도 나머지 기본값은 살아 있다(VS Code 는 객체 설정을 통째로 바꾸므로 확장이 합친다)
  const one = host.resolveQuickAccess({ 'shift+up': 'scan' });
  assert.equal(one.slots['shift+up'], 'scan');
  assert.equal(one.slots.up, 'code');
  // "" 는 그 칸을 비운 것
  assert.equal(host.resolveQuickAccess({ up: '' }).slots.up, '');
});

test('모르는 칸·기능·모양은 버리고 이유를 남긴다', () => {
  const r = host.resolveQuickAccess({ diagonal: 'code', up: 'nope', ' Shift+Left ': 'flow', down: 3 });
  assert.equal(r.slots.up, 'code', '모르는 기능이면 기본값');
  assert.equal(r.slots['shift+left'], 'flow', '칸 이름은 대소문자·공백을 다듬는다');
  assert.equal(r.slots.down, 'deploy');
  assert.equal(r.problems.length, 3);
  assert.match(r.problems.join('\n'), /diagonal/);
  assert.equal(host.resolveQuickAccess(['code']).problems.length, 1);
  assert.equal(host.resolveQuickAccess('code').slots.up, 'code');
});

test('저장은 기본값과 다른 칸만 — 비운 기본 칸은 "" 로 남긴다', () => {
  const base = host.resolveQuickAccess(undefined).slots;
  assert.equal(host.toSetting(base), undefined, '전부 기본값이면 설정을 지운다');
  const changed = host.withSlot(host.withSlot(base, 'shift+down', 'replay'), 'left', '');
  assert.deepEqual(host.toSetting(changed), { left: '', 'shift+down': 'replay' });
  assert.deepEqual(host.resolveQuickAccess(host.toSetting(changed)).slots, changed, '저장 → 읽기가 같은 결과');
  assert.equal(host.withSlot(base, 'nope', 'code'), base);
  assert.equal(host.withSlot(base, 'up', 'nope'), base);
});

// ── package.json 과 화면이 같은 표를 본다 ─────────────────────────────────

test('명령·키·설정 스키마가 기능 표와 맞고, VS Code 기본 키를 가로채지 않는다', () => {
  const cmds = new Set(pkg.contributes.commands.map((c) => c.command));
  for (const t of host.QUICK_TARGETS) assert.ok(cmds.has(`recoder.open.${t.id}`), t.id);
  for (const s of host.QUICK_SLOTS) assert.ok(cmds.has(`recoder.quick.${host.SLOT_COMMAND[s]}`), s);
  assert.ok(cmds.has('recoder.quickAccess.wheel') && cmds.has('recoder.quickAccess.configure'));
  const kb = pkg.contributes.keybindings;
  const always = kb.filter((k) => !k.when);
  assert.deepEqual(always.map((k) => [k.command, k.key]), [['recoder.quickAccess.wheel', 'alt+q']], '항상 켜진 키는 Alt+Q 하나');
  //: 방향키·Esc·E 는 휠이 떠 있을 때만(when) — 평소 편집기 방향키를 가로채지 않는다.
  const wheelKeys = kb.filter((k) => k.when);
  assert.ok(wheelKeys.every((k) => k.when === 'recoder.quickWheelOpen'));
  assert.deepEqual(wheelKeys.filter((k) => k.command === 'recoder.quickWheel.key').map((k) => [k.key, k.args]),
    host.QUICK_SLOTS.map((s) => [s, s]), '8칸 모두 방향키 하나로');
  assert.ok(wheelKeys.some((k) => k.command === 'recoder.quickWheel.close' && k.key === 'escape'));
  assert.ok(wheelKeys.some((k) => k.command === 'recoder.quickWheel.edit' && k.key === 'e'));
  const keys = JSON.stringify(kb).toLowerCase();
  assert.ok(!/ctrl\+shift\+r|cmd\+shift\+r/.test(keys), 'Ctrl+Shift+R 은 VS Code 리팩터링 — 두 번 누르는 키의 앞 키로 잡으면 리팩터링이 안 된다');
  assert.ok(!/alt\+(up|down|left|right)/.test(keys), 'Alt+방향키는 줄 이동·뒤로 가기');
  const schema = pkg.contributes.configuration.properties['recoder.quickAccess'];
  assert.deepEqual(Object.keys(schema.properties), [...host.QUICK_SLOTS]);
  for (const s of host.QUICK_SLOTS) assert.deepEqual(schema.properties[s].enum, [...host.QUICK_TARGETS.map((t) => t.id), '']);
  assert.equal(schema.additionalProperties, false);
});

test('화면의 기능 → 화면 표가 확장의 표와 같다', () => {
  for (const t of host.QUICK_TARGETS) {
    const v = model.viewFor(t.id);
    assert.ok(v, t.id);
    assert.equal(v.view, t.view, t.id);
    assert.equal(v.mapMode, t.mapMode, t.id);
  }
  assert.equal(model.viewFor('nope'), null);
});

// ── 화면 모델 ────────────────────────────────────────────────────────────

const STATE = {
  slots: { up: 'code', right: 'map', down: 'deploy', left: 'build', 'shift+up': 'flow', 'shift+right': '', 'shift+down': '', 'shift+left': '' },
  problems: [], targets: host.QUICK_TARGETS.map((t) => ({ id: t.id, label: t.label })),
  keys: Object.fromEntries(host.QUICK_SLOTS.map((s) => [s, host.slotKeyLabel(s)])), wheelKey: 'Alt+Q',
};

test('받은 상태를 다듬는다 — 모르는 기능은 빈 칸, 키 표시는 기본값으로 채움', () => {
  const s = model.toState({ ...STATE, slots: { ...STATE.slots, up: 'evil' }, keys: {}, problems: ['a', 5] });
  assert.equal(s.slots.up, '');
  assert.equal(s.keys['shift+left'], 'Alt+Shift+←');
  assert.deepEqual(s.problems, ['a']);
  assert.equal(model.toState({ slots: {} }), null, '기능 목록이 없으면 그리지 않는다');
  assert.equal(model.toState(null), null);
});

test('방향키 → 칸, 편집 중 칸 옮기기', () => {
  assert.equal(model.slotForKey('ArrowUp', false), 'up');
  assert.equal(model.slotForKey('ArrowLeft', true), 'shift+left');
  assert.equal(model.slotForKey('a', false), null);
  assert.equal(model.moveSelection('up', 'ArrowDown'), 'down');
  assert.equal(model.moveSelection('shift+up', 'ArrowRight'), 'shift+right', '안쪽 고리 안에서');
  assert.equal(model.moveSelection('right', 'Tab'), 'shift+right');
  assert.equal(model.moveSelection('shift+right', 'Tab'), 'right');
});

test('도넛 조각 경로는 닫힌 모양이고 칸 방향에 놓인다', () => {
  const d = model.petalPath(180, 180, 96, 172, -132, -48);
  assert.match(d, /^M .* A 172 172 0 0 1 .* L .* A 96 96 0 0 0 .* Z$/);
  const up = model.labelPoint(180, 180, 130, 'up');
  assert.ok(Math.abs(up.x - 180) < 0.01 && up.y < 180);
  const left = model.labelPoint(180, 180, 130, 'shift+left');
  assert.ok(left.x < 180 && Math.abs(left.y - 180) < 0.01);
});

// ── 휠 그리기 ────────────────────────────────────────────────────────────

const noop = () => {};
const render = (props) => renderToStaticMarkup(React.createElement(QuickWheelView, {
  state: STATE, mode: 'pick', selected: null, onSlot: noop, onAssign: noop, onMode: noop, onReset: noop, onKeybindings: noop, onClose: noop, ...props,
}));

test('고르기: 방향키 4칸은 아이콘·이름·키, Shift 칸은 넣은 것만 — 빈 "추가" 칸·가운데 원·긴 설명이 없다', () => {
  const html = render({});
  for (const t of ['코드 생성', '아키텍처', '배포 캔버스', '에러 분석', '↑', '→', 'Esc 닫기', 'E 칸 바꾸기', 'data-icon="code"', 'data-icon="flow"']) assert.ok(html.includes(t), t);
  assert.ok(!/[⚠☁✎⌂]/.test(html), '2.0.46 글자 기호 대신 선 아이콘');
  assert.equal((html.match(/data-slot=/g) || []).length, 5, '바깥 4칸 + 넣어 둔 Shift+↑ 하나');
  assert.ok(html.includes('data-slot="shift+up"') && !html.includes('data-slot="shift+right"'), '빈 Shift 칸은 고르기에서 숨긴다');
  assert.ok(!html.includes('+ 추가') && !html.includes('빠른 이동</text>'), '빈 칸 "추가"와 가운데 원 글자가 없다');
  assert.ok(!html.includes('방향키로 열기'), '긴 설명줄이 없다');
  assert.ok(!html.includes('quick-wheel-editor'));
  // 바깥 칸과 같은 기능을 Shift 칸에도 넣었으면 중복은 숨긴다
  const dup = render({ state: { ...STATE, slots: { ...STATE.slots, 'shift+up': 'code' } } });
  assert.equal((dup.match(/data-slot=/g) || []).length, 4);
});

test('편집: 고른 칸에 넣을 기능 목록과 다른 칸에서 쓰는 표시, 설정 문제 안내', () => {
  const html = render({ mode: 'edit', selected: 'shift+right', state: { ...STATE, problems: ['"x" 는 모르는 기능입니다'] } });
  assert.ok(html.includes('data-testid="quick-wheel-editor"'));
  assert.ok(html.includes('Shift+→ 칸'));
  for (const t of ['취약점 스캔', '시크릿 검사', '비워 두기', '기본값으로', '여는 키 바꾸기', '모르는 기능']) assert.ok(html.includes(t), t);
  assert.match(html, /코드 생성 · 수정<\/span><span[^>]*>↑/, '이미 다른 칸에 있는 기능은 그 칸 키를 보여 준다');
  assert.equal((html.match(/data-slot=/g) || []).length, 8, '칸 바꾸기에서는 빈 칸도 모두 보인다');
  assert.ok(html.includes('같은 키를 다시 누르면 다음 기능'));
  assert.ok(!/Enter/.test(html), 'Enter 를 누르라는 안내가 없다');
});

// ── 확장 쪽 명령 ─────────────────────────────────────────────────────────

function fakeVscode() {
  const store = { global: undefined, workspace: undefined, folder: undefined };
  const calls = { updates: [], shown: [], diffs: [], info: [], commands: new Map(), listeners: [], ctx: [], ev: { doc: [], sel: [], win: [] } };
  const on = (list) => (fn) => { list.push(fn); return { dispose() { const i = list.indexOf(fn); if (i >= 0) list.splice(i, 1); } }; };
  const merged = () => {
    // VS Code 처럼 객체 설정은 범위끼리 합쳐서 준다
    const parts = [store.global, store.workspace, store.folder].filter((v) => v !== undefined);
    return parts.length ? Object.assign({}, ...parts) : undefined;
  };
  const vs = {
    ConfigurationTarget: { Global: 1, Workspace: 2, WorkspaceFolder: 3 },
    TextEditorSelectionChangeKind: { Keyboard: 1, Mouse: 2, Command: 3 },
    workspace: {
      getConfiguration: (section) => section === 'terminal.integrated' ? {
        get: () => store.skipShell,
        inspect: () => ({ globalValue: store.skipShell }),
        update: async (key, value, target) => { calls.updates.push({ key, value, target }); store.skipShell = value; },
      } : ({
        get: () => merged(),
        inspect: () => ({ globalValue: store.global, workspaceValue: store.workspace, workspaceFolderValue: store.folder }),
        update: async (key, value, target) => {
          calls.updates.push({ key, value, target });
          if (calls.failNext) { calls.failNext = false; throw new Error('쓰기 실패'); }
          await new Promise((r) => setTimeout(r, 2)); // 실제 저장처럼 늦게 끝난다
          if (target === 3) store.folder = value; else if (target === 2) store.workspace = value; else store.global = value;
        },
      }),
      onDidChangeConfiguration: (fn) => { calls.listeners.push(fn); return { dispose() {} }; },
      onDidChangeTextDocument: on(calls.ev.doc),
      openTextDocument: async (uri) => { if (calls.missing === uri) throw new Error('없음'); return { uri }; },
      openNotebookDocument: async (uri) => ({ uri, notebook: true }),
    },
    window: {
      activeTextEditor: undefined,
      onDidChangeTextEditorSelection: on(calls.ev.sel),
      onDidChangeWindowState: on(calls.ev.win),
      tabGroups: { activeTabGroup: { viewColumn: 1, activeTab: undefined } },
      showTextDocument: async (doc, opts) => { calls.shown.push({ doc, opts }); },
      showNotebookDocument: async (doc, opts) => { calls.shown.push({ doc, opts }); },
      showInformationMessage: async (msg, ...items) => { calls.info.push(msg); return calls.infoAnswer; },
      showErrorMessage: async (msg) => { calls.info.push(msg); },
    },
    commands: {
      registerCommand: (id, fn) => { calls.commands.set(id, fn); return { dispose() {} }; },
      executeCommand: async (id, ...args) => { if (id === 'setContext') { calls.ctx.push(args); return; } calls.diffs.push({ id, args }); },
    },
  };
  return { vs, store, calls };
}
function loadHost(vs) {
  const file = require.resolve('../out/quickAccessHost.js');
  delete require.cache[file];
  const origLoad = Module._load;
  Module._load = function (request, ...rest) { return request === 'vscode' ? vs : origLoad.call(this, request, ...rest); };
  try { return require(file); } finally { Module._load = origLoad; }
}

test('Alt+Q → 휠, Esc 로 닫으면 치던 편집기·커서로 돌아가고, 고르거나 다른 곳을 누르면 돌아가지 않는다', async () => {
  const { vs, calls } = fakeVscode();
  const h = loadHost(vs);
  const opened = [], wheels = [];
  h.registerQuickAccess({ subscriptions: [] }, { open: (t) => opened.push(t.id), showWheel: (m) => wheels.push(m), hideWheel: noop, broadcast: noop });
  assert.equal(calls.commands.size, 3 + 3 + 8 + host.QUICK_TARGETS.length);
  vs.window.activeTextEditor = { document: { uri: 'file:///a.ts' }, viewColumn: 1, selection: 'sel-3:4' };
  await calls.commands.get('recoder.quickAccess.wheel')();
  assert.deepEqual(wheels, ['pick']);
  await h.quickWheelClosed('cancel');
  assert.equal(calls.shown.length, 1);
  assert.deepEqual(calls.shown[0].opts, { viewColumn: 1, selection: 'sel-3:4', preserveFocus: false });
  await h.quickWheelClosed('cancel');
  assert.equal(calls.shown.length, 1, '한 번만 돌아간다');
  await calls.commands.get('recoder.quickAccess.wheel')();
  await h.quickWheelClosed('pick');
  assert.equal(calls.shown.length, 1, '기능을 골랐으면 그 화면에 머문다');
  await calls.commands.get('recoder.quickAccess.wheel')();
  await h.quickWheelClosed('dismiss');
  assert.equal(calls.shown.length, 1, '다른 곳을 눌러 떠났으면 사용자가 간 곳에 둔다');
});

test('돌아갈 곳: ReCoder 창에서 다시 누르면 앞서 기억한 편집기를 지키고, diff·노트북도 그대로 연다', async () => {
  const { vs, calls } = fakeVscode();
  const h = loadHost(vs);
  h.registerQuickAccess({ subscriptions: [] }, { open: noop, showWheel: noop, hideWheel: noop, broadcast: noop });
  const wheel = calls.commands.get('recoder.quickAccess.wheel');
  vs.window.activeTextEditor = { document: { uri: 'file:///a.ts' }, viewColumn: 2, selection: 's' };
  await wheel();
  vs.window.activeTextEditor = undefined; // ReCoder 탭이 앞으로 와 편집기가 가려진 뒤
  await wheel(); // 두 번째 Alt+Q = 닫기
  await new Promise((r) => setImmediate(r));
  assert.equal(calls.shown.at(-1).doc.uri, 'file:///a.ts', '다시 눌러 닫아도 원래 파일로');

  vs.window.tabGroups.activeTabGroup.activeTab = { label: 'a ↔ b', input: { original: 'file:///a', modified: 'file:///b' } };
  await wheel();
  await h.quickWheelClosed('cancel');
  assert.equal(calls.diffs.at(-1).id, 'vscode.diff');
  assert.deepEqual(calls.diffs.at(-1).args.slice(0, 3), ['file:///a', 'file:///b', 'a ↔ b']);

  vs.window.tabGroups.activeTabGroup.activeTab = { label: 'n', input: { uri: 'file:///n.ipynb', notebookType: 'jupyter-notebook' } };
  await wheel();
  await h.quickWheelClosed('cancel');
  assert.equal(calls.shown.at(-1).doc.notebook, true);

  // 그 사이 파일이 지워졌어도 오류 없이 그대로 둔다
  vs.window.tabGroups.activeTabGroup.activeTab = undefined;
  vs.window.activeTextEditor = { document: { uri: 'file:///gone.ts' }, viewColumn: 1, selection: 's' };
  calls.missing = 'file:///gone.ts';
  await wheel();
  const before = calls.shown.length;
  await h.quickWheelClosed('cancel');
  assert.equal(calls.shown.length, before);

  // 칸 바꾸기(설정 명령)는 편집기로 돌아가지 않는다
  vs.window.activeTextEditor = { document: { uri: 'file:///a.ts' }, viewColumn: 1, selection: 's' };
  await wheel();
  await calls.commands.get('recoder.quickAccess.configure')();
  await h.quickWheelClosed('cancel');
  assert.equal(calls.shown.length, before);
});

test('칸 명령: 지정된 기능을 열고, 빈 칸이면 [지정하기] 로 편집 휠', async () => {
  const { vs, calls } = fakeVscode();
  const h = loadHost(vs);
  const opened = [], wheels = [];
  h.registerQuickAccess({ subscriptions: [] }, { open: (t) => opened.push(t.id), showWheel: (m) => wheels.push(m), hideWheel: noop, broadcast: noop });
  await calls.commands.get('recoder.quick.left')();
  await calls.commands.get('recoder.open.flow')();
  assert.deepEqual(opened, ['build', 'flow']);
  calls.infoAnswer = '지정하기';
  await calls.commands.get('recoder.quick.shiftDown')();
  assert.match(calls.info[0], /Shift\+↓/);
  assert.deepEqual(wheels, ['edit']);
});

test('휠에서 바꾼 칸은 그 칸을 정하는 가장 좁은 범위에, 그 범위 값만 저장한다', async () => {
  const { vs, store, calls } = fakeVscode();
  const h = loadHost(vs);
  const seen = [];
  h.registerQuickAccess({ subscriptions: [] }, { open: noop, showWheel: noop, hideWheel: noop, broadcast: (s) => seen.push(s) });
  assert.equal(await h.setQuickSlot('shift+up', 'scan'), true);
  assert.deepEqual(calls.updates.at(-1), { key: 'quickAccess', value: { 'shift+up': 'scan' }, target: 1 });
  assert.equal(await h.setQuickSlot('up', 'nope'), false, '모르는 기능은 저장하지 않는다');
  assert.equal(await h.setQuickSlot('diag', 'code'), false, '모르는 칸도');
  const n = calls.updates.length;

  // 작업 영역 설정이 있으면 그쪽에 — 사용자 설정(shift+up)은 작업 영역 파일로 복사되지 않는다
  store.workspace = { right: 'replay' };
  await h.setQuickSlot('left', 'adr');
  assert.equal(calls.updates.length, n + 1);
  assert.equal(calls.updates.at(-1).target, 2);
  assert.deepEqual(calls.updates.at(-1).value, { right: 'replay', left: 'adr' });
  assert.deepEqual(store.global, { 'shift+up': 'scan' }, '사용자 설정은 그대로');

  // 아래 범위(사용자 설정·기본값)와 같은 값으로 되돌리면 적지 않는다
  await h.setQuickSlot('right', 'map');
  assert.deepEqual(store.workspace, { left: 'adr' });
  await h.setQuickSlot('shift+up', 'scan');
  assert.deepEqual(store.workspace, { left: 'adr' }, '사용자 설정이 이미 주는 값');
  await h.setQuickSlot('left', 'build');
  assert.equal(store.workspace, undefined, '남은 것이 없으면 그 범위 설정을 지운다');

  // 폴더 설정이 가장 좁다
  store.folder = { down: 'ship' };
  await h.setQuickSlot('down', 'replay');
  assert.equal(calls.updates.at(-1).target, 3);
  assert.deepEqual(store.folder, { down: 'replay' });

  // 기본값으로 = 값이 있는 모든 범위를 비운다
  store.workspace = { up: 'adr' };
  await h.resetQuickAccess();
  assert.deepEqual([store.global, store.workspace, store.folder], [undefined, undefined, undefined]);
  assert.deepEqual(h.currentQuickAccess().slots, host.resolveQuickAccess(undefined).slots);

  calls.listeners[0]({ affectsConfiguration: (k) => k === 'recoder.quickAccess' });
  calls.listeners[0]({ affectsConfiguration: () => false });
  assert.equal(seen.length, 1);
  assert.equal(seen[0].wheelKey, process.platform === 'darwin' ? '⌥Q' : 'Alt+Q');
});

test('빠르게 두 칸을 바꿔도 앞의 변경이 사라지지 않고, 저장 실패는 알린 뒤 다음 저장은 된다', async () => {
  const { vs, store, calls } = fakeVscode();
  const h = loadHost(vs);
  h.registerQuickAccess({ subscriptions: [] }, { open: noop, showWheel: noop, hideWheel: noop, broadcast: noop });
  await Promise.all([h.setQuickSlot('shift+up', 'scan'), h.setQuickSlot('shift+down', 'replay'), h.setQuickSlot('shift+left', 'adr')]);
  assert.deepEqual(store.global, { 'shift+up': 'scan', 'shift+down': 'replay', 'shift+left': 'adr' });
  calls.failNext = true;
  await assert.rejects(h.setQuickSlot('shift+right', 'ship'), /쓰기 실패/);
  assert.equal(await h.setQuickSlot('shift+right', 'ship'), true);
  assert.equal(store.global['shift+right'], 'ship');
});

test('ReCoder 창이 닫혀 있을 때 요청한 화면으로 연다(마지막에 보던 화면이 아니라) — 쓰던 요청은 지킨다', async () => {
  const origResolve = Module._resolveFilename;
  Module._resolveFilename = function (request, ...args) {
    return request === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : origResolve.call(this, request, ...args);
  };
  try {
    const { SidebarProvider } = require('../out/sidebar/SidebarProvider.js');
    const vscode = require('vscode');
    const mem = new Map([['recoder.workspaceUiState', { scope: 'empty', ts: Date.now(), state: { view: 'replay', codeDraft: '쓰던 요청' } }]]);
    const globalState = { get: (k) => mem.get(k), update: async (k, v) => { mem.set(k, v); } };
    const provider = new SidebarProvider(vscode.Uri.file('/tmp/ext'), {}, { onCoreRestart: () => ({ dispose() {} }) }, { stop() {}, start() {}, getLastHealth() { return null; } }, undefined, globalState);
    provider.connectAndDiagnose = () => {};
    const messages = [];
    const webview = { postMessage: (m) => { messages.push(m); return true; }, onDidReceiveMessage: () => ({ dispose() {} }) };
    provider.attachWorkspacePanel(webview);
    provider.requestView('map', { mapMode: 'flow' });
    provider.requestWheel('edit', 'shift+up');
    await provider.handleMessage({ type: 'webview.ready', payload: { layout: 'workspace' } }, webview);
    const restore = messages.find((m) => m.type === 'ui.restore');
    assert.deepEqual(restore.payload, { view: 'map', codeDraft: '쓰던 요청', mapMode: 'flow' });
    const wheel = messages.find((m) => m.type === 'quickAccess.wheel');
    assert.equal(wheel.payload.mode, 'edit');
    assert.equal(wheel.payload.slot, 'shift+up', '빈 칸을 눌러 들어왔으면 그 칸을 고른 채');
    assert.equal(wheel.payload.state.slots.up, 'code');
    messages.length = 0;
    await provider.handleMessage({ type: 'quickAccess.get', payload: {} }, webview);
    assert.equal(messages[0].type, 'quickAccess.state');
    assert.equal(messages[0].payload.targets.length, host.QUICK_TARGETS.length);
    // 창을 그리는 사이 휠이 닫혔으면(Esc·시간 초과) 늦게 띄우지 않는다
    messages.length = 0;
    provider.requestWheel('pick');
    await provider.handleMessage({ type: 'webview.ready', payload: { layout: 'workspace' } }, webview);
    assert.ok(!messages.some((m) => m.type === 'quickAccess.wheel'));
  } finally {
    Module._resolveFilename = origResolve;
  }
});

test('App 결선: 머리글 버튼, 휠 열기·닫기 알림, 지도 모드 전달', () => {
  const app = fs.readFileSync(path.join(__dirname, '../webview-src/App.tsx'), 'utf8');
  for (const s of ['<QuickAccess onSelectMode={onSelectMode} />', 'postMessage("quickAccess.closed"', 'postMessage("quickAccess.get")', 'requestMapMode(p.mapMode)', '"quickAccess.wheel"']) assert.ok(app.includes(s), s);
  const map = fs.readFileSync(path.join(__dirname, '../webview-src/components/CodeMap.tsx'), 'utf8');
  assert.ok(map.includes('export function requestMapMode') && map.includes('takeMapMode()'));
});

test('터미널에서도 Alt+Q: 묻고 나서 사용자 설정에 한 줄만 더하고, 다른 항목·중복은 건드리지 않는다', async () => {
  const { vs, store, calls } = fakeVscode();
  const h = loadHost(vs);
  h.registerQuickAccess({ subscriptions: [] }, { open: noop, showWheel: noop, hideWheel: noop, broadcast: noop });
  const cmd = calls.commands.get('recoder.quickAccess.enableInTerminal');
  store.skipShell = ['workbench.action.quickOpen', '-recoder.quickAccess.wheel'];
  calls.infoAnswer = undefined;
  await cmd();
  assert.deepEqual(store.skipShell, ['workbench.action.quickOpen', '-recoder.quickAccess.wheel'], '취소하면 바꾸지 않는다');
  calls.infoAnswer = '더하기';
  await cmd();
  assert.deepEqual(store.skipShell, ['workbench.action.quickOpen', 'recoder.quickAccess.wheel', ...h.WHEEL_KEY_COMMANDS], '빼 두었던 표시는 지우고, 휠 방향키 명령까지 더한다');
  assert.equal(calls.updates.at(-1).target, 1, '사용자 설정에');
  assert.equal(h.wheelWorksInTerminal(), true);
  const n = calls.updates.length;
  await cmd();
  assert.equal(calls.updates.length, n, '이미 되어 있으면 다시 쓰지 않는다');
  assert.match(calls.info.at(-1), /이미/);
  const ids = pkg.contributes.commands.map((c) => c.command);
  assert.ok(ids.includes('recoder.quickAccess.enableInTerminal'));
});

test('Alt+Q → 방향키 한 번: 클릭·입력·Enter 없이 VS Code 단축키로 바로 연다', async () => {
  const { vs, calls } = fakeVscode();
  const h = loadHost(vs);
  const opened = [], wheels = [];
  let hidden = 0;
  h.registerQuickAccess({ subscriptions: [] }, { open: (t) => opened.push(t.id), showWheel: (m, slot) => wheels.push([m, slot]), hideWheel: () => hidden++, broadcast: noop });
  const key = calls.commands.get('recoder.quickWheel.key');
  await key('right');
  assert.deepEqual(opened, [], '휠이 닫혀 있으면 방향키 명령은 아무것도 하지 않는다');
  vs.window.activeTextEditor = { document: { uri: 'file:///a.ts' }, viewColumn: 1, selection: 's' };
  await calls.commands.get('recoder.quickAccess.wheel')();
  assert.deepEqual(calls.ctx.at(-1), ['recoder.quickWheelOpen', true], '휠이 떠 있는 동안만 방향키를 받는다');
  assert.deepEqual(wheels, [['pick', undefined]]);
  assert.equal(h.isWheelOpen(), true);
  await key('right');
  assert.deepEqual(opened, ['map'], '→ 한 번에 아키텍처');
  assert.deepEqual(calls.ctx.at(-1), ['recoder.quickWheelOpen', false]);
  assert.equal(hidden, 1, '화면의 휠도 닫는다');
  assert.equal(calls.shown.length, 0, '고른 화면에 머문다(편집기로 돌아가지 않음)');

  // 빈 Shift 칸 → 그 칸을 고른 채 칸 바꾸기
  await calls.commands.get('recoder.quickAccess.wheel')();
  await key({ slot: 'shift+down' });
  assert.deepEqual(wheels.at(-1), ['edit', 'shift+down']);
  assert.equal(h.isWheelOpen(), false);

  // Esc → 닫고 치던 편집기로
  await calls.commands.get('recoder.quickAccess.wheel')();
  await calls.commands.get('recoder.quickWheel.close')();
  await new Promise((r) => setImmediate(r));
  assert.equal(calls.shown.at(-1).doc.uri, 'file:///a.ts');
  assert.equal(h.isWheelOpen(), false);

  // E → 칸 바꾸기
  await calls.commands.get('recoder.quickAccess.wheel')();
  await calls.commands.get('recoder.quickWheel.edit')();
  assert.deepEqual(wheels.at(-1), ['edit', undefined]);
  // 모르는 칸 이름은 무시
  await calls.commands.get('recoder.quickAccess.wheel')();
  await key('diagonal');
  assert.equal(h.isWheelOpen(), true);
});

test('휠을 띄운 채 다른 일을 시작하면 닫힌다 — 방향키가 휠에 묶여 남지 않게', async (t) => {
  const { vs, calls } = fakeVscode();
  const h = loadHost(vs);
  h.registerQuickAccess({ subscriptions: [] }, { open: noop, showWheel: noop, hideWheel: noop, broadcast: noop });
  const wheel = calls.commands.get('recoder.quickAccess.wheel');
  await wheel();
  calls.ev.sel.forEach((fn) => fn({ kind: 3 }));
  assert.equal(h.isWheelOpen(), true, '명령으로 생긴 커서 이동(탭 전환 등)은 닫는 이유가 아니다');
  calls.ev.doc.forEach((fn) => fn({ contentChanges: [{}] }));
  assert.equal(h.isWheelOpen(), false, '글자를 치면 닫힌다');
  assert.equal(calls.ev.doc.length + calls.ev.sel.length + calls.ev.win.length, 0, '닫히면 감시도 걷어 낸다');
  await wheel();
  calls.ev.sel.forEach((fn) => fn({ kind: 2 }));
  assert.equal(h.isWheelOpen(), false, '마우스로 다른 곳을 누르면 닫힌다');
  await wheel();
  calls.ev.win.forEach((fn) => fn({ focused: false }));
  assert.equal(h.isWheelOpen(), false, 'VS Code 창을 떠나면 닫힌다');
  t.mock.timers.enable({ apis: ['setTimeout'] });
  await wheel();
  t.mock.timers.tick(h.WHEEL_IDLE_MS + 1);
  assert.equal(h.isWheelOpen(), false, '아무것도 고르지 않으면 잠시 뒤 닫힌다');
  t.mock.timers.reset();
});

test('칸 바꾸기 규칙: 같은 기능은 두 칸에 생기지 않는다(맞바꿈), 같은 방향키를 다시 누르면 다음 기능', () => {
  assert.deepEqual(model.visibleInner(STATE), ['shift+up']);
  assert.deepEqual(model.visibleInner({ ...STATE, slots: { ...STATE.slots, 'shift+up': 'map' } }), [], '바깥 칸과 같으면 숨김');
  // 맞바꿈
  assert.deepEqual(model.assignSlot(STATE, 'up', 'map'), [{ slot: 'up', target: 'map' }, { slot: 'right', target: 'code' }]);
  assert.deepEqual(model.assignSlot(STATE, 'shift+right', 'code'), [{ slot: 'shift+right', target: 'code' }, { slot: 'up', target: '' }]);
  assert.deepEqual(model.assignSlot(STATE, 'up', 'code'), [], '그대로면 바꿀 것 없음');
  assert.deepEqual(model.assignSlot(STATE, 'shift+right', ''), [], '빈 칸을 비우기');
  // 다음 기능: 다른 칸이 쓰는 기능은 건너뛴다
  const order = [];
  let st = STATE;
  for (let i = 0; i < 12; i++) {
    const n = model.nextTarget(st, 'shift+right');
    order.push(n);
    st = { ...st, slots: { ...st.slots, 'shift+right': n } };
  }
  assert.ok(!order.some((id) => ['code', 'map', 'deploy', 'build', 'flow'].includes(id)), '다른 칸의 기능은 차례에 없다');
  assert.ok(order.includes(''), 'Shift 칸은 비우기도 차례에 든다');
  assert.ok(!model.nextTarget(STATE, 'up') || model.nextTarget(STATE, 'up') !== 'code');
  for (const t of host.QUICK_TARGETS) assert.notEqual(model.iconFor(t.id), '•', `${t.id} 아이콘`);
});
