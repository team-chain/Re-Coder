const test=require('node:test'),assert=require('node:assert/strict'),Module=require('node:module'),fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
const vscode={workspace:{isTrusted:true,openTextDocument:async x=>x},window:{showTextDocument:async()=>{}},commands:{}};
const original=Module._load;Module._load=function(name,...args){return name==='vscode'?vscode:original.call(this,name,...args);};
const {ManagedSecurityHost}=require('../out/security/managedSecurityHost');Module._load=original;
const {ManagedJobs}=require('../out/security/managedJobs');
const {ManagedAwsClient,AwsRequestError}=require('../out/cloud/managedAwsClient');
const core={getManagedAwsConnection:async()=>({metadata:{account:'123456789012',region:'ap-northeast-2'},client:new ManagedAwsClient('ap-northeast-2',async()=>{throw Error('must not request credentials')})})};
function state(){const data=new Map();return {get:(k,d)=>data.get(k)||d,update:async(k,v)=>data.set(k,v)};}

test('existing AWS connection stays connected on missing permission and never asks for browser login',async t=>{
 vscode.commands.executeCommand=async()=>{throw Error('must not ask to reconnect');};
 t.mock.method(ManagedJobs.prototype,'status',async()=>{throw new AwsRequestError(403,'AccessDenied');});
 const result=await new ManagedSecurityHost(core,state(),'out/security').handle('managedLogin','project');
 assert.equal(result.connected,true);assert.equal(result.account,'123456789012');assert.equal(result.stackStatus,'PERMISSION_REQUIRED');
});
test('cancelled infrastructure consent never creates cloud resources',async t=>{
 let created=0;t.mock.method(ManagedJobs.prototype,'status',async()=>({ready:false,stackStatus:'NOT_CREATED'}));t.mock.method(ManagedJobs.prototype,'provision',async()=>{created++;});
 vscode.window.showWarningMessage=async()=>undefined;
 assert.equal((await new ManagedSecurityHost(core,state(),'out/security').handle('managedSetup','project')).cancelled,true);assert.equal(created,0);
});
test('source disclosure consent precedes any upload or job dispatch',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-managed-host-'));let starts=0,detail='';
 try{await fs.writeFile(path.join(root,'app.ts'),'const value=1');t.mock.method(ManagedJobs.prototype,'status',async()=>({ready:true,stackStatus:'CREATE_COMPLETE'}));t.mock.method(ManagedJobs.prototype,'start',async()=>{starts++;return 'unexpected';});
 vscode.window.showInputBox=async()=> 'global.anthropic.claude-haiku-4-5-20251001-v1:0';vscode.window.showWarningMessage=async(_title,options)=>{detail=options.detail;return undefined;};
 const result=await new ManagedSecurityHost(core,state(),path.resolve('out/security')).handle('managedAuditStart',root);
 assert.equal(result.cancelled,true);assert.equal(starts,0);assert.match(detail,/123456789012/);assert.match(detail,/Bedrock/);assert.match(detail,/한도|상한/);
 }finally{assert.equal(path.dirname(root),os.tmpdir());await fs.rm(root,{recursive:true,force:true});}
});
test('untrusted workspaces never inspect or provision cloud resources',async()=>{
 vscode.workspace.isTrusted=false;try{await assert.rejects(()=>new ManagedSecurityHost(core,state(),'out/security').handle('managedSetup','project'),/신뢰/);}finally{vscode.workspace.isTrusted=true;}
});
test('mode-specific history keeps another active scan visible without mixing its report',async t=>{
 const audit={id:'audit',mode:'audit',terminal:true,status:'SUCCEEDED'},scan={id:'scans',mode:'scans',status:'IN_PROGRESS'};
 const memory={get:()=>[scan,audit],update:async()=>{}};
 t.mock.method(ManagedJobs.prototype,'status',async()=>({ready:true,stackStatus:'CREATE_COMPLETE'}));
 t.mock.method(ManagedJobs.prototype,'refresh',async(run,include=true)=>({run,...(include?{report:{runId:run.id}}:{})}));
 t.mock.method(ManagedJobs.prototype,'readReport',async run=>({runId:run.id}));
 t.mock.method(ManagedJobs.prototype,'deleteSource',async()=>{});
 const result=await new ManagedSecurityHost(core,memory,'out/security').handle('managedStatus','project','audit');
 assert.equal(result.runs.length,1);assert.equal(result.runs[0].id,'audit');assert.equal(result.report.runId,'audit');assert.equal(result.activeRun.id,'scans');
});
test('invalid reports cannot erase terminal status or leave a completed job stuck active',async t=>{
 const r={id:'audit',mode:'audit',status:'IN_PROGRESS'};let saved;
 const memory={get:()=>[r],update:async(_key,value)=>{saved=value;}};
 t.mock.method(ManagedJobs.prototype,'status',async()=>({ready:true,stackStatus:'CREATE_COMPLETE'}));
 t.mock.method(ManagedJobs.prototype,'refresh',async run=>({run:{...run,status:'FAILED',terminal:true}}));
 t.mock.method(ManagedJobs.prototype,'readReport',async()=>{throw Error('invalid report');});
 t.mock.method(ManagedJobs.prototype,'deleteSource',async()=>{});
 const result=await new ManagedSecurityHost(core,memory,'out/security').handle('managedStatus','project','audit');
 assert.equal(saved[0].terminal,true);assert.equal(result.activeRun,undefined);assert.equal(result.report,undefined);assert.match(result.reportError,/통과로 처리하지/);
});
