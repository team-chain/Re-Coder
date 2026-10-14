const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { longHttpFetch, FETCH_HEADERS_LIMIT_MS } = require('../out/core/longHttp.js');

function server(handler) {
  return new Promise(resolve => {
    const s = http.createServer(handler);
    s.listen(0, '127.0.0.1', () => resolve(s));
  });
}

test('long requests return status and JSON body like fetch', async t => {
  const s = await server((req, res) => {
    let body = '';
    req.on('data', c => { body += c; });
    req.on('end', () => setTimeout(() => {
      res.statusCode = 422;
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ echo: JSON.parse(body), token: req.headers['x-session-token'] }));
    }, 50));
  });
  t.after(() => s.close());
  const res = await longHttpFetch(`http://127.0.0.1:${s.address().port}/api/deploy/plan?x=1`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-Token': 'tok' },
    body: JSON.stringify({ 한글: '값' }), signal: new AbortController().signal,
  });
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.deepEqual(await res.json(), { echo: { 한글: '값' }, token: 'tok' });
});

test('abort signal cancels a long request', async t => {
  const s = await server(() => { /* never answers */ });
  t.after(() => { s.closeAllConnections?.(); s.close(); });
  const controller = new AbortController();
  const pending = longHttpFetch(`http://127.0.0.1:${s.address().port}/slow`, { method: 'GET', headers: {}, signal: controller.signal });
  setTimeout(() => controller.abort(), 30);
  await assert.rejects(pending, err => err.name === 'AbortError' || /abort|socket hang up/i.test(err.message));
});

test('ApiClient routes requests longer than the undici header limit through node:http', () => {
  const src = fs.readFileSync(path.join(__dirname, '../src/core/ApiClient.ts'), 'utf8');
  assert.match(src, /timeoutMs > FETCH_HEADERS_LIMIT_MS \? await longHttpFetch\(url, init\) : await fetch\(url, init\)/);
  assert.ok(FETCH_HEADERS_LIMIT_MS < 300_000);
  assert.match(src, /'\/api\/deploy\/plan'[\s\S]{0,300}false, 600000/);
});
