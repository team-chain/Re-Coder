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
const provider = new SidebarProvider(vscode.Uri.file(path.join(__dirname,'..')),api,core,polling,undefined,memento);
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
