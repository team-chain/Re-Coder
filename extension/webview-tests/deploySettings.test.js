const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {DeploySettingsPanel}=require('../out/webview-test/components/DeploySettingsPanel');
const {DeliveryTrack,creepFor,stepSeconds}=require('../out/webview-test/components/DeliveryTrack');
const {planBlockedBySettings}=require('../out/webview-test/components/ShipMode');
const {BuildFailure,isSettingsFailure}=require('../out/webview-test/components/ReadinessPanel');
const {DeploymentActivity}=require('../out/webview-test/components/DeploymentActivity');
const {teamWorking,emptyTeamView,reduceTeam}=require('../out/webview-test/components/teamState');

const settings=[
 {name:'JWT_SECRET',label:'JWT_SECRET',hint:'',status:'ready',source:'generated',secret:true,min_length:32},
 {name:'STRIPE_SECRET_KEY',label:'Stripe 비밀 키',hint:'sk_test_…',status:'missing',source:'',secret:true},
 {name:'STRIPE_WEBHOOK_SECRET',label:'Stripe 웹훅 서명 키',hint:'whsec_…',status:'missing',source:'',secret:true},
];
const demo={available:true,enabled:false,label:'키 없이 결제를 끈 로컬 데모로 실행',note:'모의 결제 서버를 함께 띄웁니다.'};
const noop=()=>{};

test('필요한 설정: 자동 생성은 표시만, 외부 키는 비밀번호 입력, 키가 없으면 데모 버튼',()=>{
 const html=renderToStaticMarkup(React.createElement(DeploySettingsPanel,{settings,missing:['STRIPE_SECRET_KEY','STRIPE_WEBHOOK_SECRET'],demo,onSave:noop,onDemo:noop}));
 assert.match(html,/필요한 설정/);assert.match(html,/2개 입력 필요/);assert.match(html,/자동 생성/);
 assert.equal((html.match(/type="password"/g)||[]).length,2);
 assert.match(html,/키 없이 결제를 끈 로컬 데모로 실행/);assert.match(html,/저장하고 계속/);
 assert.doesNotMatch(html,/value="sk_/);
});

test('데모 모드가 켜지면 결제 꺼짐 표시와 실제 키로 되돌리기',()=>{
 const on={...demo,enabled:true};
 const ready=settings.map(s=>s.status==='missing'?{...s,status:'ready',source:'demo'}:s);
 const html=renderToStaticMarkup(React.createElement(DeploySettingsPanel,{settings:ready,missing:[],demo:on,onSave:noop,onDemo:noop}));
 assert.match(html,/로컬 데모 · 결제 꺼짐/);assert.match(html,/실제 키로 실행하기/);assert.match(html,/모두 준비됨/);
 assert.doesNotMatch(html,/data-testid="demo-button"/);assert.doesNotMatch(html,/type="password"/);
});

test('모의 결제를 지원하지 않는 앱에는 데모 버튼이 없다',()=>{
 const html=renderToStaticMarkup(React.createElement(DeploySettingsPanel,{settings,missing:['STRIPE_SECRET_KEY'],demo:null,onSave:noop,onDemo:noop}));
 assert.doesNotMatch(html,/로컬 데모/);
});

test('필요한 설정이 비어 있으면 승인할 수 없다',()=>{
 assert.equal(planBlockedBySettings({settings_missing:['A']}),true);
 assert.equal(planBlockedBySettings({settings_missing:[]}),false);
 assert.equal(planBlockedBySettings(null),false);
});

test('설정값 실패는 코드 수정(문서 근거 수정) 대신 설정 입력으로 안내',()=>{
 const d={code:'APP_MISSING_SETTING',title:'앱에 필요한 설정값이 비어 있음',cause:'JWT_SECRET',fix:'설정',lines:[],missing_env:['JWT_SECRET']};
 assert.equal(isSettingsFailure(d),true);
 const html=renderToStaticMarkup(React.createElement(BuildFailure,{diagnosis:d,onSettings:noop}));
 assert.match(html,/필요한 설정 입력하고 다시 배포/);assert.doesNotMatch(html,/문서 근거로 오류 수정/);
 const code=renderToStaticMarkup(React.createElement(BuildFailure,{diagnosis:{...d,code:'APP_START_ERROR'}}));
 assert.match(code,/문서 근거로 오류 수정/);
});

test('배달 애니메이션: 실제 단계로만 이동, 단계 안에서는 다음 단계 직전까지만',()=>{
 const stages=['준비','이미지 빌드','보안 확인','컨테이너 시작'];
 const at=i=>Number(renderToStaticMarkup(React.createElement(DeliveryTrack,{stages,index:i,state:'running',destination:'Docker',animal:'dog'})).match(/data-ratio="([\d.]+)"/)[1]);
 assert.ok(at(0)<at(1)&&at(1)<at(2));
 assert.ok(creepFor(10**9,8000)<0.81 && creepFor(0,8000)===0);
 assert.ok(stepSeconds(500)<stepSeconds(60000));
 const done=renderToStaticMarkup(React.createElement(DeliveryTrack,{stages,index:4,state:'done',destination:'Docker',animal:'cat'}));
 assert.match(done,/data-ratio="1.000"/);assert.match(done,/리코더가 배포 박스를 옮기는 중 — Docker에 배달 완료/);assert.match(done,/<img src="data:image\/png;base64,/);assert.doesNotMatch(done,/고양이|강아지|판다/);
 const failed=renderToStaticMarkup(React.createElement(DeliveryTrack,{stages,index:1,state:'failed',destination:'Docker',animal:'panda'}));
 assert.match(failed,/이미지 빌드 단계에서 멈췄어요/);assert.match(failed,/data-state="failed"/);
});

test('Docker 배포 진행 화면에 리코더 배달이 붙는다',()=>{
 const html=renderToStaticMarkup(React.createElement(DeploymentActivity,{target:'docker',event:{step:'build',message:'빌드 중'}}));
 assert.match(html,/delivery-track/);assert.match(html,/리코더가 배포 박스를 옮기는 중/);assert.match(html,/<img src="data:image\/png/);
});

test('검증된 기반처럼 에이전트가 일하지 않으면 팀 보드를 띄우지 않는다',()=>{
 let v=emptyTeamView();
 assert.equal(teamWorking(v),false);
 v=reduceTeam(v,{step:'consistency',message:'컨테이너 빌드로 확인',job_id:'abc123def456'});
 assert.equal(teamWorking(v),false);
 v=reduceTeam(v,{step:'planning',job_id:'abc123def456'});
 assert.equal(teamWorking(v),true);
});
