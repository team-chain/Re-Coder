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
