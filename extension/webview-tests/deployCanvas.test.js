/**
 * 배포 캔버스 1단계 — 렌더러 선택과 잠긴 타겟 계약.
 *
 * 여기서 막는 사고
 *   1. **WebGL 이 없는 환경에서 빈 검은 박스.** 배포 캔버스는 배포로 가는
 *      유일한 길이 될 예정이라, 못 그리면 기능 자체가 막힌다. Remote SSH·
 *      devcontainer·`--disable-gpu` 로 띄운 VSCode 가 실제로 그렇다. 3D 가
 *      안 되면 반드시 2D 로 내려가야 한다.
 *   2. **잠긴 타겟에 배포가 되는 것.** AWS 를 연결하지 않았는데 ECS 로
 *      보내지면 그때부터는 실패가 아니라 혼란이다. 잠긴 노드는 드롭 대상에서
 *      빠지고, 그 노드로 가는 연결선도 그리지 않는다(선이 있으면 "연결돼
 *      있다"로 읽힌다).
 *   3. **충돌.** 2026-09-22 협의로 `DeploymentCenter.tsx` 는 다른 사람이
 *      고치는 중이다. 캔버스가 그 파일을 참조하기 시작하면 분리한 의미가
 *      없어진다 — 소스에서 직접 확인한다.
 *
 * 음성 대조
 *   "잠기면 못 쓴다" 만 검사하면 항상 잠기게 만들어도 통과한다. 그래서
 *   연결된 상태에서는 잠금 문구가 사라지고 드롭이 가능해지는 것도 같이 본다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

const { pickRenderMode } = require('../out/webview-test/components/canvas/capability.js');
const {
  buildCanvasModel,
  droppableTargets,
} = require('../out/webview-test/components/canvas/model.js');
const {
  Canvas2DFallback,
} = require('../out/webview-test/components/canvas/Canvas2DFallback.js');
const { DeployCanvas } = require('../out/webview-test/components/canvas/DeployCanvas.js');

const DISCONNECTED = {
  projectName: 'test temp',
  imageTag: 'board-app:v1',
  docker: { ready: true },
  github: { connected: true, repo: 'team-chain/Re-Coder', branch: 'main' },
  aws: { connected: false },
};

const CONNECTED = {
  ...DISCONNECTED,
  aws: {
    connected: true,
    account: '123456789012',
    region: 'ap-northeast-2',
    bucket: 'recoder-site-ldk511',
    cluster: 'recoder-cluster',
    service: 'board-app-svc',
  },
};

// ── 1. 렌더러 선택 ──────────────────────────────────────────────

test('WebGL 이 되면 3D', () => {
  assert.strictEqual(pickRenderMode({ webgl: true }), '3d');
});

test('WebGL 이 안 되면 2D — 빈 캔버스를 보여 주지 않는다', () => {
  assert.strictEqual(pickRenderMode({ webgl: false }), '2d');
});

test('설정에서 2D 를 고정하면 WebGL 이 돼도 2D', () => {
  assert.strictEqual(pickRenderMode({ webgl: true, forced2d: true }), '2d');
});

test('한 번 3D 초기화에 실패했으면 다시 시도하지 않는다', () => {
  assert.strictEqual(pickRenderMode({ webgl: true, failedBefore: true }), '2d');
});

test('DOM 이 없는 곳에서도 컴포넌트가 던지지 않고 2D 로 그려진다', () => {
  //: 서버 렌더에는 document 가 없다. 3D 로 시작하면 여기서 터진다.
  const html = renderToStaticMarkup(
    React.createElement(DeployCanvas, { model: buildCanvasModel(CONNECTED) }),
  );
  assert.ok(html.includes('배포 대상'), '2D 폴백이 그려져야 한다');
  assert.ok(!html.includes('deploy-canvas-3d'), '3D 호스트를 먼저 그리면 안 된다');
});

// ── 2. 잠긴 타겟 ────────────────────────────────────────────────

test('AWS 미연결이면 S3·ECS 가 잠기고 드롭 대상에서 빠진다', () => {
  const model = buildCanvasModel(DISCONNECTED);
  const byId = Object.fromEntries(model.nodes.map((n) => [n.id, n]));

  assert.strictEqual(byId.s3.availability, 'locked');
  assert.strictEqual(byId.ecs.availability, 'locked');
  assert.strictEqual(byId.s3.droppable, false);
  assert.strictEqual(byId.ecs.droppable, false);
  assert.strictEqual(byId.ecs.lockedAction, 'aws-connect');

  const ids = droppableTargets(model).map((n) => n.id);
  assert.deepStrictEqual(ids.sort(), ['docker', 'github']);
});

test('잠긴 타겟으로는 연결선을 그리지 않는다 — 선이 있으면 연결된 것으로 읽힌다', () => {
  const model = buildCanvasModel(DISCONNECTED);
  const targets = model.edges.map((e) => e.to);
  assert.ok(!targets.includes('s3'));
  assert.ok(!targets.includes('ecs'));
  assert.ok(targets.includes('docker'));
});

test('[음성 대조] AWS 를 연결하면 잠금이 풀리고 실제 리소스 이름이 뜬다', () => {
  const model = buildCanvasModel(CONNECTED);
  const byId = Object.fromEntries(model.nodes.map((n) => [n.id, n]));

  assert.strictEqual(byId.ecs.availability, 'ready');
  assert.strictEqual(byId.ecs.droppable, true);
  assert.strictEqual(byId.ecs.name, 'recoder-cluster');
  assert.ok(byId.ecs.sub.includes('board-app-svc'));
  assert.strictEqual(byId.s3.name, 'recoder-site-ldk511');
  assert.strictEqual(model.account, '123456789012');

  const ids = droppableTargets(model).map((n) => n.id);
  assert.deepStrictEqual(ids.sort(), ['docker', 'ecs', 'github', 's3']);
});

test('Docker 데몬이 꺼져 있으면 Docker 도 같은 규칙으로 잠긴다', () => {
  const model = buildCanvasModel({ ...CONNECTED, docker: { ready: false } });
  const docker = model.nodes.find((n) => n.id === 'docker');
  assert.strictEqual(docker.availability, 'locked');
  assert.strictEqual(docker.lockedAction, 'docker-start');
});

// ── 3. 2D 폴백이 같은 판단을 보여 준다 ──────────────────────────

test('2D 폴백에서도 잠긴 타겟은 잠금 사유가 뜨고 보내기 버튼이 없다', () => {
  const html = renderToStaticMarkup(
    React.createElement(Canvas2DFallback, { model: buildCanvasModel(DISCONNECTED) }),
  );
  assert.ok(html.includes('AWS 연결 필요'), '잠금 사유가 보여야 한다');
  assert.ok(html.includes('data-availability="locked"'));
  //: 잠긴 카드에는 "여기로 보내기" 가 붙지 않는다 — 2D 라고 규칙이 달라지면 안 된다.
  const lockedCards = html.split('data-availability="locked"').length - 1;
  assert.strictEqual(lockedCards, 2, 'S3 · ECS 두 개가 잠겨야 한다');
});

test('[음성 대조] 연결된 상태의 2D 폴백에는 잠금 문구가 없다', () => {
  const html = renderToStaticMarkup(
    React.createElement(Canvas2DFallback, { model: buildCanvasModel(CONNECTED) }),
  );
  assert.ok(!html.includes('AWS 연결 필요'));
  assert.ok(html.includes('recoder-cluster'));
  assert.ok(html.includes('123456789012'));
});

// ── 4. 충돌 회피 계약 ───────────────────────────────────────────

test('캔버스 모듈은 DeploymentCenter 를 참조하지 않는다', () => {
  //: 2026-09-22 협의 — 배포 화면은 다른 사람이 고치는 중이다. 캔버스가 그
  //: 파일을 건드리기 시작하면 따로 만든 이유가 없어진다.
  const dir = path.join(__dirname, '../webview-src/components/canvas');
  const files = [];
  (function walk(d) {
    for (const entry of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, entry.name);
      if (entry.isDirectory()) walk(p);
      else if (/\.tsx?$/.test(entry.name)) files.push(p);
    }
  })(dir);

  assert.ok(files.length > 0, '캔버스 소스를 찾지 못했다 — 경로가 바뀌었나');
  for (const file of files) {
    const source = fs.readFileSync(file, 'utf8');
    //: 주석에서 배경을 설명하는 건 괜찮다. 막는 것은 **실제 의존**이다.
    const imports = /(?:from\s+|require\()\s*['"][^'"]*DeploymentCenter/;
    assert.ok(
      !imports.test(source),
      `${path.basename(file)} 가 DeploymentCenter 를 import 한다`,
    );
  }
});
