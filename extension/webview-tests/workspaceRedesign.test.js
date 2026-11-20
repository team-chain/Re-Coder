const test = require('node:test');
const assert = require('node:assert/strict');
const { overview } = require('../out/webview-test/components/canvas/model');
const { ScanQueue } = require('../out/webview-test/components/scanQueue');

test('security sequence ignores other panels and stale results, serializes scans, and can retry after timeout', () => {
  const queue = new ScanQueue();
  const first = queue.start(['trivy','hadolint','gitleaks']);
  assert.equal(first.scanType, 'trivy');
  assert.equal(queue.start(['gitleaks']), null);
  assert.equal(queue.matches({ scan_type:'trivy' }), false);
  assert.equal(queue.matches({ scan_type:'trivy',requestId:'different-panel' }), false);
  assert.equal(queue.matches({ scan_type:'hadolint',requestId:first.requestId }), false);
  assert.equal(queue.matches({ scan_type:'trivy',requestId:first.requestId }), true);
  assert.equal(queue.complete().scanType, 'hadolint');
  assert.equal(queue.matches({ scan_type:'trivy',requestId:first.requestId }), false);
  assert.equal(queue.complete().scanType, 'gitleaks');
  assert.equal(queue.complete(), null);
  const before = queue.start(['trivy']); queue.stop();
  const retry = queue.start(['trivy']);
  assert.notEqual(before.requestId, retry.requestId);
  assert.equal(queue.matches({ scan_type:'trivy',requestId:before.requestId }), false);
});
