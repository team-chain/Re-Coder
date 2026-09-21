/**
 * 보안 스캔 미실행이 "통과"로 보이던 문제 — 보드 이슈
 * 「보안 스캔이 바이너리 없으면 조용히 건너뜀」의 화면 쪽.
 *
 * 무엇이 사고였나
 *   코어는 스캐너가 없으면 `status:"error"` 와 사유를 내려주고 있었다. 그런데
 *   화면(renderScanSummary)이 그 필드를 **읽지 않고** findings 의 취약점 수만
 *   셌다. 스캐너가 없으면 findings 가 비므로 결과는 0건 → 초록색
 *   **"취약점 없음 ✓"**. 검사하지 않은 것이 통과로 표시됐고, 사용자는 그걸
 *   믿고 배포로 넘어갔다.
 *
 * 검사 대상은 순수 판정 함수 `scanVerdict` 다. 렌더 안에 있던 로직을 밖으로
 * 꺼냈기 때문에, "초록 배지가 뜨는 조건"을 직접 검사할 수 있다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { scanVerdict } = require('../out/webview-test/components/ShipMode.js');

const CLEAN = { scan_type: 'trivy', exit_code: 0, status: 'ok', findings: { Results: [] } };

test('스캐너가 없으면 not_run — 통과가 아니다', () => {
  const result = {
    scan_type: 'trivy', exit_code: 1, status: 'error', findings: [],
    summary: 'Docker is not available on this host. Start Docker Desktop and retry.',
    message: 'docker_not_found',
  };
  assert.strictEqual(scanVerdict(result), 'not_run');
});

test('타임아웃도 not_run', () => {
  assert.strictEqual(scanVerdict({
    scan_type: 'trivy', exit_code: 1, status: 'error', findings: [],
    summary: "Scan 'trivy' exceeded 300s timeout.", message: 'timeout',
  }), 'not_run');
});

test('status 가 없는 구버전 응답도 findings 가 없으면 not_run', () => {
  //: "결과가 없다"를 "깨끗하다"로 바꾸지 않는다.
  assert.strictEqual(scanVerdict({ scan_type: 'trivy', exit_code: 0, findings: null }), 'not_run');
});

test('실제로 돌았고 취약점이 있으면 vulnerable', () => {
  assert.strictEqual(scanVerdict({
    scan_type: 'trivy', exit_code: 0, status: 'ok',
    findings: { Results: [{ Vulnerabilities: [{ Severity: 'CRITICAL' }, { Severity: 'LOW' }] }] },
  }), 'vulnerable');
});

test('[음성 대조] 실제로 돌았고 취약점이 없으면 clean — 경고가 과하면 안 된다', () => {
  assert.strictEqual(scanVerdict(CLEAN), 'clean');
});

test('not_run 일 때 화면이 초록 통과 문구를 쓰지 않는다', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '../webview-src/components/ShipMode.tsx'), 'utf8',
  );
  const start = source.indexOf("if (verdict === \"not_run\")");
  assert.notStrictEqual(start, -1, 'not_run 분기가 없다');
  const branch = source.slice(start, source.indexOf('const findings =', start));
  assert.ok(!/취약점 없음/.test(branch), '미실행인데 "취약점 없음" 문구가 들어 있다');
  //: 사용자가 오해하지 않도록 "확인하지 못했다"가 명시돼야 한다.
  assert.match(branch, /확인하지 못했다|하지 못했습니다/);
});

// ── 코어의 정규화 형태(critical_count/high_count, findings:[{severity}]) — 실기기 B1 회귀 ──
// 실제로 돈 검사가 "검사를 하지 못했습니다" 헤더 + "no critical … detected" 본문의
// 모순 박스로 보였다: 화면이 Trivy 원본 형태만 읽었기 때문.

test('코어 정규화 형태 — 돌았고 0건이면 clean', () => {
  assert.strictEqual(scanVerdict({
    scan_type: 'trivy', exit_code: 0, status: 'ok', critical_count: 0, high_count: 0, findings: [],
    summary: 'The Trivy image scan completed with no critical or high-severity vulnerabilities detected.',
  }), 'clean');
});

test('코어 정규화 형태 — 카운트가 있으면 vulnerable', () => {
  assert.strictEqual(scanVerdict({
    scan_type: 'trivy', exit_code: 0, status: 'ok', critical_count: 1, high_count: 0,
    findings: [{ severity: 'CRITICAL', id: 'CVE-1' }],
  }), 'vulnerable');
});

test('코어 정규화 형태 — 카운트 없이 findings 배열만 있어도 severity 로 센다', () => {
  assert.strictEqual(scanVerdict({ scan_type: 'trivy', exit_code: 0, status: 'ok', findings: [{ severity: 'HIGH' }] }), 'vulnerable');
  assert.strictEqual(scanVerdict({ scan_type: 'trivy', exit_code: 0, status: 'ok', findings: [] }), 'clean');
});
