const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),Module=require('node:module');
const resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const vscode=require('vscode');
const active=require('../out/activeProject');
const {CanvasHost}=require('../out/sidebar/canvasHost');
Module._resolveFilename=resolve;
const {gateVerdict}=require('../out/webview-test/components/securityGate');

const ok=(scan_type,extra={})=>({scan_type,status:'ok',critical_count:0,high_count:0,medium_count:0,findings:[],...extra});
const fail=(scan_type,reason_code)=>({scan_type,status:'error',reason_code});
const kinds=['trivy','hadolint','gitleaks'];

test('security gate is green only when every scan that could run passed',()=>{
 assert.equal(gateVerdict(kinds,{},false).state,'idle');
 assert.equal(gateVerdict(kinds,{gitleaks:ok('gitleaks')},true).state,'running');
 assert.equal(gateVerdict(kinds,{trivy:ok('trivy'),hadolint:ok('hadolint'),gitleaks:ok('gitleaks')},false).state,'clean');
 // 대상이 아직 없는 검사(이미지 미빌드, Dockerfile 없음)는 초록을 막지 않고 목록으로 알린다.
 const na=gateVerdict(kinds,{trivy:fail('trivy','image_not_found'),hadolint:fail('hadolint','dockerfile_missing'),gitleaks:ok('gitleaks')},false);
 assert.equal(na.state,'clean');assert.deepEqual(na.notApplicable,['trivy','hadolint']);
});
test('any finding turns the gate red; secrets count even without severity',()=>{
 const secret=gateVerdict(kinds,{trivy:ok('trivy'),hadolint:ok('hadolint'),gitleaks:ok('gitleaks',{findings:[{rule:'aws-key'}]})},false);
 assert.equal(secret.state,'issues');assert.match(secret.label,/1건/);
 assert.equal(gateVerdict(kinds,{trivy:ok('trivy',{critical_count:2}),gitleaks:ok('gitleaks')},false).state,'issues');
});
test('scans that could not run are never shown as passed',()=>{
 assert.equal(gateVerdict(kinds,{trivy:fail('trivy','docker_not_running'),hadolint:ok('hadolint'),gitleaks:ok('gitleaks')},false).state,'unverified');
 assert.equal(gateVerdict(kinds,{trivy:ok('trivy'),hadolint:ok('hadolint'),gitleaks:fail('gitleaks','scanner_missing')},false).state,'unverified');
 // 시크릿 검사는 "해당 없음"이 될 수 없다.
 assert.equal(gateVerdict(kinds,{trivy:ok('trivy'),hadolint:ok('hadolint'),gitleaks:fail('gitleaks','image_not_found')},false).state,'unverified');
 assert.equal(gateVerdict(kinds,{gitleaks:ok('gitleaks')},false).state,'unverified');
});

function workspaceFixture(t){
 const base=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-projects-'));t.after(()=>fs.rmSync(base,{recursive:true,force:true}));
 const first=path.join(base,'test temp'),second=path.join(base,'board-app');fs.mkdirSync(first);fs.mkdirSync(second);
 fs.writeFileSync(path.join(second,'index.html'),'<h1>board</h1>');
 const listeners=[],state=new Map(),memento={get:k=>state.get(k),update:async(k,v)=>{v===undefined?state.delete(k):state.set(k,v);}};
 const original=vscode.workspace.onDidChangeWorkspaceFolders;
 vscode.workspace.onDidChangeWorkspaceFolders=fn=>{listeners.push(fn);return{dispose(){}};};
 t.after(()=>{vscode.workspace.onDidChangeWorkspaceFolders=original;});
 vscode.workspace.workspaceFolders=[{uri:vscode.Uri.file(first),name:'test temp'}];
 active.initActiveProject({workspaceState:memento,globalState:memento,subscriptions:[]});
 const add=dir=>{const folder={uri:vscode.Uri.file(dir),name:path.basename(dir)};vscode.workspace.workspaceFolders=[...vscode.workspace.workspaceFolders,folder];listeners.forEach(fn=>fn({added:[folder],removed:[]}));};
 return {first,second,add,memento,state};
}

test('single folder behaves as before; an added project becomes the deploy target',async t=>{
 const w=workspaceFixture(t),changes=[];active.onDidChangeActiveProject(p=>changes.push(p));
 assert.equal(active.activeProjectPath(),w.first);
 w.add(w.second);
 assert.equal(active.activeProjectPath(),w.second);assert.deepEqual(changes.slice(-1),[w.second]);
 const output=[],host=new CanvasHost({getCanvasSnapshot:async()=>({success:true,data:{}})},async()=>({repository:'',branch:'',dirty:false,connected:false,head:'',fingerprint:''}));
 const send=(type,p={})=>host.handle(type,{requestId:'r',...p},(type,payload)=>output.push({type,payload}),()=>'',async()=>assert.fail('no deploy'));
 await send('canvas.snapshot');
 const snap=output.at(-1).payload.snapshot;
 assert.equal(snap.workspace,w.second);assert.equal(snap.projectName,'board-app');
 assert.deepEqual(snap.projects.map(p=>[p.name,p.active]),[['test temp',false],['board-app',true]]);
 await send('canvas.selectProject',{path:w.first});
 assert.equal(output.at(-1).type,'canvas.projectChanged');assert.equal(active.activeProjectPath(),w.first);
 await send('canvas.selectProject',{path:path.join(os.tmpdir(),'not-open')});
 assert.equal(output.at(-1).type,'canvas.error');assert.equal(active.activeProjectPath(),w.first);
});

test('a project added before an extension restart is picked up once, and removal falls back',async t=>{
 const w=workspaceFixture(t);
 await active.markPendingActiveProject(w.second);
 vscode.workspace.workspaceFolders=[...vscode.workspace.workspaceFolders,{uri:vscode.Uri.file(w.second),name:'board-app'}];
 active.initActiveProject({workspaceState:w.memento,globalState:w.memento,subscriptions:[]});
 assert.equal(active.activeProjectPath(),w.second);
 assert.equal(w.state.has('recoder.activeProject.pending'),false);
 vscode.workspace.workspaceFolders=vscode.workspace.workspaceFolders.slice(0,1);
 assert.equal(active.activeProjectPath(),w.first);
});

test('워크스페이스 밖 폴더는 워크스페이스에 넣지 않고 외부 프로젝트로 쓴다(확장 재시작 없음)',async t=>{
 const w=workspaceFixture(t),changes=[];active.onDidChangeActiveProject(p=>changes.push(p));
 const before=vscode.workspace.workspaceFolders;
 const outside=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-external-'));t.after(()=>fs.rmSync(outside,{recursive:true,force:true}));
 await active.useExternalProject(outside);
 assert.equal(vscode.workspace.workspaceFolders,before,'워크스페이스 폴더가 바뀌었다');
 assert.equal(active.activeProjectPath(),outside);assert.deepEqual(changes.slice(-1),[outside]);
 assert.ok(active.isInsideProjectRoot(path.join(outside,'src','a.js')));
 assert.ok(!active.isInsideProjectRoot(path.join(os.tmpdir(),'elsewhere')));
 const listed=active.projectFolders();
 assert.deepEqual(listed.map(p=>[p.name,p.active,!!p.external]),[['test temp',false,false],[path.basename(outside),true,true]]);
 // 다시 시작해도(같은 워크스페이스 상태) 그 선택이 남는다.
 active.initActiveProject({workspaceState:w.memento,globalState:w.memento,subscriptions:[]});
 assert.equal(active.activeProjectPath(),outside);
 // 워크스페이스 폴더로 돌아갈 수 있고, 외부 폴더도 목록에서 다시 고를 수 있다.
 assert.equal(await active.selectActiveProject(w.first),true);assert.equal(active.activeProjectPath(),w.first);
 assert.equal(await active.selectActiveProject(outside),true);assert.equal(active.activeProjectPath(),outside);
 // 폴더가 지워지면 첫 워크스페이스 폴더로 돌아간다.
 fs.rmSync(outside,{recursive:true,force:true});
 assert.equal(active.activeProjectPath(),w.first);
 await assert.rejects(active.useExternalProject(outside));
});
