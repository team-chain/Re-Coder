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

test('① 기반 프로필이 저장 안 된 역할 모드도 재주입한다 (전환 버튼 경로)', () => {
  //: "배포 전용 역할로 전환" 은 프로필 없이 역할을 만든다. 코어가 알려 준 기반
  //: 프로필(status.profile)을 저장해야 재시작 뒤 재주입할 기반이 생긴다.
  const setup = block(HOST, "case 'aws.role.setup'", "case 'aws.role.refresh'");
  assert.match(setup, /profile \|\| \(result\.status\.profile \?\? ''\)\.trim\(\)/, '코어가 알려 준 기반 프로필을 저장하지 않는다');
  const fn = block(HOST, 'async healAwsConnection(', 'async healDocker(');
  assert.match(fn, /if \(!stored && !roleArn\) \{ return 'nothing_stored'; \}/, '역할 ARN 만 있어도 시도해야 한다');
  assert.match(fn, /storeAwsProfile\(role\.status\.profile/, '재주입에서 알아낸 기반을 저장하지 않는다');
});

test('③ Docker 자동 조치는 동시 호출을 하나로 합친다', () => {
  //: 진단이 시작한 조치 위에 버튼을 또 누르면 두 번째가 락 대기로 abort 되던 문제.
  const fn = block(HOST, 'async healDocker(', 'private async _healDockerOnce(');
  assert.match(fn, /if \(this\._dockerHealInFlight\) \{ return this\._dockerHealInFlight; \}/);
  assert.match(API, /'POST', '\/api\/docker\/ensure', \{\}, false, 200000/, '제한 시간이 코어 대기보다 넉넉해야 한다');
});

test('③ Docker 를 띄웠지만 준비가 늦으면 실패로 끝내지 않고 뒤에서 이어 확인한다', () => {
  const once = block(HOST, 'private async _healDockerOnce(', 'private async _waitForDocker(');
  //: launched && !ready → pending 알림 → 후속 대기 → 최종 결과. "실패" 는 launched=false 때만 즉시.
  assert.match(once, /if \(!r\.launched\)/, 'launched 로 즉시 실패/후속 대기를 가르지 않는다');
  assert.match(once, /pending: true/, '대기 중임을 화면에 알리지 않는다');
  assert.match(once, /await this\._waitForDocker\(r\.waited_seconds\)/, '후속 대기가 없다');
  const wait = block(HOST, 'private async _waitForDocker(', 'private async _selfHealFromDiagnostics(');
  assert.match(wait, /DOCKER_FOLLOWUP_MS/);
  assert.match(wait, /ensureDocker\(\)/, '후속 확인이 코어 ensure 를 재사용하지 않는다');
  //: 화면은 pending 동안 버튼을 "조치 중…" 으로 잠가 둔다 — 두 번 누르지 않게.
  assert.match(PANEL, /p\?\.pending && p\?\.key\) \{ setHealing\(p\.key\)/, '대기 중 버튼이 풀린다');
});
