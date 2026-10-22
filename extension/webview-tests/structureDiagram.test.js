// 2.0.42 구조 다이어그램 — 역할 추정, 레인 배치(겹침 없음·방향), 아키텍처 맵(폴더 단위·펼치기), dry run 을 구조 위에서.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const L = require('../out/webview-test/components/structureLayout.js');
const { projectDiagram, fileDiagram, FOLDER_VIEW_THRESHOLD } = require('../out/webview-test/components/CodeMap.js');
const { StructureDiagram } = require('../out/webview-test/components/StructureDiagram.js');
const { FlowDryRun } = require('../out/webview-test/components/FlowDryRun.js');
const { analyzeFlow } = require('../out/codemap/flow.js');
const { analyzeProject } = require('../out/codemap/analyzer.js');

test('역할 추정: 폴더·파일 이름·확장자', () => {
  const cases = {
    'server.js': 'entry', 'backend/src/server.js': 'entry', 'routes/cart.js': 'entry', 'src/controllers/user.ts': 'entry',
    'pages/api/orders.ts': 'entry', 'app/api/routes.py': 'entry', 'main.py': 'entry',
    'services/cart.js': 'logic', 'src/lib/price.ts': 'logic', 'middleware.js': 'logic', 'core/llm/router.py': 'logic',
    'models/user.js': 'data', 'app/models.py': 'data', 'db.js': 'data', 'prisma/schema.prisma': 'data', 'migrations/001.sql': 'data',
    'frontend/src/App.jsx': 'ui', 'frontend/src/api/client.js': 'ui', 'client/models/cart.ts': 'ui', 'src/components/Cart.tsx': 'ui', 'public/index.html': 'ui', 'styles/app.css': 'ui',
    'tests/test_cart.py': 'support', 'src/cart.test.ts': 'support', 'vite.config.ts': 'support', 'package.json': 'support',
    'README.md': 'other',
  };
  for (const [file, role] of Object.entries(cases)) assert.equal(L.roleOf(file), role, file);
  assert.equal(L.roleOf('services/cart.js', { kind: 'route' }), 'entry', '경로 노드는 언제나 입구');
});

const box = (n) => ({ x: n.x, y: n.y, r: n.x + n.w, b: n.y + n.h });
const overlaps = (a, b) => a.x < b.r && b.x < a.r && a.y < b.b && b.y < a.b;

test('레인 배치: 역할 순서대로 왼쪽→오른쪽, 겹침 없음, 노드는 자기 상자 안, 레인 안 호출은 왼쪽→오른쪽', () => {
  const nodes = [
    { id: 'ui', role: 'ui', group: 'web/app.jsx', order: 0 },
    { id: 'route', role: 'entry', group: 'routes/cart.js', order: 1 },
    { id: 'h1', role: 'entry', group: 'routes/cart.js', order: 2 },
    { id: 's1', role: 'logic', group: 'services/cart.js', order: 3 },
    { id: 's2', role: 'logic', group: 'services/cart.js', order: 4 },
    { id: 's3', role: 'logic', group: 'services/price.js', order: 5 },
    { id: 'd1', role: 'data', group: 'db.js', order: 6 },
    { id: 'x', role: 'external', group: 'external', order: 9 },
  ];
  const edges = [['ui', 'route'], ['route', 'h1'], ['h1', 's1'], ['s1', 's2'], ['s2', 's3'], ['s3', 'd1'], ['d1', 'x'], ['s3', 's1']].map(([from, to]) => ({ from, to }));
  const lay = L.layoutLanes(nodes, edges);
  assert.deepEqual(lay.lanes.map((l) => l.role), ['ui', 'entry', 'logic', 'data', 'external']);
  for (let i = 1; i < lay.lanes.length; i++) assert.ok(lay.lanes[i].x >= lay.lanes[i - 1].x + lay.lanes[i - 1].w, '레인이 겹치지 않는다');
  const placed = [...lay.nodes.values()];
  assert.equal(placed.length, nodes.length);
  for (let i = 0; i < placed.length; i++) for (let j = i + 1; j < placed.length; j++) assert.ok(!overlaps(box(placed[i]), box(placed[j])), `${placed[i].id} ${placed[j].id}`);
  for (const p of placed) {
    const g = lay.groups.find((g) => g.key === p.group && g.role === p.role);
    assert.ok(p.x >= g.x && p.x + p.w <= g.x + g.w && p.y >= g.y && p.y + p.h <= g.y + g.h, `${p.id} 는 자기 상자 안`);
  }
  for (let i = 0; i < lay.groups.length; i++) for (let j = i + 1; j < lay.groups.length; j++) {
    const a = lay.groups[i], b = lay.groups[j];
    assert.ok(!overlaps({ x: a.x, y: a.y, r: a.x + a.w, b: a.y + a.h }, { x: b.x, y: b.y, r: b.x + b.w, b: b.y + b.h }), `${a.key} ${b.key}`);
  }
  const P = (id) => lay.nodes.get(id);
  assert.ok(P('route').col < P('h1').col, '같은 레인 안 호출은 오른쪽 열로');
  assert.ok(P('s1').x < P('s2').x && P('s2').x < P('s3').x, '처리 레인 안 사슬이 가로로 펼쳐진다(일자 세로 줄 아님)');
  assert.ok(new Set(placed.map((p) => Math.round(p.x))).size >= 6, '노드가 여러 열에 퍼진다');
});

test('레인 배치: 큰 그래프·깊은 사슬·순환에서도 빠르고 멈추지 않는다', () => {
  const n = 3000;
  const nodes = Array.from({ length: n }, (_, i) => ({ id: `n${i}`, role: ['entry', 'logic', 'data'][i % 3], group: `g${i % 40}`, order: i }));
  const edges = [];
  for (let i = 0; i < n - 1; i++) edges.push({ from: `n${i}`, to: `n${i + 1}` }, { from: `n${i + 1}`, to: `n${(i * 7) % n}` });
  const started = Date.now();
  const lay = L.layoutLanes(nodes, edges, { maxCols: 5 });
  assert.ok(Date.now() - started < 2000, `${Date.now() - started}ms`);
  assert.equal(lay.nodes.size, n);
  assert.ok(Math.max(...[...lay.nodes.values()].map((p) => p.col)) <= 4, '열 수 상한');
  const chain = Array.from({ length: 20000 }, (_, i) => ({ id: `c${i}`, role: 'logic', group: 'g', order: i }));
  const chainEdges = chain.slice(1).map((c, i) => ({ from: `c${i}`, to: c.id }));
  assert.equal(L.layoutLanes(chain, chainEdges).nodes.size, 20000, '깊은 사슬에서도 콜 스택이 넘치지 않는다');
});

test('선 모양: 오른쪽은 곡선, 같은 열은 옆으로, 되돌아가는 선은 표시', () => {
  const a = { x: 0, y: 0, w: 100, h: 40 }, right = { x: 200, y: 80, w: 100, h: 40 }, below = { x: 0, y: 100, w: 100, h: 40 }, left = { x: -300, y: 0, w: 100, h: 40 };
  assert.equal(L.edgeRoute(a, right).backward, false);
  assert.match(L.edgeRoute(a, right).d, /^M100,20 C/);
  assert.equal(L.edgeRoute(a, below).backward, false);
  assert.equal(L.edgeRoute(a, left).backward, true);
  assert.equal(L.fit('아주아주아주긴함수이름입니다', 60).endsWith('…'), true);
  assert.equal(L.fit('short', 200), 'short');
});

function project(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-structure-'));
  for (const [rel, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true });
    fs.writeFileSync(path.join(dir, rel), text);
  }
  return dir;
}

const SHOP = {
  'package.json': JSON.stringify({ name: 'shop', main: 'server.js' }),
  'server.js': "const express = require('express');\nconst cart = require('./routes/cart');\nconst app = express();\napp.use('/api/cart', cart);\napp.listen(3000);\n",
  'routes/cart.js': "const router = require('express').Router();\nconst svc = require('../services/cart');\nrouter.post('/', async (req, res) => res.json(await svc.add(req.body)));\nmodule.exports = router;\n",
  'services/cart.js': "const repo = require('../models/cart');\nconst price = require('./price');\nasync function add(b) { const p = await price.lookup(b.id); return repo.insert(b, p); }\nmodule.exports = { add };\n",
  'services/price.js': "async function lookup(id) { const r = await fetch('https://api.example.com/p/' + id); return r.json(); }\nmodule.exports = { lookup };\n",
  'models/cart.js': "const db = require('../db');\nfunction insert(b, p) { return db.query('INSERT INTO cart VALUES ($1,$2)', [b.id, p]); }\nmodule.exports = { insert };\n",
  'db.js': "const { Pool } = require('pg');\nconst pool = new Pool({ connectionString: process.env.DATABASE_URL });\nmodule.exports = { query: (q, v) => pool.query(q, v) };\n",
};

test('dry run 은 구조 위에서: 입구 → 처리 → 데이터 → 외부(DB·외부 API) 레인, 파일 상자, 일자 아님', () => {
  const root = project(SHOP);
  try {
    const graph = analyzeFlow(root);
    const html = renderToStaticMarkup(React.createElement(FlowDryRun, { graph }));
    for (const lane of ['entry', 'logic', 'data', 'external']) assert.match(html, new RegExp(`data-lane="${lane}"`), lane);
    assert.match(html, /data-flow-sink="db"/);
    assert.match(html, /data-flow-sink="http"/);
    assert.match(html, /data-group="services\/cart.js"/);
    assert.match(html, /data-group="models\/cart.js"/);
    const xs = new Set([...html.matchAll(/data-flow-node="[^"]+" data-state="[a-z]+" transform="translate\(([\d.]+),/g)].map((m) => Math.round(Number(m[1]))));
    assert.ok(xs.size >= 4, `노드가 여러 열에 걸친다(일자 아님): ${[...xs]}`);
    assert.match(html, /data-state="current"/);
    assert.match(html, /data-progress|data-flow-progress/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('아키텍처 맵: 역할 레인·폴더 상자·import 선, 파일이 많으면 폴더 단위 → 폴더 펼치기', () => {
  const root = project(SHOP);
  try {
    const p = analyzeProject(root);
    const d = projectDiagram(p, 'files');
    const roles = Object.fromEntries(d.nodes.map((n) => [n.id, n.role]));
    assert.equal(roles['server.js'], 'entry');
    assert.equal(roles['routes/cart.js'], 'entry');
    assert.equal(roles['services/cart.js'], 'logic');
    assert.equal(roles['models/cart.js'], 'data');
    assert.ok(d.edges.some((e) => e.from === 'services/cart.js' && e.to === 'models/cart.js'));
    const html = renderToStaticMarkup(React.createElement(StructureDiagram, { nodes: d.nodes, edges: d.edges }));
    assert.match(html, /data-lane="entry"/);
    assert.match(html, /data-group="services"/);
    assert.match(html, /data-map-node="services\/price.js"/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }

  // 큰 프로젝트: 폴더 단위 → 펼치기
  const nodes = [], edges = [];
  for (let i = 0; i < FOLDER_VIEW_THRESHOLD + 20; i++) {
    const folder = ['routes', 'services', 'models'][i % 3];
    nodes.push({ id: `${folder}/f${i}.js`, name: `f${i}.js`, module: '', layer: 'service', in_degree: 0, out_degree: 0, flags: [] });
  }
  for (let i = 0; i + 1 < nodes.length; i++) edges.push({ from: nodes[i].id, to: nodes[i + 1].id });
  const folders = projectDiagram({ nodes, edges }, 'folders');
  assert.deepEqual(folders.nodes.map((n) => n.id).sort(), ['dir:models', 'dir:routes', 'dir:services']);
  assert.ok(folders.edges.every((e) => e.weight >= 1));
  const focus = projectDiagram({ nodes, edges }, 'folders', 'services');
  assert.ok(focus.nodes.filter((n) => !n.id.startsWith('dir:')).every((n) => n.id.startsWith('services/')), '그 폴더의 파일만');
  assert.ok(focus.nodes.some((n) => n.id === 'dir:routes') && focus.nodes.some((n) => n.id === 'dir:models'), '이웃 폴더는 폴더 노드로');
  assert.ok(focus.edges.some((e) => e.from === 'dir:routes' || e.to === 'dir:models'));

  const fd = fileDiagram({ nodes: [{ id: 'a', name: 'a', cls: 'Cart', in_degree: 0, out_degree: 1, flags: [] }, { id: 'b', name: 'b', cls: null, in_degree: 1, out_degree: 0, flags: ['overloaded'] }], edges: [{ from: 'a', to: 'b' }] });
  assert.deepEqual(fd.nodes.map((n) => n.group), ['Cart', '모듈 함수']);
});

test('역할 추정 보강: Next.js API·Java web 폴더·React App·routes 컴포넌트·Django 파일', () => {
  const cases = {
    'frontend/pages/api/orders.ts': 'entry', 'web/app/api/users/route.ts': 'entry', 'src/app/route.js': 'entry',
    'src/main/java/com/shop/web/UserController.java': 'entry', 'src/main/java/com/shop/client/PayClient.java': 'logic',
    'src/main/kotlin/shop/api/OrderResource.kt': 'entry', 'handlers/cart_handler.go': 'entry',
    'src/App.jsx': 'ui', 'src/App.js': 'ui', 'src/App.tsx': 'ui', 'app.js': 'entry',
    'src/routes/Home.tsx': 'ui', 'src/routes/cart.ts': 'entry', 'src/api/Orders.jsx': 'ui',
    'shop/views.py': 'entry', 'shop/views/cart.py': 'entry', 'shop/urls.py': 'entry', 'shop/viewsets.py': 'entry',
    'mysite/settings.py': 'support', 'shop/tests.py': 'support', 'conftest.py': 'support',
    'templates/home.html': 'ui', 'core/llm/router.py': 'logic',
  };
  for (const [file, role] of Object.entries(cases)) assert.equal(L.roleOf(file), role, file);
});

test('레인 폭 상한: 한 폴더에 파일이 아주 많으면 옆으로 끝없이 늘이지 않고 줄을 늘린다', () => {
  const nodes = Array.from({ length: 120 }, (_, i) => ({ id: `f${i}`, role: 'logic', group: 'services', order: i }));
  const lay = L.layoutLanes(nodes, [], { maxRows: 8, maxCols: 5 });
  const cols = new Set([...lay.nodes.values()].map((p) => p.col));
  assert.ok(cols.size <= 7, `열 ${cols.size}개`);
  assert.equal(lay.nodes.size, 120);
  const placed = [...lay.nodes.values()];
  for (let i = 0; i < placed.length; i++) for (let j = i + 1; j < placed.length; j++) assert.ok(!overlaps(box(placed[i]), box(placed[j])));
  const g = lay.groups[0];
  for (const p of placed) assert.ok(p.y + p.h <= g.y + g.h, '늘린 줄도 상자 안');
  // 적을 때는 예전처럼 8줄에서 옆으로
  const few = L.layoutLanes(nodes.slice(0, 12), [], { maxRows: 8 });
  assert.equal(new Set([...few.nodes.values()].map((p) => p.col)).size, 2);
});

test('글자 자르기는 이모지·확장 문자를 반으로 자르지 않는다', () => {
  const out = L.fit('🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀', 40);
  assert.ok(out.endsWith('…'));
  assert.ok(!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/.test(out), '짝 없는 상위 대리 문자가 없다');
  assert.equal(L.fit('짧음', 200), '짧음');
});

test('다이어그램: 누를 수 있는 노드는 키보드로 닿고, 화살표 id 는 다이어그램마다 다르다', () => {
  const nodes = [{ id: 'a', role: 'entry', group: 'g', order: 0, title: 'routes/cart.js', sub: '3 파일' }, { id: 'b', role: 'logic', group: 'h', order: 1, title: 'services' }];
  const edges = [{ from: 'a', to: 'b' }];
  const two = renderToStaticMarkup(React.createElement('div', null,
    React.createElement(StructureDiagram, { nodes, edges, onNodeClick: () => {} }),
    React.createElement(StructureDiagram, { nodes, edges })));
  const ids = [...two.matchAll(/<marker[^>]* id="([^"]+)"/g)].map((m) => m[1]);
  assert.equal(new Set(ids).size, ids.length, '화살표 정의 id 가 겹치지 않는다');
  for (const m of two.matchAll(/marker-end="url\(#([^)]+)\)"/g)) assert.ok(ids.includes(m[1]), m[1]);
  const [interactive, plain] = two.split('data-structure-diagram').slice(1);
  assert.equal((interactive.match(/tabindex="0"/g) || []).length, 2);
  assert.ok(interactive.includes('role="button"') && interactive.includes('aria-label="routes/cart.js — 3 파일"'));
  assert.ok(interactive.includes('role="group"'), '누를 수 있는 요소를 담은 svg 는 img 가 아니다');
  assert.ok(!plain.includes('tabindex="0"') && plain.includes('role="img"'), '누를 수 없으면 Tab 순서에 넣지 않는다');
});

test('다이어그램 키 처리: Enter·Space 는 클릭, Shift+Enter 는 두 번 누르기 — 원본 결선', () => {
  const src = fs.readFileSync(path.join(__dirname, '../webview-src/components/StructureDiagram.tsx'), 'utf8');
  for (const s of ['e.key === "Enter" && e.shiftKey && onNodeDoubleClick', 'e.key === " "', 'React.useId()', 'scaleRef.current', '}, [focusId, layout]);', 'JSON.stringify(options ?? {})']) assert.ok(src.includes(s), s);
});
