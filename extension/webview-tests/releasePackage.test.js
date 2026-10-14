const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { validatePackageFiles } = require('../scripts/verify-package');
const { pruneHostOutput } = require('../scripts/prune-host-output');

// Missing worker is a runtime error even though the main extension compiles.
test('package validation detects omitted analysis worker and runtime websocket dependency', () => {
  assert.throws(() => validatePackageFiles(['package.json', 'out/extension.js']), error => {
    assert.match(error.message, /Missing runtime file: out\/codemap\/analysisWorker.js/);
    assert.match(error.message, /Missing runtime file: node_modules\/ws\/lib\/websocket.js/);
    return true;
  });
});

test('package validation rejects fixtures, secrets, source maps and obsolete compiled screens', () => {
  for (const file of ['harness/fixture-core.py', '.env', '.env.production', 'out/extension.js.map', 'out/sidebar/WorkbenchPanel.js']) {
    assert.throws(() => validatePackageFiles([file]), error => error.message.includes(`Development or obsolete file in VSIX: ${file}`));
  }
  assert.throws(() => validatePackageFiles(['bin/recoder-core'], { requireCore: true, target: 'win32-x64' }), /requires only bin\/recoder-core.exe/);
});

test('prune removes deleted host output while preserving live runtime, worker and webview output', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-build-clean-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  function write(file, content = '') {
    const target = path.join(root, file);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, content);
  }
  write('tsconfig.json', JSON.stringify({ include: ['src/**/*.ts'] }));
  write('src/extension.ts', 'export {};');
  write('src/codemap/analysisWorker.ts', 'export {};');
  const keep = ['out/extension.js', 'out/extension.js.map', 'out/codemap/analysisWorker.js', 'out/webview/webview.js', 'out/webview-test/App.js'];
  const stale = ['out/ui/sidebarProvider.js', 'out/ui/sidebarProvider.js.map', 'out/sidebar/WorkbenchPanel.js'];
  [...keep, ...stale].forEach(file => write(file));
  assert.equal(pruneHostOutput(root).length, stale.length);
  keep.forEach(file => assert.ok(fs.existsSync(path.join(root, file)), file));
  stale.forEach(file => assert.equal(fs.existsSync(path.join(root, file)), false, file));
});
