// Isolate eval state from the user's Promptfoo account and disable hosted features.
const { spawnSync } = require('node:child_process');
const { mkdirSync } = require('node:fs');
const { join } = require('node:path');
const root = __dirname;
mkdirSync(join(root, 'results'), { recursive: true });
const result = spawnSync(process.execPath, [
  join(root, 'node_modules/promptfoo/dist/src/main.js'),
  'eval', '--config', join(root, 'promptfooconfig.yaml'),
  '--output', join(root, 'results/security.json'),
  '--no-cache', '--no-write', '--no-progress-bar', '--no-table',
  '--max-concurrency', '1',
], {
  cwd: root,
  stdio: 'inherit',
  env: {
    ...process.env,
    PROMPTFOO_CONFIG_DIR: join(root, '.promptfoo'),
    PROMPTFOO_DISABLE_TELEMETRY: '1',
    PROMPTFOO_DISABLE_UPDATE: '1',
    PROMPTFOO_DISABLE_REMOTE_GENERATION: 'true',
    PROMPTFOO_DISABLE_TEMPLATE_ENV_VARS: 'true',
    RECODER_COST_LEDGER_FILE: '',
  },
});
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
