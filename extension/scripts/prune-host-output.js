// TypeScript does not remove output for deleted source files. Prune only its
// stale JS/map files; retain the independently built webview and test bundle.
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

function pruneHostOutput(root) {
  root = fs.realpathSync(root);
  const out = path.join(root, 'out');
  if (!fs.existsSync(out)) return [];
  if (fs.lstatSync(out).isSymbolicLink() || fs.realpathSync(out) !== out) {
    throw new Error('Refusing to prune a redirected output directory');
  }
  const config = ts.readConfigFile(path.join(root, 'tsconfig.json'), ts.sys.readFile);
  if (config.error) throw new Error(ts.flattenDiagnosticMessageText(config.error.messageText, '\n'));
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, root);
  if (parsed.errors.length) throw new Error('Cannot resolve TypeScript source files');
  const expected = new Set(parsed.fileNames.flatMap(file => {
    const relative = path.relative(path.join(root, 'src'), file).replace(/\.tsx?$/, '.js');
    return [relative, `${relative}.map`];
  }));
  const removed = [];
  function visit(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const file = path.join(dir, entry.name);
      const relative = path.relative(out, file);
      if (entry.isSymbolicLink()) throw new Error(`Refusing to follow output symlink: ${relative}`);
      if (entry.isDirectory()) {
        if (dir !== out || !['webview', 'webview-test'].includes(entry.name)) visit(file);
      } else if (/\.js(?:\.map)?$/.test(relative) && !expected.has(relative)) {
        // dir entries are resolved under the checked out directory. No shell deletion.
        fs.unlinkSync(file);
        removed.push(relative);
      }
    }
  }
  visit(out);
  return removed;
}

module.exports = { pruneHostOutput };
if (require.main === module) {
  const removed = pruneHostOutput(path.resolve(__dirname, '..'));
  if (removed.length) console.log(`Pruned ${removed.length} obsolete host build files.`);
}
