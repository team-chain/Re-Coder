const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
const {auditSource,auditCandidate,auditFiles}=require('../out/security/managedAudit');
const files=[{path:'src/routes.ts',text:'function lookup(id) {\n return db.query("SELECT * FROM orders WHERE id=" + id);\n}'}];
const candidate={title:'검증되지 않은 검색어가 쿼리에 사용됨',severity:'high',file:'src/routes.ts',line:2,endLine:2,evidence:'return db.query("SELECT * FROM orders WHERE id=" + id);',boundary:'untrusted request -> DB rows',impact:'query manipulation',fix:'Use bound query parameters.'};
test('source evidence must exist at exact relative file and cited lines',()=>{
 assert.ok(auditCandidate(candidate,files));
 for(const patch of [{file:'../../secret'},{line:1,endLine:1},{evidence:'invented()'},{endLine:500},{severity:'safe'}])assert.equal(auditCandidate({...candidate,...patch},files),undefined);
});
test('independent verifier disagreement is not a confirmed source finding',async()=>{
 const seen=[];
 const result=await auditSource(files,[],async(role,_instructions,data)=>{seen.push({role,data});
  if(role==='hunter')return {findings:[candidate]};if(role==='critic')return {findings:[]};
  if(role==='verifier')return {supported:true,reason:'source'};if(role==='final-verifier')return {supported:false,reason:'caller unknown'};return {boundaries:[]};},async()=>{},'test methodology');
 assert.equal(result.findings.length,0);assert.equal(result.needsValidation.length,1);assert.equal(result.coverage,'partial');
 assert.match(result.needsValidation[0].reason,/caller unknown/);
 assert.equal(seen.filter(x=>x.role==='recon').length,4);
 const v=seen.find(x=>x.role==='verifier'),f=seen.find(x=>x.role==='final-verifier');assert.deepEqual(v.data,f.data);assert.ok(!JSON.stringify(f.data).includes('caller unknown'));
});
test('candidate limit and two independent source reviews are enforced',async()=>{
 const result=await auditSource(files,['unread.py'],async role=>role==='hunter'?{findings:Array.from({length:9},(_,i)=>({...candidate,title:'issue '+i,fix:'Distinct remediation '+i}))}:role.includes('verifier')?{supported:true,reason:'source supported'}:role==='critic'?{findings:[]}:{},async()=>{},'test');
 assert.equal(result.findings.length,6);assert.ok(result.calls<=20);assert.deepEqual(result.skipped,['unread.py']);assert.match(result.findings[0].verification,/실행 검증 전/);
});

test('paraphrased duplicate findings do not inflate issues or verification calls',async()=>{
 const result=await auditSource(files,[],async role=>role==='hunter'?{findings:[candidate,{...candidate,title:'Same SQL issue',evidence:candidate.evidence.slice(0,-1),fix:candidate.fix.slice(0,-1)}]}:role.includes('verifier')?{supported:true,reason:'source supported'}:role==='critic'?{findings:[]}:{},async()=>{},'test');
 assert.equal(result.findings.length,1);assert.equal(result.calls,9);
});

test('real-model SQL paraphrases merge while a separate authorization flaw at the same line remains',async()=>{
 const sql={...candidate,title:'SQL 주입 취약점',fix:'매개변수화된 쿼리(prepared statement)를 사용합니다.'};
 const paraphrase={...candidate,title:'SQL 인젝션',severity:'critical',evidence:candidate.evidence.slice(7),fix:'Use a prepared statement: db.query(sql, [id])'};
 const authorization={...candidate,title:'다른 사용자의 주문 접근',fix:'Check the authenticated user owns this order.'};
 const result=await auditSource(files,[],async role=>role==='hunter'?{findings:[sql,paraphrase,authorization]}:role.includes('verifier')?{supported:true,reason:'source supported'}:role==='critic'?{findings:[]}:{},async()=>{},'test');
 assert.equal(result.findings.length,2);assert.equal(result.calls,11);
 assert.equal(result.findings[0].severity,'critical');
 assert.ok(result.findings.some(f=>f.title===authorization.title));
});
test('read-only collector reports oversized, binary and unsupported files as excluded',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-audit-test-'));
 try{await fs.writeFile(path.join(dir,'ok.ts'),'const x=1;');await fs.writeFile(path.join(dir,'big.py'),'x'.repeat(65*1024));await fs.writeFile(path.join(dir,'binary.js'),Buffer.from([0,1]));await fs.writeFile(path.join(dir,'README.md'),'docs');
 const result=await auditFiles(dir);assert.deepEqual(result.files.map(x=>x.path),['ok.ts']);assert.deepEqual(result.skipped.sort(),['big.py','binary.js','README.md'].sort());}
 finally{assert.equal(path.dirname(dir),os.tmpdir());await fs.rm(dir,{recursive:true,force:true});}
});

test('SQL duplicates with wider context share a sink but distinct SQL call sites survive',async()=>{
 const source=[{path:'src/routes.ts',text:files[0].text+'\nfunction other(id) {\n return db.query("SELECT * FROM orders WHERE id=" + id);\n}'}];
 const sql={...candidate,title:'SQL 인젝션'};
 const wide={...sql,title:'SQL 주입',line:1,endLine:3,fix:'매개변수 바인딩을 사용합니다.'};
 const second={...sql,line:5,endLine:5};
 const result=await auditSource(source,[],async role=>role==='hunter'?{findings:[sql,wide,second]}:role.includes('verifier')?{supported:true,reason:'source supported'}:role==='critic'?{findings:[]}:{},async()=>{},'test');
 assert.equal(result.findings.length,2);assert.equal(result.calls,11);
 assert.deepEqual(result.findings.map(f=>f.line),[2,5]);
});

test('missing or blank verifier reasons cannot confirm a finding',async()=>{
 const result=await auditSource(files,[],async role=>role==='hunter'?{findings:[candidate]}:role.includes('verifier')?{supported:true,reason:'  '}:role==='critic'?{findings:[]}:{},async()=>{},'test');
 assert.equal(result.findings.length,0);assert.equal(result.needsValidation.length,1);
 assert.match(result.needsValidation[0].reason,/이유를 확인하지 못했습니다/);
});

test('two SQL operations on one line are not collapsed by identical remediation',async()=>{
 const source=[{path:'src/routes.ts',text:'db.query("SELECT " + first); db.query("SELECT " + second);'}];
 const first={...candidate,title:'SQL 주입',line:1,endLine:1,evidence:'db.query("SELECT " + first)'};
 const second={...first,evidence:'db.query("SELECT " + second)'};
 const result=await auditSource(source,[],async role=>role==='hunter'?{findings:[first,second]}:role.includes('verifier')?{supported:true,reason:'source supported'}:role==='critic'?{findings:[]}:{},async()=>{},'test');
 assert.equal(result.findings.length,2);assert.equal(result.calls,11);
});
test('Bedrock diagnostics expose only an allowlisted error category, never raw CLI text',()=>{
 const {bedrockFailure}=require('../out/security/managedAudit');
 for(const detail of ['An error occurred (AccessDeniedException): PRIVATE_SOURCE SECRET_KEY','Invalid choice: PRIVATE_SOURCE','unknown PRIVATE_SOURCE SECRET_KEY']){
  const safe=bedrockFailure(detail);assert.doesNotMatch(safe,/PRIVATE_SOURCE|SECRET_KEY/);assert.match(safe,/^Bedrock /);
 }
 assert.match(bedrockFailure('AccessDeniedException'),/AccessDeniedException/);
});
test('Bedrock audit accepts one data-only tool result and fails closed on prose, truncation or executable tools',()=>{
 const {bedrockAuditRequest,bedrockAuditResult}=require('../out/security/managedAudit');
 const request=bedrockAuditRequest('model','Do not execute code, use tools, emit secrets.',{files:[]});
 assert.equal(request.toolConfig.tools.length,1);assert.deepEqual(request.toolConfig.toolChoice,{any:{}});
 assert.equal(request.toolConfig.tools[0].toolSpec.strict,true);
 for(const role of ['recon','hunter','critic','verifier','final-verifier']){
  const schema=bedrockAuditRequest('model','audit',{},role).toolConfig.tools[0].toolSpec.inputSchema.json;
  const walk=value=>{if(value&&typeof value==='object'){if(value.type==='object'){assert.equal(value.additionalProperties,false);assert.deepEqual(value.required,Object.keys(value.properties));}for(const nested of Object.values(value))walk(nested);}};
  walk(schema);assert.deepEqual(Object.keys(schema.properties.result.properties),role==='recon'?['boundaries','entrypoints','unknowns']:role.includes('verifier')?['supported','reason']:['findings']);
 }
 assert.match(request.system[0].text,/invoke executable tools/);
 const data={files:[{path:'app.py',text:'first\nsecond'}]};
 const numbered=JSON.parse(bedrockAuditRequest('model','audit',data).messages[0].content[0].text);
 assert.equal(numbered.files[0].numberedText,'1|first\n2|second');assert.equal(data.files[0].text,'first\nsecond');
 const tool={toolUse:{name:'submit_audit_result',input:{result:{findings:[]}}}};
 assert.deepEqual(bedrockAuditResult({stopReason:'tool_use',output:{message:{content:[{text:'Commentary is not the result'},tool]}}}),{findings:[]});
 const encoded=result=>({stopReason:'tool_use',output:{message:{content:[{toolUse:{name:'submit_audit_result',input:{result}}}]}}});
 assert.deepEqual(bedrockAuditResult(encoded('{"supported":true,"reason":"Source proof"}')),{supported:true,reason:'Source proof'});
 for(const value of ['{"supported":true} trailing prose','[{}]','null','"nested string"','{"supported":'])assert.throws(()=>bedrockAuditResult(encoded(value)),/구조화/);
 for(const response of [
  {stopReason:'end_turn',output:{message:{content:[{text:'{"findings":[]}'}]}}},
  {stopReason:'max_tokens',output:{message:{content:[tool]}}},
  {stopReason:'tool_use',output:{message:{content:[tool,tool]}}},
  {stopReason:'tool_use',output:{message:{content:[{toolUse:{name:'exec',input:{result:{}}}}]}}},
  {stopReason:'tool_use',output:{message:{content:[{toolUse:{name:'submit_audit_result',input:{result:[]}}}]}}}
 ])assert.throws(()=>bedrockAuditResult(response),/구조화/);
});
