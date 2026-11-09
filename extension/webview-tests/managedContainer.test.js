const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto'),Module=require('node:module');
const {containerReport,sourceManifest}=require('../out/security/managedContainerContract');
const {scannerSummary}=require('../out/security/managedScans');
const {runContainerWorker}=require('../out/security/managedContainerWorker');
const {managedTemplate}=require('../out/security/managedTemplate');
const vscode={workspace:{isTrusted:true},window:{},ProgressLocation:{Notification:15}};
const original=Module._load;Module._load=function(name,...args){return name==='vscode'?vscode:original.call(this,name,...args);};
const {containerBundle,ManagedContainerHost}=require('../out/deploy/managedContainer');Module._load=original;
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
const ref={run_id:'rc-rt-'+'a'.repeat(24),build_id:'recoder-container-v1:11111111-2222-3333-4444-555555555555',source_sha256:'b'.repeat(64)},account='123456789012',region='us-east-1';
const checks=()=>['gitleaks','hadolint','dependencies','image'].map(tool=>({tool,status:'completed',count:0,criticalCount:0,highCount:0,blockingCount:0,findings:[]}));
const report=()=>({schema:1,runId:ref.run_id,buildId:ref.build_id,sourceSha256:ref.source_sha256,status:'completed',
 image:`${account}.dkr.ecr.${region}.amazonaws.com/recoder-managed-images-v1@sha256:${'c'.repeat(64)}`,imageDigest:'sha256:'+'c'.repeat(64),sbomSha256:'d'.repeat(64),
 sourceFiles:[{path:'Dockerfile',size:0,sha256:hash('')}],checks:checks(),policy:{decision:'allow'}});
async function fixture(fn){const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-container-test-'));try{await fn(root);}finally{assert.equal(path.dirname(root),os.tmpdir());await fs.rm(root,{recursive:true,force:true});}}

test('container evidence rejects missing, failed, truncated critical and foreign image results',()=>{
 assert.equal(containerReport(report(),ref,account,region).status,'completed');
 for(const change of [r=>r.status='running',r=>r.runId='other',r=>r.image='other/image',r=>r.policy.decision='deny',r=>r.checks[0].status='failed',r=>Object.assign(r.checks[3],{count:201,criticalCount:1,blockingCount:1}),r=>r.checks.push({...r.checks[0]}),r=>r.sourceFiles[0].path='../Dockerfile',r=>r.sourceFiles[0].path='CON.txt',r=>r.checks[3].count=true]){
  const r=report();change(r);assert.throws(()=>containerReport(r,ref,account,region));
 }
 const vulnerabilities=Array.from({length:201},(_,i)=>({VulnerabilityID:'CVE-test',PkgName:'p',InstalledVersion:'1',Severity:i===200?'CRITICAL':'LOW'}));
 const summary=scannerSummary('image',{Results:[{Target:'fixture',Vulnerabilities:vulnerabilities}]});assert.equal(summary.criticalCount,1);assert.equal(summary.findings.length,200);
 const r=report();r.checks[3]=summary;assert.throws(()=>containerReport(r,ref,account,region));
});
test('container stack isolates image publishing without ECS/IAM/model invocation and keeps rollback images',()=>{
 const template=JSON.parse(managedTemplate(account,region,'container'));
 assert.equal(template.Resources.Images.Properties.ImageTagMutability,'IMMUTABLE');assert.equal(template.Resources.Images.DeletionPolicy,'Retain');
 assert.equal(template.Resources.Images.Properties.LifecyclePolicy,undefined);
 const role=JSON.stringify(template.Resources.Role.Properties.Policies);
 assert.match(role,/ecr:PutImage/);assert.doesNotMatch(role,/ecs:|iam:|bedrock:/);
 assert.match(template.Resources.Project.Properties.Source.BuildSpec,/managedContainerWorker/);
});
test('container bundle binds source hashes, excludes credentials and needs an approved Dockerfile',()=>fixture(async root=>{
 await fs.writeFile(path.join(root,'app.js'),'code');await fs.writeFile(path.join(root,'.env'),'SECRET');
 await assert.rejects(()=>containerBundle(root,path.resolve('out/security'),ref.run_id),/Dockerfile/);
 await fs.writeFile(path.join(root,'Dockerfile'),'FROM scratch');
 const bundle=await containerBundle(root,path.resolve('out/security'),ref.run_id);
 assert.equal(bundle.digest,hash(bundle.data));assert.deepEqual(bundle.files.sort(),['Dockerfile','app.js']);
 assert.ok(bundle.data.includes(Buffer.from(hash('code'))));assert.ok(!bundle.data.includes(Buffer.from('SECRET')));
 assert.throws(()=>sourceManifest([{path:'Dockerfile',size:0,sha256:hash('')},{path:'DOCKERFILE',size:0,sha256:hash('')}]));
}));
test('disconnected AWS keeps local behavior and existing connection can cancel cloud choice',async()=>{
 const state={get:()=>undefined,update:async()=>{}};
 assert.equal(await new ManagedContainerHost({getManagedAwsConnection:async()=>undefined},state,'runtime').build('root',region,()=>{}),undefined);
 vscode.window.showQuickPick=async()=>undefined;
 await assert.rejects(()=>new ManagedContainerHost({getManagedAwsConnection:async()=>({metadata:{account,region},client:{region}})},state,'runtime').build('root',region,()=>{}),/취소/);
});

test('AWS connection outage leaves local Docker preparation available without starting a cloud job',async()=>{
 const error=Error('AWS connection unavailable'),core={getManagedAwsConnection:async()=>{throw error;}},state={get:()=>undefined,update:async()=>{}};
 vscode.window.showWarningMessage=async()=>undefined;
 await assert.rejects(()=>new ManagedContainerHost(core,state,'runtime').build('root',region,()=>{}),e=>e===error);
 vscode.window.showWarningMessage=async()=> '내 PC의 Docker 사용';
 assert.equal(await new ManagedContainerHost(core,state,'runtime').build('root',region,()=>{}),undefined);
});
for(const scanFails of [false,true])test(`trusted worker ${scanFails?'never pushes after failed scanning':'publishes only the scanned archive by immutable digest'}`,()=>fixture(async root=>{
 const oldCwd=process.cwd(),oldExit=process.exitCode,keys=['AWS_REGION','RECODER_REPORT_BUCKET','RECODER_RUN_ID','RECODER_SOURCE_SHA256','CODEBUILD_BUILD_ID'],old=Object.fromEntries(keys.map(k=>[k,process.env[k]]));
 const calls=[],puts=[];
 try{
  await fs.mkdir(path.join(root,'source'));await fs.writeFile(path.join(root,'source','Dockerfile'),'FROM scratch');
  await fs.writeFile(path.join(root,'request.json'),JSON.stringify({id:ref.run_id,sourceFiles:[{path:'Dockerfile',size:12,sha256:hash('FROM scratch')}]}));
  process.chdir(root);Object.assign(process.env,{AWS_REGION:region,RECODER_REPORT_BUCKET:`recoder-container-${account}-${region}`,RECODER_RUN_ID:ref.run_id,RECODER_SOURCE_SHA256:ref.source_sha256,CODEBUILD_BUILD_ID:ref.build_id});
  await runContainerWorker(root,{
   scan:async(_id,progress,_signal,archive)=>{await progress('fixture scan');if(scanFails)throw Error('fixture scan failure');await fs.writeFile(archive,'image archive');await fs.writeFile('sbom.json',JSON.stringify({bomFormat:'CycloneDX',specVersion:'1.5',components:[]}));return {checks:checks(),policy:{decision:'allow'}};},
   docker:async(args,options)=>{calls.push(args);if(args[0]==='login'){assert.equal(options.input,'private-password');assert.ok(!args.includes('private-password'));}return '';},
   aws:async args=>{if(args[0]==='s3api'){puts.push({key:args[args.indexOf('--key')+1],body:await fs.readFile(args[args.indexOf('--body')+1],'utf8')});return '{}';}if(args[1]==='get-login-password')return 'private-password';return JSON.stringify({imageDetails:[{imageDigest:'sha256:'+'c'.repeat(64)}]});}
  });
  const result=JSON.parse(await fs.readFile('report.json','utf8'));assert.equal(result.status,scanFails?'failed':'completed');
  assert.equal(calls.some(a=>a[0]==='push'),!scanFails);assert.equal(calls.some(a=>a[0]==='run'),false,'host never executes image entrypoint');
  assert.ok(!JSON.stringify(puts).includes('private-password'));assert.ok(puts.some(p=>p.key.endsWith('/report.json')));
  if(!scanFails)containerReport(result,ref,account,region);
 }finally{process.chdir(oldCwd);process.exitCode=oldExit;for(const key of keys)if(old[key]===undefined)delete process.env[key];else process.env[key]=old[key];}
}));
