const test = require('node:test');
const assert = require('node:assert/strict');
const { bridgeEnabled } = require('../out/bridge/bridgeEnabled.js');

const cfg = (values) => ({ inspect: (key) => (key in values ? { globalValue: values[key], workspaceValue: 'ignored' } : { workspaceValue: 'x' }) });

test('봇 브리지 — 기본은 꺼짐, 작업 영역 값으로는 켜지지 않는다', () => {
  assert.equal(bridgeEnabled(cfg({})), false);
});

test('봇 브리지 — 사용자 설정에 봇 연결 정보가 있는 기존 사용자는 예전처럼 켜진다', () => {
  assert.equal(bridgeEnabled(cfg({ token: 'bridge-token' })), true);
  assert.equal(bridgeEnabled(cfg({ studentId: 's-1' })), true);
  assert.equal(bridgeEnabled(cfg({ token: '  ' })), false);
});

test('봇 브리지 — 명시한 enabled 가 우선한다', () => {
  assert.equal(bridgeEnabled(cfg({ enabled: false, token: 'bridge-token' })), false);
  assert.equal(bridgeEnabled(cfg({ enabled: true })), true);
});
