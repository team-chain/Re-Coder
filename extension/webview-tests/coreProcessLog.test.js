const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { CoreProcessLog } = require('../out/core/CoreProcessLog.js');

function logFile(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-log-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return path.join(dir, 'core.log');
}

test('stdout/stderr preserve split UTF-8 and complete secret values before writing a line', t => {
  const file = logFile(t);
  const secret = 'test-secret-fragmented-value';
  const log = new CoreProcessLog(file, { secrets: [secret] });
  log.setPid(1234);
  const bytes = Buffer.from(`한글 출력 ${secret}\n`);
  for (const byte of bytes) { log.write('stdout', Buffer.from([byte])); }
  log.write('stderr', Buffer.from('Traceback: deliberate test error\nlast line without newline'));
  log.finish();
  log.finish();
  const saved = fs.readFileSync(file, 'utf8');
  assert.match(saved, /\d{4}-\d\d-\d\dT.*\[host=\d+ pid=1234\] \[stdout\] 한글 출력 \[REDACTED\]/);
  assert.match(saved, /\[stderr\] Traceback: deliberate test error/);
  assert.equal(saved.match(/last line without newline/g).length, 1);
  assert.ok(!saved.includes(secret));
  assert.ok(!saved.includes('\uFFFD'));
});

test('common credentials are masked even when their values were not supplied to the logger', t => {
  const file = logFile(t);
  const log = new CoreProcessLog(file);
  const lines = [
    'Authorization: Bearer fake-bearer-value',
    'Authorization: Basic fake-basic-value',
    '{"session_token": "fake-session-value", "ok": true}',
    "{'aws_secret_access_key': 'fake-aws-secret-value'}",
    'X-Session-Token=fake-header-value',
    'https://example.invalid?token=fake-query-value&ok=true',
    'access key AKIA1234567890ABCDEF',
    'password="a fake password with spaces"',
    'password="escaped \\"private\\" value"',
  ];
  log.write('stderr', Buffer.from(lines.join('\n') + '\n'));
  const saved = fs.readFileSync(file, 'utf8');
  for (const secret of ['fake-bearer-value', 'fake-basic-value', 'fake-session-value',
    'fake-aws-secret-value', 'fake-header-value', 'fake-query-value', 'AKIA1234567890ABCDEF',
    'a fake password with spaces', 'private']) {
    assert.ok(!saved.includes(secret), secret);
  }
  assert.match(saved, /"ok": true/);
  assert.equal(saved.match(/\[REDACTED\]/g).length, lines.length);
});

test('a new Core appends to previous logs and can learn a new session token', t => {
  const file = logFile(t);
  const first = new CoreProcessLog(file);
  first.setPid(101);
  first.event('spawned');
  first.write('stderr', Buffer.from('old error\n'));
  first.event('exited code=1 signal=null');
  first.finish();
  const second = new CoreProcessLog(file);
  second.setPid(202);
  second.addSecrets(['new-session-secret-value']);
  second.event('spawned');
  second.write('stdout', Buffer.from('opaque new-session-secret-value\n'));
  const saved = fs.readFileSync(file, 'utf8');
  assert.match(saved, /pid=101.*spawned/);
  assert.match(saved, /old error/);
  assert.match(saved, /exited code=1 signal=null/);
  assert.match(saved, /pid=202.*spawned/);
  assert.ok(!saved.includes('new-session-secret-value'));
});

test('rotation bounds total files and bytes while retaining the latest entries', t => {
  const file = logFile(t);
  const log = new CoreProcessLog(file, { maxBytes: 256, backups: 2 });
  for (let i = 0; i < 25; i++) { log.event(`event-${i} 한글 로그`); }
  const files = fs.readdirSync(path.dirname(file)).sort();
  assert.deepEqual(files, ['core.log', 'core.log.1', 'core.log.2']);
  for (const name of files) { assert.ok(fs.statSync(path.join(path.dirname(file), name)).size <= 256); }
  assert.match(fs.readFileSync(file, 'utf8'), /event-24/);
  assert.ok(!files.map(name => fs.readFileSync(path.join(path.dirname(file), name), 'utf8')).join('').includes('event-0 '));
  // An older writer must append to the active file after another writer rotates it.
  const second = new CoreProcessLog(file, { maxBytes: 256, backups: 2 });
  second.event('second writer');
  log.event('first writer returns');
  assert.match(fs.readFileSync(file, 'utf8'), /first writer returns/);
});

test('log and rotated backups are readable only by their owner on POSIX', t => {
  if (process.platform === 'win32') { t.skip('POSIX file modes'); return; }
  const file = logFile(t);
  const log = new CoreProcessLog(file, { maxBytes: 256, backups: 2 });
  for (let i = 0; i < 8; i++) { log.event(`event-${i}`); }
  for (const name of fs.readdirSync(path.dirname(file))) {
    assert.equal(fs.statSync(path.join(path.dirname(file), name)).mode & 0o777, 0o600);
  }
});

test('unwritable log target does not interrupt the Core and reports the issue once', t => {
  const file = logFile(t);
  fs.mkdirSync(file);
  let errors = 0;
  const log = new CoreProcessLog(file, { onError() { errors++; throw new Error('notification also failed'); } });
  assert.doesNotThrow(() => {
    log.event('spawned');
    log.write('stdout', Buffer.from('still running\n'));
    log.write('stderr', Buffer.from('tail'));
    log.finish();
  });
  assert.equal(errors, 1);
});

test('oversized partial lines are omitted without leaking secret fragments or hiding later lines', t => {
  const file = logFile(t);
  const log = new CoreProcessLog(file);
  log.write('stdout', Buffer.from('secret-fragment-start' + 'x'.repeat(17000)));
  log.write('stdout', Buffer.from('secret-fragment-end\nnext useful line\n'));
  log.write('stderr', Buffer.from('y'.repeat(17000) + '\ntraceback follows\n'));
  log.finish();
  const saved = fs.readFileSync(file, 'utf8');
  assert.equal(saved.match(/log line omitted/g).length, 2);
  assert.ok(!saved.includes('secret-fragment'));
  assert.match(saved, /next useful line/);
  assert.match(saved, /traceback follows/);
  assert.ok(saved.length < 1000);
});
