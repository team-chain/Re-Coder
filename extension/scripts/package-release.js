'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { verifyPackage } = require('./verify-package');

async function main() {
  const target = process.argv[2];
  if (target !== `${process.platform}-${process.arch}`) throw new Error('Build releases on their native OS and architecture.');
  const root = path.resolve(__dirname, '..');
  const bin = path.join(root, 'bin');
  const binary = target.startsWith('win32-') ? 'recoder-core.exe' : 'recoder-core';
  const source = path.join(root, 'bin-dist', target, binary);
  if (!fs.statSync(source).isFile()) throw new Error('Core binary missing.');
  if (fs.existsSync(bin) && (fs.lstatSync(bin).isSymbolicLink() || fs.realpathSync(bin) !== bin)) {
    throw new Error('Refusing a redirected bin directory.');
  }
  const existed = fs.existsSync(bin);
  const prior = existed ? fs.readdirSync(bin) : [];
  if (prior.some(name => !fs.lstatSync(path.join(bin, name)).isFile())) throw new Error('bin must contain regular files only.');
  const backup = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-bin-backup-'));
  const saved = [];
  let staged = false;
  try {
    fs.mkdirSync(bin, { recursive: true });
    for (const name of prior) {
      fs.copyFileSync(path.join(bin, name), path.join(backup, name));
      saved.push(name);
      fs.unlinkSync(path.join(bin, name));
    }
    fs.copyFileSync(source, path.join(bin, binary));
    staged = true;
    fs.chmodSync(path.join(bin, binary), 0o755);
    const { version } = require('../package.json');
    fs.mkdirSync(path.join(root, 'dist'), { recursive: true });
    await require('@vscode/vsce').createVSIX({ cwd: root, target,
      packagePath: path.join(root, 'dist', `recoder-${version}-${target}.vsix`) });
    await verifyPackage({ requireCore: true, target });
  } finally {
    if (staged && fs.existsSync(path.join(bin, binary))) fs.unlinkSync(path.join(bin, binary));
    for (const name of saved) {
      fs.copyFileSync(path.join(backup, name), path.join(bin, name));
      fs.unlinkSync(path.join(backup, name));
    }
    if (!existed && fs.existsSync(bin) && fs.readdirSync(bin).length === 0) fs.rmdirSync(bin);
    if (fs.readdirSync(backup).length === 0) fs.rmdirSync(backup);
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
