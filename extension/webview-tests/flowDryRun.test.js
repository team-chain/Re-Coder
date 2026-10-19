// 2.0.34 dry run 다이어그램 — 호출 흐름 분석(확장), 단계 계산·배치(웹뷰), 화면, 적용 전 미리 보기.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { analyzeFlow, previewFlow } = require('../out/codemap/flow.js');
const { AnalysisJob } = require('../out/codemap/analysisJob.js');
const { dryRun, layoutDryRun, findingsByNode } = require('../out/webview-test/components/dryRunModel.js');
const { FlowDryRun } = require('../out/webview-test/components/FlowDryRun.js');

function project(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-flow-'));
  for (const [rel, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true });
    fs.writeFileSync(path.join(dir, rel), text);
  }
  return dir;
}
const edge = (g, from, to) => g.edges.find(e => e.from === from && e.to === to);

const EXPRESS = {
  'package.json': JSON.stringify({ name: 'shop', main: 'server.js' }),
  'server.js': [
    "const express = require('express');",
    "const cartRoutes = require('./routes/cart');",
    "const { requireAuth } = require('./middleware');",
    'const app = express();',
    "app.use('/api/cart', requireAuth, cartRoutes);",
    "app.get('/health', (req, res) => res.json({ ok: true }));",
    'app.listen(3000);',
  ].join('\n'),
  'middleware.js': [
    'function requireAuth(req, res, next) {',
    '  if (!req.headers.authorization) { return res.status(401).end(); }',
    '  req.user = verify(req.headers.authorization, process.env.JWT_SECRET);',
    '  next();',
    '}',
    'function verify(t, s) { return t; }',
    'module.exports = { requireAuth };',
  ].join('\n'),
  'routes/cart.js': [
    "const router = require('express').Router();",
    "const cart = require('../services/cart');",
    "router.post('/', async (req, res) => {",
    '  const item = await cart.addItem(req.user.id, req.body);',
    '  res.json(item);',
    '});',
    "router.get('/', listCart);",
    'async function listCart(req, res) {',
    '  try {',
    '    res.json(await cart.list(req.user.id));',
    '  } catch (e) {',
    '    logError(e);',
    '  }',
    '}',
    'function logError(e) { console.error(e); }',
    'module.exports = router;',
  ].join('\n'),
  'services/cart.js': [
    "const db = require('../db');",
    'async function addItem(userId, body) {',
    '  const price = await lookupPrice(body.sku);',
    '  if (price > 100) {',
    '    await notify(userId);',
    '  }',
    "  for (const x of body.extras || []) { await audit(x); }",
    "  return db.query('INSERT INTO cart_items (user_id, sku) VALUES ($1, $2)', [userId, body.sku]);",
    '}',
    "async function lookupPrice(sku) { const r = await fetch('https://prices.example.com/' + sku); return r.json(); }",
    'async function notify(userId) { return addItem(userId, {}); }',
    'function audit(x) { return x; }',
    "async function list(userId) { return db.query('SELECT * FROM cart_items WHERE user_id=$1', [userId]); }",
    'module.exports = { addItem, list };',
  ].join('\n'),
  'db.js': "module.exports = { query() {} };",
  'public/app.js': [
    "document.getElementById('add').addEventListener('click', async () => {",
    "  await addToCart('sku-1');",
    '});',
    "async function addToCart(sku) { return fetch('/api/cart', { method: 'POST' }); }",
  ].join('\n'),
  'tests/cart.test.js': "test('x', () => addItem());",
};

test('Express: 붙인 접두사·경로 미들웨어·핸들러·서비스 호출을 잇는다', () => {
  const root = project(EXPRESS);
  try {
    const g = analyzeFlow(root);
    const labels = g.entries.map(e => e.label);
    assert.ok(labels.includes('POST /api/cart'), labels.join(' | '));
    assert.ok(labels.includes('GET /api/cart'));
    assert.ok(labels.includes('GET /health'));
    assert.ok(labels.some(l => l.startsWith('#add · click')), labels.join(' | '));
    const post = g.entries.find(e => e.label === 'POST /api/cart');
    const out = g.edges.filter(e => e.from === post.id).sort((a, b) => a.order - b.order).map(e => e.to);
    assert.deepEqual(out, ['middleware.js#requireAuth', 'routes/cart.js#POST / 처리'], '미들웨어가 핸들러보다 먼저');
    assert.ok(edge(g, 'routes/cart.js#POST / 처리', 'services/cart.js#addItem'));
    assert.equal(edge(g, 'services/cart.js#addItem', 'services/cart.js#notify').branch, 'if');
    assert.equal(edge(g, 'services/cart.js#addItem', 'services/cart.js#audit').branch, 'loop');
    assert.equal(edge(g, 'routes/cart.js#listCart', 'routes/cart.js#logError').branch, 'catch');
    assert.equal(edge(g, 'routes/cart.js#listCart', 'services/cart.js#list').branch, undefined);
    const add = g.nodes.find(n => n.id === 'services/cart.js#addItem');
    assert.ok(add.effects.some(f => f.kind === 'db' && /INSERT/.test(f.detail)));
    assert.ok(g.nodes.find(n => n.id === 'services/cart.js#lookupPrice').effects.some(f => f.kind === 'http' && /prices\.example\.com/.test(f.detail)));
    assert.ok(g.nodes.find(n => n.id === 'middleware.js#requireAuth').effects.some(f => f.kind === 'secret' && /JWT_SECRET/.test(f.detail)));
    assert.ok(!g.nodes.some(n => n.file.startsWith('tests/')), '테스트 파일은 흐름에 넣지 않는다');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('FastAPI: 라우터 접두사·include_router·Depends·객체 메서드(추정)·__main__', () => {
  const root = project({
    'app/__init__.py': '', 'app/routers/__init__.py': '', 'app/services/__init__.py': '',
    'app/main.py': [
      'from fastapi import FastAPI',
      'from app.routers import orders',
      'app = FastAPI()',
      'app.include_router(orders.router, prefix="/api")',
      '',
      'def start():',
      '    pass',
      '',
      'if __name__ == "__main__":',
      '    start()',
    ].join('\n'),
    'app/deps.py': ['import os', '', 'def current_user(token: str = ""):', '    return os.environ["JWT_SECRET"]'].join('\n'),
    'app/routers/orders.py': [
      'from fastapi import APIRouter, Depends',
      'from app.deps import current_user',
      'from app.services.order_service import OrderService',
      'from ..services import payments',
      '',
      'router = APIRouter(prefix="/orders")',
      'service = OrderService()',
      '',
      '@router.post("/", status_code=201)',
      'async def create_order(body: dict,',
      '                       user=Depends(current_user)):',
      '    if body.get("pay"):',
      '        payments.charge(user)',
      '    for item in body["items"]:',
      '        service.add(item)',
      '    return service.save(user)',
      '',
      '@router.route("/<int:oid>", methods=["GET", "DELETE"])',
      'def order(oid):',
      '    try:',
      '        return service.find(oid)',
      '    except KeyError:',
      '        return missing(oid)',
      '',
      'def missing(oid):',
      '    return None',
    ].join('\n'),
    'app/services/order_service.py': [
      'class OrderService:',
      '    def add(self, item):',
      '        self._check(item)',
      '',
      '    def _check(self, item):',
      '        pass',
      '',
      '    def save(self, user):',
      '        session.add(user)',
      '        session.commit()',
      '',
      '    def find(self, oid):',
      '        return cursor.execute("SELECT * FROM orders WHERE id = %s", (oid,))',
    ].join('\n'),
    'app/services/payments.py': [
      'import requests, boto3',
      'def charge(user):',
      '    requests.post("https://api.pay.example.com/charge", json={})',
      '    boto3.client("sqs").send_message(QueueUrl="q", MessageBody="x")',
    ].join('\n'),
  });
  try {
    const g = analyzeFlow(root);
    const create = g.entries.find(e => e.label === 'POST /api/orders');
    assert.ok(create, g.entries.map(e => e.label).join(' | '));
    assert.ok(g.entries.some(e => e.label === 'GET|DELETE /api/orders/<int:oid>'), g.entries.map(e => e.label).join(' | '));
    const first = g.edges.filter(e => e.from === create.id).sort((a, b) => a.order - b.order).map(e => e.to);
    assert.deepEqual(first, ['app/deps.py#current_user', 'app/routers/orders.py#create_order'], 'Depends 는 핸들러보다 먼저');
    assert.equal(edge(g, 'app/routers/orders.py#create_order', 'app/services/payments.py#charge').branch, 'if');
    const add = edge(g, 'app/routers/orders.py#create_order', 'app/services/order_service.py#OrderService.add');
    assert.equal(add.certainty, 'inferred');
    assert.equal(add.branch, 'loop');
    assert.equal(edge(g, 'app/services/order_service.py#OrderService.add', 'app/services/order_service.py#OrderService._check').certainty, 'direct');
    assert.equal(edge(g, 'app/routers/orders.py#order', 'app/routers/orders.py#missing').branch, 'catch');
    const main = g.entries.find(e => e.kind === 'main');
    assert.equal(edge(g, main.id, 'app/main.py#start').branch, undefined, '__main__ 블록 자체는 갈림이 아니다');
    const pay = g.nodes.find(n => n.id === 'app/services/payments.py#charge');
    assert.ok(pay.effects.some(f => f.kind === 'http') && pay.effects.some(f => f.kind === 'aws'));
    assert.ok(g.nodes.find(n => n.id === 'app/services/order_service.py#OrderService.find').effects.some(f => f.kind === 'db'));
    assert.ok(g.nodes.find(n => n.id === 'app/deps.py#current_user').effects.some(f => f.kind === 'secret'));
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('React·Next.js: 화면 동작(onClick)·app 라우트 파일·TS 타입 주석', () => {
  const root = project({
    'src/api/client.ts': "export async function saveItem(item: { name: string }): Promise<Response> { return fetch('/items', { method: 'POST' }); }\nexport const removeItem = async (id: string) => { await fetch(`/items/${id}`); };",
    'src/components/Editor.tsx': [
      "import React, { useState } from 'react';",
      "import { saveItem, removeItem } from '../api/client';",
      'export default function Editor({ id }: { id: string }) {',
      "  const [name, setName] = useState('');",
      '  const handleSave = async (): Promise<void> => {',
      '    if (!name) { return; }',
      '    await saveItem({ name });',
      '  };',
      '  return (<div><button onClick={handleSave}>저장</button><button onClick={() => removeItem(id)}>삭제</button></div>);',
      '}',
    ].join('\n'),
    'app/api/items/[id]/route.ts': "export async function DELETE(req: Request) { await purge('1'); return Response.json({}); }\nasync function purge(id: string) { await db.query('DELETE FROM items WHERE id = $1', [id]); }",
  });
  try {
    const g = analyzeFlow(root);
    const labels = g.entries.map(e => e.label);
    assert.ok(labels.includes('Editor · click → handleSave'), labels.join(' | '));
    assert.ok(labels.some(l => /^Editor · click \(\d+행\)$/.test(l)), labels.join(' | '));
    assert.ok(labels.includes('DELETE /api/items/:id'), labels.join(' | '));
    assert.ok(edge(g, 'src/components/Editor.tsx#handleSave', 'src/api/client.ts#saveItem'));
    assert.ok(!edge(g, 'src/components/Editor.tsx#Editor', 'src/api/client.ts#removeItem'), '화면이 그려질 때가 아니라 눌렀을 때 실행된다');
    assert.ok(g.nodes.find(n => n.id === 'app/api/items/[id]/route.ts#purge').effects.some(f => f.kind === 'db'));
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('깨진 코드·압축된 코드·빈 프로젝트에서도 멈추거나 던지지 않는다', () => {
  const root = project({
    'broken.js': 'function a( { if (x { b(); \n const = => ;;; }}}}} class {',
    'min.js': 'function a(){b()}function b(){return c()}function c(){return fetch("https://x.example.com")}module.exports={a};'.repeat(200),
    'weird.py': 'def f(:\n  g(\n\nclass :\n    def h(self) pass\n',
    'big.js': `${'function f0() { return 1; }\n'}${Array.from({ length: 3000 }, (_, i) => `function f${i + 1}() { return f${i}(); }`).join('\n')}`,
  });
  try {
    const started = Date.now();
    const g = analyzeFlow(root);
    assert.ok(Date.now() - started < 20000, '큰 파일도 제한 시간 안에');
    assert.ok(g.nodes.length > 3000);
    const r = dryRun(g, 'big.js#f3000');
    assert.equal(r.truncated, 'depth');
    assert.ok(r.steps.length <= 300);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
  const empty = project({ 'README.md': '# 문서만' });
  try {
    const g = analyzeFlow(empty);
    assert.equal(g.nodes.length, 0);
    assert.ok(g.notes.length > 0);
    const html = renderToStaticMarkup(React.createElement(FlowDryRun, { graph: g }));
    assert.match(html, /data-flow-empty/);
  } finally { fs.rmSync(empty, { recursive: true, force: true }); }
});

test('dry run: 호출 순서대로 깊이 우선 · 이미 지난 곳·재귀는 다시 들어가지 않음 · 요약', () => {
  const root = project(EXPRESS);
  try {
    const g = analyzeFlow(root);
    const post = g.entries.find(e => e.label === 'POST /api/cart');
    const r = dryRun(g, post.id);
    const names = r.steps.map(s => s.node.split('#')[1]);
    assert.deepEqual(names.slice(0, 6), ['route:POST /api/cart@3', 'requireAuth', 'verify', 'POST / 처리', 'addItem', 'lookupPrice']);
    const notifyStep = r.steps.find(s => s.node === 'services/cart.js#notify');
    assert.equal(notifyStep.branch, 'if');
    const back = r.steps.find(s => s.from === 'services/cart.js#notify');
    assert.equal(back.revisit, 'cycle', 'notify → addItem 은 재귀');
    assert.equal(r.summary.effects.db, 1);
    assert.equal(r.summary.effects.http, 1);
    assert.equal(r.summary.effects.secret, 1);
    assert.ok(r.summary.branches >= 2);
    const limited = dryRun(g, post.id, { maxSteps: 3 });
    assert.equal(limited.truncated, 'steps');
    assert.equal(limited.steps.length, 3);
    assert.equal(dryRun(g, 'no-such-node').steps.length, 0);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('dry run 배치: 좁은 사이드바부터 넓은 화면까지 노드가 겹치지 않는다', () => {
  const nodes = Array.from({ length: 30 }, (_, i) => ({ id: `n${i}`, file: 'a.js', name: `n${i}`, line: i + 1, endLine: i + 1, kind: 'function', effects: [], hash: '' }));
  const edges = nodes.slice(1).map((n, i) => ({ from: i < 10 ? 'n0' : `n${i - 9}`, to: n.id, line: 1, order: i, certainty: 'direct' }));
  const g = { kind: 'flow', source: 'source', root: '/', files_scanned: 1, nodes, edges, entries: [], truncated: false, notes: [] };
  const r = dryRun(g, 'n0');
  for (const width of [320, 540, 960]) {
    const layout = layoutDryRun(r, width);
    const pos = [...layout.pos.values()];
    assert.equal(pos.length, new Set(r.steps.map(s => s.node)).size);
    for (const p of pos) assert.ok(p.x >= 75 && p.x <= width - 75, `width ${width}: x ${p.x}`);
    for (let i = 0; i < pos.length; i++) for (let j = i + 1; j < pos.length; j++) {
      assert.ok(Math.abs(pos[i].x - pos[j].x) >= 160 || Math.abs(pos[i].y - pos[j].y) >= 80, `겹침 ${JSON.stringify([pos[i], pos[j]])}`);
    }
  }
});

test('보안 발견은 그 줄을 품은 가장 안쪽 함수에 붙는다(대상 폴더 기준 경로 포함)', () => {
  const g = { nodes: [
    { id: 'web/a.js#outer', file: 'web/a.js', line: 1, endLine: 20, kind: 'function' },
    { id: 'web/a.js#inner', file: 'web/a.js', line: 5, endLine: 8, kind: 'function' },
    { id: 'web/a.js#route:GET /', file: 'web/a.js', line: 6, endLine: 6, kind: 'route' },
  ] };
  const map = findingsByNode(g, [{ file: 'a.js', line: 6, message: 'x' }, { file: 'web/a.js', line: 15, message: 'y' }, { file: 'zz.js', line: 1, message: 'z' }], 'web');
  assert.deepEqual(map.get('web/a.js#inner').map(f => f.message), ['x']);
  assert.deepEqual(map.get('web/a.js#outer').map(f => f.message), ['y']);
  assert.equal(map.size, 2);
});

test('적용 전 미리 보기: 바뀐·새 함수, 지나가는 진입점, 없어지는 함수 — 디스크는 그대로', () => {
  const root = project(EXPRESS);
  try {
    const before = fs.readFileSync(path.join(root, 'services/cart.js'), 'utf8');
    const changedCart = before
      .replace("const price = await lookupPrice(body.sku);", "const price = await lookupPrice(body.sku);\n  await applyCoupon(body);")
      .replace('function audit(x) { return x; }', "async function applyCoupon(body) { return db.query('UPDATE coupons SET used = 1'); }");
    const p = previewFlow(root, new Map([['services/cart.js', changedCart], ['public/app.js', null]]));
    assert.equal(p.changed['services/cart.js#addItem'], 'modified');
    assert.equal(p.changed['services/cart.js#applyCoupon'], 'added');
    assert.equal(p.changed['services/cart.js#list'], undefined, '안 바뀐 함수는 표시하지 않는다');
    assert.ok(p.removed.some(r => r.name === 'audit'));
    assert.ok(p.removed.some(r => r.name === 'addToCart'), '지운 파일의 함수');
    const labels = p.affected.map(id => p.graph.entries.find(e => e.id === id).label);
    assert.ok(labels.includes('POST /api/cart'));
    assert.ok(!labels.includes('GET /api/cart'), 'list 만 지나가는 경로는 영향 없음');
    assert.equal(fs.readFileSync(path.join(root, 'services/cart.js'), 'utf8'), before, '미리 보기는 파일을 쓰지 않는다');
    assert.ok(fs.existsSync(path.join(root, 'public/app.js')));
    // 새 파일만 추가하는 변경
    const added = previewFlow(root, new Map([['routes/coupon.js', "const r = require('express').Router();\nr.post('/apply', (req, res) => res.json({}));\nmodule.exports = r;"]]));
    assert.ok(Object.values(added.changed).includes('added'));
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('작업 스레드: flow·preview 모드가 확장 호스트를 막지 않고 결과를 돌려준다', async () => {
  const root = project(EXPRESS);
  try {
    const g = await new AnalysisJob(30000).run(root, '', { mode: 'flow' });
    assert.equal(g.kind, 'flow');
    assert.ok(g.entries.length >= 3);
    const p = await new AnalysisJob(30000).run(root, '', { mode: 'preview', overrides: [['db.js', 'module.exports = { query() { return 1; } };']] });
    assert.ok(p.graph && p.changed && Array.isArray(p.affected));
    await assert.rejects(new AnalysisJob(30000).run(path.join(root, 'nope'), '', { mode: 'flow' }));
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('화면: 시작점 목록·진행 표시·노드 상태·바뀐 함수 표시·보안 발견', () => {
  const root = project(EXPRESS);
  try {
    const before = fs.readFileSync(path.join(root, 'services/cart.js'), 'utf8');
    const p = previewFlow(root, new Map([['services/cart.js', before.replace('return db.query(', 'await lookupPrice("x");\n  return db.query(')]]));
    const html = renderToStaticMarkup(React.createElement(FlowDryRun, {
      graph: p.graph, changed: p.changed, removed: p.removed, affected: p.affected,
      findings: [{ file: 'services/cart.js', line: 3, message: '인증 없이 쓰기', label: '인증' }],
    }));
    assert.match(html, /<optgroup label="이번 변경이 지나가는 곳">/);
    assert.match(html, /data-flow-progress[^>]*>1 \/ \d+</);
    assert.match(html, /data-state="current"/);
    assert.match(html, /data-state="future"/);
    assert.match(html, /이번 변경/);
    assert.match(html, /정적 분석입니다/);
    const plain = renderToStaticMarkup(React.createElement(FlowDryRun, { graph: analyzeFlow(root) }));
    assert.match(plain, /<optgroup label="API 경로">/);
    assert.match(plain, /<optgroup label="화면 동작">/);
    assert.doesNotMatch(plain, /이번 변경/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('긴 공백·긴 이름이 섞인 파일에서도 분석이 몇 초 안에 끝난다(정규식 되돌아가기 폭발 회귀)', () => {
  const W = ' '.repeat(60000), I = 'a'.repeat(3000);
  const { analyzeProject, analyzeFile } = require('../out/codemap/analyzer.js');
  const cases = {
    'a.js': `export default${W}${I}${W}(`, 'b.js': `import {a} from${W}x\n${W}import I${W}(`, 'c.js': `class X${W}x${W}class X${W}`,
    'd.js': `foo<${W}${I}${W}(`, 'e.py': `from . import${W}${I}${W}(`, 'f.py': `import${W}${I}${W}(`, 'g.py': `from x import (${W}${I}${W}`,
  };
  for (const [name, text] of Object.entries(cases)) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-slow-'));
    try {
      fs.writeFileSync(path.join(dir, name), text);
      const started = Date.now();
      analyzeFlow(dir); analyzeProject(dir); analyzeFile(path.join(dir, name));
      assert.ok(Date.now() - started < 3000, `${name}: ${Date.now() - started}ms`);
    } finally { fs.rmSync(dir, { recursive: true, force: true }); }
  }
});
