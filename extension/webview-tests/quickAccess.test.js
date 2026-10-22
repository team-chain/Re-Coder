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
  assert.deepEqual(kb.map((k) => [k.command, k.key]), [['recoder.quickAccess.wheel', 'alt+q']], '기본 키는 Alt+Q 하나');
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

test('고르기: 8칸 모두 이름·키가 보이고 빈 칸은 "추가"', () => {
  const html = render({});
  for (const t of ['코드 생성', '아키텍처', '배포 캔버스', '에러 분석', 'dry run', '+ 추가', '↑', 'Shift+→', 'Esc 닫기', 'Alt+Q']) assert.ok(html.includes(t), t);
  assert.equal((html.match(/data-slot=/g) || []).length, 8);
  assert.ok(html.includes('aria-label="Shift+→ 칸 비어 있음 — 기능 지정"'));
  assert.ok(!html.includes('quick-wheel-editor'));
});

test('편집: 고른 칸에 넣을 기능 목록과 다른 칸에서 쓰는 표시, 설정 문제 안내', () => {
  const html = render({ mode: 'edit', selected: 'shift+right', state: { ...STATE, problems: ['"x" 는 모르는 기능입니다'] } });
  assert.ok(html.includes('data-testid="quick-wheel-editor"'));
  assert.ok(html.includes('Shift+→ 칸'));
  for (const t of ['취약점 스캔', '시크릿 검사', '비워 두기', '기본값으로', '여는 키 바꾸기', '모르는 기능']) assert.ok(html.includes(t), t);
  assert.match(html, /코드 생성 · 수정<\/span><span[^>]*>↑/, '이미 다른 칸에 있는 기능은 그 칸 키를 보여 준다');
});

// ── 확장 쪽 명령 ─────────────────────────────────────────────────────────

function fakeVscode() {
  const store = { global: undefined, workspace: undefined };
  const calls = { updates: [], shown: [], info: [], commands: new Map(), listeners: [] };
  const vs = {
    ConfigurationTarget: { Global: 1, Workspace: 2, WorkspaceFolder: 3 },
    workspace: {
      getConfiguration: () => ({
        get: () => store.workspace ?? store.global,
        inspect: () => ({ globalValue: store.global, workspaceValue: store.workspace }),
        update: async (key, value, target) => { calls.updates.push({ key, value, target }); if (target === 2) store.workspace = value; else store.global = value; },
      }),
      onDidChangeConfiguration: (fn) => { calls.listeners.push(fn); return { dispose() {} }; },
      openTextDocument: async (uri) => ({ uri }),
    },
    window: {
      activeTextEditor: undefined,
      showTextDocument: async (doc, opts) => { calls.shown.push({ doc, opts }); },
      showInformationMessage: async (msg, ...items) => { calls.info.push(msg); return calls.infoAnswer; },
    },
    commands: { registerCommand: (id, fn) => { calls.commands.set(id, fn); return { dispose() {} }; } },
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

test('Alt+Q → 휠, Esc 로 닫으면 치던 편집기·커서로 돌아가고, 고르면 돌아가지 않는다', async () => {
  const { vs, calls } = fakeVscode();
  const h = loadHost(vs);
  const opened = [], wheels = [];
  h.registerQuickAccess({ subscriptions: [] }, { open: (t) => opened.push(t.id), showWheel: (m) => wheels.push(m), broadcast: noop });
  assert.equal(calls.commands.size, 2 + 8 + host.QUICK_TARGETS.length);
  vs.window.activeTextEditor = { document: { uri: 'file:///a.ts' }, viewColumn: 1, selection: 'sel-3:4' };
  await calls.commands.get('recoder.quickAccess.wheel')();
  assert.deepEqual(wheels, ['pick']);
  await h.quickWheelClosed(true);
  assert.equal(calls.shown.length, 1);
  assert.deepEqual(calls.shown[0].opts, { viewColumn: 1, selection: 'sel-3:4', preserveFocus: false });
  await h.quickWheelClosed(true);
  assert.equal(calls.shown.length, 1, '한 번만 돌아간다');
  await calls.commands.get('recoder.quickAccess.wheel')();
  await h.quickWheelClosed(false);
  assert.equal(calls.shown.length, 1, '기능을 골랐으면 그 화면에 머문다');
});

test('칸 명령: 지정된 기능을 열고, 빈 칸이면 [지정하기] 로 편집 휠', async () => {
  const { vs, calls } = fakeVscode();
  const h = loadHost(vs);
  const opened = [], wheels = [];
  h.registerQuickAccess({ subscriptions: [] }, { open: (t) => opened.push(t.id), showWheel: (m) => wheels.push(m), broadcast: noop });
  await calls.commands.get('recoder.quick.left')();
  await calls.commands.get('recoder.open.flow')();
  assert.deepEqual(opened, ['build', 'flow']);
  calls.infoAnswer = '지정하기';
  await calls.commands.get('recoder.quick.shiftDown')();
  assert.match(calls.info[0], /Shift\+↓/);
  assert.deepEqual(wheels, ['edit']);
});

test('휠에서 바꾼 칸은 저장되고, 작업 영역 설정이 덮고 있으면 그쪽에 쓴다', async () => {
  const { vs, store, calls } = fakeVscode();
  const h = loadHost(vs);
  const seen = [];
  h.registerQuickAccess({ subscriptions: [] }, { open: noop, showWheel: noop, broadcast: (s) => seen.push(s) });
  assert.equal(await h.setQuickSlot('shift+up', 'scan'), true);
  assert.deepEqual(calls.updates.at(-1), { key: 'quickAccess', value: { 'shift+up': 'scan' }, target: 1 });
  assert.equal(await h.setQuickSlot('up', 'nope'), false, '모르는 기능은 저장하지 않는다');
  store.workspace = { right: 'replay' };
  await h.setQuickSlot('left', 'adr');
  assert.equal(calls.updates.at(-1).target, 2);
  assert.deepEqual(calls.updates.at(-1).value, { right: 'replay', left: 'adr' });
  await h.saveQuickAccess(null);
  assert.equal(calls.updates.at(-1).value, undefined, '기본값으로 = 설정 지우기');
  calls.listeners[0]({ affectsConfiguration: (k) => k === 'recoder.quickAccess' });
  calls.listeners[0]({ affectsConfiguration: () => false });
  assert.equal(seen.length, 1);
  assert.equal(seen[0].wheelKey, process.platform === 'darwin' ? '⌥Q' : 'Alt+Q');
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
    provider.requestWheel('pick');
    await provider.handleMessage({ type: 'webview.ready', payload: { layout: 'workspace' } }, webview);
    const restore = messages.find((m) => m.type === 'ui.restore');
    assert.deepEqual(restore.payload, { view: 'map', codeDraft: '쓰던 요청', mapMode: 'flow' });
    const wheel = messages.find((m) => m.type === 'quickAccess.wheel');
    assert.equal(wheel.payload.mode, 'pick');
    assert.equal(wheel.payload.state.slots.up, 'code');
    messages.length = 0;
    await provider.handleMessage({ type: 'quickAccess.get', payload: {} }, webview);
    assert.equal(messages[0].type, 'quickAccess.state');
    assert.equal(messages[0].payload.targets.length, host.QUICK_TARGETS.length);
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
