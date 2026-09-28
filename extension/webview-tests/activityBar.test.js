const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function load(name, vscode) {
  const exports = {};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../out/sidebar', name + '.js'), 'utf8'), {
    exports, require: id => {
      if (id === 'vscode') return vscode;
      // Other provider imports are irrelevant to navigation, and must not start Core in this test.
      return {};
    }, setTimeout, clearTimeout, console,
  });
  return exports;
}

function fixture(values = {}) {
  const stored = new Map(Object.entries(values)), calls = [];
  const commands = {
    getCommands: async () => ['recoder.sidebarView.resetViewLocation'],
    executeCommand: async (...args) => { calls.push(args); },
  };
  return {
    stored, calls, commands,
    state: { get: (k, fallback) => stored.has(k) ? stored.get(k) : fallback,
      update: async (k, v) => { stored.set(k, v); } },
    ...load('activityBar', { commands }),
  };
}

test('Activity Bar entry is visible and keeps existing providers and commands', () => {
  const manifest = require('../package.json');
  const containers = manifest.contributes.viewsContainers.activitybar;
  assert.equal(containers.length, 1, 'no duplicate ReCoder icons');
  assert.notEqual(containers[0].id, 'recoder', 'must escape the old saved container location');
  assert.ok(fs.existsSync(path.join(__dirname, '..', containers[0].icon)));
  const views = manifest.contributes.views[containers[0].id];
  assert.equal(views.find(v => v.id === 'recoder.sidebarView').visibility, 'visible');
  assert.ok(views.some(v => v.id === 'recoder.workbenchView'), 'retain legacy Workbench access');
});

test('fresh install does not open a tab or move unrelated views at startup', async () => {
  const f = fixture();
  await f.migrateActivityBar(f.state);
  assert.deepEqual(f.calls, []);
  assert.equal(f.stored.get('recoder.layout.activityBarV2'), true);
});

test('upgrade restores only ReCoder and respects subsequent user layout changes', async () => {
  const f = fixture({ 'recoder.layout.initializedV1': true });
  await f.migrateActivityBar(f.state);
  assert.deepEqual(f.calls, [['recoder.sidebarView.resetViewLocation']]);
  await f.migrateActivityBar(f.state);
  assert.equal(f.calls.length, 1, 'migration must run only once');
});

test('unavailable reset command leaves migration pending for next activation', async () => {
  const f = fixture({ 'recoder.layout.initializedV1': true });
  f.commands.getCommands = async () => [];
  await assert.rejects(f.migrateActivityBar(f.state), /VS Code/);
  assert.equal(f.stored.has('recoder.layout.activityBarV2'), false);
  assert.deepEqual(f.calls, []);
  f.commands.getCommands = async () => ['recoder.sidebarView.resetViewLocation'];
  await f.migrateActivityBar(f.state);
  assert.equal(f.stored.get('recoder.layout.activityBarV2'), true);
});

test('failed relocation is retried, never reported as a successful migration', async () => {
  const f = fixture({ 'recoder.layout.initializedV1': true });
  f.commands.executeCommand = async () => { throw new Error('view not ready'); };
  await assert.rejects(f.migrateActivityBar(f.state), /view not ready/);
  assert.equal(f.stored.has('recoder.layout.activityBarV2'), false);
});

test('manual relocation targets ReCoder even when Explorer has focus', async () => {
  const f = fixture();
  await f.chooseSidebarLocation();
  assert.deepEqual(f.calls, [['workbench.action.moveFocusedView', 'recoder.sidebarView']]);
});

test('returning to the Activity Bar reveals the workspace again; delayed initial event is deduplicated', () => {
  const { SidebarProvider } = load('SidebarProvider', {});
  const provider = Object.create(SidebarProvider.prototype);
  let opened = 0;
  provider._openWorkspace = () => opened++;
  provider._openWorkspaceFromSidebar();
  provider._openWorkspaceFromSidebar();
  assert.equal(opened, 1);
  provider._openWorkspaceFromSidebar(true);
  assert.equal(opened, 2);
});

test('reopening workspace reuses the existing panel and moves focus into it', () => {
  const reveals = [];
  let sidebarCloses = 0;
  const { ReCoderPanel } = load('ReCoderPanel', {
    window: { activeTextEditor: { viewColumn: 2 } }, ViewColumn: { One: 1 },
  });
  ReCoderPanel._current = {
    _panel: { reveal: (...args) => reveals.push(args) },
    _hideSidebar: () => sidebarCloses++,
  };
  const first = ReCoderPanel.createOrShow({}, {});
  assert.equal(ReCoderPanel.createOrShow({}, {}), first);
  assert.deepEqual(reveals, [[2, false], [2, false]]);
  assert.equal(sidebarCloses, 2, 'each re-entry must prepare the next one-click launch');
});
