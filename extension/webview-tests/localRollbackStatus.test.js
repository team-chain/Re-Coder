const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {LocalRollbackStatus, rollbackBanner, rollbackWatchId} = require('../out/webview-test/components/LocalRollbackStatus.js');
const recovered = {status:'ok',health_ok:true,rolled_back_to:'app:old',verification_resumed:true,restored_deployment_id:'old-id'};
const render = (result,watch) => renderToStaticMarkup(React.createElement(LocalRollbackStatus,{result,watch}));

test('healthy rollback switches banner to restored release and actual monitoring state',()=>{
  assert.ok(render(recovered,{status:'running'}).includes('롤백으로 복구됨 · 이전 버전 감시 중'));
  assert.ok(render(recovered,{status:'stable'}).includes('이전 버전 검증 완료'));
  assert.ok(render(recovered,null).includes('감시 상태 확인 중'));
  assert.ok(render(recovered,'none').includes('감시 상태 없음'));
  assert.ok(!render({...recovered,verification_resumed:false},{status:'running'}).includes('감시 중'));
});

test('legacy or unprobed rollback does not claim confirmed recovery',()=>{
  assert.ok(render({status:'ok'},null).includes('복구 확인 필요'));
  assert.equal(rollbackBanner({...recovered,health_ok:null},null).ok,false);
  assert.ok(!render({status:'ok',warning:'포트 기록 없음'},null).includes('롤백으로 복구됨'));
});

test('failed rollback and later restored-release anomalies stay visible',()=>{
  assert.ok(render({status:'failed',error:'이미지 없음'},null).includes('이미지 없음'));
  assert.ok(render(recovered,{status:'unstable',anomalies:[{}]}).includes('이전 버전에서 이상 감지'));
  assert.equal(rollbackBanner(recovered,{status:'error'}).ok,false);
});

test('monitoring follows restored deployment only if it was actually resumed',()=>{
  assert.equal(rollbackWatchId('failed-id',null),'failed-id');
  assert.equal(rollbackWatchId('failed-id',recovered),'old-id');
  assert.equal(rollbackWatchId('failed-id',{...recovered,verification_resumed:false}),undefined);
  assert.equal(rollbackWatchId('failed-id',{status:'failed'}),undefined);
  assert.equal(rollbackWatchId('failed-id',{status:'failed',container_untouched:true}),'failed-id');
  assert.equal(rollbackWatchId('failed-id',{...recovered,restored_deployment_id:null}),undefined);
});
