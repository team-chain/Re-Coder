const test=require('node:test'),assert=require('node:assert/strict');
const path=require('node:path'),Module=require('node:module');
const resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const {AlertWatcher,alertMessage}=require('../out/ops/alertWatcher');
const {validateConfig}=require('../out/sidebar/canvasHost');
Module._resolveFilename=resolve;
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {EcsProtection,protectionRequest,DEFAULT_PROTECTION}=require('../out/webview-test/components/EcsProtection');
const {OpsAlertCard}=require('../out/webview-test/components/OpsAlertsPanel');
const {ApprovalCard}=require('../out/webview-test/components/canvas/ApprovalCard');

const A=(id,extra={})=>({id,at:'2026-10-14T00:00:00Z',kind:'spike',severity:'high',title:'요청 급증',detail:'최근 5분 요청 38,412회',region:'us-east-1',cluster:'c',service:'shop',status:'open',
 sources:{ips:[{value:'8.8.4.4',count:27000}],paths:[{value:'/api/auth/login',count:24000}]},suggestions:[{action:'block_ip',value:'8.8.4.4',label:'8.8.4.4 차단',detail:'d'}],...extra});
function memento(){const m=new Map();return {get:(k,d)=>m.has(k)?m.get(k):d,update:async(k,v)=>{m.set(k,v);},m};}

test('2.0.25 알림 감시: 처음 켤 때 쌓인 알림은 띄우지 않고, 이후 새 알림만 VS Code·디스코드로',async()=>{
 let pages=[[A('a1'),A('a2')],[A('a3'),A('a4',{status:'applied'})],[]];const asked=[];
 const source={listOpsAlerts:async(since)=>{asked.push(since);return {alerts:pages.shift()||[]};}};
 const shown=[],sent=[],opened=[];const state=memento();
 const win={showWarningMessage:async(m,...b)=>{shown.push(m);return '자세히';},showErrorMessage:async(m)=>{shown.push('E:'+m);}};
 const w=new AlertWatcher(source,state,{send:async(t,d)=>{sent.push([t,d]);}},a=>opened.push(a.id),60000,win);
 assert.equal(await w.tick(),0);assert.equal(state.m.get('recoder.ops.alerts.last'),'a2');assert.deepEqual(shown,[]);
 assert.equal(await w.tick(),1);assert.deepEqual(asked,['','a2']);
 assert.equal(shown.length,1);assert.match(shown[0],/요청 급증 — shop \(us-east-1\)/);
 assert.equal(sent.length,1);assert.match(sent[0][1],/상위 IP 8\.8\.4\.4/);assert.match(sent[0][1],/VS Code 에서 승인/);
 await new Promise(r=>setTimeout(r,5));assert.deepEqual(opened,['a3']);
 assert.equal(state.m.get('recoder.ops.alerts.last'),'a4');
 const broken=new AlertWatcher({listOpsAlerts:async()=>{throw new Error('down');}},memento(),null,()=>{},60000,win);
 assert.equal(await broken.tick(),0);
 const crit=alertMessage(A('x',{kind:'outage',severity:'critical',title:'장애 — 실행 태스크 부족',sources:{},suggestions:[]}));
 assert.equal(crit.title,'[ReCoder] 장애 — 실행 태스크 부족 — shop (us-east-1)');
});

test('2.0.25 보호·운영 설정: 기본은 방패 보통·알림 켬, 메일 형식 검사, 끄면 메일을 보내지 않는다',()=>{
 assert.deepEqual(protectionRequest(DEFAULT_PROTECTION),{shield:true,shield_level:'normal',alerts:true,alert_email:''});
 assert.throws(()=>protectionRequest({...DEFAULT_PROTECTION,alert_email:'nope'}),/메일/);
 assert.deepEqual(protectionRequest({...DEFAULT_PROTECTION,alerts:false,alert_email:'bad'}),{shield:true,shield_level:'normal',alerts:false,alert_email:''});
 const html=renderToStaticMarkup(React.createElement(EcsProtection,{value:{...DEFAULT_PROTECTION,shield_level:'strict'},onChange(){}}));
 assert.match(html,/기본 방패/);assert.match(html,/IP당 초당 5회/);assert.match(html,/checked="" value="strict"/);assert.match(html,/알림 메일/);
 const off=renderToStaticMarkup(React.createElement(EcsProtection,{value:{...DEFAULT_PROTECTION,shield:false,alerts:false},onChange(){}}));
 assert.doesNotMatch(off,/방패 강도/);assert.doesNotMatch(off,/알림 메일/);
});

test('2.0.25 캔버스: 보호 설정을 검사해 배포 요청과 승인 카드에 싣는다',()=>{
 const base={target:'ecs',image_name:'shop',tag:'v1',aws_region:'us-east-1',ecs_cluster:'recoder-cluster',ecs_service:'shop',task_family:'recoder-task',container_port:3000,cpu:'256',memory:'512',environment:'staging',dir:''};
 const c=validateConfig({...base,shield:true,shield_level:'loose',alerts:true,alert_email:'me@example.com'});
 assert.equal(c.shield,true);assert.equal(c.shield_level,'loose');assert.equal(c.alert_email,'me@example.com');
 assert.throws(()=>validateConfig({...base,shield_level:'max'}),/방패 강도/);
 assert.throws(()=>validateConfig({...base,alert_email:'x@'}),/메일/);
 assert.throws(()=>validateConfig({...base,shield:'yes'}),/true\/false/);
});

test('2.0.25 운영 화면 알림 카드: 원인 단서와 승인할 대응만 보인다',()=>{
 let chosen=null;
 const html=renderToStaticMarkup(React.createElement(OpsAlertCard,{alert:A('a1'),onChoose:s=>{chosen=s;}}));
 assert.match(html,/요청 급증/);assert.match(html,/8\.8\.4\.4/);assert.match(html,/\/api\/auth\/login/);assert.match(html,/8\.8\.4\.4 차단<\/button>/);
 const done=renderToStaticMarkup(React.createElement(OpsAlertCard,{alert:A('a2',{status:'applied',resolution:'방패에 차단 IP 를 넣은 새 버전으로 교체를 시작했습니다'}),onChoose(){}}));
 assert.doesNotMatch(done,/<button/);assert.match(done,/대응함/);assert.match(done,/교체를 시작했습니다/);
});
