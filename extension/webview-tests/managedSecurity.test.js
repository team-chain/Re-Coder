const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
const {ManagedAwsClient,signedManagedRequest,AwsRequestError}=require('../out/cloud/managedAwsClient');
const {ManagedJobs}=require('../out/security/managedJobs');
const {managedTemplate}=require('../out/security/managedTemplate');
const {projectBundle}=require('../out/security/managedBundle');
const {validateManagedInput}=require('../out/security/managedPentagiWorker');
const credentials={accessKeyId:'AKIATEST',secretAccessKey:'SECRET_NOT_FOR_LOGS',sessionToken:'SESSION_SECRET',expiration:'2030-01-01'};
const run={id:'rc-rt-'+ 'a'.repeat(24),createdAt:'2026-11-02T00:00:00Z',digest:'hash',files:1,bytes:10,status:'SUBMITTING',uncertain:true};
const bucket='recoder-security-123456789012-ap-northeast-2';
function build(status='IN_PROGRESS'){return {id:'recoder-security-v1:1234',projectName:'recoder-security-v1',buildStatus:status,currentPhase:'BUILD',source:{location:`${bucket}/sources/${run.id}.zip`},environment:{environmentVariables:[{name:'RECODER_RUN_ID',value:run.id},{name:'RECODER_SOURCE_SHA256',value:run.digest}]}};}

test('signed AWS requests bind body, method, key and session to constructed AWS hosts',()=>{
 const base={method:'PUT',bucket,key:'sources/한 글.zip'};const a=signedManagedRequest('s3','ap-northeast-2',credentials,Buffer.from('a'),base,new Date('2026-11-02T00:00:00Z'));
 assert.match(a.url,/https:\/\/recoder-security-123456789012-ap-northeast-2.s3.ap-northeast-2.amazonaws.com\/sources\/%/);assert.ok(!a.url.includes(' '));
 for(const change of [{body:'b'}, {method:'DELETE'}, {key:'reports/other.zip'}]){
 const x=signedManagedRequest('s3','ap-northeast-2',credentials,Buffer.from(change.body||'a'),{...base,...change},new Date('2026-11-02T00:00:00Z'));assert.notEqual(a.headers.Authorization,x.headers.Authorization);}
 assert.throws(()=>signedManagedRequest('s3','../example.com',credentials,Buffer.alloc(0),base));
 assert.throws(()=>signedManagedRequest('s3','ap-northeast-2',credentials,Buffer.alloc(0),{bucket:'attacker.example'}));
});
test('successful empty S3 response is accepted; upstream secrets are never put in errors',async()=>{
 const good=new ManagedAwsClient('ap-northeast-2',async()=>credentials,async()=>new Response(null,{status:200}));assert.equal(await good.object(bucket,'sources/a.zip','PUT',Buffer.from('a')),'');
 const bad=new ManagedAwsClient('ap-northeast-2',async()=>credentials,async()=>new Response('<Code>AccessDenied</Code><Message>SECRET_NOT_FOR_LOGS</Message>',{status:403}));
 await assert.rejects(()=>bad.object(bucket,'x','GET'),e=>!e.message.includes('SECRET_NOT_FOR_LOGS')&&e.code==='AccessDenied');
});
test('ambiguous StartBuild is never retried and recovers only the exact source and run',async()=>{
 let starts=0;const api={region:'ap-northeast-2',dynamodb:async()=>({}),object:async()=>JSON.stringify({schema:1,runId:run.id,sourceSha256:run.digest,mode:'redteam',status:'running',stage:'검사',tasks:[]}),codebuild:async action=>{if(action==='StartBuild'){starts++;throw Error('timeout');}if(action==='ListBuildsForProject')return {ids:['recoder-security-v1:1234']};return {builds:[build()]};}};
 const jobs=new ManagedJobs(api,'123456789012');await assert.rejects(()=>jobs.start(run,{data:Buffer.from('zip')}));assert.equal(starts,1);
 const recovered=await jobs.refresh(run);assert.equal(recovered.run.buildId,'recoder-security-v1:1234');assert.equal(recovered.run.uncertain,false);
 api.codebuild=async action=>action==='ListBuildsForProject'?{ids:['other']}:{builds:[{...build(),source:{location:'someone-else/sources/a.zip'}}]};await assert.rejects(()=>jobs.refresh({...run,buildId:'recoder-security-v1:1234'}),/확인/);
});
test('stop waits for AWS status, missing reports never imply a passed test',async()=>{
 const actions=[];const api={region:'ap-northeast-2',object:async()=>JSON.stringify({schema:1,runId:run.id,sourceSha256:run.digest,mode:'redteam',status:'failed',stage:'검사',tasks:[]}),codebuild:async action=>{actions.push(action);return {builds:[build()]};}};
 const jobs=new ManagedJobs(api,'123456789012');await jobs.stop({...run,buildId:'recoder-security-v1:1234'});assert.deepEqual(actions,['BatchGetBuilds','StopBuild']);assert.equal(run.terminal,undefined);
 api.codebuild=async()=>({builds:[build('FAILED')]});const x=await jobs.refresh({...run,buildId:'recoder-security-v1:1234'});assert.equal(x.run.status,'FAILED');assert.equal(x.report.status,'failed');
 api.object=async()=>JSON.stringify({schema:1,runId:'another',tasks:[]});await assert.rejects(()=>jobs.refresh({...run,buildId:'recoder-security-v1:1234'}),/올바르지/);
});
test('managed infrastructure is private, retained deliberately, scoped and bounded',()=>{
 const t=JSON.parse(managedTemplate('123456789012','ap-northeast-2'));const p=t.Resources.Project.Properties;
 assert.equal(p.ConcurrentBuildLimit,1);assert.equal(p.TimeoutInMinutes,60);assert.equal(t.Resources.Bucket.Properties.PublicAccessBlockConfiguration.BlockPublicPolicy,true);
 assert.equal(t.Resources.Bucket.DeletionPolicy,'Retain');const policy=JSON.stringify(t.Resources.Role.Properties.Policies);assert.doesNotMatch(policy,/iam:|sts:|ec2:|ecs:/);assert.doesNotMatch(policy,/"Action":"\*"/);
 assert.equal(t.Resources.Role.Properties.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals['aws:SourceAccount'],'123456789012');
});
test('snapshot excludes credentials and dependencies and preserves UTF-8 names in a valid ZIP',async()=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-managed-'));try{
 const marker='RECODER_TEST_PRIVATE_MUST_NOT_BE_UPLOADED_829349';
 await fs.writeFile(path.join(root,'Dockerfile'),'FROM scratch');await fs.writeFile(path.join(root,'한 글.txt'),'검사');await fs.writeFile(path.join(root,'.env'),marker);await fs.writeFile(path.join(root,'secret.pem'),marker);await fs.mkdir(path.join(root,'node_modules'));await fs.writeFile(path.join(root,'node_modules','a.js'),marker);
 const b=await projectBundle(root,path.resolve('out/security'),{id:run.id,port:3000,model:'anthropic.claude-test'});
 assert.deepEqual(b.files.sort(),['Dockerfile','한 글.txt'].sort());
 assert.ok(!b.data.includes(Buffer.from(marker)));
 const yauzl=require('yauzl');const entries=await new Promise((resolve,reject)=>yauzl.fromBuffer(b.data,{lazyEntries:true},(err,zip)=>{if(err)return reject(err);const names=[];zip.on('error',reject);zip.on('entry',e=>{names.push(e.fileName);zip.readEntry();});zip.on('end',()=>resolve(names));zip.readEntry();}));assert.ok(entries.includes('source/한 글.txt'));assert.ok(entries.includes('runtime/managedPentagiWorker.js'));
 }finally{await fs.rm(root,{recursive:true,force:true});}
});
test('worker input forbids external targets, invalid ports and model configuration injection',()=>{
 for(const x of [{id:run.id,port:0,model:'anthropic.claude-test'},{id:'../x',port:3000,model:'anthropic.claude-test'},{id:run.id,port:3000,model:'anthropic.claude-test\nmalicious: true'}])assert.throws(()=>validateManagedInput(x));
 assert.equal(validateManagedInput({id:run.id,port:3000,model:'global.anthropic.claude-haiku-4-5-20251001-v1:0'}).port,3000);
});
test('snapshot preserves complete source when filesystem reads return short chunks',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-short-read-'));
 try{
  const content='source-chunk-'.repeat(700);await fs.writeFile(path.join(root,'app.ts'),content);
  const open=fs.open.bind(fs);t.mock.method(fs,'open',async(...args)=>{
   const h=await open(...args);return {stat:()=>h.stat(),close:()=>h.close(),read:(buffer,offset,length,position)=>h.read(buffer,offset,Math.min(length,37),position)};
  });
  const bundle=await projectBundle(root,path.resolve('out/security'),{id:run.id,mode:'audit',model:'anthropic.claude-test'});
  assert.equal(bundle.bytes,Buffer.byteLength(content));assert.ok(bundle.data.includes(Buffer.from(content)));
 }finally{await fs.rm(root,{recursive:true,force:true});}
});

test('a shared conditional lease prevents a second client from uploading or starting a job',async()=>{
 let owner,uploads=0,starts=0;
 const api={region:'ap-northeast-2',dynamodb:async(action,value)=>{
  if(action==='PutItem'){if(owner)throw new AwsRequestError(400,'ConditionalCheckFailedException');owner=value.Item;return {};}
  if(action==='GetItem')return {Item:owner};if(action==='UpdateItem'){assert.equal(value.ExpressionAttributeValues[':id'].S,owner.runId.S);owner.stage={S:'starting'};return {};}
  throw Error('unexpected delete');
 },object:async()=>{uploads++;return '';},codebuild:async action=>{if(action==='StartBuild'){starts++;return {build:build()};}if(action==='ListBuildsForProject')return {ids:['recoder-security-v1:1234']};return {builds:[build()]};}};
 const first=new ManagedJobs(api,'123456789012'),second=new ManagedJobs(api,'123456789012');
 const outcomes=await Promise.allSettled([first.start({...run},{data:Buffer.from('zip')}),second.start({...run,id:'rc-rt-'+'b'.repeat(24)},{data:Buffer.from('zip')})]);
 assert.equal(outcomes.filter(x=>x.status==='fulfilled').length,1);assert.equal(uploads,1);assert.equal(starts,1);assert.equal(owner.stage.S,'starting');
});

test('stopping before dispatch requires the lease still be claimed by that exact run',async()=>{
 let released=0;const api={region:'ap-northeast-2',codebuild:async()=>({ids:[]}),dynamodb:async(action,value)=>{assert.equal(action,'DeleteItem');assert.match(value.ConditionExpression,/#phase = :claimed/);assert.equal(value.ExpressionAttributeValues[':id'].S,run.id);released++;return {};}};
 const pending={...run};await new ManagedJobs(api,'123456789012').stop(pending);assert.equal(pending.status,'NOT_STARTED');assert.equal(pending.terminal,true);assert.equal(released,1);
});
