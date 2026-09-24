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
  const core={getPort:()=>server.address().port,getSessionToken:()=>'canvas-test-session',refreshToken:async()=>true};
  const provider=new SidebarProvider(vscode.Uri.file(workspace),new ApiClient(core),core,{});
  const webview={postMessage:async m=>{messages.push(m);return true;}};
  const other={postMessage:async m=>{otherMessages.push(m);return true;}};
  provider._view={webview};
  vscode.workspace.workspaceFolders=[{uri:vscode.Uri.file(workspace)}];
  const send=(type,payload={},view=webview)=>provider.handleMessage({type,payload:{requestId:type,...payload}},view);
  const last=type=>messages.findLast(m=>m.type===type)?.payload;
  await send('canvas.prepare',{config});const plan=last('canvas.plan');assert.ok(plan,JSON.stringify(messages));
  assert.equal(plan.targetState.task_definition,'task:7');assert.equal(plan.config.aws_region,'us-east-1');
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
 } finally {
  server.closeAllConnections();await new Promise(r=>server.close(r));
  for(const name of ['index.html','.env'])if(fs.existsSync(path.join(workspace,'dist',name)))fs.unlinkSync(path.join(workspace,'dist',name));
  if(fs.existsSync(path.join(workspace,'dist')))fs.rmdirSync(path.join(workspace,'dist'));fs.rmdirSync(workspace);
 }
});
