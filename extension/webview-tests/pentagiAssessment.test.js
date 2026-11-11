const test=require('node:test'),assert=require('node:assert/strict');
const {cvss31,assessPentagi,assessmentMarkdown,redactDiagnostic}=require('../out/security/pentagiAssessment');
const {PentagiClient}=require('../out/security/pentagiClient');
const vector='CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N';
const report=()=>({schema:1,checks:[
 {kind:'discovery',status:'tested',reason:'Landing page and search form',requests:['GET /','GET /search?q=public']},
 {kind:'sql_injection',status:'tested',reason:'Boolean pair and parameterized control',requests:['GET /search?q=public','GET /search?q=%27%20OR%201%3D1--','GET /search?q=%27%20AND%201%3D2--','GET /safe?q=%27%20OR%201%3D1--']},
 {kind:'authentication',status:'blocked',reason:'No test accounts supplied',requests:[]},
 {kind:'authorization',status:'blocked',reason:'No ownership policy supplied',requests:[]},
 {kind:'input_handling',status:'not_tested',reason:'Browser execution not exercised',requests:[]}
],findings:[{title:'SQL input changes selected rows',kind:'sql_injection',route:'/search',observation:'A synthetic marker appeared for the true branch only.',remediation:'Bind the query parameter.',request:'GET /search?q=%27%20OR%201%3D1--',response:'{"marker":"synthetic"}',controlRequest:'GET /search?q=%27%20AND%201%3D2--',controlResponse:'[]',cvssVector:vector,cvssScore:9.1}],limitations:['Black-box inspection, no source code examined.']});
const assess=x=>assessPentagi([{result:'```recoder-report\n'+JSON.stringify(x)+'\n```'}],[],'http://localhost:3000/');
test('the observed AWS 9.1 arithmetic error is recomputed as 7.5; zero and scope-changed cases',()=>{
 assert.equal(cvss31(vector),7.5);
 assert.equal(cvss31('CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H'),9.8);
 assert.equal(cvss31('CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H'),10);
 assert.equal(cvss31('CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:N'),0);
 for(const invalid of ['',null,'CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H',vector+'/AV:N',vector.replace('/A:N',''),vector.replace('PR:N','PR:X'),vector.replace('/A:N','/C:N')])assert.equal(cvss31(invalid),undefined);
 const a=assess(report());assert.equal(a.findings[0].cvss.score,7.5);assert.ok(!JSON.stringify(a).includes('9.1'));assert.equal(a.findings[0].evidence,'agent_reported');
});
test('ordinary, empty and random punctuation input cannot claim SQL coverage',()=>{
 const r=report();r.checks[1].requests=['GET /search?q=normal','GET /search?q=','GET /search?q=%7B%7D'];
 const a=assess(r);assert.equal(a.checks[1].status,'not_tested');assert.equal(a.findings.length,0);assert.match(a.warnings.join(' '),/SQL/);
});
test('missing control, same response, out of scope and ambiguous schemas never produce a qualified candidate',()=>{
 for(const change of [{controlRequest:''},{controlRequest:'GET /search?q=%27%20OR%201%3D1--'},{controlResponse:'{"marker":"synthetic"}'},{request:'GET //external.example/path'},{route:'/../outside'}]){
  const r=report();Object.assign(r.findings[0],change);const a=assessPentagi([{result:'```recoder-report\n'+JSON.stringify(r)+'\n```'}],[],'http://localhost:3000/search');assert.equal(a.findings.length,0);
 }
 assert.equal(assess({schema:1,checks:null,findings:[],limitations:[]}).status,'incomplete');
 assert.equal(assessPentagi([{result:'All tasks finished. No vulnerabilities.'}]).checks.filter(c=>c.status==='not_tested').length,5);
});
test('agent claims cannot supply a trusted authentication policy; duplicate root causes do not inflate count',()=>{
 const r=report();r.checks[2]={...r.checks[2],status:'tested',requests:['GET /admin']};r.findings.push({...r.findings[0],title:'Same injection renamed'});
 const a=assess(r);assert.equal(a.checks[2].status,'blocked');assert.equal(a.findings.length,1);assert.equal(a.status,'incomplete');
});
test('duplicate coverage entries do not overwrite missing evidence',()=>{
 const r=report();r.checks.push({...r.checks[1],status:'not_tested'});const a=assess(r);assert.equal(a.checks[1].status,'not_tested');assert.equal(a.findings.length,0);
});
test('POST JSON bodies remain reproducible, different endpoint pairs and invented request links are rejected',()=>{
 const r=report();const req=x=>'POST /search HTTP/1.1\nContent-Type: application/json\n\n'+JSON.stringify({q:x});
 r.checks[1].requests=[req('public'),req("' OR 1=1--"),req("' AND 1=2--")];
 r.findings[0].request=r.checks[1].requests[1];r.findings[0].controlRequest=r.checks[1].requests[2];
 assert.equal(assess(r).findings.length,1);
 r.findings[0].controlRequest=req("' AND 1=3--");assert.equal(assess(r).findings.length,0);
 r.findings[0].controlRequest=r.checks[1].requests[2].replace('/search','/safe');r.checks[1].requests.push(r.findings[0].controlRequest);assert.equal(assess(r).findings.length,0);
});
test('duplicate probes, malformed journals and encoded scope escapes fail closed',()=>{
 const r=report();r.checks[1].requests=Array(3).fill('GET /search?q=%27%20OR%201%3D1--');assert.equal(assess(r).checks[1].status,'not_tested');
 assert.equal(assessPentagi(null,[null],undefined).findings.length,0);
 for(const route of ['/app/%252e%252e/admin','/app/%2f../admin','/app/%5cadmin']){
  const s=report();s.checks[0].requests=['GET '+route];assert.equal(assess(s).checks[0].status,'not_tested');
 }
});
test('Markdown uses literal evidence fences and never hides coverage gaps',()=>{
 const r=report();r.findings[0].response='```\n# untrusted content\n```';const md=assessmentMarkdown(assess(r));
 assert.match(md,/````text/);assert.match(md,/미검증/);assert.match(md,/7\.5/);assert.doesNotMatch(md,/9\.1/);
});
test('tokens are redacted before reports reach persistence or the webview',()=>{
 const value='Authorization: Bearer abc.def.ghi\nAWS AKIA1234567890123456\npassword="fixture-secret" sk-123456789012345678901234';
 const clean=redactDiagnostic(value);for(const s of ['abc.def.ghi','AKIA1234567890123456','fixture-secret','sk-123456789012345678901234'])assert.ok(!clean.includes(s));
});
test('all existing providers receive the same bounded coverage and final-report contract',async()=>{
 const calls=[];const client=new PentagiClient('http://127.0.0.1:8000','test',async(url,options)=>{calls.push(JSON.parse(options.body));return Response.json({data:{createFlow:{id:1,title:'test',status:'created',createdAt:''}}});});
 for(const provider of ['openai','anthropic','bedrock'])await client.start('http://127.0.0.1:3000/app',provider);
 for(const c of calls){assert.match(c.variables.input,/Sibling paths are out of scope/);assert.match(c.variables.input,/recoder-report/);assert.match(c.variables.input,/paired boolean test/);assert.match(c.variables.input,/No stacked writes/);}
});
