const test=require('node:test');
const assert=require('node:assert/strict');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');
const {overview,deploymentScene,dropTargetAt,analysisScene,available,colors}=require('../out/webview-test/components/canvas/model.js');
const {Scene}=require('../out/webview-test/components/canvas/Scene.js');
const {ApprovalCard}=require('../out/webview-test/components/canvas/ApprovalCard.js');
const snapshot=(extra={})=>({workspace:'/project',projectName:'real-app',aws:{ready:false,region:'',account:''},git:{repository:'org/repo',branch:'main'},deployment:{running:false,stage:'idle'},resource:null,topology:null,scan:null,s3:null,warnings:[],...extra});
const config={target:'ecs',image_name:'app',tag:'v1',aws_region:'us-east-1',ecs_cluster:'custom-cluster',ecs_service:'custom-service',task_family:'task',container_port:8000,cpu:'256',memory:'512',environment:'staging',dir:''};
const {SnapshotRequests,mergeDeployment}=require('../out/webview-test/components/canvas/state.js');

test('slow AWS snapshots survive multiple polling intervals and retry after errors',()=>{
 const requests=new SnapshotRequests();
 assert.equal(requests.begin('first'),true);
 assert.equal(requests.begin('15-seconds'),false);assert.equal(requests.begin('30-seconds'),false);
 assert.equal(requests.finish('unrelated-error'),false);
 assert.equal(requests.finish('first'),true);
 assert.equal(requests.begin('retry'),true);
 assert.equal(requests.finish('first'),false);
 assert.equal(requests.finish('retry'),true);
});
test('late snapshots cannot resurrect completed deployments or overwrite newer deployment status',()=>{
 const done={deployment_id:'new',running:false,stage:'done',observed_at:'2026-09-23T01:10:00Z',started_at:'2026-09-23T01:00:00Z'};
 assert.equal(mergeDeployment(done,{...done,running:true,stage:'in_progress',observed_at:'2026-09-23T01:09:00Z'}),done);
 assert.equal(mergeDeployment(done,{...done,running:true,stage:'in_progress',observed_at:'2026-09-23T01:11:00Z'}),done);
 assert.equal(mergeDeployment(done,{deployment_id:'old',running:false,stage:'failed',started_at:'2026-09-22T01:00:00Z'}),done);
 assert.equal(mergeDeployment(done,{running:false,stage:'idle'}),done);
 const newer={deployment_id:'next',running:true,stage:'pending',started_at:'2026-09-24T01:00:00Z'};
 assert.equal(mergeDeployment(done,newer),newer);
});

test('AWS disconnected targets are visible, locked and unavailable; Docker and GitHub remain reachable',()=>{
 const s=snapshot(),scene=overview(s,'',false);
 for(const target of ['ecs','s3']) {assert.equal(available(target,s),false);assert.equal(scene.nodes.find(n=>n.target===target).locked,true);}
 for(const target of ['docker','github']) assert.equal(available(target,s),true);
 const html=renderToStaticMarkup(React.createElement(Scene,{...scene,busy:false,onActivate(){},onDrop(){},force2D:true}));
 assert.ok(html.includes('AWS 연결 필요'));assert.ok(html.includes('data-canvas-target="ecs"'));
 assert.ok(html.includes('2D'));assert.ok(!html.includes('https://'));
});
test('connected canvas names and task identities come from the snapshot, not reference illustrations',()=>{
 const s=snapshot({aws:{ready:true,region:'eu-west-1',account:'123'},resource:{cluster:'custom-cluster',service:'custom-service'},s3:{bucket:'my-real-bucket',region:'eu-west-1'},topology:{service:'custom-service',running:1,desired:3,tasks:[{id:'real-task-id',status:'RUNNING',health:'HEALTHY'}]}});
 const {nodes}=overview(s,'alerts',false);
 assert.equal(nodes.find(n=>n.id==='s3').name,'my-real-bucket');
 assert.equal(nodes.find(n=>n.id==='ecs').name,'custom-cluster');
 assert.equal(nodes.filter(n=>n.kind==='task').length,1);
 assert.ok(nodes.some(n=>n.id==='task-real-task-id'));
});

test('drag-first overview keeps all destinations and reveals security or full hierarchy on demand',()=>{
 const s=snapshot({aws:{ready:true,region:'eu-west-1',account:'123'},resource:{cluster:'custom-cluster',service:'custom-service'},topology:{service:'custom-service',running:1,desired:1,tasks:[{id:'real-task',status:'RUNNING',health:'HEALTHY'}]}});
 const simple=deploymentScene(s,'alerts',false);
 assert.deepEqual(simple.nodes.map(n=>n.id).sort(),['discord','docker','ecs','gate','github','project','s3']);
 assert.ok(simple.edges.some(e=>e.from==='project'&&e.to==='gate'));
 assert.ok(deploymentScene(s,'',false).nodes.some(n=>n.id==='discord'&&!n.target));
 assert.equal(simple.nodes.find(n=>n.id==='ecs').name,'custom-cluster');
 assert.ok(deploymentScene(s,'alerts',false,true).nodes.some(n=>n.id==='task-real-task'));
 const blocked=deploymentScene({...s,scan:{blocked:true,tools:[],findings:[]}},'',false);
 assert.equal(blocked.nodes.find(n=>n.id==='gate').color,colors.bad);
 assert.ok(blocked.edges.some(e=>e.to==='ecs'));
});

test('drag hit testing accepts the platform and respects empty space and locked destinations',()=>{
 const {nodes}=deploymentScene(snapshot(),'',false);
 // The platform extends below the clickable foreignObject, so DOM hit tests missed it.
 assert.equal(dropTargetAt(nodes,710,570)?.target,'ecs');
 assert.equal(dropTargetAt(nodes,710,570)?.locked,true);
 assert.equal(dropTargetAt(nodes,750,255)?.target,'github');
 assert.equal(dropTargetAt(nodes,330,535)?.target,'docker');
 assert.equal(dropTargetAt(nodes,450,370),undefined);
 assert.equal(dropTargetAt(nodes,180,245),undefined);
});
test('large graph folders preserve all files, search and actual graph edges',()=>{
 const nodes=Array.from({length:100},(_,i)=>({id:`src/f${i}.ts`,name:`f${i}`,flags:i===0?['orphan']:i===1?['overloaded']:[],in_degree:0,out_degree:1}));
 const graph={kind:'project',nodes,edges:[{from:nodes[0].id,to:nodes[1].id}],findings:[]};
 const grouped=analysisScene(graph,null,'');assert.equal(grouped.grouped,true);assert.equal(grouped.nodes.length,1);
 const expanded=analysisScene(graph,'src','');assert.equal(expanded.nodes.length,100);assert.equal(expanded.edges.length,1);
 assert.equal(expanded.nodes[0].color,colors.bad);assert.equal(expanded.nodes[1].color,colors.warn);
 assert.equal(analysisScene(graph,null,'f99').nodes[0].id,'src/f99.ts');
});
test('approval discloses exact target, rollback and unknown budget; cross-region requires separate acknowledgement',()=>{
 const plan={id:'p',config,projectName:'my-app',repository:'org/repo',branch:'main',commit:'sha',dirty:true,account:'123',coreRegion:'ap-northeast-2',preflight:{blocked:false},targetState:{exists:true,task_definition:'td:previous',images:[{image:'app:v0',digest:'sha256:previous'}],budget:null,warnings:[]}};
 const html=renderToStaticMarkup(React.createElement(ApprovalCard,{plan,rollback:'td:previous',onApprove(){},onCancel(){},onFix(){}}));
 for(const text of ['custom-cluster','custom-service','td:previous','조회되지 않았습니다','이 리전으로 배포','disabled','role="dialog"']) assert.ok(html.includes(text),text);
});
test('security overlay does not invent secret findings or claim a confirmed commit/exposure',()=>{
 const clean=overview(snapshot(),'',true);assert.ok(!clean.edges.some(e=>e.color===colors.bad));
 const found=overview(snapshot({scan:{findings:[{tool:'gitleaks'}],tools:[],blocked:true}}),'',true);
 const edge=found.edges.find(e=>e.color===colors.bad);assert.ok(edge);assert.equal(edge.dashed,true);
});

const Module=require('node:module'),path=require('node:path');
const resolve=Module._resolveFilename;
Module._resolveFilename=function(request,...args){return request==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,request,...args);};
const vscode=require('vscode');
const {CanvasHost,validateConfig}=require('../out/sidebar/canvasHost.js');
Module._resolveFilename=resolve;
function setup() {
 vscode.workspace.workspaceFolders=[{uri:{fsPath:'/project'}}];
 const output=[],executions=[],pushes=[];
 let git={repository:'org/repo',branch:'main',head:'sha',dirty:false,connected:true,fingerprint:'one'};
 const api={getAwsStatus:async()=>({ready:true,identity:{account:'123'},region:'us-east-1'}),getDeployPreflight:async()=>({blocked:false,evidence:[]}),checkAwsPermissions:async()=>({ready:true,permission_check:{inspected:true,missing_actions:[]}}),runScan:async()=>({status:'ok',critical_count:0,findings:[]}),gitPush:async p=>{pushes.push(p);return {status:'ok'};}};
 const host=new CanvasHost(api,async()=>git);
 api.getCanvasTarget=async()=>({exists:true,task_definition:'task:1',images:[],budget:null,warnings:[]});
 const send=async(type,p={})=>host.handle(type,{requestId:type,...p},(type,payload)=>output.push({type,payload}),()=> 'project',async(type,payload)=>executions.push({type,payload}));
 return {api,host,send,output,executions,pushes,setGit(value){git={...git,...value};},plan:()=>output.findLast(e=>e.type==='canvas.plan')?.payload};
}
test('prepare never deploys; explicit approval executes once through the existing ECS path',async()=>{
 const s=setup();await s.send('canvas.prepare',{config});assert.equal(s.executions.length,0);
 const id=s.plan().id;await s.send('canvas.execute',{planId:id,approved:true});await s.send('canvas.execute',{planId:id,approved:true});
 assert.equal(s.executions.length,1);assert.equal(s.executions[0].type,'workspace.deploy.ecs');assert.equal(s.executions[0].payload.ecs_service,'custom-service');
});
test('only boolean approval is accepted and concurrent requests consume a plan once',async()=>{
 const s=setup();await s.send('canvas.prepare',{config});const id=s.plan().id;
 for(const approved of [undefined,false,'false','true',1,{}])await s.send('canvas.execute',{planId:id,approved});
 assert.equal(s.executions.length,0);
 await Promise.all([s.send('canvas.execute',{planId:id,approved:true}),s.send('canvas.execute',{planId:id,approved:true})]);
 assert.equal(s.executions.length,1);
});
test('approval tokens are isolated per webview and expire after ten minutes',async()=>{
 const s=setup(),other=setup();await s.send('canvas.prepare',{config});const id=s.plan().id;
 await other.send('canvas.execute',{planId:id,approved:true});assert.equal(other.executions.length,0);
 const now=Date.now;try {const future=now()+11*60*1000;Date.now=()=>future;await s.send('canvas.execute',{planId:id,approved:true});}finally {Date.now=now;}
 assert.equal(s.executions.length,0);assert.ok(s.output.at(-1).payload.message.includes('유효 시간'));
});
test('cancel, changed workspace, changed branch and changed AWS account invalidate approval',async()=>{
 for(const mode of ['cancel','workspace','branch','account']) {
  const s=setup();await s.send('canvas.prepare',{config});const id=s.plan().id;
  if(mode==='cancel')await s.send('canvas.cancel',{planId:id});
  if(mode==='workspace')vscode.workspace.workspaceFolders=[{uri:{fsPath:'/other'}}];
  if(mode==='branch')s.setGit({fingerprint:'different'});
  if(mode==='account')s.api.getAwsStatus=async()=>({ready:true,identity:{account:'other'}});
  await s.send('canvas.execute',{planId:id,approved:true});assert.equal(s.executions.length,0,mode);
 }
});
test('changed preflight or denied permissions block the new entry point',async()=>{
 const s=setup();await s.send('canvas.prepare',{config});s.api.getDeployPreflight=async()=>({blocked:true,reasons:[{message:'secret'}]});await s.send('canvas.execute',{planId:s.plan().id,approved:true});assert.equal(s.executions.length,0);assert.equal(s.output.at(-1).type,'canvas.blocked');
 const t=setup();await t.send('canvas.prepare',{config});t.api.checkAwsPermissions=async()=>({ready:true,permission_check:{inspected:true,missing_actions:['ecs:UpdateService']}});await t.send('canvas.execute',{planId:t.plan().id,approved:true});assert.equal(t.executions.length,0);
});
test('GitHub push explicitly disables automatic commit and force; scanner failure prevents push',async()=>{
 const s=setup();await s.send('canvas.prepare',{config:{...config,target:'github'}});await s.send('canvas.execute',{planId:s.plan().id,approved:true});assert.deepEqual(s.pushes,[{workspace_path:'/project',branch:'main',force:false,auto_commit:false}]);
 const t=setup();await t.send('canvas.prepare',{config:{...config,target:'github'}});t.api.runScan=async()=>({status:'not_run',exit_code:0});await t.send('canvas.execute',{planId:t.plan().id,approved:true});assert.equal(t.pushes.length,0);
});
test('invalid requests cannot silently adopt a region or invalid resource size',()=>{
 assert.throws(()=>validateConfig({...config,aws_region:''}));assert.throws(()=>validateConfig({...config,container_port:0}));assert.throws(()=>validateConfig({...config,cpu:'256',memory:'8192'}));assert.throws(()=>validateConfig({...config,tag:'latest'}));
});
test('service revision changes after review require a new approval',async()=>{
 const s=setup();await s.send('canvas.prepare',{config});
 s.api.getCanvasTarget=async()=>({exists:true,task_definition:'task:other',images:[],budget:null,warnings:[]});
 await s.send('canvas.execute',{planId:s.plan().id,approved:true});
 assert.equal(s.executions.length,0);assert.ok(s.output.at(-1).payload.message.includes('태스크 정의'));
});
test('slower old prepare cannot invalidate the approval currently visible to the user',async()=>{
 const s=setup();let release;
 s.api.getDeployPreflight=()=>new Promise(resolve=>{release=resolve;});
 const first=s.send('canvas.prepare',{requestId:'old',config});
 s.api.getDeployPreflight=async()=>({blocked:false});
 await s.send('canvas.prepare',{requestId:'new',config:{...config,ecs_service:'new-service'}});
 const id=s.plan().id;release({blocked:false});await first;
 assert.equal(s.plan().id,id);await s.send('canvas.execute',{planId:id,approved:true});
 assert.equal(s.executions[0].payload.ecs_service,'new-service');
});
test('GitHub push remains possible without deployment infrastructure preflight',async()=>{
 const s=setup();s.api.getDeployPreflight=async()=>{throw new Error('deployment-only configuration missing');};
 s.api.getAwsStatus=async()=>{throw new Error('AWS must not be needed for GitHub');};
 await s.send('canvas.prepare',{config:{...config,target:'github'}});
 await s.send('canvas.execute',{planId:s.plan().id,approved:true});assert.equal(s.pushes.length,1);
});
test('S3 approval selects the build directory and binds the actual filtered file contents',async()=>{
 const fs=require('node:fs'),os=require('node:os');
 const workspace=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-canvas-test-'));
 try {
  fs.mkdirSync(path.join(workspace,'dist'));fs.writeFileSync(path.join(workspace,'dist/index.html'),'<h1>one</h1>');fs.writeFileSync(path.join(workspace,'dist/.env'),'PRIVATE=fixture');
  const s=setup();vscode.workspace.workspaceFolders=[{uri:{fsPath:workspace}}];
  await s.send('canvas.prepare',{config:{...config,target:'s3'},autoDir:true});
  assert.equal(s.plan().config.dir,'dist');assert.deepEqual(s.plan().staticSite.files,['index.html']);assert.ok(s.plan().staticSite.excluded.includes('.env'));
  fs.writeFileSync(path.join(workspace,'dist/index.html'),'<h1>changed</h1>');
  await s.send('canvas.execute',{planId:s.plan().id,approved:true});assert.equal(s.executions.length,0);assert.ok(s.output.at(-1).payload.message.includes('정적 파일'));
  await s.send('canvas.prepare',{config:{...config,target:'s3'},autoDir:true});
  await s.send('canvas.execute',{planId:s.plan().id,approved:true});assert.equal(s.executions[0].type,'workspace.deploy.s3');assert.equal(s.executions[0].payload.dir,'dist');
  await s.send('canvas.prepare',{config:{...config,target:'s3',dir:path.join(workspace,'dist')}});
  assert.equal(s.plan().config.dir,'dist');
  await s.send('canvas.execute',{planId:s.plan().id,approved:true});assert.equal(s.executions[1].payload.dir,'dist');
 } finally {fs.unlinkSync(path.join(workspace,'dist/.env'));fs.unlinkSync(path.join(workspace,'dist/index.html'));fs.rmdirSync(path.join(workspace,'dist'));fs.rmdirSync(workspace);}
});
