/**
 * 자가 조치(Self-healing) 레이어 배선 — 보드 카드 「자가 조치 레이어 — ON/OFF
 * 전제조건을 코어가 알아서 복구」 1순위 묶음.
 *
 * 여기서 고정하는 것
 *   ① 코어에 붙자마자(재사용 포함) 보안 금고의 AWS 연결을 코어에 다시 넣는다 —
 *      "재시작하면 연결 풀림" 의 근본 원인. 진단보다 **먼저** 돈다.
 *   ② 진단이 X 를 내면 자동 조치 등급(AWS 재주입·Docker 시작)을 시도하고 한 번
 *      더 진단한다. 인스턴스당 한 번만 — 매 진단마다 STS 를 두드리지 않는다.
 *   ③ 진단판 X 항목의 버튼은 "자동 조치" 이고, 무엇을 했는지(자동 조치함 /
 *      실패) 를 항목 아래 남긴다. Docker 는 안내만 하던 것을 코어가 직접 띄운다.
 *   ④ 재주입은 키를 SecretStorage 밖 파일로 내보내지 않는다 — 코어 connect API 로만.
 *   ⑤ ApiClient 가 부르는 코어 경로가 실제로 있다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

const HOST = read('../src/sidebar/SidebarProvider.ts');
const MANAGER = read('../src/core/CoreManager.ts');
const API = read('../src/core/ApiClient.ts');
const PANEL = read('../webview-src/components/DiagnosticsPanel.tsx');
const CORE_HEALTH = fs.readFileSync(path.join(__dirname, '../../core/api/routes/health.py'), 'utf8');

const block = (src, start, end) => {
  const a = src.indexOf(start);
  assert.notStrictEqual(a, -1, `${start} 가 없다`);
  const b = src.indexOf(end, a + start.length);
  return src.slice(a, b === -1 ? undefined : b);
};

test('① 코어가 뜨면 진단보다 먼저 보안 금고의 AWS 연결을 다시 넣는다', () => {
  const startup = block(HOST, 'if (coreOk) {', '})();');
  const heal = startup.indexOf("healAwsConnection('startup')");
  const diag = startup.indexOf("type: 'runDiagnostics'");
  assert.ok(heal !== -1, '기동 시 재주입이 없다');
  assert.ok(heal < diag, '재주입이 진단보다 뒤에 온다 — 첫 진단이 X 로 뜬다');
});

test('① 재주입은 키 → connect, 프로필 → connect-profile, 역할 모드면 역할까지', () => {
  const fn = block(HOST, 'async healAwsConnection(', 'async healDocker(');
  assert.match(fn, /getStoredAwsConnection\(\)/);
  assert.match(fn, /connectAws\(\{/);
  assert.match(fn, /connectAwsProfile\(\{ profile: stored\.profile/);
  assert.match(fn, /getAwsRoleArn\(\)/);
  assert.match(fn, /setupAwsRole\(\{/, '역할 모드였으면 역할을 다시 빌려야 한다');
  //: 이미 연결돼 있으면 건드리지 않는다 — 역할 모드 저장 상태면 역할로 연결돼 있어야 한다.
  assert.match(fn, /status\.ready && \(!roleArn \|\| status\.storage === 'assumed_role'\)/);
  //: 자동으로 했으면 반드시 말한다.
  assert.match(fn, /자동 조치함/);
});

test('② 진단 X → 자동 조치 → 재진단, 인스턴스당 한 번', () => {
  const fn = block(HOST, 'private async _selfHealFromDiagnostics(', '    private async handleMessage(');
  assert.match(fn, /coreInstanceKey\(\)/);
  assert.match(fn, /this\._awsHealedFor === key\) \{ return false; \}/, '인스턴스당 1회 가드가 없다');
  assert.match(fn, /notReady\('aws_deploy_ready'\) \|\| notReady\('ai_ready'\)/);
  assert.match(fn, /notReady\('docker_ready'\)/);
  const run = block(HOST, "case 'runDiagnostics'", "case 'switchMode'");
  assert.match(run, /_selfHealFromDiagnostics\(/);
  assert.match(run, /if \(healed\) \{[\s\S]*type: 'runDiagnostics'/, '고친 뒤 재진단이 없다');
});

test('③ 진단판 버튼은 자동 조치이고 결과를 항목 아래 남긴다', () => {
  assert.match(PANEL, /"자동 조치"/);
  assert.doesNotMatch(PANEL, />\s*Retry check\s*</, '예전 Retry check 버튼이 남아 있다');
  assert.match(PANEL, /type === "selfHeal"/, '자가 조치 결과를 받지 않는다');
  assert.match(PANEL, /heal\[item\.key\]\.message/, '자동 조치함 문구를 그리지 않는다');
  //: Docker 는 안내가 아니라 코어가 띄운다.
  const fix = block(HOST, "case 'webview.diagnostics.fix'", "case 'webview.open.external'");
  assert.match(fix, /healDocker\('fix'\)/);
  assert.doesNotMatch(fix, /자동 시작은 위험/, 'Docker 자동 시작을 여전히 안내로만 막는다');
  //: AWS 는 입력창을 열기 전에 재주입을 먼저 시도한다.
  assert.match(fix, /healAwsConnection\('fix'\)[\s\S]*recoder\.awsConfigure/);
});

test('④ 보관된 연결은 SecretStorage 에서만 읽고 파일로 내보내지 않는다', () => {
  const fn = block(MANAGER, 'async getStoredAwsConnection(', 'coreInstanceKey()');
  assert.match(fn, /secrets\.get\(AWS_ACCESS_KEY_SECRET\)/);
  assert.doesNotMatch(fn, /writeFile|fs\./, '키를 파일에 쓴다');
});

test('⑤ ApiClient 의 docker 경로가 코어에 있다', () => {
  assert.ok(API.includes("'POST', '/api/docker/ensure'"), 'ApiClient 에 ensureDocker 경로가 없다');
  assert.ok(CORE_HEALTH.includes('"/api/docker/ensure"'), 'Core 에 /api/docker/ensure 라우트가 없다 (= 404)');
  assert.match(CORE_HEALTH, /ensure_docker\b/);
});
