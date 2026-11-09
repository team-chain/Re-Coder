'use strict';
// Explicit live-model check: synthetic source only, existing Core connection, no source execution.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),Module=require('node:module');
async function main(){
 if(process.env.RECODER_LIVE_AUDIT!=='1')throw Error('Set RECODER_LIVE_AUDIT=1 to allow the bounded live-model verification.');
 const root=path.resolve(process.env.RECODER_LIVE_FIXTURE);
 let source="import express from 'express';\nimport mysql from 'mysql2/promise';\nconst app = express();\nconst db = await mysql.createConnection(process.env.DATABASE_URL);\napp.get('/search', async (req, res) => {\n  const [rows] = await db.query(\"SELECT id, title FROM posts WHERE title = '\" + req.query.q + \"'\");\n  res.json(rows);\n});\napp.listen(3000);\n";
 const scenario=process.env.RECODER_LIVE_SCENARIO||'vulnerable';
 assert.ok(['vulnerable','parameterized','unknown-caller'].includes(scenario),'unsupported audit fixture');
 if(scenario==='parameterized')source=source.replace("db.query(\"SELECT id, title FROM posts WHERE title = '\" + req.query.q + \"'\")","db.execute('SELECT id, title FROM posts WHERE title = ?', [req.query.q])");
 if(scenario==='unknown-caller')source="export async function search(db, value) { return db.query(\"SELECT id FROM posts WHERE title='\" + value + \"'\"); }\n";
 await fs.mkdir(root,{recursive:true});await fs.writeFile(path.join(root,'app.js'),source);
 const vscode={workspace:{isTrusted:true},window:{showInformationMessage:async()=> '검토한 파일 감사'}};
 const original=Module._load;Module._load=function(name,...args){return name==='vscode'?vscode:original.call(this,name,...args);};
 let ApiClient,SourceAuditHost;
 try {({ApiClient}=require('../out/core/ApiClient'));({SourceAuditHost}=require('../out/security/sourceAuditHost'));}finally{Module._load=original;}
 const port=Number(process.env.RECODER_LIVE_PORT),token=process.env.RECODER_LIVE_SESSION;
 assert.ok(port&&token,'existing Core connection required');
 const api=new ApiClient({getPort:()=>port,getSessionToken:()=>token,refreshToken:async()=>{}});
 const store=new Map(),memory={get:k=>store.get(k),update:async(k,v)=>store.set(k,v)};
 const host=new SourceAuditHost(api,memory,path.resolve(__dirname,'../media/security-audit'));
 await host.handle('start',root);let prior='',state;
 const deadline=Date.now()+17*60*1000;
 while(Date.now()<deadline){
  state=await host.handle('status',root);const label=state.phase+' '+state.stage+' calls='+(state.calls||0);
  if(label!==prior){console.log(label);prior=label;}
  if(!['preparing','running','stopping'].includes(state.phase))break;
  await new Promise(r=>setTimeout(r,1000));
 }
 if(!state||['preparing','running','stopping'].includes(state.phase)){await host.handle('stop',root);throw Error('Live audit exceeded bounded verification time');}
 await fs.writeFile(path.join(path.dirname(root),'audit-result.json'),JSON.stringify(state,null,2));
 assert.equal(state.phase,'completed',state.error||state.stage);
 assert.equal(state.stale,false);assert.ok(state.calls>=7&&state.calls<=20);
 const sql=state.report.findings.filter(f=>f.file==='app.js'&&f.line<=6&&f.endLine>=6&&/sql|prepared|매개변수화/i.test(f.title+' '+f.fix));
 if(scenario==='vulnerable'){
  assert.equal(sql.length,1,'synthetic SQL injection must be confirmed once, not duplicated');
  assert.equal(state.report.findings.length,1,'fixture has one demonstrated vulnerability');
  assert.equal(state.report.needsValidation.length,0,'duplicate unconfirmed copies must not remain');
 }else assert.equal(state.report.findings.length,0,'safe binding or missing attacker provenance cannot be a confirmed vulnerability');
 assert.equal(await fs.readFile(path.join(root,'app.js'),'utf8'),source);
 assert.deepEqual((await new SourceAuditHost(api,memory,path.resolve(__dirname,'../media/security-audit')).handle('status',root)).report,state.report);
 console.log(JSON.stringify({live:true,scenario,provider:state.provider,model:state.model,calls:state.calls,findings:state.report.findings.length,inputTokens:state.report.inputTokens,outputTokens:state.report.outputTokens,sourceUnchanged:true,persistedResult:true}));
}
main().catch(e=>{console.error(e.message);process.exitCode=1;});
