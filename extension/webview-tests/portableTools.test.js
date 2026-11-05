const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { extractToolZip, systemTool, toolEnvironment } = require('../out/runtime/portableTools');
const { zipFiles } = require('../out/security/managedBundle');

async function fixture(fn) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'recoder-tools-test-'));
  try { await fn(root); }
  finally { assert.equal(path.dirname(root), os.tmpdir()); await fs.rm(root, { recursive: true, force: true }); }
}
test('tool detection uses absolute PATH entries, preserves system tools and requires npm with Windows Node', () => fixture(async root => {
  await fs.writeFile(path.join(root, 'git.exe'), 'fixture');
  await fs.writeFile(path.join(root, 'node.exe'), 'fixture');
  const env = { Path: `.;${root}`, TEST: 'unchanged' };
  assert.equal(systemTool('git', env, 'win32'), path.join(root, 'git.exe'));
  assert.equal(systemTool('node', env, 'win32'), undefined);
  await fs.writeFile(path.join(root, 'npm.cmd'), 'fixture');
  assert.equal(systemTool('node', env, 'win32'), path.join(root, 'node.exe'));
  const copy = toolEnvironment(env); copy.TEST = 'changed';
  assert.equal(env.TEST, 'unchanged'); assert.equal(env.Path, `.;${root}`);
}));
test('portable extraction preserves binary files and never overwrites existing files', () => fixture(async root => {
  const archive = path.join(root, 'tool.zip'), dest = path.join(root, 'extract');
  await fs.mkdir(dest);
  const data = Buffer.from([0, 255, 1, 128]);
  await fs.writeFile(archive, zipFiles([{ name: 'source/bin/tool.exe', data }]));
  await extractToolZip(archive, dest, 'source/');
  assert.deepEqual(await fs.readFile(path.join(dest, 'bin/tool.exe')), data);
  await assert.rejects(() => extractToolZip(archive, dest, 'source/'), /EEXIST/);
  await assert.rejects(() => extractToolZip(archive, dest, 'runtime/'), /예상하지/);
}));
test('portable extraction rejects Windows aliases, alternate streams, symlinks and oversized entries', () => fixture(async root => {
  const archive = path.join(root, 'tool.zip');
  for (const name of ['source/CON.txt', 'source/tool.exe:ads', 'source/x.', 'source/x ', 'source/x?y']) {
    await fs.writeFile(archive, zipFiles([{ name, data: Buffer.from('bad') }]));
    await assert.rejects(() => extractToolZip(archive, path.join(root, 'bad'), 'source/'));
  }
  const zip = zipFiles([{ name: 'source/link', data: Buffer.from('outside') }]);
  const central = zip.indexOf(Buffer.from([0x50, 0x4b, 0x01, 0x02]));
  zip.writeUInt32LE((0xa1ff << 16) >>> 0, central + 38);
  await fs.writeFile(archive, zip);
  await assert.rejects(() => extractToolZip(archive, path.join(root, 'bad'), 'source/'), /경로/);
  const large = zipFiles([{ name: 'source/large', data: Buffer.from('bad') }]);
  const c = large.indexOf(Buffer.from([0x50, 0x4b, 0x01, 0x02]));
  large.writeUInt32LE(151 * 1024 * 1024, c + 24);
  await fs.writeFile(archive, large);
  await assert.rejects(() => extractToolZip(archive, path.join(root, 'bad'), 'source/'));
}));
