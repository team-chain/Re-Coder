const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const C=require('../out/webview-test/components/teamConfig');
const {TeamSetup,teamButtonText,estimateText}=require('../out/webview-test/components/TeamSetup');

const CHOICES=[
 {spec:'quality',label:'품질',ready:true,provider:'bedrock',model:'us.anthropic.claude-sonnet-4-5-20250929-v1:0'},
 {spec:'fast',label:'빠른',ready:true,provider:'bedrock',model:'us.anthropic.claude-haiku-4-5-20251001-v1:0'},
 {spec:'anthropic',label:'Claude API',ready:false,provider:'anthropic',model:'claude-sonnet-4-5'},
 {spec:'openai',label:'OpenAI API',ready:true,provider:'openai',model:'gpt-5-mini'},
];

test('프리셋과 직접 설정: 직접 설정은 추천과 같아져도 유지',()=>{
 let c=C.defaultConfig();
 assert.equal(c.auto,true); assert.deepEqual(c.models,C.PRESETS.balanced);
 c=C.setModel(c,'review','quality'); assert.equal(c.preset,'custom');
 c=C.setModel(c,'review','fast'); assert.equal(c.preset,'custom');
 assert.equal(C.setDevs(c,99).devs,C.MAX_DEVS); assert.equal(C.setDevs(c,0).devs,C.MIN_DEVS);
});
test('전송: 준비 안 된 간단 모델은 품질, 혼자 만들기는 개발·테스트만',()=>{
 const cfg={...C.defaultConfig(),auto:false,preset:'economy',models:{...C.PRESETS.economy}};
 assert.deepEqual(C.modelsForRequest(cfg,false,new Set(['quality','fast'])),{dev:'fast',test:'fast'});
 assert.deepEqual(C.modelsForRequest({...cfg,test:false},false,null),{dev:'fast'});
 assert.deepEqual(C.modelsForRequest(cfg,true,new Set(['quality'])),C.PRESETS.quality);
});
test('두 모델 이름은 라우팅 정보에서, 공급자 중복 옵션은 없음',()=>{
 const options=C.modelOptions(CHOICES);
 assert.deepEqual(options.map(x=>[x.spec,x.label]),[['quality','Claude Sonnet 4.5'],['fast','Claude Haiku 4.5']]);
 assert.equal(C.modelOptions(null).length,2);
 assert.equal(C.prettyModel('claude-3-5-haiku-20241022'),'Claude Haiku 3.5');
 assert.equal(C.prettyModel('gpt-6.1-terra'),'GPT-6.1 Terra');
});

test('추천 인원·예상 시간/비용: 기록이 없으면 지어내지 않고, 같은 방식의 지난 실행 범위만',()=>{
 assert.deepEqual(C.recommend('버튼 색 바꿔줘'),{size:'small',devs:2});
 assert.deepEqual(C.recommend('게시판에 댓글과 좋아요 기능을 추가해줘'),{size:'medium',devs:3});
 assert.equal(C.recommend('쇼핑몰 전체를 만들어줘: 상품 목록, 장바구니, 결제, 주문 내역, 관리자 페이지').size,'large');
 const cfg=C.defaultConfig();
 assert.equal(estimateText([],cfg,true),null);
 let h=[];
 h=C.addRun(h,{seconds:200,costUsd:0.4,files:12,devs:3,preset:'balanced',at:1,team:true});
 h=C.addRun(h,{seconds:290,costUsd:0.7,files:14,devs:3,preset:'balanced',at:2,team:true});
 h=C.addRun(h,{seconds:30,costUsd:null,files:1,devs:1,preset:'balanced',at:3,team:false});
 assert.deepEqual(estimateText(h,cfg,true),{time:'3–5분',cost:'$0.40–$0.70',basis:'지난 2회 기준'});
 assert.deepEqual(estimateText(h,cfg,false),{time:'1분',cost:'',basis:'지난 1회 기준'},'혼자 만든 기록과 섞지 않는다');
 assert.equal(C.cleanHistory([{seconds:-1,files:1},{seconds:5,files:2,costUsd:-3},'x']).length,1);
 assert.equal(C.cleanHistory(Array.from({length:30},()=>({seconds:5,files:1}))).length,C.MAX_HISTORY);
});

const props={teamMode:true,config:C.defaultConfig(),options:C.modelOptions(CHOICES),history:[],instruction:'게시판에 댓글과 좋아요 기능을 추가해줘',onTeamMode(){},onChange(){}};
const render=(extra={})=>renderToStaticMarkup(React.createElement(TeamSetup,{...props,defaultOpen:true,...extra}));
test('팀 창: 순서·네 역할·프리셋 읽기 전용·실제 기록 없는 예상',()=>{
 assert.equal(teamButtonText({...C.defaultConfig(),test:false},false),'혼자 · 균형');
 const html=render(); assert.match(html,/팀 6명 · 균형/);
 assert.equal((html.match(/data-role="/g)||[]).length,4); assert.doesNotMatch(html,/<select/);
 assert.match(html,/설계·개발은 복잡한 작업 모델\(Claude Sonnet 4.5\)/);
 assert.match(html,/예상 시간·비용은 첫 실행 뒤 표시/); assert.match(html,/모델 설정/);
 assert.ok(html.indexOf('data-preset="quality"')<html.indexOf('data-testid="team-recommend"'));
 const solo=render({teamMode:false}); assert.match(solo,/class="rc-ts-row off" data-role="planner"/); assert.match(solo,/<b>만들기<\/b>/);
});
test('작은 요청·직접 설정·민감 요청 화면',()=>{
 const small=render({instruction:'버튼 색 바꿔줘'}); assert.match(small,/팀 6명 · 비용 절약/); assert.match(small,/작은 요청이라 모든 역할에 간단한 작업 모델/);
 assert.match(small,/class="rc-ts-badge">추천/); assert.doesNotMatch(small,/>되돌리기</);
 const custom=C.setModel(C.applyPreset(C.defaultConfig(),'custom',props.instruction),'review','quality');
 const html=render({config:custom}); assert.equal((html.match(/<select/g)||[]).length,4);
 assert.match(html,/추천\(균형\)과 다르게 골랐습니다\./); assert.match(html,/>되돌리기</); assert.doesNotMatch(html,/추천대로면/);
 assert.match(html,/rc-ts-dot on/); assert.doesNotMatch(html,/class="rc-ts-badge"/);
 const locked=render({instruction:'카드 결제 추가',config:{...custom,models:C.PRESETS.economy}});
 assert.match(locked,/aria-label="설계에 쓸 모델" disabled/); assert.match(locked,/aria-label="개발에 쓸 모델" disabled/);
 const payment=render({instruction:'카드 결제 추가'}); assert.match(payment,/결제가 들어간 요청이라 설계·개발은 복잡한 작업 모델/);
 const same=render({config:C.applyPreset(C.defaultConfig(),'custom',props.instruction)}); assert.match(same,/역할마다 모델을 고르세요. 지금은 추천과 같습니다\./);
});
