// 4.0.1 명세 전체 dry run — NestJS 경로 · 의존성 주입을 따라가고, 게이트웨이 API 는 받는 서비스까지 이어서 판정한다.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { analyzeFlow } = require('../out/codemap/flow.js');
const { runSpec, normPath, effectText } = require('../out/webview-test/components/specDryRun.js');

function project() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'spec-dry-'));
  const w = (f, s) => { fs.mkdirSync(path.dirname(path.join(root, f)), { recursive: true }); fs.writeFileSync(path.join(root, f), s); };
  w('services/gateway/src/proxy.controller.ts', `import { Controller, Post, Get } from '@nestjs/common';
import { ProxyService } from './proxy.service';
@Controller('api')
export class ProxyController {
  constructor(private readonly proxy: ProxyService) {}
  @Post('orders')
  create() { return this.proxy.forward('orders'); }
  @Get('ping')
  ping() { const m = /^Bearer (.+)$/.exec('x'); return m; }
}`);
  w('services/gateway/src/proxy.service.ts', `export class ProxyService {
  async forward(s: string) { return fetch('http://' + s); }
}`);
  w('services/orders/src/orders.controller.ts', `import { Controller, Post, Delete } from '@nestjs/common';
import { OrdersService } from './orders.service';
@Controller('orders')
export class OrdersController {
  constructor(private readonly orders: OrdersService) {}
  @Post()
  create() { return this.orders.create(); }
  @Delete(':id')
  remove() { return { ok: true }; }
}`);
  w('services/orders/src/orders.service.ts', `export class OrdersService {
  constructor(private readonly prisma: any) {}
  async create() { return this.prisma.order.create({ data: {} }); }
}`);
  return root;
}
const api = (service, method, p, file, line) => ({ service, method, path: p, auth: true, admin: false, public: false, implemented: true, file, line, tests: [] });

test('NestJS: @Controller 접두사 + @Post · @Delete(:id) 가 진입점, 생성자 주입 객체의 메서드까지 따라간다', () => {
  const g = analyzeFlow(project());
  const labels = g.entries.filter(e => e.kind === 'route').map(e => e.label).sort();
  assert.deepEqual(labels, ['DELETE /orders/:id', 'GET /api/ping', 'POST /api/orders', 'POST /orders']);
  const db = g.nodes.find(n => n.name === 'create' && n.cls === 'OrdersService');
  assert.ok(db && db.effects.some(e => e.kind === 'db'), 'Prisma 호출을 DB 로 보지 못했다');
  // 정규식 .exec() 는 명령 실행이 아니다
  assert.ok(!g.nodes.some(n => n.effects.some(e => e.kind === 'exec')), '정규식 exec 를 명령 실행으로 봤다');
});

test('명세 dry run: 게이트웨이 → 받는 서비스로 이어 DB 까지, 저장 없는 쓰기 · 코드 없는 경로를 잡는다', () => {
  const g = analyzeFlow(project());
  const spec = {
    available: true,
    services: [{ name: 'gateway', kind: 'api', depends_on: ['orders'], databases: [] }, { name: 'orders', kind: 'api', depends_on: [], databases: ['orders-db'] }],
    endpoints: [
      api('gateway', 'POST', '/api/orders', 'services/gateway/src/proxy.controller.ts', 6),
      api('orders', 'POST', '/orders', 'services/orders/src/orders.controller.ts', 6),
      api('orders', 'DELETE', '/orders/{orderId}', 'services/orders/src/orders.controller.ts', 8),
      api('orders', 'GET', '/orders/missing', 'services/orders/src/orders.controller.ts', 40),
    ],
  };
  const r = runSpec(spec, g);
  const gw = r.runs['gateway POST /api/orders'];
  assert.deepEqual(gw.hops.map(h => h.service), ['gateway', 'orders']);
  assert.equal(gw.verdict, 'ok');
  assert.ok(gw.effects.db >= 1 && gw.effects.http >= 1);
  assert.match(effectText(gw.effects), /DB 1 · 서비스 호출 1/);
  assert.equal(r.runs['orders DELETE /orders/{orderId}'].verdict, 'warn', '저장 없는 삭제를 통과시켰다');
  assert.equal(r.runs['orders GET /orders/missing'].verdict, 'fail');
  assert.deepEqual(r.counts, { ok: 2, warn: 1, fail: 1 });
  assert.equal(normPath('/a/{x}/b/<y>/:z/'), '/a/:/b/:/:');
});
