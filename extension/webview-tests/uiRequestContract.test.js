/**
 * 확장 UI → Core 요청 계약 고정 — 보드 이슈
 * 「확장 UI 경로가 E2E 검증 밖에 있음 — UI가 만드는 요청 검증」.
 *
 * 무엇이 사고였나
 *   e2e_verify.py 는 Core API 를 직접 부른다. 그래서 **확장이 만드는 요청**
 *   (경로·본문 필드)은 아무도 검증하지 않았고, 존재하지 않는 엔드포인트를
 *   부르거나 필드 이름이 어긋나도 배포 버튼을 눌러보기 전까지 몰랐다.
 *
 * 여기서 고정하는 것 (DoD)
 *   1. ApiClient 가 부르는 /api/deploy/* 경로가 Core 에 실제로 존재한다.
 *   2. 플랜·실행·S3 요청 본문의 필드 이름이 Core 모델과 1:1 로 맞는다
 *      (image · container_name · host_port · container_port 포함).
 *   3. 화면(workbenchHtml)이 보내는 payload 를 호스트가 빠짐없이 읽고,
 *      포트는 숫자로 변환해 넘긴다.
 *
 * 알려진 간극(KNOWN_GAPS)
 *   /api/deploy/ec2(+/status) 는 ApiClient 에 있지만 Core 에 라우트가 없다.
 *   이 테스트를 만들며 발견된 실제 사고다 — 라우트가 생기면 목록에서 빼야
 *   테스트가 통과하도록, 반대로 목록에 있는데 라우트가 생겨도 실패하도록
 *   양방향으로 고정한다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

const API = read('../src/core/ApiClient.ts');
const HOST = read('../src/sidebar/workbenchHost.ts');
const HTML = read('../src/sidebar/workbenchHtml.ts');
const CORE_DIR = path.join(__dirname, '../../core');

/** Core 전체에서 절대 경로 리터럴("/api/...")을 수집한다. */
function coreApiPaths() {
  const out = new Set();
  const walk = (dir) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      if (e.isDirectory()) {
        if (['__pycache__', 'tests', '.venv', 'node_modules'].includes(e.name)) { continue; }
        walk(path.join(dir, e.name));
      } else if (e.name.endsWith('.py')) {
        const src = fs.readFileSync(path.join(dir, e.name), 'utf8');
        for (const m of src.matchAll(/["'](\/api\/[a-z0-9_\/{}\-]+)["']/g)) { out.add(m[1]); }
      }
    }
  };
  walk(CORE_DIR);
  return out;
}

/** pydantic 모델 클래스의 필드 이름들을 꺼낸다. */
function coreModelFields(fileRel, className) {
  const src = fs.readFileSync(path.join(CORE_DIR, fileRel), 'utf8');
  const m = src.match(new RegExp(`class ${className}\\(BaseModel\\):([\\s\\S]*?)(?=\\nclass |\\n@|\\ndef )`));
  assert.ok(m, `${className} 모델을 ${fileRel} 에서 못 찾았다`);
  const fields = new Set();
  for (const line of m[1].split('\n')) {
    const f = line.match(/^    ([a-z_][a-z0-9_]*)\s*:/);
    if (f) { fields.add(f[1]); }
  }
  assert.ok(fields.size > 0, `${className} 필드 파싱 실패`);
  return fields;
}

/** ApiClient 소스에서 request('METHOD', '/path', { ...body }) 의 본문 키를 꺼낸다. */
function clientBodyKeys(anchor) {
  const start = API.indexOf(anchor);
  assert.notStrictEqual(start, -1, `${anchor} 를 ApiClient 에서 못 찾았다`);
  const seg = API.slice(start, start + 1200);
  const body = seg.match(/this\.request[^(]*\([^,]+,[^,]+,\s*\{([\s\S]*?)\}\s*[,)]/);
  assert.ok(body, `${anchor} 의 요청 본문을 못 찾았다`);
  const keys = new Set();
  for (const part of body[1].split(',')) {
    const t = part.trim();
    if (!t) { continue; }
    const name = (t.includes(':') ? t.slice(0, t.indexOf(':')) : t).trim();
    if (/^[a-z_][a-z0-9_]*$/.test(name)) { keys.add(name); }
  }
  return keys;
}

//: 한때 /api/deploy/ec2 3종이 여기 있었다 — ApiClient 에는 있는데 Core 에
//: 라우트가 없어 404 를 부르던 실제 간극. EC2 경로를 UI·클라이언트에서
//: 통째로 제거하면서 간극도 사라졌다. 새 간극은 여기에 추가하되,
//: 아래 '해소되면 목록에서 빼도록 강제' 테스트가 청소를 강제한다.
const KNOWN_GAPS = new Set([]);

// ---------------------------------------------------------------------------
// 1. 경로 — 확장이 부르는 /api/deploy/* 가 Core 에 있다
// ---------------------------------------------------------------------------

test('ApiClient 의 /api/deploy/* 경로가 Core 에 존재한다 (알려진 간극 제외)', () => {
  const core = coreApiPaths();
  const called = new Set();
  for (const m of API.matchAll(/["'`](\/api\/deploy[a-z0-9_\/\-]*)["'`]/g)) { called.add(m[1]); }
  assert.ok(called.size >= 8, `확장이 부르는 배포 경로를 제대로 못 모았다 (${called.size}개)`);

  const missing = [...called].filter((p) => !core.has(p) && !KNOWN_GAPS.has(p));
  assert.deepStrictEqual(missing, [],
    `확장이 부르는데 Core 에 없는 경로(= 부르면 404): ${missing.join(', ')}`);
});

test('알려진 간극이 해소되면 목록에서 빼도록 강제한다', () => {
  const core = coreApiPaths();
  const healed = [...KNOWN_GAPS].filter((p) => core.has(p));
  assert.deepStrictEqual(healed, [],
    `Core 에 라우트가 생겼다 — KNOWN_GAPS 에서 제거할 것: ${healed.join(', ')}`);
});

// ---------------------------------------------------------------------------
// 2. 본문 — 필드 이름이 Core 모델과 맞는다
// ---------------------------------------------------------------------------

test('배포 플랜 요청 본문이 DeployPlanRequest 필드와 맞는다', () => {
  const fields = coreModelFields('api/routes/deploy.py', 'DeployPlanRequest');
  const keys = clientBodyKeys('async createDeploymentPlan');
  for (const must of ['workspace_path', 'method', 'image', 'container_name', 'host_port', 'container_port']) {
    assert.ok(keys.has(must), `플랜 요청에 ${must} 가 없다`);
  }
  const unknown = [...keys].filter((k) => !fields.has(k));
  assert.deepStrictEqual(unknown, [],
    `Core 가 모르는 필드(조용히 버려진다): ${unknown.join(', ')}`);
});

test('배포 실행 요청 본문이 ExecuteRequest 필드와 맞는다', () => {
  const fields = coreModelFields('api/routes/deploy.py', 'ExecuteRequest');
  const keys = clientBodyKeys('async executeDeployment');
  assert.ok(keys.has('plan_id') && keys.has('approved'));
  const unknown = [...keys].filter((k) => !fields.has(k));
  assert.deepStrictEqual(unknown, [], `Core 가 모르는 필드: ${unknown.join(', ')}`);
});

test('S3 스트리밍 요청 본문이 S3DeployRequest 필드와 맞는다', () => {
  const fields = coreModelFields('api/routes/deploy_s3.py', 'S3DeployRequest');
  //: deployS3Stream 은 fetch 직접 호출이라 본문 조립부를 따로 잡는다.
  const seg = API.slice(API.indexOf('async deployS3Stream'), API.indexOf('async deployS3Stream') + 1600);
  const keys = new Set();
  for (const m of seg.matchAll(/body(?:\.|\[["'])([a-z_]+)/g)) { keys.add(m[1]); }
  for (const m of (seg.match(/=\s*\{([^}]*)\}/) || ['', ''])[1].matchAll(/([a-z_]+)\s*:/g)) { keys.add(m[1]); }
  for (const must of ['project', 'files']) { assert.ok(keys.has(must), `${must} 가 없다`); }
  const unknown = [...keys].filter((k) => !fields.has(k));
  assert.deepStrictEqual(unknown, [], `Core 가 모르는 필드: ${unknown.join(', ')}`);
});

// ---------------------------------------------------------------------------
// 3. 화면 → 호스트 — payload 가 버려지지 않고, 포트는 숫자다
// ---------------------------------------------------------------------------

test('wb.local.deploy: 화면이 보내는 payload 를 호스트가 모두 읽는다', () => {
  const sendStart = HTML.indexOf("type:'wb.local.deploy'");
  assert.notStrictEqual(sendStart, -1);
  const sendBlock = HTML.slice(sendStart, HTML.indexOf('});', sendStart));
  const sent = new Set();
  for (const m of sendBlock.matchAll(/^\s*([a-z_]+)\s*:/gm)) {
    if (!['type', 'payload'].includes(m[1])) { sent.add(m[1]); }
  }
  assert.ok(sent.size >= 3, `화면이 보내는 필드를 못 모았다 (${sent.size})`);

  const handler = HOST.slice(
    HOST.indexOf("case 'wb.local.deploy'"),
    HOST.indexOf("case '", HOST.indexOf("case 'wb.local.deploy'") + 10),
  );
  const dropped = [...sent].filter((k) => !handler.includes(`p.${k}`));
  assert.deepStrictEqual(dropped, [],
    `화면은 보내는데 호스트가 안 읽는 필드(= 조용히 무시): ${dropped.join(', ')}`);
  //: 포트는 문자열로 흘러가면 Core 검증(Port must be numeric)에 걸린다.
  assert.match(handler, /Number\(p\.host_port/, 'host_port 를 숫자로 변환하지 않는다');
  assert.match(handler, /Number\(p\.container_port/, 'container_port 를 숫자로 변환하지 않는다');
});
