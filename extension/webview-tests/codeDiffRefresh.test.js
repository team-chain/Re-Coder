const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');

const resolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...args) {
  return request === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : resolve.call(this, request, ...args);
};
const vscode = require('vscode');
const { SidebarProvider } = require('../out/sidebar/SidebarProvider.js');
Module._resolveFilename = resolve;

// Model VS Code's document cache: requesting an already-open URI does not
// re-read its provider. Each opened proposal must retain its own contents.
async function withEditor(run) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-diff-'));
  const providers = new Map();
  const cache = new Map();
  const closed = [];
  const opened = [];
  const original = {
    parse: vscode.Uri.parse,
    register: vscode.workspace.registerTextDocumentContentProvider,
    close: vscode.workspace.onDidCloseTextDocument,
    execute: vscode.commands.executeCommand,
    folders: vscode.workspace.workspaceFolders,
  };
  vscode.Uri.parse = (value) => {
    const uri = original.parse(value);
    uri.path = decodeURIComponent(new URL(value).pathname);
    return uri;
  };
  vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(dir) }];
  vscode.workspace.registerTextDocumentContentProvider = (scheme, provider) => {
    providers.set(scheme, provider);
    provider.onDidChange?.((uri) => cache.set(uri.toString(), provider.provideTextDocumentContent(uri)));
    return { dispose() {} };
  };
  vscode.workspace.onDidCloseTextDocument = (listener) => { closed.push(listener); return { dispose() {} }; };
  const read = (uri) => {
    if (uri.scheme === 'file') { return fs.readFileSync(uri.fsPath, 'utf8'); }
    if (!cache.has(uri.toString())) {
      cache.set(uri.toString(), providers.get(uri.scheme).provideTextDocumentContent(uri));
    }
    return cache.get(uri.toString());
  };
  vscode.commands.executeCommand = async (command, left, right) => {
    assert.equal(command, 'vscode.diff');
    opened.push({ left, right, before: read(left), after: read(right) });
  };
  const sidebar = new SidebarProvider(vscode.Uri.file(dir), {}, {}, {});
  const diff = (file, content) => sidebar.handleMessage({ type: 'code.diff', payload: { file, content } });
  try {
    await run({ dir, diff, opened, providers, cache, close: (uri) => {
      cache.delete(uri.toString());
      for (const listener of closed) { listener({ uri }); }
    } });
  } finally {
    vscode.Uri.parse = original.parse;
    vscode.workspace.registerTextDocumentContentProvider = original.register;
    vscode.workspace.onDidCloseTextDocument = original.close;
    vscode.commands.executeCommand = original.execute;
    vscode.workspace.workspaceFolders = original.folders;
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test('same-file second proposal displays new content while the first stays unchanged', async () => {
  await withEditor(async ({ dir, diff, opened, providers }) => {
    fs.writeFileSync(path.join(dir, 'app.js'), 'saved original');
    await diff('app.js', 'first: delete todos');
    await diff('app.js', 'second: keep todos, change version');
    assert.equal(opened[1].after, 'second: keep todos, change version');
    assert.notEqual(opened[0].right.toString(), opened[1].right.toString());
    assert.equal(providers.get('recoder-codegen').provideTextDocumentContent(opened[0].right), 'first: delete todos');
    assert.equal(fs.readFileSync(path.join(dir, 'app.js'), 'utf8'), 'saved original');
    await diff('app.js', 'first: delete todos');
    assert.equal(opened[2].after, 'first: delete todos');
  });
});

test('identical relative filenames in different workspaces have separate previews', async () => {
  await withEditor(async ({ dir, diff, opened }) => {
    for (const name of ['a', 'b']) {
      fs.mkdirSync(path.join(dir, name));
      fs.writeFileSync(path.join(dir, name, 'app.js'), `saved ${name}`);
      vscode.workspace.workspaceFolders = [{ uri: vscode.Uri.file(path.join(dir, name)) }];
      await diff('app.js', 'same proposed text');
    }
    assert.notEqual(opened[0].right.toString(), opened[1].right.toString());
    assert.equal(opened[0].before, 'saved a');
    assert.equal(opened[1].before, 'saved b');
  });
});

test('spaces, Korean, hash and question marks in filenames remain intact', async () => {
  await withEditor(async ({ dir, diff, opened }) => {
    const file = '한글 test #1?.js';
    fs.writeFileSync(path.join(dir, file), 'original');
    await diff(file, 'new contents');
    assert.equal(opened[0].after, 'new contents');
    assert.ok(opened[0].right.path.endsWith(file));
  });
});

test('new-file diff has empty left side and closed snapshots can be reopened', async () => {
  await withEditor(async ({ diff, opened, providers, close }) => {
    await diff('new.js', 'first contents');
    await diff('new.js', 'second contents');
    assert.equal(opened[0].before, '');
    close(opened[0].right);
    assert.equal(providers.get('recoder-codegen').provideTextDocumentContent(opened[0].right), '');
    assert.equal(providers.get('recoder-codegen').provideTextDocumentContent(opened[1].right), 'second contents');
    await diff('new.js', 'first contents');
    assert.equal(opened[2].after, 'first contents');
  });
});

test('concurrent diff requests for one file retain their respective proposals', async () => {
  await withEditor(async ({ dir, diff, opened, providers }) => {
    fs.writeFileSync(path.join(dir, 'app.js'), 'original');
    await Promise.all([diff('app.js', 'first'), diff('app.js', 'second')]);
    assert.deepEqual(new Set(opened.map((item) => item.after)), new Set(['first', 'second']));
    for (const item of opened) {
      assert.equal(providers.get('recoder-codegen').provideTextDocumentContent(item.right), item.after);
    }
  });
});
