const test=require('node:test'),assert=require('node:assert/strict'),http=require('node:http'),{once}=require('node:events');
const {runAccessChecks,validateAccessPolicy,withAccessResult}=require('../out/security/redTeamAccess');
const {assessPentagi}=require('../out/security/pentagiAssessment');
function policy(base){return {schema:1,target:base,users:[{label:'A',kind:'bearer',credential:'TOKEN_A_TEST',path:'/orders/a',marker:'owner-a-test-marker'},{label:'B',kind:'bearer',credential:'TOKEN_B_TEST',path:'/orders/b',marker:'owner-b-test-marker'}]};}
async function fixture(t,mode){
 const calls=[];const server=http.createServer((req,res)=>{
  calls.push(req.url);const owner=req.url.endsWith('/a')?'A':'B',auth=req.headers.authorization;
  if(mode==='redirect'){res.writeHead(302,{Location:'/should-not-follow'});res.end();return;}
  if(mode==='expired'){res.writeHead(401);res.end();return;}
  if(mode==='safe'&&auth!=='Bearer TOKEN_'+owner+'_TEST'){res.writeHead(auth?403:401);res.end();return;}
  res.setHeader('Content-Type','application/json');res.end(JSON.stringify({marker:'owner-'+owner.toLowerCase()+'-test-marker'}));
 });
 server.listen(0,'127.0.0.1');await once(server,'listening');
 t.after(async()=>{server.closeAllConnections();await new Promise(r=>server.close(r));});
 return {base:'http://127.0.0.1:'+server.address().port+'/',calls};
}
test('real HTTP access matrix distinguishes protected and vulnerable ownership',async t=>{
 for(const mode of ['safe','vulnerable']){
  const {base,calls}=await fixture(t,mode),p=policy(base),result=await runAccessChecks(p);
  assert.equal(result.status,'verified',JSON.stringify(result));assert.equal(calls.length,6);assert.equal(result.observations.length,6);
  assert.equal(result.findings.length,mode==='safe'?0:4);
  assert.ok(result.findings.every(f=>f.evidence==='http_observed'));
  assert.ok(!JSON.stringify(result).includes('TOKEN_'));assert.ok(!JSON.stringify(result).includes('owner-a-test-marker'));
  const assessment=withAccessResult(assessPentagi(),result);
  assert.equal(assessment.checks.find(c=>c.kind==='authorization').status,'tested');
  assert.equal(assessment.status,'incomplete'); // Other checks were not executed.
 }
});
test('expired sessions and redirects are blocked, never counted as secure',async t=>{
 for(const mode of ['expired','redirect']){
  const {base,calls}=await fixture(t,mode),result=await runAccessChecks(policy(base));
  assert.equal(result.status,'blocked');assert.equal(result.findings.length,0);
  assert.ok(calls.length<=2);assert.ok(!calls.includes('/should-not-follow'));
 }
});
test('access policy rejects wrong scopes, duplicate identities and ambiguous paths',()=>{
 const original=policy('http://localhost:3000/app');
 for(const path of ['//evil.test/a','/app/../outside','/app/%2e%2e/outside','/other','/app\\outside']){
  const p=structuredClone(original);p.users[0].path=path;p.users[1].path='/app/b';assert.throws(()=>validateAccessPolicy(p));
 }
 const p=policy('http://localhost:3000/');p.users[1].credential=p.users[0].credential;assert.throws(()=>validateAccessPolicy(p));
 assert.throws(()=>validateAccessPolicy(policy('http://localhost:3000/'),'http://localhost:3001/'));
 assert.throws(()=>validateAccessPolicy(policy('http://192.168.0.2:3000/')));
});
test('oversized response and cancelled checks are incomplete',async()=>{
 const p=policy('http://localhost:3000/');
 const large=async()=>new Response('x'.repeat(70000));
 assert.equal((await runAccessChecks(p,undefined,undefined,large)).status,'blocked');
 const c=new AbortController();c.abort();let calls=0;
 const result=await runAccessChecks(p,c.signal,undefined,async()=>{calls++;return new Response('');});
 assert.equal(calls,0);assert.equal(result.status,'blocked');
});

test('access evidence resolves only its own gaps and preserves invalid report warnings',()=>{
 const base={schema:1,status:'incomplete',checks:['discovery','sql_injection','input_handling','authentication','authorization'].map(kind=>({kind,status:kind==='authentication'||kind==='authorization'?'blocked':'tested',reason:'test',requests:[]})),findings:[],warnings:['인증·권한 검사는 승인된 테스트 계정이 필요합니다.','미실행·차단된 검사 범위가 있습니다.'],limitations:[]};
 const access={status:'verified',reason:'six responses compared',observations:[],findings:[]};
 assert.equal(withAccessResult(base,access).status,'review_required');
 assert.deepEqual(withAccessResult(base,access).warnings,[]);
 base.warnings.push('유효하지 않은 CVSS 벡터를 제외했습니다.');
 assert.equal(withAccessResult(base,access).status,'incomplete');
});
