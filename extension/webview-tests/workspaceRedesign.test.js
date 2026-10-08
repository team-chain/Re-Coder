const test = require('node:test');
const assert = require('node:assert/strict');
const THREE = require('three');
const { createArchitectureScene } = require('../out/webview-test/components/canvas/threeScene');
const { overview } = require('../out/webview-test/components/canvas/model');
const { ScanQueue } = require('../out/webview-test/components/scanQueue');

test('3D platforms project underneath the DOM targets and own disposable resources', () => {
  const { nodes, edges } = overview(null, '', false);
  const architecture = createArchitectureScene(nodes, edges, 680);
  architecture.camera.updateMatrixWorld();
  architecture.scene.updateMatrixWorld(true);
  for (const node of nodes) {
    const group = architecture.scene.getObjectByName(node.id);
    const projected = group.getWorldPosition(new THREE.Vector3()).project(architecture.camera);
    assert.ok(Math.abs((projected.x + 1) * 560 - node.x) < .01);
    assert.ok(Math.abs((1 - projected.y) * 340 - node.y) < .01);
    if (node.kind !== 'gate') {
      const platform = group.children.find(child => child.geometry?.type === 'ExtrudeGeometry');
      assert.ok(platform, `${node.id} must have volume`);
      platform.geometry.computeBoundingBox();
      assert.ok(platform.geometry.boundingBox.max.z - platform.geometry.boundingBox.min.z > 20);
    }
  }
  const resources = new Set();
  architecture.scene.traverse(object => {
    if (object.geometry) resources.add(object.geometry);
    for (const material of [].concat(object.material || [])) resources.add(material);
  });
  let disposed = 0;
  resources.forEach(resource => resource.addEventListener('dispose', () => disposed++));
  architecture.dispose(); architecture.dispose();
  assert.equal(disposed, resources.size, 'dispose once even for shared platform geometry');
});

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
