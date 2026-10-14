const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const http = require('node:http'), Module = require('node:module');
const resolve = Module._resolveFilename;
Module._resolveFilename = function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const vscode = require('vscode');
const {AnalysisJob} = require('../out/codemap/analysisJob');
const {ApiClient} = require('../out/core/ApiClient');
const {SidebarProvider} = require('../out/sidebar/SidebarProvider');

test('workspace approval survives a hidden sidebar ready and resumes only in its workspace', async()=>{
  let pending={instruction:'test',targetFolder:'',ts:Date.now(),surface:'workspace'};
  const memento={get:()=>pending,update:async(k,value)=>{pending=value;}};
  const messages=[];
  const provider=new SidebarProvider(vscode.Uri.file(__dirname),{}, {}, {getLastHealth:()=>null}, undefined, memento);
  const view={postMessage:async m=>{messages.push(m);return true;}};
  await provider.handleMessage({type:'webview.ready',payload:{layout:'sidebar'}},view);
  assert.ok(pending);assert.equal(messages.filter(m=>m.type==='chat.actionAccepted').length,0);
  await provider.handleMessage({type:'webview.ready',payload:{layout:'workspace'}},view);
  assert.equal(pending,undefined);assert.equal(messages.filter(m=>m.type==='chat.actionAccepted').length,1);
  await provider.handleMessage({type:'webview.ready',payload:{layout:'workspace'}},view);
  assert.equal(messages.filter(m=>m.type==='chat.actionAccepted').length,1);
});

test('real project/file analysis responds without blocking host and supports cancel/retry', async()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-analysis-'));
  fs.writeFileSync(path.join(dir,'index.html'),'<script src="app.js"></script>');
  fs.writeFileSync(path.join(dir,'app.js'),'function render(){loadPosts();} function loadPosts(){return [];} render();');
  const job=new AnalysisJob();
  try {
    const result=job.run(dir);
    let tick=false;setImmediate(()=>{tick=true;});
    const project=await result;assert.ok(tick,'host must stay responsive');
    assert.equal(project.nodes.length,2);assert.deepEqual(project.edges,[{from:'index.html',to:'app.js'}]);
    const file=await job.run(dir,path.join(dir,'app.js'));
    assert.equal(file.kind,'file');assert.deepEqual(file.edges,[{from:'render',to:'loadPosts'}]);
    const cancelled=job.run(dir);job.cancel();await assert.rejects(cancelled,/취소/);
    assert.equal((await job.run(dir)).nodes.length,2);
    await assert.rejects(new AnalysisJob(1).run(dir),/시간이 초과/);
    await assert.rejects(job.run(path.join(dir,'missing')),/ENOENT/);
  }finally{job.cancel();fs.unlinkSync(path.join(dir,'index.html'));fs.unlinkSync(path.join(dir,'app.js'));fs.rmdirSync(dir);}
});

test('headers arriving without a complete body cannot leave development requests pending', async()=>{
  const server=http.createServer((req,res)=>{res.writeHead(200,{'Content-Type':'application/json'});res.write('{"summary":');});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  try {
    const api=new ApiClient({getPort:()=>server.address().port,getSessionToken:()=>'test'});
    const response=await api.request('POST','/api/code/generate',{},false,100);
    assert.equal(response.success,false);assert.match(response.error,/응답 대기 시간/);
  }finally{server.closeAllConnections();await new Promise(r=>server.close(r));}
});

test('exceptions before code API calls still return a correlated error to the requesting webview', async()=>{
  const messages=[];
  const provider=new SidebarProvider(vscode.Uri.file(__dirname),{}, {}, {});
  provider._codeScope=()=>{throw new Error('fixture scope error');};
  await provider.handleMessage({type:'code.plan',payload:{requestId:74,instruction:'test'}},{postMessage:async m=>{messages.push(m);return true;}});
  assert.equal(messages.at(-1).type,'code.error');
  assert.equal(messages.at(-1).payload.requestId,74);assert.match(messages.at(-1).payload.message,/scope error/);
});

test('canvas requests return real source analysis and missing workspace errors to their own view', async()=>{
  const messages=[];
  const provider=new SidebarProvider(vscode.Uri.file(__dirname),{}, {}, {});
  const view={postMessage:async m=>{messages.push(m);return true;}};
  vscode.workspace.workspaceFolders=[{uri:vscode.Uri.file(path.join(__dirname,'../harness'))}];
  await provider.handleMessage({type:'canvas.graph',payload:{requestId:'graph-1'}},view);
  assert.equal(messages.at(-1).type,'canvas.graphResult');assert.equal(messages.at(-1).payload.requestId,'graph-1');
  assert.ok(messages.at(-1).payload.graph.nodes.some(n=>n.name==='vscode-mock.js'));
  vscode.workspace.workspaceFolders=[];
  await provider.handleMessage({type:'canvas.graph',payload:{requestId:'graph-2'}},view);
  assert.equal(messages.at(-1).type,'canvas.error');assert.equal(messages.at(-1).payload.requestId,'graph-2');
  provider.detachWorkspacePanel(view);
});
