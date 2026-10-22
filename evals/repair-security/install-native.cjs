// --omit=optional excludes cloud SDKs and the required platform SQLite binding.
// Restore only the current platform binding, at the version already in our lockfile.
const { spawnSync } = require('node:child_process');
const { join } = require('node:path');
const { currentTarget } = require('@neon-rs/load');
const name = `@libsql/${currentTarget()}`;
const lock = require('./package-lock.json');
const entry = lock.packages[`node_modules/${name}`];
if (!entry?.version || !entry.integrity || !/^@libsql\/[a-z0-9-]+$/.test(name)) {
  throw new Error(`No locked SQLite binding for ${name}`);
}
// npm_execpath comes from `npm run setup:native`; invoke through the same Node.
if (!process.env.npm_execpath) throw new Error('Run with npm run setup:native');
const result = spawnSync(process.execPath, [
  process.env.npm_execpath, 'install', '--no-save', '--package-lock=false',
  '--ignore-scripts', '--omit=optional', '--no-audit', '--no-fund',
  `${name}@${entry.version}`,
], { cwd: __dirname, stdio: 'inherit' });
if (result.error) console.error(result.error.message);
if (result.status !== 0) process.exit(result.status ?? 1);
const installed = require(join(__dirname, 'node_modules', name, 'package.json'));
if (installed.version !== entry.version) throw new Error('SQLite binding version changed');
