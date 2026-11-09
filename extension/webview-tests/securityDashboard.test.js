const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {overview} = require('../out/webview-test/components/securityOverview');
const {SecurityDashboard} = require('../out/webview-test/components/SecurityDashboard');
const {PentagiClient,serviceUrl,testTarget} = require('../out/security/pentagiClient');

test('security summary never treats missing, excluded or failed scans as passed', () => {
 const code={score:100,findings:[]};
 for(const status of ['error','not_run','unverified',undefined]) {
  const r=overview({gitleaks:{status},trivy:{status:'ok'},hadolint:{status:'ok'}},code);
  assert.equal(r.partial,true);assert.doesNotMatch(r.title,/문제를 찾지 못/);
 }
 assert.equal(overview({trivy:{status:'not_run',reason_code:'image_not_found'},hadolint:{status:'ok'},gitleaks:{status:'ok'}},code).partial,true);
 assert.equal(overview({trivy:{status:'ok'},hadolint:{status:'ok'},gitleaks:{status:'ok'}},{score:null,findings:[]}).partial,true);
 assert.equal(overview({trivy:{status:'ok'},hadolint:{status:'ok'},gitleaks:{status:'ok'}},code).partial,false);
});
test('dashboard retains scan remediation, policy, monitoring and red team entry without deploy assurance', () => {
 const html=renderToStaticMarkup(React.createElement(SecurityDashboard));
 for(const label of ['보안 검사 시작','레드팀 테스트','시크릿','Dockerfile','OPA','SBOM','업데이트 제안'])assert.ok(html.includes(label),label);
 assert.doesNotMatch(html,/지금 배포해도 됩니다|준비 중|100점/);
});
test('managed scan summary counts full remote findings and never masks failed or unexecuted checks',()=>{
 const code={score:100,findings:[]};
 const report={mode:'scans',status:'completed',scans:{checks:['gitleaks','hadolint','dependencies','image'].map(tool=>({tool,status:'completed',count:0,findings:[]})),policy:{decision:'allow'}}};
 assert.equal(overview({},code,false,'',report).partial,false);
 report.scans.checks[3].count=250;assert.equal(overview({},code,false,'',report).count,250);
 report.scans.checks[3].count=0;
 for(const status of ['failed','not_applicable']){report.scans.checks[3].status=status;assert.equal(overview({},code,false,'',report).partial,true);}
 report.scans.checks[3].status='completed';report.status='incomplete';assert.equal(overview({},code,false,'',report).partial,true);
 report.status='completed';report.scans.policy.decision='deny';assert.match(overview({},code,false,'',report).title,/정책/);
});
test('PentAGI credential destination and test targets are validated',()=>{
 assert.equal(serviceUrl('https://pentagi.example'),'https://pentagi.example');
 for(const url of ['http://pentagi.example','https://user:password@host','https://host/path','https://host/?token=x'])assert.throws(()=>serviceUrl(url));
 for(const target of ['http://host.docker.internal:3000','http://192.168.1.2:3000','http://127.0.0.1:3000'])assert.ok(testTarget(target));
 for(const target of ['https://example.com','http://169.254.169.254','file:///C:/test','http://127.0.0.1?token=secret','http://127.0.0.1.example.org'])assert.throws(()=>testTarget(target));
});
test('PentAGI creates once using variables, polls actual tasks and requires stop acknowledgement',async()=>{
 const calls=[];
 const transport=async(url,options)=>{calls.push({url,options,body:JSON.parse(options.body)});const q=calls.at(-1).body.query;
  return new Response(JSON.stringify({data:q.includes('createFlow')?{createFlow:{id:'12',title:'test',status:'created'}}:q.includes('stopFlow')?{stopFlow:'success'}:{flow:{id:'12',title:'test',status:'running'},tasks:[{id:'1',title:'verify',status:'running',result:''}]}}));};
 const client=new PentagiClient('https://pentagi.example','secret',transport);
 assert.equal((await client.start('http://host.docker.internal:3000','my-provider')).id,'12');
 assert.equal((await client.snapshot('12')).tasks.length,1);await client.stop('12');
 assert.equal(calls.length,3);assert.equal(calls[0].options.redirect,'error');assert.equal(calls[0].body.variables.provider,'my-provider');
 assert.ok(!calls[0].body.query.includes('host.docker.internal'));assert.equal(calls[0].options.headers.Authorization,'Bearer secret');
});
test('PentAGI never retries ambiguous creation or exposes upstream secrets',async()=>{
 let calls=0;const client=new PentagiClient('https://pentagi.example','TOKEN',async()=>{calls++;return new Response(JSON.stringify({errors:[{message:'TOKEN user secret'}]}));});
 await assert.rejects(()=>client.start('http://127.0.0.1:3000','openai'),e=>!e.message.includes('TOKEN'));
 assert.equal(calls,1);
 const refused=new PentagiClient('https://pentagi.example','TOKEN',async()=>new Response(JSON.stringify({data:{stopFlow:'error'}})));
 await assert.rejects(()=>refused.stop('12'),/중단/);
});
test('official PentAGI numeric GraphQL IDs normalize for creation and snapshots',async()=>{
 const client=new PentagiClient('http://127.0.0.1:8443','TOKEN',async(url,options)=>new Response(JSON.stringify({data:JSON.parse(options.body).query.includes('createFlow')?{createFlow:{id:17,title:'untitled',status:'created'}}:{flow:{id:17,title:'test',status:'waiting'},tasks:[{id:23,title:'check',status:'running',result:''}]}})));
 assert.equal((await client.start('http://host.docker.internal:3000','openai')).id,'17');
 const snapshot=await client.snapshot('17');assert.equal(snapshot.flow.id,'17');assert.equal(snapshot.tasks[0].id,'23');
});
test('PentAGI scope includes app routes but keeps explicit path boundaries and untrusted content limits',async()=>{
 const prompts=[];
 const client=new PentagiClient('http://127.0.0.1:8443','TOKEN',async(url,options)=>{
  prompts.push(JSON.parse(options.body).variables.input);
  return Response.json({data:{createFlow:{id:'1',title:'check',status:'created'}}});
 });
 await client.start('http://127.0.0.1:3000/','openai');
 await client.start('http://127.0.0.1:3000/my-app','openai');
 assert.match(prompts[0],/All application paths on origin http:\/\/127.0.0.1:3000/);
 assert.match(prompts[1],/exact path \/my-app and descendants under \/my-app\//);
 assert.match(prompts[1],/Sibling paths are out of scope/);
 for(const input of prompts){assert.match(input,/untrusted evidence/);assert.match(input,/Do not follow redirects or links to other hosts/);}
});
