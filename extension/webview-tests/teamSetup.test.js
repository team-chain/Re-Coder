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

test('프리셋: 균형은 쓰는 역할(설계·개발)은 품질, 읽는 역할(검토·테스트)은 빠른 모델 · 하나만 바꾸면 직접 설정',()=>{
 let c=C.defaultConfig();
 assert.equal(c.preset,'balanced');assert.deepEqual(c.models,{planner:'quality',dev:'quality',review:'fast',test:'fast'});
 assert.equal(C.applyPreset(c,'economy').models.dev,'fast');
 c=C.setModel(c,'review','openai');assert.equal(c.preset,'custom');
 c=C.setModel(c,'review','fast');assert.equal(c.preset,'balanced','프리셋과 같아지면 다시 그 이름');
 assert.equal(C.setDevs(c,99).devs,C.MAX_DEVS);assert.equal(C.setDevs(c,0).devs,C.MIN_DEVS);
 //: 저장값은 믿지 않는다
 assert.deepEqual(C.cleanConfig({devs:'x',models:{dev:'evil',test:'openai'},test:false}),
  {preset:'custom',devs:3,test:false,models:{planner:'quality',dev:'quality',review:'fast',test:'openai'}});
});

test('요청에 싣는 역할별 모델: 기본(품질)은 빼고, 키 없는 공급자는 기본으로, 팀 모드가 꺼지면 개발·테스트만',()=>{
 const ready=new Set(['quality','fast','openai']);
 let c=C.setModel(C.setModel(C.defaultConfig(),'planner','anthropic'),'dev','openai');
 assert.deepEqual(C.modelsForRequest(c,true,ready),{dev:'openai',review:'fast',test:'fast'},'키 없는 anthropic 은 싣지 않는다');
 assert.deepEqual(C.modelsForRequest(c,false,ready),{dev:'openai',test:'fast'});
 assert.deepEqual(C.modelsForRequest({...c,test:false},false,ready),{dev:'openai'},'테스트를 끄면 테스트 모델도 싣지 않는다');
 assert.equal(C.modelsForRequest(C.applyPreset(c,'quality'),true,ready),null);
});

test('모델 목록: 사람이 읽는 이름, 키 없는 공급자는 고를 수 없게',()=>{
 const o=C.modelOptions(CHOICES);
 assert.deepEqual(o.map(x=>[x.spec,x.label,x.ready]),[['quality','Claude Sonnet 4.5',true],['fast','Claude Haiku 4.5',true],
  ['anthropic','Claude Sonnet 4.5 (Claude API)',false],['openai','GPT-5 mini',true]]);
 assert.match(o[2].detail,/AI 연결에서 추가/);
 assert.equal(C.modelOptions(null).length,2,'받기 전에는 기본 두 개만');
 assert.equal(C.prettyModel('claude-3-5-haiku-20241022'),'Claude Haiku 3.5');
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

test('팀 버튼과 창: "팀 6명 · 균형", 역할 네 줄·모델 고르기·인원·테스트, 키 없는 모델은 비활성',()=>{
 const cfg=C.defaultConfig();
 assert.equal(teamButtonText(cfg,true),'팀 6명 · 균형');assert.equal(teamButtonText({...cfg,test:false},false),'혼자 · 균형 · 테스트 끔');
 const props={teamMode:true,config:cfg,options:C.modelOptions(CHOICES),history:[],instruction:'게시판에 댓글과 좋아요 기능을 추가해줘',onTeamMode(){},onChange(){}};
 const closed=renderToStaticMarkup(React.createElement(TeamSetup,props));
 assert.match(closed,/data-testid="team-setup-button"/);assert.match(closed,/팀 6명 · 균형/);assert.doesNotMatch(closed,/role="dialog"/);
 const html=renderToStaticMarkup(React.createElement(TeamSetup,{...props,defaultOpen:true}));
 assert.match(html,/role="dialog" aria-label="팀 구성"/);
 assert.equal((html.match(/data-role="/g)||[]).length,4);
 for(const w of ['품질 우선','균형','비용 절약','직접 설정','설계','개발','검토','테스트','요청 크기 중간 · 추천 개발 3명','예상 시간·비용은 첫 실행 뒤 표시','기본값으로'])assert.ok(html.includes(w),w);
 assert.match(html,/<option value="anthropic" disabled=""[^>]*>Claude Sonnet 4\.5 \(Claude API\) — 키 없음<\/option>/);
 assert.match(html,/aria-checked="true" data-preset="balanced"/);
 assert.match(html,/data-testid="team-devs">3</);
 //: 혼자 만들기 — 설계·검토 줄은 흐리게, 개발은 "만들기"
 const solo=renderToStaticMarkup(React.createElement(TeamSetup,{...props,teamMode:false,defaultOpen:true}));
 assert.match(solo,/class="rc-ts-row off" data-role="planner"/);assert.match(solo,/<b>만들기<\/b>/);assert.doesNotMatch(solo,/data-testid="team-devs"/);
});
