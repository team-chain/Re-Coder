const test = require('node:test'), assert = require('node:assert/strict');
const C = require('../out/webview-test/components/teamConfig');
const small='버튼 색 바꿔줘', medium='게시판에 댓글과 좋아요 기능을 추가해줘', sensitive='카드 결제와 로그인 추가';
test('추천: 크기와 민감 낱말, 개인정보 띄어쓰기, 영어 OAuth',()=>{
 for(const [text,preset] of [[small,'economy'],[medium,'balanced'],['가'.repeat(170),'quality'],[sensitive,'quality'],['전화 번호 수정','quality'],['OAuth','quality']]) assert.equal(C.recommendPreset(text),preset);
 assert.deepEqual(C.sensitiveOf('카드 환불 로그인 개인 정보'),['결제','인증','개인정보']);
});
test('민감 고정은 모든 프리셋과 직접 설정에 적용, 꺼 두면 선택 유지',()=>{
 for(const preset of ['quality','balanced','economy','custom']) {
  const cfg={...C.defaultConfig(),auto:false,preset,models:{...C.PRESETS.economy}};
  const result=C.effectiveConfig(cfg,sensitive);
  assert.equal(result.models.planner,'quality'); assert.equal(result.models.dev,'quality');
 }
 const cfg={...C.defaultConfig(),auto:false,preset:'economy'};
 assert.deepEqual(C.modelsForRequest(cfg,true,null,sensitive),{planner:'quality',dev:'quality',review:'fast',test:'fast'});
 assert.deepEqual(C.modelsForRequest(cfg,false,null,sensitive),{dev:'quality',test:'fast'});
 assert.equal(C.effectiveConfig(cfg,sensitive,false).models.dev,'fast');
 assert.deepEqual(C.lockedRoles(sensitive,false),[]);
});
test('직접 설정 시작값·수정·되돌리기와 프리셋 클릭',()=>{
 let cfg=C.applyPreset(C.defaultConfig(),'custom',small);
 assert.equal(cfg.auto,false); assert.deepEqual(cfg.models,C.PRESETS.economy);
 cfg=C.setModel(cfg,'review','quality'); assert.equal(cfg.preset,'custom');
 cfg=C.setModel(cfg,'review','fast'); assert.equal(cfg.preset,'custom');
 cfg=C.restoreRecommendation(cfg,sensitive); assert.equal(cfg.auto,true); assert.equal(cfg.preset,'quality');
 assert.equal(C.applyPreset(cfg,'economy',small).auto,true);
 assert.equal(C.applyPreset(cfg,'quality',small).auto,false);
 assert.equal(C.applyPreset(C.defaultConfig(),'custom',sensitive).models.dev,'quality');
});
test('요청 추천 변경: 직접 설정 유지, 모델 초기화, 나머지는 자동 복귀; 끄면 유지',()=>{
 const custom=C.setModel(C.applyPreset(C.defaultConfig(),'custom',small),'dev','quality');
 const next=C.updateRecommendation(custom,small,medium);
 assert.equal(next.preset,'custom'); assert.equal(next.auto,false); assert.deepEqual(next.models,C.PRESETS.balanced);
 assert.equal(C.updateRecommendation(custom,small,small),custom);
 assert.equal(C.updateRecommendation(custom,small,medium,false),custom);
 const manual=C.applyPreset(C.defaultConfig(),'quality',small);
 assert.equal(C.updateRecommendation(manual,small,medium).auto,true);
 assert.equal(C.effectiveConfig(manual,small,true,false).preset,'quality');
});
test('저장값 복원: auto는 boolean만, 직접 설정과 수동 프리셋 보존',()=>{
 const cfg=C.cleanConfig({auto:'false',preset:'custom',models:C.PRESETS.economy});
 assert.equal(cfg.auto,true); assert.equal(cfg.preset,'custom');
 assert.deepEqual(C.cleanConfig({...cfg,auto:false}),{...cfg,auto:false});
 assert.equal(C.cleanConfig({models:{dev:'openai'}}).models.dev,'quality');
 assert.equal(C.effectiveConfig(C.defaultConfig(),small).preset,'economy');
});
