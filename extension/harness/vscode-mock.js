// 하네스용 vscode 목(mock) — 통합 검증에서 실제 SidebarProvider/ApiClient 를
// VSCode 밖에서 구동하기 위한 최소 표면. 테스트가 쓰는 API 만 실제처럼 동작하고
// 나머지는 no-op. 검증 전용이며 제품 번들에는 포함되지 않는다.
'use strict';
const nodePath = require('path');
const nodeFs = require('fs');

class Uri {
    constructor(fsPath, scheme = 'file') { this.fsPath = fsPath; this.path = fsPath.replace(/\\/g, '/'); this.scheme = scheme; }
    static file(p) { return new Uri(nodePath.resolve(p)); }
    static parse(s) { const u = new Uri(s, (s.split(':')[0] || 'https')); u._raw = s; return u; }
    static joinPath(base, ...parts) { return Uri.file(nodePath.join(base.fsPath, ...parts)); }
    with() { return this; }
    toString() { return this._raw || ('file://' + this.path); }
}
class EventEmitter {
    constructor() { this._ls = []; this.event = (l) => { this._ls.push(l); return { dispose() {} }; }; }
    fire(v) { for (const l of this._ls) l(v); }
    dispose() {}
}
const _watcher = () => ({ onDidChange: () => ({ dispose() {} }), onDidCreate: () => ({ dispose() {} }), onDidDelete: () => ({ dispose() {} }), dispose() {} });
const workspace = {
    workspaceFolders: undefined,
    updateWorkspaceFolders(start, del, ...items) {
        const cur = workspace.workspaceFolders ? [...workspace.workspaceFolders] : [];
        cur.splice(start, del || 0, ...items.map((i, k) => ({ uri: i.uri, name: nodePath.basename(i.uri.fsPath), index: start + k })));
        workspace.workspaceFolders = cur;
        if (workspace.__onUpdateWorkspaceFolders) { workspace.__onUpdateWorkspaceFolders(); }
        return true;
    },
    asRelativePath(u) {
        const p = typeof u === 'string' ? u : u.fsPath;
        const root = workspace.workspaceFolders && workspace.workspaceFolders[0];
        if (!root) { return p; }
        const rel = nodePath.relative(root.uri.fsPath, p);
        return rel.startsWith('..') ? p : rel;
    },
    getConfiguration: () => ({ get: () => undefined, update: async () => {} }),
    createFileSystemWatcher: _watcher,
    onDidChangeWorkspaceFolders: () => ({ dispose() {} }),
    onDidChangeConfiguration: () => ({ dispose() {} }),
    onDidSaveTextDocument: () => ({ dispose() {} }),
    openTextDocument: async (arg) => ({ getText: () => '', uri: typeof arg === 'string' ? Uri.file(arg) : arg, fileName: '', isUntitled: false }),
    registerTextDocumentContentProvider: () => ({ dispose() {} }),
    textDocuments: [],
    fs: {
        async createDirectory(uri) { nodeFs.mkdirSync(uri.fsPath, { recursive: true }); },
        async writeFile(uri, bytes) { nodeFs.mkdirSync(nodePath.dirname(uri.fsPath), { recursive: true }); nodeFs.writeFileSync(uri.fsPath, Buffer.from(bytes)); },
        async readFile(uri) { return nodeFs.readFileSync(uri.fsPath); },
        async stat(uri) { const s = nodeFs.statSync(uri.fsPath); return { type: s.isDirectory() ? 2 : 1, size: s.size, ctime: s.ctimeMs, mtime: s.mtimeMs }; },
        async readDirectory(uri) { return nodeFs.readdirSync(uri.fsPath, { withFileTypes: true }).map((e) => [e.name, e.isDirectory() ? 2 : 1]); },
        async delete() {},
    },
};
const window = {
    activeTextEditor: undefined, visibleTextEditors: [],
    showErrorMessage: async () => undefined, showWarningMessage: async () => undefined, showInformationMessage: async () => undefined,
    showOpenDialog: async () => undefined, showInputBox: async () => undefined, showQuickPick: async () => undefined,
    createOutputChannel: () => ({ appendLine() {}, append() {}, show() {}, clear() {}, dispose() {} }),
    showTextDocument: async () => ({}),
    withProgress: async (_o, task) => task({ report() {} }, { isCancellationRequested: false }),
    createWebviewPanel: () => { throw new Error('harness: createWebviewPanel unused'); },
    registerWebviewViewProvider: () => ({ dispose() {} }),
    onDidChangeActiveTextEditor: () => ({ dispose() {} }),
    activeColorTheme: { kind: 2 }, tabGroups: { all: [], close: async () => true },
};
module.exports = {
    Uri, EventEmitter, workspace, window,
    commands: { executeCommand: async () => undefined, registerCommand: () => ({ dispose() {} }) },
    env: { clipboard: { writeText: async () => {} }, openExternal: async () => true, appName: 'harness' },
    Disposable: class { static from() { return { dispose() {} }; } dispose() {} },
    ThemeIcon: class { constructor(id) { this.id = id; } },
    ViewColumn: { One: 1, Two: 2, Active: -1, Beside: -2 },
    ProgressLocation: { Notification: 15, Window: 10 },
    ExtensionMode: { Production: 1, Development: 2, Test: 3 },
    ConfigurationTarget: { Global: 1, Workspace: 2 },
    FileType: { File: 1, Directory: 2 },
    languages: { registerCodeLensProvider: () => ({ dispose() {} }), createDiagnosticCollection: () => ({ set() {}, clear() {}, dispose() {} }) },
    Range: class { constructor(a, b, c, d) { this.start = { line: a, character: b }; this.end = { line: c, character: d }; } },
    Position: class { constructor(l, c) { this.line = l; this.character = c; } },
    version: '0.0.0-harness',
};
