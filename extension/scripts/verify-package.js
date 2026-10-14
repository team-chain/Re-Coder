const fs = require('node:fs');
const path = require('node:path');

function validatePackageFiles(files, { requireCore = false, target = `${process.platform}-${process.arch}` } = {}) {
  const names = new Set(files.map(file => file.replace(/\\/g, '/')));
  const required = [
    'package.json', 'out/extension.js', 'out/sidebar/SidebarProvider.js',
    'out/sidebar/ReCoderPanel.js', 'out/sidebar/WorkbenchSidebarProvider.js',
    'out/sidebar/workbenchHost.js', 'out/sidebar/workbenchHtml.js',
    'out/sidebar/canvasHost.js', 'out/codemap/analysisJob.js',
    'out/codemap/analysisWorker.js', 'out/codemap/analyzer.js',
    'out/webview/webview.js', 'media/icon.png', 'media/recoder-icon.svg',
    'node_modules/ws/index.js', 'node_modules/ws/lib/websocket.js',
  ];
  const problems = required.filter(file => !names.has(file)).map(file => `Missing runtime file: ${file}`);
  for (const file of names) {
    if (/^(?:harness|scripts|src|webview-src|webview-tests|bin-dist|dist)\//.test(file)
        || /^out\/(?:test|webview-test|ui|collectors)\//.test(file)
        || /(?:^|\/)(?:\.env(?:\.[^/]*)?|[^/]*\.(?:map|ts|tsx|log|bundle|vsix|pyc))$/.test(file)
        || /^(?:media\/(?:sidebar|workbench)\.js|out\/sidebar\/WorkbenchPanel\.js)$/.test(file)) {
      problems.push(`Development or obsolete file in VSIX: ${file}`);
    }
  }
  if (requireCore) {
    const expected = target.startsWith('win32-') ? 'bin/recoder-core.exe' : 'bin/recoder-core';
    const binaries = [...names].filter(file => file.startsWith('bin/'));
    if (binaries.length !== 1 || binaries[0] !== expected) {
      problems.push(`Release ${target} requires only ${expected}; build and stage the matching Core binary first.`);
    }
  }
  if (problems.length) throw new Error(problems.join('\n'));
}

async function verifyPackage(options = {}) {
  const root = path.resolve(__dirname, '..');
  const files = await require('@vscode/vsce').listFiles({ cwd: root });
  validatePackageFiles(files, options);
  // Guard against a stale .js from a source removed since the previous build.
  for (const file of files) {
    const name = file.replace(/\\/g, '/');
    if (name.startsWith('out/') && name.endsWith('.js') && !name.startsWith('out/webview/')) {
      const source = path.join(root, 'src', name.slice(4).replace(/\.js$/, '.ts'));
      if (!fs.existsSync(source)) throw new Error(`Stale host output: ${name}`);
    }
  }
  console.log(`Package contents verified: ${files.length} runtime/distribution files${options.requireCore ? ' (bundled Core required)' : ''}.`);
  return files;
}

module.exports = { validatePackageFiles, verifyPackage };
if (require.main === module) {
  const targetIndex = process.argv.indexOf('--target');
  verifyPackage({ requireCore: process.argv.includes('--require-core'), target: targetIndex >= 0 ? process.argv[targetIndex + 1] : undefined })
    .catch(error => { console.error(error.message); process.exitCode = 1; });
}
