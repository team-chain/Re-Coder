// 2.0.48 모니터 · 24시간 감시자 — 화면 계산 · 화면 · 밤사이 요약 · 확장 호스트(실제 SidebarProvider → ApiClient → 루프백 HTTP).
const test=require('node:test'),assert=require('node:assert/strict');
const path=require('node:path'),http=require('node:http'),fs=require('node:fs'),os=require('node:os'),Module=require('node:module');
const resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const vscode=require('vscode');
const {SidebarProvider}=require('../out/sidebar/SidebarProvider.js');
const {ApiClient}=require('../out/core/ApiClient.js');
const {MonitorDigest,pendingCount,unseen,digestText,SEEN_KEY}=require('../out/ops/monitorDigest.js');
Module._resolveFilename=resolve;
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const model=require('../out/webview-test/components/monitorModel');
const {MonitorView}=require('../out/webview-test/components/MonitorPage');
const {HUBS}=require('../out/webview-test/components/Hubs');

const NOW=Date.parse('2026-10-24T08:00:00Z');
const T=(h)=>Math.round(NOW/1000-h*3600);
const SK=(h,r='aa')=>`${new Date(T(h)*1000).toISOString().replace('Z','000+00:00')}#${r}`;
const TD=(r)=>`arn:aws:ecs:ap-northeast-2:1:task-definition/app:${r}`;
const ev=(h,kind,extra={})=>({sk:SK(h,kind),at:new Date(T(h)*1000).toISOString(),ts:T(h),kind,severity:'high',title:kind,detail:`${kind} detail`,source:'alarm',actions:[],...extra});
const cur=(extra={})=>({key:'r/c/app',service:'app',region:'ap-northeast-2',cluster:'c',metrics:true,
 service_state:{status:'ACTIVE',running:2,desired:2,revision:'app:4',task_definition:TD(4)},
 series:{requests:[1,2,30,2],errors:[0,0,5,0],blocked:[0,0,0,0]},now:{rpm:42,error_rate:0.004,points:288},
 watcher:{installed:true,healthy:true,last_tick:T(0.02),ticks:1440,approval_link:true,mail:true,discord:true,stable:'app:3'},
 events:[ev(5,'errors',{actions:[{action:'rollback',status:'offered',target:TD(3),from_td:TD(4),expires:T(-5)}]}),ev(6,'crash')],
 offers:[{event:SK(5,'errors'),title:'오류 증가',at:'x',target:TD(3),from_td:TD(4),label:'app:3 로 롤백',expires:T(-5)}],
 alerts:[{id:'a1',at:new Date(T(4)*1000).toISOString(),ts:T(4),kind:'spike',severity:'high',title:'요청 급증',detail:'d',service:'app',region:'r',status:'open',
   suggestions:[{action:'block_ip',value:'1.2.3.4',label:'1.2.3.4 차단',detail:'d'},{action:'rollback',value:'',label:'롤백',detail:'d'}]},
  {id:'a2',at:new Date(T(9)*1000).toISOString(),ts:T(9),kind:'spike',severity:'high',title:'요청 급증',detail:'d',service:'app',region:'r',status:'applied',suggestions:[]}],
 gaps:[],...extra});

// ── 화면 계산 ──
test('2.0.48 일어난 일: 감시자 기록과 VS Code 알림을 시간순으로, 본 뒤 생긴 것만 「새로」',()=>{
 const items=model.timeline(cur(),SK(5.5));
 assert.deepEqual(items.map(i=>i.id.slice(0,2)),['a:','w:','w:','a:']);
 assert.deepEqual(items.map(i=>i.fresh),[true,true,false,false]);
 assert.equal(model.timeline(cur(),'').some(i=>i.fresh),false); // 처음 보는 사람에게는 「새로」를 붙이지 않는다
});

test('2.0.48 승인 대기: 열린 알림의 대응만(롤백은 감시자 카드와 겹쳐 뺀다) · 요약 문구',()=>{
 const p=model.pendingAlerts(cur());
 assert.equal(p.length,1);assert.deepEqual(p[0].suggestions.map(s=>s.action),['block_ip']);
 const items=model.timeline(cur(),SK(5.5));
 const h=model.hero(cur(),items,2);
 assert.equal(h.title,'새 기록 2건 · 승인 대기 2건');assert.match(h.sub,/지금 app:4/);
 assert.equal(model.hero(cur({events:[],alerts:[],offers:[]}),[],0).title,'이상 없음');
 assert.match(model.hero(cur({watcher:{installed:false}}),[],0).title,/PC 가 꺼지면/);
 assert.match(model.hero(cur({watcher:{installed:true,healthy:false,waiting_first_tick:false,last_tick:T(2)}}),[],0).title,/감시자를 확인/);
 assert.equal(model.minutesAgo(T(0.5),NOW),'30분 전');assert.equal(model.minutesAgo(undefined,NOW),'아직');
});

// ── 화면 ──
test('2.0.48 모니터 화면: 서비스 · 지표 · 일어난 일 · 승인 대기 · 감시자, 이모지 없음',()=>{
 const html=renderToStaticMarkup(React.createElement(MonitorView,{data:{monitors:[{key:'r/c/app',service:'app',region:'r',cluster:'c'}],current:cur(),seen:SK(5.5)},now:NOW}));
 assert.match(html,/모니터/);assert.match(html,/감시자 동작 중/);assert.match(html,/승인 대기 2건/);
 assert.match(html,/data-testid="mon-offer"/);assert.match(html,/app:3 로 롤백/);assert.match(html,/1\.2\.3\.4 차단/);
 assert.equal((html.match(/data-testid="mon-alert"/g)||[]).length,2); // 넓은 화면 칸 + 좁은 화면 칸(CSS 로 하나만 보인다)
 assert.match(html,/data-testid="tile-tasks"[\s\S]*2<small>\/ 2 태스크<\/small>[\s\S]*app:4 · 모두 떠 있어요/);
 assert.match(html,/새로/);assert.match(html,/자동 실행<\/span><b>꺼짐 · 모두 승인 후/);
 assert.doesNotMatch(html,/[\u{1F300}-\u{1FAFF}☀-⛿✅]/u);
 assert.doesNotMatch(html,/rollback-confirm/); // 확인 단계는 누른 뒤에만
});

test('2.0.48 모니터 화면: 감시자 없음 · 앱 없음 · 서비스 없음',()=>{
 const setup=renderToStaticMarkup(React.createElement(MonitorView,{data:{monitors:[],current:cur({watcher:{installed:false},events:[],offers:[],alerts:[]})},now:NOW}));
 assert.match(setup,/data-testid="mon-setup"/);assert.match(setup,/설치 내용 확인/);assert.match(setup,/감시자 없음 · VS Code 가 켜져 있을 때만/);
 const plan={role:{arn:'arn:aws:iam::1:role/recoder-watcher',exists:false},resources:[{kind:'Lambda',name:'x',detail:'d'}],permissions:'p',cost:'c',notes:['역할이 없습니다']};
 const planned=renderToStaticMarkup(React.createElement(MonitorView,{data:{monitors:[],current:cur({watcher:{installed:false}})},plan,now:NOW}));
 assert.match(planned,/recoder-watcher · 없음/);assert.match(planned,/학교 계정은 LabRole/);assert.match(planned,/<button[^>]*aria-disabled="true"[^>]*data-testid="mon-install"/);
 const none=renderToStaticMarkup(React.createElement(MonitorView,{data:{monitors:[],current:null},now:NOW}));
 assert.match(none,/감시 중인 앱이 없어요/);assert.match(none,/배포 캔버스 열기/);
 const gone=renderToStaticMarkup(React.createElement(MonitorView,{data:{monitors:[],current:cur({service_state:{status:'INACTIVE',running:0,desired:0,revision:'',task_definition:''}})},now:NOW}));
 assert.match(gone,/서비스를 찾지 못했어요/);
});

test('2.0.48 위쪽 탭에 Monitor',()=>{ assert.deepEqual(HUBS.map(h=>h.id),['develop','deploy','security','monitor']); });

// ── 밤사이 요약 ──
function memento(init={}){const m=new Map(Object.entries(init));return {get:(k,d)=>m.has(k)?m.get(k):d,update:async(k,v)=>{m.set(k,v);},m};}

test('2.0.48 밤사이 요약: 본 뒤 생긴 기록 · 승인 대기를 알리고, 같은 내용은 두 번 알리지 않는다',async()=>{
 const c=cur();
 assert.equal(pendingCount(c),2);
 assert.equal(unseen(c,{'r/c/app':SK(5.5)},NOW),1);assert.equal(unseen(c,{},NOW),2);
 assert.equal(unseen({key:'k',events:[ev(1,'rollback',{severity:'info'})]},{},NOW),0);
 const source={monitorOverview:async(key)=>key?{monitors:[],current:c}:{monitors:[{key:'r/c/app'}],current:null}};
 const shown=[],badges=[],opened=[];
 const win={showWarningMessage:async(m,b)=>{shown.push(m);return b;},showInformationMessage:async(m)=>{shown.push('I:'+m);}};
 const state=memento({[SEEN_KEY]:{'r/c/app':SK(5.5)}});
 const d=new MonitorDigest(source,state,()=>opened.push(1),b=>badges.push(b),win,0,1e9);
 assert.deepEqual(await d.run(NOW),{fresh:1,pending:2});
 assert.equal(shown.length,1);assert.match(shown[0],/밤사이 1건 · 승인 대기 2건 — app/);
 assert.deepEqual(badges.at(-1),{'r/c/app':2});
 await new Promise(r=>setTimeout(r,5));assert.equal(opened.length,1);
 await d.run(NOW);assert.equal(shown.length,1,'같은 내용은 다시 알리지 않는다');
 const down=new MonitorDigest({monitorOverview:async()=>{throw new Error('core down');}},memento(),()=>{},()=>{},win,0,1e9);
 assert.deepEqual(await down.run(NOW),{fresh:0,pending:0});
 assert.match(digestText([{service:'a',fresh:0,pending:1}]),/새 기록 없음 · 승인 대기 1건/);
 d.dispose();down.dispose();
});

// ── 확장 호스트 ──
test('2.0.48 확장 호스트: 승인 없는 설치 · 롤백은 코어로 보내지 않고, 승인한 것만 보낸 뒤 화면을 새로 그린다',async()=>{
 const workspace=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-monitor-'));
 const requests=[];
 const overview={monitors:[{key:'r/c/app',service:'app',region:'r',cluster:'c'}],current:cur()};
 const server=http.createServer(async(req,res)=>{
  let raw='';for await(const ch of req)raw+=ch;
  const url=new URL(req.url,'http://x');requests.push({path:url.pathname,query:Object.fromEntries(url.searchParams),body:raw?JSON.parse(raw):null});
  res.setHeader('Content-Type','application/json');
  if(url.pathname==='/api/ops/watch/overview')return res.end(JSON.stringify(overview));
  if(url.pathname==='/api/ops/watch/plan')return res.end(JSON.stringify({role:{arn:'a',exists:true},resources:[],permissions:'p',cost:'c',notes:[]}));
  if(url.pathname==='/api/ops/watch/install')return res.end(JSON.stringify({installed:true,message:'설치했습니다',notes:[]}));
  if(url.pathname==='/api/ops/watch/rollback'){res.statusCode=400;return res.end(JSON.stringify({detail:'그 사이 버전이 바뀌었습니다(지금 app:5)'}));}
  res.statusCode=404;res.end('{}');
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 try {
  const core={getPort:()=>server.address().port,getSessionToken:()=>'t',refreshToken:async()=>true};
  const state=memento();
  const provider=new SidebarProvider(vscode.Uri.file(workspace),new ApiClient(core),core,{},undefined,state);
  const msgs=[];const webview={postMessage:async m=>{msgs.push(m);return true;}};
  provider._view={webview};
  const send=(type,payload={})=>provider.handleMessage({type,payload},webview);
  const last=t=>msgs.findLast(m=>m.type===t)?.payload;
  await send('monitor.load',{key:'r/c/app'});
  assert.equal(last('monitor.result').current.key,'r/c/app');assert.equal(last('monitor.result').seen,'');
  assert.equal(last('monitor.badge').count,2);
  await send('monitor.seen',{key:'r/c/app',sk:SK(5)});await send('monitor.seen',{key:'r/c/app',sk:SK(9)}); // 뒤로 가지 않는다
  assert.equal(state.m.get(SEEN_KEY)['r/c/app'],SK(5));
  await send('monitor.load',{key:'r/c/app'});assert.equal(last('monitor.result').seen,SK(5));
  const before=requests.length;
  await send('monitor.watch.install',{key:'r/c/app',approved:false});
  await send('monitor.rollback',{key:'r/c/app',target_td:TD(3),from_td:TD(4)});
  assert.equal(requests.length,before,'승인 없는 요청이 코어로 갔다');
  assert.match(last('monitor.error').message,/승인/);
  await send('monitor.watch.install',{key:'r/c/app',role:'LabRole',discord:'',approved:true});
  const inst=requests.find(r=>r.path==='/api/ops/watch/install');
  assert.deepEqual(inst.body,{key:'r/c/app',role:'LabRole',discord:'',approved:true});
  assert.equal(last('monitor.done').op,'install');assert.equal(requests.at(-1).path,'/api/ops/watch/overview');
  await send('monitor.rollback',{key:'r/c/app',target_td:TD(3),from_td:TD(4),approved:true});
  assert.equal(requests.filter(r=>r.path==='/api/ops/watch/rollback').length,1,'400 은 다시 보내지 않는다');
  assert.match(last('monitor.error').message,/버전이 바뀌었습니다/);assert.equal(last('monitor.error').op,'rollback');
  await send('monitor.badge.get');assert.equal(last('monitor.badge').count,2);
 } finally { server.close(); }
});
