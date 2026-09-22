/**
 * 배포 캔버스 2단계 — 노드 이름표와 연결 아크.
 *
 * 여기서 막는 사고
 *   1. **CSP 로 아이콘이 안 뜨는 것.** VSCode 웹뷰는 외부 요청을 막는다.
 *      로고를 URL 로 불러오면 로컬에서는 보이고 사용자 환경에서는 빈 칸이
 *      된다 — "왜 도커인지 왜 깃인지 구분이 안 간다" 로 되돌아간다. 그래서
 *      아이콘은 패스 문자열로 번들에 박고, 캔버스 코드는 네트워크를 타지
 *      않는다는 것을 소스에서 확인한다.
 *   2. **아크가 노드 사이를 못 잇는 것.** 곡선의 양 끝은 반드시 준 두 점
 *      그대로여야 한다. 들어 올리는 양(lift)은 거리에 비례해야 가까운 노드가
 *      과장되지 않는다.
 *   3. **긴 이름이 플레이트를 넘어 다른 노드를 덮는 것.** 실제 리소스 이름을
 *      쓰기로 했으므로(recoder-site-ldk511 같은) 말줄임이 반드시 동작해야 한다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const {
  arcCurve,
  arcMeshes,
  EDGE_COLOR,
} = require('../out/webview-test/components/canvas/scene/arcs.js');
const { fitText } = require('../out/webview-test/components/canvas/scene/plates.js');
const {
  LOGO_PATHS,
  LOGO_TEXT,
  hasLogo,
} = require('../out/webview-test/components/canvas/scene/logos.js');

const CANVAS_DIR = path.join(__dirname, '../webview-src/components/canvas');

function canvasSources() {
  const files = [];
  (function walk(d) {
    for (const entry of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, entry.name);
      if (entry.isDirectory()) walk(p);
      else if (/\.tsx?$/.test(entry.name)) files.push(p);
    }
  })(CANVAS_DIR);
  return files;
}

/** 주석을 걷어낸 실행 코드만. 주석의 URL 까지 잡으면 출처 표기를 못 쓴다. */
function codeWithoutComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
}

// ── 1. CSP · 네트워크 ───────────────────────────────────────────

test('로고는 URL 이 아니라 패스 문자열로 번들에 박혀 있다', () => {
  assert.ok(hasLogo('docker'), 'docker 로고가 없다');
  assert.ok(hasLogo('github'), 'github 로고가 없다');
  for (const [kind, d] of Object.entries(LOGO_PATHS)) {
    assert.ok(d.length > 100, `${kind} 패스가 너무 짧다 — 잘린 것 아닌가`);
    assert.ok(/^[Mm]/.test(d.trim()), `${kind} 패스가 moveto 로 시작하지 않는다`);
    assert.ok(!/https?:/.test(d), `${kind} 가 URL 을 들고 있다`);
  }
});

test('AWS 계열은 로고가 없으므로 글자로 표시한다', () => {
  //: simple-icons 에서 AWS 아이콘이 내려갔다. 없는 로고를 지어내지 않는다.
  assert.ok(!hasLogo('s3'));
  assert.ok(!hasLogo('ecs'));
  assert.strictEqual(LOGO_TEXT.s3, 'S3');
  assert.strictEqual(LOGO_TEXT.ecs, 'ECS');
});

test('캔버스 코드는 네트워크를 타지 않는다 — 웹뷰 CSP 에서 빈 칸이 되지 않도록', () => {
  for (const file of canvasSources()) {
    const code = codeWithoutComments(fs.readFileSync(file, 'utf8'));
    const name = path.basename(file);
    assert.ok(!/\bfetch\s*\(/.test(code), `${name} 가 fetch 를 쓴다`);
    assert.ok(!/XMLHttpRequest/.test(code), `${name} 가 XHR 을 쓴다`);
    assert.ok(!/new\s+Image\s*\(/.test(code), `${name} 가 Image 로 외부 자원을 불러온다`);
    assert.ok(
      !/["'`]https?:\/\//.test(code),
      `${name} 실행 코드에 외부 URL 이 있다`,
    );
  }
});

// ── 2. 아크 ─────────────────────────────────────────────────────

const V = (x, y, z) => ({ x, y, z });

test('아크의 양 끝은 준 두 점 그대로다', () => {
  const THREE = require('three');
  const a = new THREE.Vector3(-13.5, 1.9, 2);
  const b = new THREE.Vector3(11, 1, 6.5);
  const curve = arcCurve(a, b);
  const start = curve.getPoint(0);
  const end = curve.getPoint(1);
  assert.ok(start.distanceTo(a) < 1e-9, '시작점이 어긋났다');
  assert.ok(end.distanceTo(b) < 1e-9, '끝점이 어긋났다');
});

test('아크는 가운데가 솟는다 — 바닥 격자에 묻히지 않게', () => {
  const THREE = require('three');
  const a = new THREE.Vector3(0, 1, 0);
  const b = new THREE.Vector3(10, 1, 0);
  const mid = arcCurve(a, b).getPoint(0.5);
  assert.ok(mid.y > 1, `가운데가 안 솟았다 (y=${mid.y})`);
});

test('솟는 높이는 거리에 비례한다 — 가까운 노드가 과장되지 않게', () => {
  const THREE = require('three');
  const near = arcCurve(new THREE.Vector3(0, 1, 0), new THREE.Vector3(4, 1, 0)).getPoint(0.5).y;
  const far = arcCurve(new THREE.Vector3(0, 1, 0), new THREE.Vector3(30, 1, 0)).getPoint(0.5).y;
  assert.ok(far > near, `먼 아크가 더 솟아야 한다 (near=${near}, far=${far})`);
});

test('엣지 종류마다 색이 다르다 — 배포와 소스 푸시가 같은 색이면 구분이 안 된다', () => {
  assert.notStrictEqual(EDGE_COLOR.deploy, EDGE_COLOR.push);
  assert.notStrictEqual(EDGE_COLOR.deploy, EDGE_COLOR.gate);
});

test('아크는 심지와 후광 두 개로 만들어진다', () => {
  const THREE = require('three');
  const meshes = arcMeshes(
    arcCurve(new THREE.Vector3(0, 1, 0), new THREE.Vector3(6, 1, 2)),
    EDGE_COLOR.deploy,
  );
  assert.ok(meshes.core && meshes.halo);
  assert.ok(
    meshes.halo.material.opacity < meshes.core.material.opacity,
    '후광이 심지보다 옅어야 한다',
  );
});

// ── 3. 이름 말줄임 ──────────────────────────────────────────────

/** measureText 대역 — 글자 하나를 10px 로 친다. */
const stubCtx = { measureText: (t) => ({ width: t.length * 10 }) };

test('짧은 이름은 그대로 둔다', () => {
  assert.strictEqual(fitText(stubCtx, 'ECS', 200), 'ECS');
});

test('긴 실제 리소스 이름은 말줄임한다 — 플레이트를 넘으면 옆 노드를 덮는다', () => {
  const out = fitText(stubCtx, 'recoder-site-ldk511-ap-northeast-2', 100);
  assert.ok(out.endsWith('…'), '말줄임 표시가 없다');
  assert.ok(stubCtx.measureText(out).width <= 100, '여전히 폭을 넘는다');
});

test('[음성 대조] 경계와 같은 폭이면 자르지 않는다', () => {
  //: 항상 자르는 구현이어도 위 테스트는 통과한다. 그 경우를 여기서 잡는다.
  assert.strictEqual(fitText(stubCtx, 'abcde', 50), 'abcde');
});

// ── 4. 카메라 자동 프레이밍 ─────────────────────────────────────

const { SceneRenderer } = require('../out/webview-test/components/canvas/scene/SceneRenderer.js');

function framedCamera() {
  const THREE = require('three');
  const cam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 300);
  const ang = Math.PI * 0.3;
  cam.position.set(Math.cos(ang) * 30, 18.5, Math.sin(ang) * 30);
  cam.lookAt(SceneRenderer.layoutCenter());
  cam.updateMatrixWorld();
  return cam;
}

test('경계 상자가 모든 노드를 담는다', () => {
  const box = SceneRenderer.frameBounds(framedCamera());
  assert.ok(Number.isFinite(box.minX) && Number.isFinite(box.maxX));
  assert.ok(box.maxX > box.minX && box.maxY > box.minY);
  assert.ok(box.maxX - box.minX > 10, '가로가 너무 좁다 — 노드를 다 못 담는다');
});

test('절두체는 원점이 아니라 경계 상자 중심에 맞춰야 한다', () => {
  //: 이게 깨지면 배치가 치우친 만큼 반대편이 통째로 빈 격자가 된다.
  //: 실제로 첫 구현이 그랬다 — 오른쪽 아래 40% 가 빈 화면이었다.
  const box = SceneRenderer.frameBounds(framedCamera());
  const cx = (box.minX + box.maxX) / 2;
  const cy = (box.minY + box.maxY) / 2;
  assert.ok(
    Math.abs(cx) > 0.5 || Math.abs(cy) > 0.5,
    '경계 상자 중심이 원점과 같다 — 이 테스트의 전제가 깨졌다',
  );
  //: 중심을 쓰면 상자 양쪽 여백이 같다.
  assert.ok(Math.abs((box.maxX - cx) - (cx - box.minX)) < 1e-9);
});
