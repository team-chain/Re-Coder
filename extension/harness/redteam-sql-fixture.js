// Local acceptance fixture only. Never imported or shipped by the extension.
// Requires Node with node:sqlite (22.13+). Does not call an AI provider itself.
const http=require('node:http'),fs=require('node:fs/promises'),path=require('node:path'),{randomUUID}=require('node:crypto');
const {assessPentagi}=require('../out/security/pentagiAssessment');
function checkJournal(report,events){
 const a=assessPentagi(report.tasks,report.evidence,report.target);
 const seen=(request,response)=>events.some(e=>e.request===request&&e.response===response);
 const hit=a.findings.find(f=>f.kind==='sql_injection'&&f.route==='/search'&&seen(f.request,f.response)&&seen(f.controlRequest,f.controlResponse)&&f.response.includes('synthetic-marker-')&&!f.controlResponse.includes('synthetic-marker-'));
 const wrong=a.findings.filter(f=>f.route==='/safe');
 const vectorErrors=a.warnings.filter(w=>w.includes('CVSS'));
 const safeRequests=new Set(events.filter(e=>{try{return e.request.startsWith('GET /safe?')&&e.response==='[]'&&/\b(?:OR|AND)\s+\S+\s*=/i.test(decodeURIComponent(e.request.replace(/\+/g,' ')));}catch{return false;}}).map(e=>e.request));
 const negativeControlsExecuted=safeRequests.size>=2;
 const completedAndCleaned=report.outcome==='finished'&&report.phase==='removed'||report.status==='completed'&&report.cleaned===true;
 return {passed:!!hit&&!wrong.length&&!vectorErrors.length&&negativeControlsExecuted&&completedAndCleaned,completedAndCleaned,negativeControlsExecuted,sqlDetectedWithRecordedResponses:!!hit,falsePositivesOnBoundRoute:wrong.length,invalidVectors:vectorErrors.length,assessment:a,
  limitation:'This validates one synthetic SQL case, not all routes, authentication, authorization or other models.'};
}
async function main(){
 const args=process.argv.slice(2),value=name=>args[args.indexOf(name)+1];
 if(args[0]==='verify'){
  if(!args.includes('--report')||!args.includes('--journal'))throw Error('verify --report report.json --journal requests.jsonl');
  const report=JSON.parse(await fs.readFile(value('--report'),'utf8')),events=(await fs.readFile(value('--journal'),'utf8')).trim().split(/\r?\n/).filter(Boolean).map(JSON.parse);
  const result=checkJournal(report,events);console.log(JSON.stringify(result,null,2));if(!result.passed)process.exitCode=1;return;
 }
 if(args[0]!=='serve')throw Error('serve [--port 19371] [--out requests.jsonl] or verify --report report.json --journal requests.jsonl');
 const port=args.includes('--port')?Number(value('--port')):19371;if(!Number.isInteger(port)||port<1024||port>65535)throw Error('Port must be 1024–65535.');
 const destination=path.resolve(args.includes('--out')?value('--out'):'../.test-artifacts/redteam-requests-'+Date.now()+'.jsonl');
 await fs.mkdir(path.dirname(destination),{recursive:true});await fs.writeFile(destination,'',{flag:'wx',mode:0o600});
 const {DatabaseSync}=require('node:sqlite'),db=new DatabaseSync(':memory:');db.exec('CREATE TABLE items(name TEXT, public INTEGER);');
 const insert=db.prepare('INSERT INTO items VALUES (?,?)');insert.run('public',1);insert.run('synthetic-marker-'+randomUUID(),0);
 let writes=Promise.resolve();
 const server=http.createServer(async(req,res)=>{
  let response='',status=200;
  try{
   const url=new URL(req.url,'http://localhost');
   if(req.method!=='GET'){status=405;response='Only read-only GET requests are supported.';}
   else if(url.pathname==='/'){res.setHeader('Content-Type','text/html; charset=utf-8');response='<h1>Synthetic test catalog</h1><p>Disposable fixture. No real users or records.</p><a href="/search?q=public">Search catalog</a> <a href="/safe?q=public">Bound-query negative control</a>';}
   else if(['/search','/safe'].includes(url.pathname)){
    const q=url.searchParams.get('q')||'';if(q.length>1024)throw Error('size');
    // Intentionally vulnerable SELECT, solely in this disposable in-memory fixture.
    const rows=url.pathname==='/search'?db.prepare("SELECT name FROM items WHERE public=1 AND name='"+q+"'").all():db.prepare('SELECT name FROM items WHERE public=1 AND name=?').all(q);
    response=JSON.stringify(rows);res.setHeader('Content-Type','application/json');
   }else{status=404;response='Unknown fixture route';}
  }catch{status=400;response='Invalid fixture input';}
  const event={at:new Date().toISOString(),request:req.method+' '+req.url,status,response};
  writes=writes.then(()=>fs.appendFile(destination,JSON.stringify(event)+'\n'));
  try{await writes;res.writeHead(status);res.end(response);}catch{res.writeHead(500);res.end('Fixture evidence could not be saved');}
 });
 server.listen(port,'127.0.0.1',()=>console.log(`Synthetic fixture: http://localhost:${port}/\nRequest journal: ${destination}\nUse ReCoder's existing red-team connection. AI charges require the normal approval. Ctrl+C stops only this fixture.`));
 const close=()=>{server.closeAllConnections();server.close(()=>{void writes.finally(()=>{db.close();});});};
 process.once('SIGINT',close);process.once('SIGTERM',close);
}
module.exports={checkJournal};
if(require.main===module)main().catch(e=>{console.error(e.message);process.exitCode=1;});
