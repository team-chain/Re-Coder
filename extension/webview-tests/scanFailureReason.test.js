/**
 * 스캔 실패 표시 — raw 에러 대신 원인·다음 행동. 보드 카드
 * 「스캔 실패 표시가 raw 에러 — 원인·다음 행동을 담은 미검증 문구로 교체」.
 *
 * 무엇이 사고였나 (2026-09-20 실기기)
 *   로컬 Docker 배포 화면에서 스캔이 실패하면 "Error: trivy 스캔 실패" 만 떴다.
 *   Docker Desktop 미실행인지 이미지 미빌드인지 화면으로는 알 수 없었다.
 *
 * 여기서 고정하는 것
 *   1. 호스트는 스캔 **요청 실패**도 errorMessage(raw) 가 아니라 scanResult
 *      (status error + cause + next_action) 로 내려보낸다 — 화면은 한 경로.
 *   2. ApiClient 는 "trivy 스캔 실패" 같은 뭉뚱그린 문구 대신 코어 오류를 전달한다.
 *   3. 화면(ShipMode) 은 not_run/unverified 도 not_run 판정이고, 원인·다음 행동을
 *      그리며, raw 원문은 접힌 영역에만 둔다.
 *   4. Hubs 의 검사 패널도 cause → next_action 을 쓴다.
 *   5. 코어 쪽 분류기가 내려주는 필드 이름과 화면이 읽는 이름이 같다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');
const { scanVerdict } = require('../out/webview-test/components/ShipMode.js');

const HOST = read('../src/sidebar/SidebarProvider.ts');
const API = read('../src/core/ApiClient.ts');
const SHIP = read('../webview-src/components/ShipMode.tsx');
const HUBS = read('../webview-src/components/Hubs.tsx');
const CORE_SCAN = fs.readFileSync(path.join(__dirname, '../../core/scan_failure.py'), 'utf8');

test('호스트는 스캔 요청 실패를 scanResult 로 내려보낸다 (raw errorMessage 아님)', () => {
  const start = HOST.indexOf("case 'runScan'");
  const block = HOST.slice(start, HOST.indexOf("case '", start + 10));
  assert.doesNotMatch(block, /postMessage\('errorMessage'/, '실패가 여전히 raw errorMessage 로 나간다');
  assert.match(block, /postMessage\('scanResult', \{\s*status: 'error'/);
  assert.match(block, /reason_code: 'request_failed'/);
  assert.match(block, /next_action:/);
  assert.match(block, /message: raw/, '원문은 message 에 남겨야 한다');
});

test('ApiClient 는 코어 오류를 뭉뚱그리지 않는다', () => {
  assert.doesNotMatch(API, /throw new Error\(`\$\{scanType\} 스캔 실패`\)/);
  assert.match(API, /resp\.error \?\? `\$\{scanType\} 스캔 요청 실패`/);
});

test('not_run · unverified 상태도 통과가 아니다', () => {
  for (const status of ['error', 'not_run', 'unverified']) {
    assert.strictEqual(scanVerdict({ scan_type: 'trivy', exit_code: 1, status, findings: [] }), 'not_run', status);
  }
});

test('화면은 원인·다음 행동을 그리고 raw 는 접는다', () => {
  const start = SHIP.indexOf('if (verdict === "not_run")');
  const box = SHIP.slice(start, SHIP.indexOf('const findings = result.findings as', start));
  assert.match(box, /result\.cause \|\| result\.summary/, '원인은 cause 를 먼저 읽어야 한다');
  assert.match(box, /result\.next_action/);
  assert.match(box, /다음 행동/);
  assert.match(box, /<details/, 'raw 원문은 접힌 영역');
  assert.match(box, /없다는 뜻이 아니라 확인하지 못했다/, 'fail-closed 문구가 사라졌다');
});

test('Hubs 검사 패널도 cause → next_action', () => {
  assert.match(HUBS, /r\.cause \?\? r\.summary \?\? r\.message/);
  assert.match(HUBS, /r\.next_action/);
});

test('코어 분류기 필드 이름과 화면이 읽는 이름이 같다', () => {
  for (const key of ['reason_code', 'cause', 'next_action', 'summary', 'message']) {
    assert.ok(CORE_SCAN.includes(`"${key}"`), `코어 as_fields 에 ${key} 가 없다`);
  }
  //: 실기기에서 난 두 원인이 코어에 코드로 존재한다.
  assert.ok(CORE_SCAN.includes('DOCKER_NOT_RUNNING = "docker_not_running"'));
  assert.ok(CORE_SCAN.includes('IMAGE_NOT_FOUND = "image_not_found"'));
});
