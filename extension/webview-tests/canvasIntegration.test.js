// Real SidebarProvider -> CanvasHost -> existing handlers -> ApiClient -> loopback HTTP.
// The HTTP boundary is a fixture: no cloud operations or credentials are used.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),http=require('node:http'),Module=require('node:module');
const resolve=Module._resolveFilename;
Module._resolveFilename=function(request,...args){return request==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,request,...args);};
const vscode=require('vscode');
const {SidebarProvider}=require('../out/sidebar/SidebarProvider.js');
const {ApiClient}=require('../out/core/ApiClient.js');
Module._resolveFilename=resolve;
const config={target:'ecs',image_name:'fixture-app',tag:'v3',aws_region:'us-east-1',ecs_cluster:'chosen-cluster',ecs_service:'chosen-service',task_family:'chosen-task',container_port:8080,cpu:'256',memory:'512',environment:'staging',dir:''};

test('canvas approval reaches existing ECS/S3 HTTP contracts with workspace, region and filtered files intact',async()=>{
 const workspace=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-canvas-integration-'));
 const requests=[],messages=[],otherMessages=[];
 let deny=false;
 const aws={ready:true,identity:{account:'123456789012'},region:'ap-northeast-2',permission_check:{inspected:true,missing_actions:[]}};
 const server=http.createServer(async(req,res)=>{
  let raw='';for await(const chunk of req)raw+=chunk;
  const body=raw?JSON.parse(raw):null,url=new URL(req.url,'http://localhost');
  requests.push({path:url.pathname,query:url.searchParams,method:req.method,body,token:req.headers['x-session-token']});
  res.setHeader('Content-Type','application/json');
  const reply=(data)=>res.end(JSON.stringify(data));
  if(url.pathname==='/api/aws/status'||url.pathname==='/api/aws/permissions/check')return reply(aws);
  if(url.pathname==='/api/deploy/preflight')return reply({blocked:false,reasons:[],warnings:[]});
  if(url.pathname==='/api/deploy/canvas/target')return reply({exists:true,task_definition:'task:7',images:[],budget:null,warnings:[]});
  if(url.pathname==='/api/deploy/ecs') {
   if(deny){res.statusCode=403;return reply({detail:{error:'policy_denied',message:'fixture policy denied',deny:['fixture'],fix:'Pin image'}});}
   return reply({deployment_id:'accepted-fixture',message:'accepted'});
  }
  if(url.pathname==='/api/deploy/s3/stream'){
   res.setHeader('Content-Type','text/event-stream');
   res.write('data: '+JSON.stringify({step:'upload',message:'uploading',done_count:1,total:1})+'\n\n');
   return res.end('data: '+JSON.stringify({step:'done',result:{url:'https://example.test',bucket:'fixture-bucket',region:body.region,uploaded:body.files.map(f=>f.path)}})+'\n\n');
  }
  res.statusCode=404;reply({error:'Unexpected fixture route'});
 });
 try {
  fs.mkdirSync(path.join(workspace,'dist'));fs.writeFileSync(path.join(workspace,'dist/index.html'),'<h1>verified</h1>');fs.writeFileSync(path.join(workspace,'dist/.env'),'SECRET=fixture');
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  let managedConnections=0;
  const core={getPort:()=>server.address().port,getSessionToken:()=>'canvas-test-session',refreshToken:async()=>true,getManagedAwsConnection:async()=>{managedConnections++;throw Error('Default deployment must not ask for a managed connection');}};
  const provider=new SidebarProvider(vscode.Uri.file(workspace),new ApiClient(core),core,{},undefined,{get:(_key,fallback)=>fallback,update:async()=>{}});
  const webview={postMessage:async m=>{messages.push(m);return true;}};
  const other={postMessage:async m=>{otherMessages.push(m);return true;}};
  provider._view={webview};
  vscode.workspace.workspaceFolders=[{uri:vscode.Uri.file(workspace)}];
  const send=(type,payload={},view=webview)=>provider.handleMessage({type,payload:{requestId:type,...payload}},view);
  const last=type=>messages.findLast(m=>m.type===type)?.payload;
  await send('canvas.prepare',{config});const plan=last('canvas.plan');assert.ok(plan,JSON.stringify(messages));
  assert.equal(plan.targetState.task_definition,'task:7');assert.equal(plan.config.aws_region,'us-east-1');
  assert.equal(managedConnections,0,'default ECS prepare must retain the original connection/deployment path');
  assert.equal(requests.some(r=>r.path==='/api/deploy/ecs'),false);
  await send('canvas.execute',{planId:plan.id,approved:true},other);
  assert.equal(otherMessages.at(-1).type,'canvas.error');
  await send('canvas.execute',{planId:plan.id,approved:true});
  assert.equal(last('workspace.deploy.result').deployment_id,'accepted-fixture');
  const ecs=requests.find(r=>r.path==='/api/deploy/ecs');
  assert.equal(ecs.body.workspace_path,workspace);assert.equal(ecs.body.aws_region,'us-east-1');assert.equal(ecs.body.ecs_service,'chosen-service');
  const permissions=requests.find(r=>r.path==='/api/aws/permissions/check').body.deployment_context;
  assert.equal(permissions.ecs_cluster,'chosen-cluster');assert.equal(permissions.aws_region,'us-east-1');
  await send('canvas.execute',{planId:plan.id,approved:true});
  assert.equal(requests.filter(r=>r.path==='/api/deploy/ecs').length,1);
  deny=true;await send('canvas.prepare',{config});await send('canvas.execute',{planId:last('canvas.plan').id,approved:true});
  assert.equal(messages.at(-1).type,'workspace.deploy.ecs.policyDenied');
  assert.equal(requests.filter(r=>r.path==='/api/deploy/ecs').length,2,'policy denial must not retry deployment');
  await send('canvas.prepare',{config:{...config,target:'s3',dir:path.join(workspace,'dist')}});
  assert.equal(last('canvas.plan').config.dir,'dist');
  await send('canvas.execute',{planId:last('canvas.plan').id,approved:true});
  const s3=requests.find(r=>r.path==='/api/deploy/s3/stream');
  assert.equal(s3.body.region,'us-east-1');assert.deepEqual(s3.body.files.map(f=>f.path),['index.html']);assert.equal(s3.body.files[0].content,'<h1>verified</h1>');
  assert.equal(last('workspace.deploy.s3.result').ok,true,JSON.stringify(messages.at(-1)));
  assert.ok(last('workspace.deploy.s3.result').result.excluded_note.includes('.env'));
  assert.ok(last('workspace.deploy.s3.progress'));
  assert.ok(requests.every(r=>r.token==='canvas-test-session'));

  // Managed build output must use the SAME approval and S3 HTTP path; never publish local stale dist.
  fs.writeFileSync(path.join(workspace,'package.json'),JSON.stringify({scripts:{build:'vite build'}}));
  fs.utimesSync(path.join(workspace,'dist','index.html'),new Date(0),new Date(0));
  const managedDir='.recoder/cloud-build-fixture';fs.mkdirSync(path.join(workspace,managedDir),{recursive:true});
  let builds=0;
  provider._canvasHosts.get(webview).frontendBuilder=async(root,project,progress,region)=>{
   assert.equal(root,workspace);assert.equal(region,'us-east-1');assert.equal(project.tool,'vite');builds++;
   fs.writeFileSync(path.join(workspace,managedDir,'index.html'),'<h1>managed output</h1>');progress('fixture managed build complete');
   return {ok:true,via:'managed',outDir:managedDir,output:'verified'};
  };
  await send('canvas.prepare',{config:{...config,target:'s3',dir:''},autoDir:true});
  assert.equal(last('canvas.plan').config.dir,path.normalize(managedDir));assert.equal(builds,1);
  fs.writeFileSync(path.join(workspace,managedDir,'index.html'),'<h1>changed after approval</h1>');
  await send('canvas.execute',{planId:last('canvas.plan').id,approved:true});
  assert.equal(requests.filter(r=>r.path==='/api/deploy/s3/stream').length,1,'changed managed output must not upload');
  await send('canvas.prepare',{config:{...config,target:'s3',dir:''},autoDir:true});
  await send('canvas.execute',{planId:last('canvas.plan').id,approved:true});
  assert.equal(requests.filter(r=>r.path==='/api/deploy/s3/stream').length,2);
  assert.equal(requests.findLast(r=>r.path==='/api/deploy/s3/stream').body.files[0].content,'<h1>managed output</h1>');
  assert.equal(fs.readFileSync(path.join(workspace,'dist','index.html'),'utf8'),'<h1>verified</h1>');

  // A cloud image uses the same native approval -> host -> authenticated HTTP path.
  // Neither canvas.prepare nor a forged execute payload can replace its host-owned reference.
  deny=false;
  const managedRef={run_id:'rc-rt-'+'a'.repeat(24),build_id:'recoder-container-v1:11111111-2222-3333-4444-555555555555',source_sha256:'b'.repeat(64)};
  provider._canvasHosts.get(webview).containerBuilder=async(root,region,progress)=>{assert.equal(root,workspace);assert.equal(region,'us-east-1');progress('fixture image inspected');return managedRef;};
  await send('canvas.prepare',{config:{...config,managed_build:{passed:true}}});
  const managedPlan=last('canvas.plan');assert.ok(managedPlan.id);
  await send('canvas.execute',{planId:managedPlan.id,approved:true,managed_build:{passed:true}});
  const managedEcs=requests.findLast(r=>r.path==='/api/deploy/ecs').body;
  assert.deepEqual(managedEcs.managed_build,managedRef);assert.equal(managedEcs.repo_name,'recoder-managed-images-v1');
  assert.equal(managedEcs.approved,true);assert.equal(managedEcs.workspace_path,workspace);
 } finally {
  server.closeAllConnections();await new Promise(r=>server.close(r));
  assert.equal(path.dirname(path.resolve(workspace)),os.tmpdir());fs.rmSync(workspace,{recursive:true,force:true});
 }
});
