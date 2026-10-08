const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');

const resolve = Module._resolveFilename;
Module._resolveFilename = function (name, ...args) {
  return name === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : resolve.call(this, name, ...args);
};
const vscode = require('vscode');
const { SidebarProvider } = require('../out/sidebar/SidebarProvider.js');
const { initActiveProject, useExternalProject, selectActiveProject } = require('../out/activeProject.js');
Module._resolveFilename = resolve;

function project(t, api) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-repair-project-'));
  const open = path.join(base, 'open'), external = path.join(base, 'external');
  fs.mkdirSync(open); fs.mkdirSync(external);
  const previous = vscode.workspace.workspaceFolders;
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(open), name: 'open' }];
  const values = new Map();
  const state = { get: (key, fallback) => values.get(key) ?? fallback, update: async (key, value) => values.set(key, value) };
  const context = { workspaceState: state, globalState: state, subscriptions: [] };
  initActiveProject(context);
  const host = new SidebarProvider(vscode.Uri.file(open), api, {}, {});
  const replies = [];
  const webview = { postMessage: message => { replies.push(message); return Promise.resolve(true); } };
  t.after(() => {
    context.subscriptions.forEach(s => s.dispose());
    values.clear(); vscode.workspace.workspaceFolders = previous;
    const cleanup = { ...context, subscriptions: [] };
    initActiveProject(cleanup); cleanup.subscriptions.forEach(s => s.dispose());
    fs.rmSync(base, { recursive: true, force: true });
  });
  return { open, external, replies, send: (type, payload) => host.handleMessage({ type, payload }, webview) };
}

test('repair uses the selected external project for prepare and approval', async t => {
  const calls = [];
  const p = project(t, {
    prepareRepair: async (workspace, log, stage) => { calls.push({ workspace, log, stage }); return { id: 'r', workspace }; },
    getRepair: async () => ({ workspace: fs.realpathSync(p.external) }),
    approveRepair: async (id, workspace) => { calls.push({ id, workspace }); return { status: 'applied' }; },
  });
  await useExternalProject(p.external);
  await p.send('repair.prepare', { requestId: 'prepare', log: 'ETARGET', stage: 'build', workspace: p.open });
  await p.send('repair.approve', { requestId: 'approve', runId: 'r', approved: true });
  assert.deepEqual(calls, [{ workspace: p.external, log: 'ETARGET', stage: 'build' }, { id: 'r', workspace: p.external }]);
  assert.equal(p.replies.at(-1).payload.result.status, 'applied');
});

test('switching project while preparing discards the old result', async t => {
  const p = project(t, { prepareRepair: async workspace => {
    await selectActiveProject(p.open);
    return { id: 'old', workspace };
  } });
  await useExternalProject(p.external);
  await p.send('repair.prepare', { requestId: 'prepare', log: 'ETARGET' });
  assert.equal(p.replies.at(-1).type, 'repair.error');
  assert.match(p.replies.at(-1).payload.message, /프로젝트가 바뀌/);
  assert.ok(!p.replies.some(r => r.type === 'repair.result'));
});

test('switching project during approval lookup never applies files', async t => {
  let applied = false, lookedUp = false;
  const p = project(t, {
    getRepair: async () => { lookedUp = true; await selectActiveProject(p.open); return { workspace: p.external }; },
    approveRepair: async () => { applied = true; },
  });
  await useExternalProject(p.external);
  await p.send('repair.approve', { requestId: 'approve', runId: 'r', approved: true });
  assert.equal(lookedUp, true);
  assert.equal(applied, false);
  assert.equal(p.replies.at(-1).type, 'repair.error');
  assert.match(p.replies.at(-1).payload.message, /프로젝트가 바뀌/);
});
