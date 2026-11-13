// Production webview + compiled SidebarProvider. Local fixtures only; no cloud calls.
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const resolve = Module._resolveFilename;
Module._resolveFilename = function(name, ...args) {
  return name === 'vscode' ? path.join(__dirname, 'vscode-mock.js') : resolve.call(this, name, ...args);
};
const vscode = require('vscode');
const { SidebarProvider } = require('../out/sidebar/SidebarProvider');
const { ApiClient } = require('../out/core/ApiClient');
const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-workspace-preview-'));
fs.writeFileSync(path.join(workspace, 'index.html'), '<script src="app.js"></script>');
fs.writeFileSync(path.join(workspace, 'app.js'), 'function render() { loadPosts(); }\nfunction loadPosts() { return []; }\nrender();');
vscode.workspace.workspaceFolders = [{uri:vscode.Uri.file(workspace)}];
const core = {getPort:()=>Number(process.env.HARNESS_PORT || 18795), getSessionToken:()=>'harness-token-123', ensureRunning:async()=>true, refreshToken:async()=>true,coreInstanceKey:()=> 'fixture-core', getStoredAwsConnection:async()=>null, getAwsRoleArn:async()=>'', isBrowserAwsLogin:()=>false, getManagedAwsConnection:async()=>undefined};
const api = new ApiClient(core);
// Explicit, offline security UI fixture. Never contacts PentAGI, AWS or a scanner.
if (process.env.HARNESS_SECURITY === '1') {
  vscode.workspace.isTrusted = true;
  // Use the real source-audit host and UI; only the AI response is a fixture.
  vscode.window.showInformationMessage=async(_title,_options,choice)=>choice;
  api.securityAuditStep=async(role)=>{await new Promise(resolve=>setTimeout(resolve,150));return {result:['hunter','critic'].includes(role)?{findings:[]}:{boundaries:[],entrypoints:[],unknowns:[]},provider:'UI fixture only',model:'no paid model call',inputTokens:0,outputTokens:0};};
  let sampleRun=null;
  let redteamPolls=0;
  require('../out/security/redTeamHost').RedTeamHost.prototype.handle=async action=>{
    if(process.env.HARNESS_REDTEAM_PROGRESS==='1'){
      if(action==='localStart'){redteamPolls=0;sampleRun={session:'ui-fixture',target:'http://localhost:3000/ · UI 예시',createdAt:new Date().toISOString(),updatedAt:new Date().toISOString(),phase:'running',step:'UI 예시 · SQL 입력과 대조 응답 검사',tasks:[{id:'1',title:'앱 경로 탐색',status:'finished',result:'UI 예시: /search 입력 확인'},{id:'2',title:'SQL 입력 · 대조 응답 비교',status:'running',result:''}]};}
      if(action==='localStart')Object.assign(sampleRun,{target:'http://localhost:3000/',isolationVerified:true,httpEvents:[{id:1,method:'GET',path:'/search',status:200,durationMs:42,blocked:false},{id:2,method:'GET',path:'/search',status:200,durationMs:58,blocked:false}]});
      if(action==='localStatus'&&sampleRun&&sampleRun.phase==='running'){
        redteamPolls++;
        if(redteamPolls===1)throw Error('UI 예시 · 일시적 연결 끊김');
        if(redteamPolls>=2){sampleRun.phase='removed';sampleRun.outcome='finished';sampleRun.step='UI 예시 · 검사 환경 삭제 완료';sampleRun.tasks[1]={...sampleRun.tasks[1],status:'finished',result:'UI 예시: 구조화된 근거가 없는 과거 결과'};sampleRun.assessment=require('../out/security/pentagiAssessment').assessPentagi(sampleRun.tasks);}
      }
      if(action==='localStop'&&sampleRun){sampleRun.phase='removed';sampleRun.outcome='cancelled';sampleRun.step='UI 예시 · 중단 후 환경 삭제 완료';}
      return {localMode:true,local:sampleRun,localHistory:[]};
    }
    if(action==='localStart') sampleRun={session:'ui-fixture',target:'http://host.docker.internal:3000/',createdAt:new Date().toISOString(),phase:'removed',step:'UI 예시 · 검사 환경 삭제 완료',outcome:'finished',tasks:[{id:'1',title:'보안 헤더 점검 · UI 예시',status:'finished',result:'이 결과는 화면 검증용 예시입니다. 실제 Docker·모델·스캐너를 실행하지 않았습니다.'}]};
    return {localMode:true,local:sampleRun,localHistory:[]};
  };
  api.securityCheck = async kind => {
    await new Promise(resolve => setTimeout(resolve, 300));
    if (kind === 'code') return {score:70, files_checked:12, scope:'UI 검증용 예시 데이터', findings:[
      {rule:'OWNER_CHECK',file:'app.js',line:1,message:'다른 사용자의 주문을 조회할 수 있습니다',severity:'medium',fix:'요청 사용자와 주문 소유자를 비교하세요.',precise:false},
      {rule:'SQL_INJECTION',file:'app.js',line:2,message:'검색 입력이 SQL 쿼리에 직접 들어갑니다',severity:'high',fix:'자리표시자와 값 배열을 사용하세요.',precise:true}]};
    if (kind === 'policy') return {opa_available:false,is_valid:false};
    if (kind === 'recheck') return {status:'not_run',message:'재검사할 배포 SBOM이 없습니다.'};
    return {};
  };
  api.runScan = async scanType => {await new Promise(resolve=>setTimeout(resolve,300));return {scan_type:scanType,status:'ok',findings:[],critical_count:0,high_count:0,medium_count:0};};
  if(process.env.HARNESS_MANAGED==='1') {
    // Explicit UI samples. The real host tests cover consent and dispatch separately.
    require('../out/security/managedSecurityHost').ManagedSecurityHost.prototype.handle=async(_action,_workspace,mode='redteam')=>{
      const run={id:'ui-sample-'+mode,mode,createdAt:new Date().toISOString(),files:12,bytes:1024,status:'SUCCEEDED',terminal:true};
      const report={schema:1,runId:run.id,mode,sourceSha256:'UI sample only',status:'completed',stage:'UI 예시 · 실제 검사를 실행하지 않았습니다',tasks:[]};
      if(mode==='scans')report.scans={checks:['gitleaks','hadolint','dependencies','image'].map(tool=>({tool,status:'completed',count:0,blockingCount:0,findings:[]})),policy:{decision:'allow',reason:'UI 예시 — 실제 정책 평가 아님',scope:'화면 검증용'}};
      if(mode==='audit')report.audit={scope:'UI 예시 · 실제 모델 호출 없음',files:['app.js'],skipped:[],calls:0,inputTokens:0,outputTokens:0,findings:[{title:'주문 소유자 확인 필요 · UI 예시',file:'app.js',line:1,severity:'high',impact:'다른 사용자 데이터 접근 여부 확인',fix:'소유자 검증 추가',verification:'화면 검증용 예시'}],needsValidation:[]};
      if(mode==='redteam'){report.isolationVerified=true;report.cleaned=true;report.tasks=[{id:'ui-task',title:'실행 증거 확인 · UI 예시',status:'finished',result:'화면 표시 검증용 예시입니다. PentAGI 또는 AWS 작업을 실행하지 않았습니다.'}];}
      return {managed:true,connected:true,ready:true,account:'123456789012',region:'ap-northeast-2',stackStatus:'CREATE_COMPLETE',runs:[run],report};
    };
  }
}
const ready = {core_ready:'ok',ai_ready:'ok',docker_ready:'ok',aws_deploy_ready:'ok',ops_ready:'ok',issues:[]};
api.getDiagnostics = async()=>ready;
api.runDiagnostics = async()=>{await new Promise(resolve=>setTimeout(resolve,1000));return ready;};
api.getCanvasSnapshot = async()=>({success:true,data:{aws:{ready:false,account:'',region:''},deployment:{running:false,stage:'idle'},warnings:[]}});
api.getHealth = async()=>({status:'ok',uptime:1,ai_connected:true});
api.getCostSummary = async()=>({total_cost:0,budget_limit:10});
const polling = {start(fn){fn({status:'ok',version:'fixture',uptime:1});},stop(){},poll:async()=>({status:'ok'}),getLastHealth:()=>({status:'ok'})};
const saved = process.env.HARNESS_RESTORE ? {'recoder.pendingChatAction':{instruction:'게시판 생성 - 글 작성/목록/삭제',targetFolder:'',absolutePath:workspace,folderCreated:true,ts:Date.now(),surface:'workspace'}} : {};
const memento = {get:key=>saved[key],update:async(key,value)=>{if(value===undefined)delete saved[key];else saved[key]=value;}};
const secretValues=new Map();
const secrets={get:async key=>secretValues.get(key),store:async(key,value)=>secretValues.set(key,value),delete:async key=>secretValues.delete(key)};
// 2.0.48 모니터 화면 — 감시자 기록 · 승인 대기 픽스처(외부 호출 없음).
if(process.env.HARNESS_MONITOR==='1') {
  const now=Date.now()/1000, iso=t=>new Date(t*1000).toISOString(), sk=(t,r)=>`${new Date(t*1000).toISOString().replace('Z','000+00:00')}#${r}`;
  const td=r=>`arn:aws:ecs:ap-northeast-2:123456789012:task-definition/temp-app:${r}`;
  const events=[{sk:sk(now-6*3600,'a1'),at:iso(now-6*3600),ts:Math.round(now-6*3600),kind:'errors',severity:'high',title:'오류 증가',detail:'서버 오류(5xx) 비율이 5%를 넘었습니다.',source:'alarm',
    actions:[{action:'rollback',label:'temp-app:3 로 롤백',status:'offered',target:td(3),from_td:td(4),expires:Math.round(now+5*3600)}]},
    {sk:sk(now-6.2*3600,'b2'),at:iso(now-6.2*3600),ts:Math.round(now-6.2*3600),kind:'crash',severity:'high',title:'컨테이너가 멈춤',detail:'Essential container in task exited (app 종료 코드 1)',source:'ecs',actions:[]}];
  const current={key:'ap-northeast-2/recoder-cluster/temp-app',service:'temp-app',region:'ap-northeast-2',cluster:'recoder-cluster',metrics:true,
    service_state:{status:'ACTIVE',running:2,desired:2,revision:'temp-app:4',task_definition:td(4)},
    series:{requests:Array.from({length:48},(_,i)=>40+(i===30?400:0)),errors:Array.from({length:48},(_,i)=>i===28?30:1),blocked:Array(48).fill(0)},
    now:{rpm:42,error_rate:0.004,points:288},watcher:{installed:true,healthy:true,last_tick:Math.round(now-60),ticks:1440,approval_link:true,mail:true,discord:true,stable:'temp-app:3'},
    events,offers:[{event:events[0].sk,title:'오류 증가',at:events[0].at,target:td(3),from_td:td(4),label:'temp-app:3 로 롤백',expires:Math.round(now+5*3600)}],alerts:[],gaps:[]};
  api.monitorOverview=async()=>({monitors:[{key:current.key,service:'temp-app',region:'ap-northeast-2',cluster:'recoder-cluster'}],current});
}
if(process.env.HARNESS_DEPLOY==='1') {
  // Real local Git and host handlers, isolated API fixtures. Never publish.
  fs.mkdirSync(path.join(workspace,'docs/adr'),{recursive:true});
  fs.writeFileSync(path.join(workspace,'docs/adr/ADR-001-stack.md'),'# ADR-001: 게시판 설계\n\n- 상태: 승인됨\n\n## 결정\nHTML/CSS/JS');
  fs.writeFileSync(path.join(workspace,'.env'),'TOKEN=fixture-only');
  api.getGithubStatus=async()=>({status:'authenticated',user:'fixture-user'});
  api.listGithubRepos=async()=>({status:'ok',repos:[{name:'fixture-user/board',private:true}]});
  api.githubConnectRepository=async()=>({status:'ok'});
  api.runScan=async(scan_type)=>({status:'ok',scan_type,exit_code:0,findings:[],critical_count:0,high_count:0});
  api.gitPush=async()=>({status:'ok',message:'픽스처 푸시 확인 — 외부 전송 없음'});
  const originalFetch=global.fetch;
  global.fetch=async(url,options)=>String(url).startsWith('https://discord.com/api/webhooks/')
    ? new Response(JSON.stringify(options?.method==='POST'?{id:'fixture-message'}:{type:1,channel_id:'234567890123456789',guild_id:'345678901234567890',name:'fixture-alerts'}),{status:200})
    : originalFetch(url,options);
  vscode.window.showInputBox=async()=> 'https://discord.com/api/webhooks/123456789012345678/'+'a'.repeat(60);
}
const provider = new SidebarProvider(vscode.Uri.file(path.join(__dirname,'..')),api,core,polling,undefined,memento,secrets);
// Claude picker verification: production host/UI, synthetic keys, no paid API calls.
if(process.env.HARNESS_AI_PICKER==='1') {
  saved['recoder.ai.provider']='anthropic';
  secretValues.set('recoder.ai.anthropicKey','sk-ant-fixture-only-not-a-real-key');
  if(process.env.HARNESS_AI_MISSING!=='1') secretValues.set('recoder.ai.openaiKey','sk-fixture-only-not-a-real-key');
  const defaults={anthropic:{model:'claude-sonnet-5-5',fast_model:'claude-haiku-5-5'},openai:{model:'gpt-6-sol',fast_model:'gpt-6-sol'}};
  api.getModelRouting=async()=>{
    const p=saved['recoder.ai.provider'],models=defaults[p];
    return {quality:{provider:p,model:models.model},economy:{spec:'fast',provider:p,model:models.fast_model,separate:true,scrubbed:false,note:''},
      choices:[{spec:'quality',label:'복잡한 작업',ready:true,provider:p,model:models.model},{spec:'fast',label:'간단한 작업',ready:true,provider:p,model:models.fast_model}],
      provider_defaults:defaults,configurable:true,catalog:[...new Set(Object.values(models))].map(model=>({model,ready:true,checked:true})),checked_at:Date.now()};
  };
  api.checkModels=api.getModelRouting;
  api.getModelCosts=async()=>({calls:0,total_cost_usd:0,unknown_cost_calls:0,economy_saved_usd:0,groups:[],days:30,persisted:true});
  api.getModelCompare=async()=>null;
  let restarts=0;
  vscode.commands.executeCommand=async(name,p)=>{
    if(name==='recoder.ai.connect') return provider.changeAiProvider(p,true);
    if(name==='recoder.ai.switch') return provider.changeAiProvider(p);
    if(name==='recoder.restartCore') {await new Promise(resolve=>setTimeout(resolve,1800));return !(process.env.HARNESS_AI_FAIL==='1'&&++restarts===1);}
  };
}
if(process.env.HARNESS_PROGRESS==='1') {
  // Real ApiClient SSE parsing + host/webview wiring. No Docker/AWS side effects.
  const aws={ready:true,region:'ap-northeast-2',account:'123456789012',identity:{account:'123456789012'}};
  api.getAwsStatus=async()=>aws;
  api.getCanvasSnapshot=async()=>({success:true,data:{aws,deployment:{running:false,stage:'idle'},warnings:[]}});
  api.getDeployPreflight=async()=>({blocked:false,reasons:[],warnings:[],summary:'픽스처 검사'});
  api.generateDockerfile=async()=>({proposal_id:'fixture-dockerfile',file_type:'dockerfile',target_path:'Dockerfile',content:'FROM nginxinc/nginx-unprivileged:alpine\nCOPY . /usr/share/nginx/html\n',required_secrets:[],risk_level:'low',risk_reasons:[],approval_level:1});
  api.approveDockerfile=async()=>({status:'saved',file_type:'dockerfile',path:'Dockerfile'});
  api.createDeploymentPlan=async()=>({plan_id:'fixture-local',method:'local_docker',action:'docker_run',image:'fixture:v1',container_name:'fixture',ports:{'18120':'3000'},health_check_path:'/health',risk_level:'medium',risk_reasons:[],approval_level:2});
  api.getVerificationStatus=async()=>null;
  http.createServer(async(req,res)=>{
    if(req.headers['x-session-token']!=='harness-token-123'){res.writeHead(401);res.end();return;}
    let raw='';for await(const c of req)raw+=c;
    const body=JSON.parse(raw||'{}');
    const s3=req.url==='/api/deploy/s3/stream';
    const result=s3?{url:'https://example.test',bucket:'fixture-bucket',region:aws.region,uploaded:body.files.map(f=>f.path)}:{status:'success',health_ok:true,deployment_id:'fixture-deployment'};
    const events=s3?[{step:'plan'},{step:'bucket'},{step:'website'},...body.files.map((f,i)=>({step:'upload',done_count:i+1,total:body.files.length,key:f.path})),{step:'prune'}]:['queued','build','scan','start','health','record'].map(step=>({step,message:`${step} · 격리된 SSE 검증`,plan_id:body.plan_id}));
    events.push({step:'done',result});
    res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache'});
    const timer=setInterval(()=>{const event=events.shift();if(!event){clearInterval(timer);res.end();return;}res.write(`data: ${JSON.stringify(event)}\n\n`);},1500);
    res.on('close',()=>clearInterval(timer));
  }).listen(core.getPort(),'127.0.0.1');
}
let receiver, queue = [];
const webview = {postMessage:async m=>{queue.push(m);return true;},onDidReceiveMessage(fn){receiver=fn;return{dispose(){}};}};
provider.attachWorkspacePanel(webview);
const html = `<!doctype html><html lang="ko" data-recoder-layout="workspace" data-recoder-logo="/logo.png"><meta charset="utf-8"><title>ReCoder production flow fixture</title><style>@font-face{font-family:Pretendard;src:url(/font.woff2) format("woff2");font-weight:100 900}body{margin:0;background:#181818;color:#ddd;font-family:Segoe UI,sans-serif}button{cursor:pointer}</style><body class="${process.env.HARNESS_LIGHT ? "vscode-light" : "vscode-dark"}"><div id="root"></div><script>
window.acquireVsCodeApi=()=>({postMessage:message=>{if(new URLSearchParams(location.search).has('dropGraph')&&message.type==='canvas.graph')return;return fetch('/message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(message)});},getState:()=>(${process.env.HARNESS_AI_PICKER==='1'?JSON.stringify({view:'code'}):'null'}),setState:()=>{}});
setInterval(async()=>{for(const m of await(await fetch('/events')).json())window.dispatchEvent(new MessageEvent('message',{data:m}));},30);
</script><script src="/webview.js"></script></html>`;
http.createServer(async(req,res)=>{
  if(req.url==='/webview.js'){res.setHeader('Content-Type','text/javascript');return fs.createReadStream(path.join(__dirname,'../out/webview/webview.js')).pipe(res);}
  if(req.url==='/font.woff2'){res.setHeader('Content-Type','font/woff2');return fs.createReadStream(path.join(__dirname,'../media/fonts/PretendardVariable.woff2')).pipe(res);}
  if(req.url==='/logo.png'){res.setHeader('Content-Type','image/png');return fs.createReadStream(path.join(__dirname,'../media/icon.png')).pipe(res);}
  if(req.url==='/events'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify(queue));queue=[];return;}
  if(req.url==='/message'&&req.method==='POST'){
    let data='';for await(const c of req)data+=c;
    const m=JSON.parse(data);console.log('request',m.type);
    receiver(m);res.end('{}');return;
  }
  res.setHeader('Content-Type','text/html;charset=utf-8');res.end(html);
}).listen(Number(process.env.PREVIEW_PORT || 4180),'127.0.0.1',()=>console.log(`Production flow fixture: http://127.0.0.1:${process.env.PREVIEW_PORT || 4180}`));
