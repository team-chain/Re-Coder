const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto'),Module=require('node:module');
const {frontendRequest,frontendManifest,safeArtifactPath}=require('../out/security/managedFrontendWorker');
const vscode={workspace:{isTrusted:true},window:{},ProgressLocation:{Notification:15}};
const original=Module._load;Module._load=function(name,...args){return name==='vscode'?vscode:original.call(this,name,...args);};
const {downloadFrontend,frontendBundle,ManagedFrontendHost}=require('../out/deploy/managedFrontend');Module._load=original;
const {ManagedJobs}=require('../out/security/managedJobs');
const {ManagedAwsClient}=require('../out/cloud/managedAwsClient');
const {managedTemplate}=require('../out/security/managedTemplate');
const {managedPermissions}=require('../out/cloud/managedPermissions');
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
const run={id:'rc-rt-'+'a'.repeat(24),digest:'b'.repeat(64),status:'SUCCEEDED',terminal:true};
const manifest=(files=[{path:'index.html',size:2,sha256:hash('ok')}])=>({schema:1,runId:run.id,sourceSha256:run.digest,status:'completed',files});
const makeRoot=()=>fs.mkdtemp(path.join(os.tmpdir(),'recoder-cloud-build-'));
const remove=async root=>{assert.equal(path.dirname(root),os.tmpdir());await fs.rm(root,{recursive:true,force:true});};

test('build manifests reject traversal, Windows aliases, collisions, missing index, mismatched run and digest',()=>{
 for(const p of ['../index.html','/index.html','C:/file','x\\file','x/./file','.env','NUL.txt','x/CON','x.','foo:stream','a//b'])assert.equal(safeArtifactPath(p),false,p);
 assert.ok(safeArtifactPath('assets/한글 파일.js'));
 const good=manifest();assert.equal(frontendManifest(good,run.id,run.digest),good);
 for(const bad of [{...good,runId:'other'},{...good,sourceSha256:'c'.repeat(64)},{...good,status:'failed'},manifest([]),manifest([{...good.files[0],path:'logo.png'}]),manifest([...good.files,{...good.files[0],path:'INDEX.html'}]),manifest([...good.files,{...good.files[0],path:'x'},{...good.files[0],path:'x/y'}])])assert.throws(()=>frontendManifest(bad,run.id,run.digest));
});
test('build requests keep output inside selected project and forbid shell or flag injection',()=>{
 assert.equal(frontendRequest({id:run.id,projectDir:'frontend',outDir:'frontend/dist',script:'build:client'}).script,'build:client');
 for(const x of [{projectDir:'',outDir:'../dist',script:'build'},{projectDir:'client',outDir:'dist',script:'build'},{projectDir:'',outDir:'dist',script:'build;env'},{projectDir:'',outDir:'dist',script:'--help'}])assert.throws(()=>frontendRequest({id:run.id,...x}));
});
test('downloads verify every byte and preserve existing source and dist',async()=>{
 const root=await makeRoot();try{
  await fs.mkdir(path.join(root,'dist'));await fs.writeFile(path.join(root,'dist','index.html'),'existing');
  const binary=Buffer.from([0,255,128,34]),data=[Buffer.from('ok'),binary],m=manifest([{path:'index.html',size:2,sha256:hash(data[0])},{path:'assets/logo.png',size:4,sha256:hash(binary)}]);
  const client={object:async()=>JSON.stringify(m),objectBytes:async(_b,key)=>data[Number(key.split('/').pop())]};
  const dir=await downloadFrontend(client,'bucket',run,root);assert.match(dir,/^\.recoder\/cloud-build-/);
  assert.equal(await fs.readFile(path.join(root,'dist','index.html'),'utf8'),'existing');assert.deepEqual(await fs.readFile(path.join(root,dir,'assets/logo.png')),binary);
  client.objectBytes=async()=>Buffer.from('NO');await assert.rejects(()=>downloadFrontend(client,'bucket',run,root),/SHA256/);
  await assert.rejects(()=>downloadFrontend(client,'bucket',{...run,status:'FAILED'},root),/성공한/);
 }finally{await remove(root);}
});
test('cloud bundle needs no Dockerfile and excludes keys and dependencies',async()=>{
 const root=await makeRoot();try{
  await fs.writeFile(path.join(root,'package.json'),'{}');await fs.writeFile(path.join(root,'.env'),'SECRET');await fs.mkdir(path.join(root,'node_modules'));await fs.writeFile(path.join(root,'node_modules','x'),'x');
  const bundle=await frontendBundle(root,path.resolve('out/security'),{id:run.id,projectDir:'',outDir:'dist',script:'build'});
  assert.deepEqual(bundle.files,['package.json']);assert.equal(bundle.digest,hash(bundle.data));assert.ok(!bundle.data.includes(Buffer.from('SECRET')));
 }finally{await remove(root);}
});
test('build infrastructure and permissions are separate from scanners and user deployments',()=>{
 const security=JSON.parse(managedTemplate('123456789012','us-east-1')),build=JSON.parse(managedTemplate('123456789012','us-east-1','build'));
 assert.equal(security.Resources.Project.Properties.Name,'recoder-security-v1');assert.equal(build.Resources.Project.Properties.Name,'recoder-build-v1');
 assert.equal(build.Resources.Lock.Properties.TableName,'recoder-build-lock-v1');assert.ok(!JSON.stringify(build).includes('bedrock:'));assert.ok(!JSON.stringify(build).includes('ecs:'));
 const policies=managedPermissions('123456789012','us-east-1');
 assert.ok(JSON.stringify(policies.bootstrap).length<=6144);
 const actions=policies.runtime.Statement.flatMap(s=>s.Action);assert.ok(!actions.some(a=>/Create|DeleteStack|PutRolePolicy/.test(a)));
 const cb=policies.runtime.Statement.find(s=>s.Sid==='RunOwnedProjects');assert.ok(cb.Resource.every(x=>x.includes(':project/')));assert.ok(cb.Action.includes('codebuild:BatchGetBuilds'));
 const pass=policies.bootstrap.Statement.find(s=>s.Sid==='PassRunnerRoleToCodeBuild');assert.equal(pass.Condition.StringEquals['iam:PassedToService'],'codebuild.amazonaws.com');
 assert.throws(()=>managedPermissions('invalid','us-east-1'));
});
test('build jobs use their own stack and lock, and reject security stack identity',async()=>{
 const calls=[],client={region:'us-east-1',cloudformation:async(action,args)=>{calls.push([action,args]);return '<StackStatus>CREATE_COMPLETE</StackStatus><Key>recoder:purpose</Key><Value>build-runner-v1</Value>';}};
 const jobs=new ManagedJobs(client,'123456789012','build');assert.equal((await jobs.status()).ready,true);await jobs.provision();
 assert.equal(calls[0][1].StackName,'recoder-build-v1');assert.equal(calls[1][1].StackName,'recoder-build-v1');assert.match(jobs.bucket,/^recoder-build-/);
 await assert.rejects(()=>new ManagedJobs(client,'123456789012').status(),/환경인지/);
});
test('binary AWS results preserve arbitrary bytes and enforce size bounds',async()=>{
 const body=Buffer.from([255,0,22,128]),creds=async()=>({accessKeyId:'test',secretAccessKey:'test',sessionToken:'test'});
 const client=new ManagedAwsClient('us-east-1',creds,async()=>new Response(body));assert.deepEqual(await client.objectBytes('recoder-build-123456789012-us-east-1','reports/a'),body);
 const large=new ManagedAwsClient('us-east-1',creds,async()=>new Response(Buffer.alloc(5*1024*1024+1)));await assert.rejects(()=>large.objectBytes('recoder-build-123456789012-us-east-1','reports/a'),/크기 제한/);
});
test('host does not create AWS resources if region differs or native build choice is cancelled',async()=>{
 const core={getManagedAwsConnection:async()=>({metadata:{account:'123456789012',region:'us-east-1'},client:new ManagedAwsClient('us-east-1',async()=>{throw Error('no key export')})})},memory={get:()=>undefined,update:async()=>{}};
 vscode.window.showQuickPick=async()=>undefined;await assert.rejects(()=>new ManagedFrontendHost(core,memory,'out/security').build('x',{},()=>{},'us-east-1'),/취소/);
 vscode.window.showQuickPick=async()=>({cloud:true});await assert.rejects(()=>new ManagedFrontendHost(core,memory,'out/security').build('x',{},()=>{},'ap-northeast-2'),/리전/);
});
test('source upload refusal leaves existing build and deploy resources untouched',async t=>{
 const root=await makeRoot();let starts=0;
 try{
  await fs.writeFile(path.join(root,'package.json'),'{}');
  t.mock.method(ManagedJobs.prototype,'status',async()=>({ready:true,stackStatus:'CREATE_COMPLETE'}));t.mock.method(ManagedJobs.prototype,'start',async()=>{starts++;});
  vscode.window.showQuickPick=async()=>({cloud:true});vscode.workspace.openTextDocument=async x=>x;vscode.window.showTextDocument=async()=>{};vscode.window.showWarningMessage=async()=>undefined;
  vscode.window.withProgress=async(_options,fn)=>fn({report:()=>{}},{isCancellationRequested:false});
  const core={getManagedAwsConnection:async()=>({metadata:{account:'123456789012',region:'us-east-1'},client:new ManagedAwsClient('us-east-1',async()=>{throw Error('no key export')})})};
  await assert.rejects(()=>new ManagedFrontendHost(core,{get:()=>undefined,update:async()=>{}},path.resolve('out/security')).build(root,{projectDir:'',outDir:'dist',script:'build'},()=>{},'us-east-1'),/취소/);assert.equal(starts,0);
 }finally{await remove(root);}
});

test('AWS connection outage does not prevent the user from choosing the existing local frontend build',async t=>{
 const error=Error('AWS connection unavailable'),core={getManagedAwsConnection:async()=>{throw error;}},state={get:()=>undefined,update:async()=>{}};
 let builds=0;t.mock.method(require('../out/deploy/frontendBuild'),'buildFrontend',async()=>{builds++;return {outDir:'dist'};});
 vscode.window.showWarningMessage=async()=>undefined;
 await assert.rejects(()=>new ManagedFrontendHost(core,state,'runtime').build('root',{},()=>{},'us-east-1'),e=>e===error);assert.equal(builds,0);
 vscode.window.showWarningMessage=async()=> '내 PC에서 빌드';
 assert.deepEqual(await new ManagedFrontendHost(core,state,'runtime').build('root',{},()=>{},'us-east-1'),{outDir:'dist'});assert.equal(builds,1);
});
