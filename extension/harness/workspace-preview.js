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
const core = {getPort:()=>Number(process.env.HARNESS_PORT || 18795), getSessionToken:()=>'harness-token-123', ensureRunning:async()=>true, refreshToken:async()=>true,coreInstanceKey:()=> 'fixture-core', getStoredAwsConnection:async()=>null, getAwsRoleArn:async()=>''};
const api = new ApiClient(core);
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
const html = `<!doctype html><html lang="ko" data-recoder-layout="workspace"><meta charset="utf-8"><title>ReCoder production flow fixture</title><style>body{margin:0;background:#181818;color:#ddd;font-family:Segoe UI,sans-serif}button{cursor:pointer}</style><div id="root"></div><script>
window.acquireVsCodeApi=()=>({postMessage:message=>{if(new URLSearchParams(location.search).has('dropGraph')&&message.type==='canvas.graph')return;return fetch('/message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(message)});},getState:()=>null,setState:()=>{}});
setInterval(async()=>{for(const m of await(await fetch('/events')).json())window.dispatchEvent(new MessageEvent('message',{data:m}));},30);
</script><script src="/webview.js"></script></html>`;
http.createServer(async(req,res)=>{
  if(req.url==='/webview.js'){res.setHeader('Content-Type','text/javascript');return fs.createReadStream(path.join(__dirname,'../out/webview/webview.js')).pipe(res);}
  if(req.url==='/events'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify(queue));queue=[];return;}
  if(req.url==='/message'&&req.method==='POST'){
    let data='';for await(const c of req)data+=c;
    const m=JSON.parse(data);console.log('request',m.type);
    receiver(m);res.end('{}');return;
  }
  res.setHeader('Content-Type','text/html;charset=utf-8');res.end(html);
}).listen(Number(process.env.PREVIEW_PORT || 4180),'127.0.0.1',()=>console.log(`Production flow fixture: http://127.0.0.1:${process.env.PREVIEW_PORT || 4180}`));
