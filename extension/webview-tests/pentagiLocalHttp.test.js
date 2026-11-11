// Deterministic transport/SQLite oracle, NOT a real PentAGI/model accuracy test.
const test=require('node:test'),assert=require('node:assert/strict'),http=require('node:http');
const {once}=require('node:events');
const {PentagiClient,pentagiOutcome}=require('../out/security/pentagiClient');
const {assessPentagi}=require('../out/security/pentagiAssessment');
let DatabaseSync;try{({DatabaseSync}=require('node:sqlite'));}catch{}
test('loopback HTTP + SQLite: vulnerable and parameter-bound routes differ; adapter preserves actual evidence', {skip:!DatabaseSync}, async t=>{
 const db=new DatabaseSync(':memory:');
 db.exec("CREATE TABLE items(name TEXT); INSERT INTO items VALUES ('public'),('synthetic-marker');");
 let final='',flow='created',stopped=false;const received=[];
 const server=http.createServer(async(req,res)=>{
  try{
   const u=new URL(req.url,'http://localhost');
   if(u.pathname==='/api/v1/graphql'){
    let body='';for await(const chunk of req)body+=chunk;
    const message=JSON.parse(body);received.push(message);
    assert.equal(req.headers.authorization,'Bearer fixture-token');
    let data;
    if(message.query.includes('createFlow'))data={createFlow:{id:1,title:'fixture only',status:flow,createdAt:'2026-11-11T00:00:00Z'}};
    else if(message.query.includes('messageLogs'))data={messageLogs:[{id:1,type:'report',message:final,result:''}]};
    else if(message.query.includes('stopFlow')){stopped=true;data={stopFlow:'success'};}
    else data={flow:{id:1,title:'fixture only',status:flow,createdAt:''},tasks:[{id:1,title:'SQLite differential fixture',status:flow==='waiting'?'finished':'running',result:final}]};
    res.setHeader('Content-Type','application/json');res.end(JSON.stringify({data}));return;
   }
   if(!['/search','/safe'].includes(u.pathname)){res.writeHead(404);res.end();return;}
   const q=u.searchParams.get('q');const rows=u.pathname==='/safe'?db.prepare('SELECT name FROM items WHERE name = ?').all(q):db.prepare("SELECT name FROM items WHERE name = '"+q+"'").all();
   res.setHeader('Content-Type','application/json');res.end(JSON.stringify(rows));
  }catch{res.writeHead(400);res.end('invalid request');}
 });
 server.listen(0,'127.0.0.1');await once(server,'listening');
 t.after(async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));db.close();});
 const base='http://127.0.0.1:'+server.address().port;
 const payloads=['public',"' OR 1=1--","' AND 1=2--"],requests=[],responses=[];
 for(const route of ['/search','/safe']){
  for(const q of payloads){const routeQuery=route+'?q='+encodeURIComponent(q);requests.push('GET '+routeQuery);responses.push(await(await fetch(base+routeQuery)).text());}
 }
 assert.match(responses[1],/synthetic-marker/);assert.equal(responses[2],'[]');
 assert.equal(responses[4],'[]');assert.equal(responses[5],'[]');
 const client=new PentagiClient(base,'fixture-token');
 await client.start(base,'openai');flow='running';assert.equal(pentagiOutcome(await client.snapshot('1')),undefined);
 const contract={schema:1,checks:[{kind:'discovery',status:'tested',reason:'Known synthetic test routes',requests:['GET /search?q=public','GET /safe?q=public']},{kind:'sql_injection',status:'tested',reason:'True/false requests and bound-query negative route',requests}],findings:[{title:'Synthetic SQL differential',kind:'sql_injection',route:'/search',request:requests[1],response:responses[1],controlRequest:requests[2],controlResponse:responses[2],observation:'Synthetic marker is present only in the true case.',remediation:'Bind query parameters.',cvssVector:'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N'}],limitations:['No AI called; no authentication or authorization fixtures.']};
 final='```recoder-report\n'+JSON.stringify(contract)+'\n```';flow='waiting';
 const snapshot=await client.snapshot('1');assert.equal(pentagiOutcome(snapshot),'finished');
 const assessment=assessPentagi(snapshot.tasks,await client.evidence('1'),base);
 assert.equal(assessment.findings.length,1);assert.equal(assessment.findings[0].cvss.score,7.5);assert.equal(assessment.status,'incomplete');
 assert.equal(assessment.findings.some(f=>f.route==='/safe'),false);
 await client.stop('1');assert.equal(stopped,true);assert.match(received[0].variables.input,/recoder-report/);
});
