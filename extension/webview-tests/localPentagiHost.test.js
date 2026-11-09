const test=require('node:test'),assert=require('node:assert/strict'),Module=require('node:module');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const mock={workspace:{},window:{}};const original=Module._load;
Module._load=function(name,...args){return name==='vscode'?mock:original.call(this,name,...args);};
const {LocalPentagiHost}=require('../out/security/localPentagiHost');Module._load=original;
const {saveReport,readReport,LocalPentagiRuntime}=require('../out/security/localPentagi');
const id='rc-rt-0123456789abcdef01234567';
async function setup(t){const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-localhost-test-'));t.after(async()=>{assert.equal(path.dirname(root),os.tmpdir());assert.ok(path.basename(root).startsWith('recoder-localhost-test-'));await fs.rm(root,{recursive:true,force:true});});return root;}
test('native approval cancellation creates no environment and stores no model key',async t=>{
 const root=await setup(t);let stored=0;
 const host=new LocalPentagiHost(root,{get:async()=> 'existing-model-key',store:async()=>{stored++;}});let approvalDetail='';
 mock.window.showInputBox=async()=> 'http://localhost:3000';mock.window.showQuickPick=async()=>({label:'OpenAI',provider:'openai'});mock.window.showWarningMessage=async(_title,options)=>{approvalDetail=options.detail;return undefined;};
 assert.equal((await host.handle('localStart','project')).cancelled,true);
 assert.match(approvalDetail,/localhost로 제한/);assert.match(approvalDetail,/강제 격리가 아닙니다/);assert.match(approvalDetail,/EULA/);
 assert.equal(stored,0);assert.deepEqual(await fs.readdir(root),[]);
});
test('a leftover environment blocks duplicate starts; manual cleanup preserves evidence',async t=>{
 const root=await setup(t);let cleaned=0,prompts=0;
 await saveReport(root,{session:id,workspace:'project',target:'http://host.docker.internal:3000/',createdAt:new Date().toISOString(),phase:'cleanup_failed',step:'retry',tasks:[{result:'evidence'}]});
 const host=new LocalPentagiHost(root,{get:async()=>undefined,store:async()=>{}});
 mock.window.showInputBox=async()=>{prompts++;return undefined;};
 await assert.rejects(()=>host.handle('localStart','project'),/정리/);assert.equal(prompts,0);
 t.mock.method(LocalPentagiRuntime.prototype,'cleanup',async()=>{cleaned++;});
 const result=await host.handle('localCleanup','project');assert.equal(result.local.phase,'removed');assert.equal(cleaned,1);
 assert.equal((await readReport(root,id)).tasks[0].result,'evidence');
});
test('other projects never expose or clean this project report',async t=>{
 const root=await setup(t);let cleaned=0;
 await saveReport(root,{session:id,workspace:'private-project',target:'http://host.docker.internal:3000/',createdAt:new Date().toISOString(),phase:'cleanup_failed',step:'retry'});
 t.mock.method(LocalPentagiRuntime.prototype,'cleanup',async()=>{cleaned++;});
 const result=await new LocalPentagiHost(root,{}).handle('localCleanup','different-project');
 assert.equal(result.local,null);assert.equal(cleaned,0);
 await assert.rejects(()=>new LocalPentagiHost(root,{}).handle('localStart','different-project'),/다른 프로젝트/);
});
