const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),React=require('react');
const {DockerNotifications}=require('../out/webview-test/components/canvas/dockerNotifications');

test('Docker completion waits for health, deduplicates results and handles delayed verification',()=>{
 const events=new DockerNotifications();
 assert.equal(events.consume('deploy.progress',{plan_id:'p',step:'done',result:{status:'success'}}),null);
 const pending=events.consume('deployResult',{plan_id:'p',deployment_id:'d',status:'pending',health_ok:false});
 assert.equal(pending.title,'Docker 헬스 확인 대기');
 assert.equal(events.consume('deploy.verificationStatus',{deploymentId:'other',snapshot:{deployment_id:'other',status:'stable'}}),null);
 assert.equal(events.consume('deploy.verificationStatus',{deploymentId:'d',snapshot:{deployment_id:'other',status:'stable'}}),null);
 const complete=events.consume('deploy.verificationStatus',{deploymentId:'d',snapshot:{deployment_id:'d',status:'stable'}});
 assert.equal(complete.title,'Docker 배포 완료');assert.match(complete.detail,/배포 ID: d/);
 assert.equal(events.consume('deployResult',{plan_id:'p',deployment_id:'d',status:'success',health_ok:true}),null);
 assert.equal(events.consume('deploy.verificationStatus',{deploymentId:'d',snapshot:{deployment_id:'d',status:'stable'}}),null);
 assert.equal(events.consume('deployResult',{plan_id:'p',status:'pending'}),null);
});
test('Docker failures and later unhealthy verification are reported once without forwarding raw logs',()=>{
 const events=new DockerNotifications();
 const failure=events.consume('deploy.progress',{plan_id:'p',step:'error',message:'TOKEN=private'});
 assert.equal(failure.title,'Docker 배포 실패');assert.ok(!failure.detail.includes('private'));
 assert.equal(events.consume('deployResult',{plan_id:'p',status:'error',stderr:'TOKEN=private'}),null);
 assert.equal(events.consume('deployResult',{plan_id:'q',deployment_id:'d',status:'success',health_ok:true}).title,'Docker 배포 완료');
 assert.equal(events.consume('deploy.verificationStatus',{deploymentId:'d',snapshot:{deployment_id:'d',status:'unstable',anomalies:[{message:'private'}]}}).title,'Docker 배포 실패');
 assert.equal(events.consume('deploy.verificationStatus',{deploymentId:'d',snapshot:{deployment_id:'d',status:'error'}}),null);
 assert.equal(events.consume('deployResult',{plan_id:'cancel',status:'rejected'}),null);
 assert.equal(events.consume('history.result',{plan_id:'history',status:'success',health_ok:true}),null);
});

// Run the compiled canvas's real message handler and toggle, without a browser or deployment.
function mount(){
 const slots=[],effects=[],messages=[];let cursor=0,tree,receive;
 const postMessage=(type,payload)=>messages.push({type,payload});
 const hooks={...React,useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return [slots[i],next=>slots[i]=typeof next==='function'?next(slots[i]):next];},useRef(initial){const i=cursor++;return slots[i]||(slots[i]={current:initial});},useCallback:fn=>fn,useMemo:fn=>fn(),useEffect(fn){const i=cursor++;if(!(i in slots)){slots[i]=true;effects.push(fn);}}};
 const exports={},base=path.join(__dirname,'../out/webview-test/components/canvas');
 vm.runInNewContext(fs.readFileSync(path.join(base,'DeploymentCanvas.js'),'utf8'),{exports,window:{setInterval(){return 1;},clearInterval(){}},setTimeout(){return 1;},clearTimeout(){},require(id){if(id==='react')return hooks;if(id==='../../hooks/useVSCodeApi')return {useVSCodeApi:()=>({postMessage,useMessage:fn=>receive=fn})};return require(path.resolve(base,id));}});
 const render=()=>{cursor=0;tree=exports.default({onOpenDocker(){}});effects.splice(0).forEach(fn=>fn());};
 function nodes(n=tree){return React.isValidElement(n)?[n,...React.Children.toArray(n.props.children).flatMap(nodes)]:[];}
 render();
 return {messages,emit(type,payload){receive({type,payload});render();},panel(){nodes().find(n=>n.type.name==='Scene').props.onActivate({id:'discord'});render();return nodes().find(n=>n.type.name==='DiscordPanel').props;},events:()=>messages.filter(m=>m.type==='canvas.discord.event')};
}
const status={mode:'oauth',project_id:'a',canvas_project_id:'a',active_channel_id:'channel',authenticated:true};
function snapshot(ui,workspace='/project-a'){
 const request=ui.messages.filter(m=>m.type==='canvas.snapshot').at(-1);
 ui.emit('canvas.snapshotResult',{requestId:request.payload.requestId,snapshot:{workspace,projectName:'A',aws:{ready:false},git:{},deployment:{stage:'idle',running:false},resource:null,scan:null,warnings:[]}});
}
function restore(ui,enabled){const p=ui.messages.filter(m=>m.type==='canvas.discord.notifications').at(-1).payload;ui.emit('canvas.discord.notificationsResult',{...p,enabled});}
test('live canvas sends Docker completion through the existing Discord path only when enabled',()=>{
 const ui=mount();snapshot(ui);ui.emit('canvas.discord.statusResult',status);restore(ui,true);
 assert.equal(ui.panel().enabled,true);
 ui.emit('deployResult',{plan_id:'p',deployment_id:'d',status:'success',health_ok:true});
 assert.equal(ui.events().length,1);assert.equal(ui.events()[0].payload.title,'Docker 배포 완료');
 ui.emit('deploy.progress',{plan_id:'p',step:'done'});ui.emit('deployResult',{plan_id:'p',deployment_id:'d',status:'success',health_ok:true});
 assert.equal(ui.events().length,1);
 ui.emit('canvas.discord.eventResult',{event_id:'docker:p:done'});
 assert.equal(ui.panel().events[0].delivery,'채널 전송 완료');
 ui.panel().onEnabled(false);restore(ui,false);
 ui.emit('deployResult',{plan_id:'q',status:'failed'});assert.equal(ui.events().length,1);
 assert.equal(ui.panel().events[0].delivery,'이 화면에 기록됨');
});
test('notification preference restores in either response order and never enables another selected project',()=>{
 const ui=mount();snapshot(ui);restore(ui,true);assert.equal(ui.panel().enabled,false);
 ui.emit('canvas.discord.statusResult',status);assert.equal(ui.panel().enabled,true);
 ui.emit('canvas.discord.statusResult',{...status,project_id:'b'});assert.equal(ui.panel().enabled,false);
 ui.emit('deployResult',{plan_id:'p',status:'success',health_ok:true});assert.equal(ui.events().length,0);
 ui.emit('canvas.discord.statusResult',status);ui.panel().onEnabled(false);
 ui.emit('canvas.discord.notificationsResult',{requestId:'stale',workspace:'/project-a',enabled:true});
 assert.equal(ui.panel().enabled,false);
});

const Module=require('node:module'),resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const vscode=require('vscode'),{CanvasHost}=require('../out/sidebar/canvasHost');Module._resolveFilename=resolve;
test('notification setting survives host recreation, stays project-scoped and rejects a stale workspace',async t=>{
 const previous=vscode.workspace.workspaceFolders;t.after(()=>vscode.workspace.workspaceFolders=previous);
 const values=new Map(),preferences={get:(key,fallback)=>values.get(key)??fallback,update:async(key,value)=>values.set(key,value)};
 const output=[],send=(host,p)=>host.handle('canvas.discord.notifications',p,(type,payload)=>output.push({type,payload}),()=>'',async()=>assert.fail('must not deploy'));
 vscode.workspace.workspaceFolders=[{uri:{fsPath:'/project-a'}}];
 await send(new CanvasHost({},undefined,undefined,preferences),{workspace:'/project-a',enabled:true});
 const reopened=new CanvasHost({},undefined,undefined,preferences);await send(reopened,{workspace:'/project-a'});
 assert.equal(output.at(-1).payload.enabled,true);
 vscode.workspace.workspaceFolders=[{uri:{fsPath:'/project-b'}}];await send(reopened,{workspace:'/project-b'});assert.equal(output.at(-1).payload.enabled,false);
 await send(reopened,{workspace:'/project-a',enabled:true});assert.equal(output.at(-1).type,'canvas.error');
 await send(reopened,{workspace:'/project-b'});assert.equal(output.at(-1).payload.enabled,false);
});
