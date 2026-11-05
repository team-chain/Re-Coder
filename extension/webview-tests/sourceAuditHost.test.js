const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),Module=require('node:module');
const vscode={workspace:{isTrusted:true},window:{showInformationMessage:async()=> '검토한 파일 감사'}};
const original=Module._load;Module._load=function(name,...args){return name==='vscode'?vscode:original.call(this,name,...args);};
const {SourceAuditHost}=require('../out/security/sourceAuditHost');Module._load=original;
const {auditFiles}=require('../out/security/managedAudit');
const assets=path.resolve(__dirname,'../media/security-audit');
const source='function get(id) {\n return db.query("select " + id);\n}';
const finding={title:'검증되지 않은 입력',severity:'high',file:'app.ts',line:2,endLine:2,evidence:'return db.query("select " + id);',boundary:'request -> database',impact:'query manipulation',fix:'Use bound parameters.'};
const reply=result=>({result,provider:'existing-ai',model:'current-model',inputTokens:2,outputTokens:1});
async function fixture(fn){const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-source-audit-'));const store=new Map(),memory={get:k=>store.get(k),update:async(k,v)=>{store.set(k,v);}};try{await fs.writeFile(path.join(root,'app.ts'),source);vscode.window.showInformationMessage=async()=> '검토한 파일 감사';await fn(root,memory,store);}finally{assert.equal(path.dirname(root),os.tmpdir());await fs.rm(root,{recursive:true,force:true});}}
async function finish(host,root){for(let i=0;i<200;i++){const s=await host.handle('status',root);if(!['preparing','running','stopping'].includes(s.phase))return s;await new Promise(r=>setTimeout(r,5));}throw Error('audit did not settle');}

test('existing AI performs source audit, verifies evidence, persists result and never writes project files',()=>fixture(async(root,memory)=>{
 const calls=[];const host=new SourceAuditHost({securityAuditStep:async(role)=>{calls.push(role);return reply(role==='hunter'?{findings:[finding]}:role==='critic'?{findings:[]}:role.includes('verifier')?{supported:true,reason:'source'}:{boundaries:[]});}},memory,assets);
 assert.equal((await host.handle('status',root)).phase,'idle');assert.equal(calls.length,0);
 await host.handle('start',root);const result=await finish(host,root);
 assert.equal(result.phase,'completed');assert.equal(result.provider,'existing-ai');assert.equal(result.calls,9);assert.equal(result.report.findings.length,1);assert.equal(result.stale,false);
 assert.equal(await fs.readFile(path.join(root,'app.ts'),'utf8'),source);
 assert.equal((await new SourceAuditHost({},memory,assets).handle('status',root)).report.findings.length,1);
}));

test('source approval rejection never calls the AI',()=>fixture(async(root,memory)=>{
 vscode.window.showInformationMessage=async()=>undefined;let calls=0;
 const host=new SourceAuditHost({securityAuditStep:async()=>{calls++;}},memory,assets);
 assert.equal((await host.handle('start',root)).phase,'cancelled');assert.equal(calls,0);
}));

test('stop prevents subsequent model calls and stale running journal is not reported successful',()=>fixture(async(root,memory)=>{
 let resolve,calls=0;const host=new SourceAuditHost({securityAuditStep:async()=>{calls++;return new Promise(r=>{resolve=r;});}},memory,assets);
 await host.handle('start',root);for(let i=0;i<50&&!resolve;i++)await new Promise(r=>setTimeout(r,5));
 assert.equal(calls,1);assert.equal((await new SourceAuditHost({},memory,assets).handle('status',root)).phase,'interrupted');
 assert.equal((await host.handle('stop',root)).phase,'stopping');resolve(reply({}));
 assert.equal((await finish(host,root)).phase,'cancelled');assert.equal(calls,1);
}));

test('model failure is not a clean audit and changed source is explicitly stale',()=>fixture(async(root,memory)=>{
 const fail=new SourceAuditHost({securityAuditStep:async()=>{throw Error('AI unavailable');}},memory,assets);
 await fail.handle('start',root);assert.equal((await finish(fail,root)).phase,'failed');
 const host=new SourceAuditHost({securityAuditStep:async role=>{await fs.writeFile(path.join(root,'app.ts'),source+'\n// edited');return reply(['hunter','critic'].includes(role)?{findings:[]}:{boundaries:[]});}},memory,assets);
 await host.handle('start',root);const result=await finish(host,root);assert.equal(result.phase,'completed');assert.equal(result.stale,true);
}));

test('audit excludes secret files, generated output and installed dependencies before reading source',()=>fixture(async root=>{
 for(const dir of ['.aws','node_modules','dist']){await fs.mkdir(path.join(root,dir));await fs.writeFile(path.join(root,dir,'secret.ts'),'never send');}
 for(const file of ['.env','secrets.json','key.pem'])await fs.writeFile(path.join(root,file),'never send');
 const result=await auditFiles(root);assert.deepEqual(result.files.map(f=>f.path),['app.ts']);assert.equal(result.skipped.length,6);
}));

test('journal failure never starts AI or leaves the host permanently busy',()=>fixture(async root=>{
 let calls=0,fail=true;const host=new SourceAuditHost({securityAuditStep:async role=>{calls++;return reply(['hunter','critic'].includes(role)?{findings:[]}:{});}},
  {get:()=>undefined,update:async()=>{if(fail)throw Error('disk full');}},assets);
 assert.equal((await host.handle('start',root)).phase,'failed');assert.equal(calls,0);
 fail=false;await host.handle('start',root);assert.equal((await finish(host,root)).phase,'completed');assert.ok(calls>0);
}));
