/**
 * 로컬 Docker 배포 — 검사 뒤 "docker build / run" 이 실제로 플랜을 만들고 승인으로 간다.
 * (실기기 검증 C2: 버튼 라벨은 있는데 눌러도 아무 일도 없었다.)
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');
const SHIP = read('../webview-src/components/ShipMode.tsx');
const HOST = read('../src/sidebar/SidebarProvider.ts');

test('scanDone 에서 버튼을 누르면 배포 플랜을 만든다', () => {
  const onClick = SHIP.slice(SHIP.indexOf('if (step === "idle") {'), SHIP.indexOf('style={{', SHIP.indexOf('if (step === "idle") {')));
  assert.match(onClick, /else if \(step === "scanDone"\) \{\s*[\s\S]*?handleCreatePlan\(\);/, 'scanDone 분기가 없다 → 버튼이 죽어 있다');
});

test('배포 플랜 응답(plan_id)을 받아 planReady 로 간다', () => {
  assert.match(SHIP, /\(payload as \{ plan_id\?: string \}\)\.plan_id/, '플랜 응답을 받는 분기가 없다');
  const branch = SHIP.slice(SHIP.indexOf('plan_id?: string }).plan_id'), SHIP.indexOf('plan_id?: string }).plan_id') + 300);
  assert.match(branch, /setPlan\(payload as DeploymentPlan\)/);
  assert.match(branch, /setStep\("planReady"\)/);
});

test('스캔·플랜 요청의 빈 workspacePath 는 호스트가 워크스페이스로 채운다', () => {
  assert.match(HOST, /const scanRoot = scanWs \|\| \(vscode\.workspace\.workspaceFolders/);
  assert.match(HOST, /runScan\(scanType, scanRoot, targetPath\)/);
  assert.match(HOST, /const planRoot = dpWs \|\| \(vscode\.workspace\.workspaceFolders/);
  assert.match(HOST, /createDeploymentPlan\(\s*planRoot,/);
});

test('배포 실패는 코어 stderr 원문을 보여 준다 ("stderr 를 확인하세요" 금지)', () => {
  assert.doesNotMatch(SHIP, /배포 실패\. stderr를 확인하세요/);
  assert.match(SHIP, /const detail = \(r\.stderr \|\| r\.error \|\| r\.message \|\| r\.stdout \|\| ""\)\.trim\(\);/);
  assert.match(SHIP, /whiteSpace: "pre-wrap"/, '여러 줄 stderr 가 한 줄로 뭉개진다');
});

test('executeDeployment 은 코어의 거절 사유(4xx/5xx)를 error 로 넘긴다', () => {
  const API = read('../src/core/ApiClient.ts');
  const fn = API.slice(API.indexOf('async executeDeployment('), API.indexOf('async listDeploymentRecords('));
  assert.doesNotMatch(fn, /: \{ status: 'error' \};/, '거절 사유를 버린다');
  assert.match(fn, /error: resp\.error/);
});

test('헬스 실패한 배포는 "Health Check 통과" 로 칠하지 않는다', () => {
  const SHIP2 = read('../webview-src/components/ShipMode.tsx');
  assert.match(SHIP2, /deployResult\?\.health_ok === false \?/, '헬스 결과로 배너를 가르지 않는다');
  assert.match(SHIP2, /컨테이너는 떴지만 Health Check 는 실패했습니다/);
});

test('배포 뒤 감시 스냅샷을 묻고, 이상이면 롤백을 제안만 한다(자동 실행 금지)', () => {
  const SHIP3 = read('../webview-src/components/ShipMode.tsx');
  const HOST3 = read('../src/sidebar/SidebarProvider.ts');
  assert.match(SHIP3, /postMessage\("deploy\.verification\.status", \{ deploymentId \}\)/, '감시 스냅샷을 묻지 않는다');
  assert.match(SHIP3, /if \(unhealthy && rollbackDecision === "idle"\) \{ setRollbackDecision\("proposed"\); \}/, '이상 감지 → 제안 전환이 없다');
  assert.match(SHIP3, /롤백 제안 — 자동으로 실행하지 않습니다/);
  //: 롤백 실행은 사용자가 버튼을 눌러 handleRollback 을 부를 때만.
  const auto = SHIP3.match(/postMessage\("rollback"/g) || [];
  assert.strictEqual(auto.length, 1, '롤백 요청이 버튼 핸들러 밖에도 있다');
  assert.match(HOST3, /case 'deploy\.verification\.status'/);
  assert.match(HOST3, /postMessage\('deploy\.rollbackResult'/);
});

test('교체 실패 뒤 복원 결과는 셋을 구분한다 — 복원됨 / 떴지만 헬스 실패 / 못 띄움 (실기기 D4)', () => {
  const SHIP = fs.readFileSync(path.join(__dirname, '../webview-src/components/ShipMode.tsx'), 'utf8');
  assert.match(SHIP, /restore_stderr/, '복원 사유(restore_stderr)를 읽지 않는다');
  assert.match(SHIP, /이전 컨테이너 복원: \$\{restoreDetail\}/, '복원이 false 일 때 사유를 버린다 — 컨테이너는 떠 있는데 복원 안 됐다는 화면이 된다');
  assert.match(SHIP, /헬스 확인 통과/, '복원 성공 문구가 "떠 있음" 과 "서비스됨" 을 구분하지 않는다');
});
