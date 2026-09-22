const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');

function clientWith(fetch, { empty = false } = {}) {
  const file = path.join(__dirname, '../out/core/ApiClient.js');
  const mod = { exports: {} };
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), {
    module: mod, exports: mod.exports, require: createRequire(file),
    fetch, AbortController, TextDecoder, setTimeout, clearTimeout,
  }, { filename: file });
  let token = empty ? '' : 'old-token';
  let port = empty ? 0 : 17894;
  let refreshes = 0;
  const manager = {
    getSessionToken: () => token, getPort: () => port,
    refreshToken: async () => { refreshes++; token = 'new-token'; port = 17895; return true; },
  };
  return { api: new mod.exports.ApiClient(manager), refreshes: () => refreshes };
}

const denied = () => new Response('{"detail":"Invalid session token."}', { status: 401 });
const stream = text => new Response(text, { headers: { 'Content-Type': 'text/event-stream' } });
const input = { project: 'test', files: [{ path: 'index.html', content: 'ok', encoding: 'utf-8' }] };

test('first protected polling request reloads empty runtime before sending', async () => {
  const { api, refreshes } = clientWith(async (url, init) => {
    assert.equal(url, 'http://127.0.0.1:17895/api/status');
    assert.equal(init.headers['X-Session-Token'], 'new-token');
    return new Response('{"status":"ok"}');
  }, { empty: true });
  assert.equal((await api.request('GET', '/api/status')).success, true);
  assert.equal(refreshes(), 1);
});

test('401 reloads port and token once and preserves a protected request body', async () => {
  const calls = [];
  const { api, refreshes } = clientWith(async (url, init) => {
    calls.push({ url, init });
    return calls.length === 1 ? denied() : new Response('{"ok":true}');
  });
  const result = await api.request('POST', '/api/aws/connect', { region: 'ap-northeast-2' });
  assert.equal(result.success, true);
  assert.equal(calls.length, 2);
  assert.equal(refreshes(), 1);
  assert.equal(calls[0].init.headers['X-Session-Token'], 'old-token');
  assert.equal(calls[1].init.headers['X-Session-Token'], 'new-token');
  assert.match(calls[1].url, /:17895\//);
  assert.equal(calls[1].init.method, 'POST');
  assert.equal(calls[1].init.body, calls[0].init.body);
});

test('persistent 401 stops after one refresh instead of retrying indefinitely', async () => {
  let requests = 0;
  const { api, refreshes } = clientWith(async () => { requests++; return denied(); });
  const result = await api.request('GET', '/workbench/events');
  assert.equal(result.status, 401);
  assert.equal(requests, 2);
  assert.equal(refreshes(), 1);
});

test('S3 authentication rejection retries before streaming using fresh runtime', async () => {
  const calls = [];
  const events = [];
  const { api, refreshes } = clientWith(async (url, init) => {
    calls.push({ url, init });
    return calls.length === 1 ? denied() : stream('data: {"step":"done","result":{"status":"deployed"}}\n\n');
  });
  assert.equal((await api.deployS3Stream(input, event => events.push(event))).status, 'deployed');
  assert.equal(calls.length, 2);
  assert.equal(refreshes(), 1);
  assert.equal(calls[1].init.headers['X-Session-Token'], 'new-token');
  assert.match(calls[1].url, /:17895\/api\/deploy\/s3\/stream$/);
  assert.equal(calls[0].init.body, calls[1].init.body);
  assert.equal(events.length, 1);
});

test('S3 persistent authentication failure also stops after one retry', async () => {
  let requests = 0;
  const { api, refreshes } = clientWith(async () => { requests++; return denied(); });
  await assert.rejects(api.deployS3Stream(input, () => {}), /Invalid session token/);
  assert.equal(requests, 2);
  assert.equal(refreshes(), 1);
});

for (const [name, reply] of [
  ['policy denial', () => new Response('{"detail":{"error":"policy_denied","message":"denied"}}', { status: 403 })],
  ['server error', () => new Response('error', { status: 500 })],
  ['network failure', () => { throw new Error('fetch failed'); }],
  ['opened stream that closes early', () => stream('data: {"step":"upload"}\n\n')],
  ['stream error event', () => stream('data: {"step":"error","message":"upload failed"}\n\n')],
]) {
  test(`S3 ${name} never replays the deployment`, async () => {
    let requests = 0;
    const { api, refreshes } = clientWith(async () => { requests++; return reply(); });
    await assert.rejects(api.deployS3Stream(input, () => {}));
    assert.equal(requests, 1);
    assert.equal(refreshes(), 0);
  });
}
