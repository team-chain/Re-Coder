'use strict';
// Explicit local Docker check. Starts no AI flow and uses no real model credential.
const assert=require('node:assert/strict'),http=require('node:http'),crypto=require('node:crypto');
const fs=require('node:fs/promises'),path=require('node:path'),{once}=require('node:events');
const {LocalPentagiRuntime,docker}=require('../out/security/localPentagi');
const {ensureRedTeamCapacity}=require('../out/security/redTeamCapacity');
const {LOCAL_WORKER,SCOPE_NETWORK}=require('../out/security/localRedTeamScope');
const {PentagiClient}=require('../out/security/pentagiClient');
async function main(){
 const root=path.resolve(__dirname,'../../.test-artifacts');await fs.mkdir(root,{recursive:true});
 await ensureRedTeamCapacity(root);
 const session='rc-rt-'+crypto.randomBytes(12).toString('hex'),engine=session+'-engine';
 const evidence={session,modelCalls:0,stage:'preparing',checks:[],startedAt:new Date().toISOString()};
 const save=()=>fs.writeFile(path.join(root,'redteam-runtime-result.json'),JSON.stringify(evidence,null,2));
 const calls=[];
 const app=http.createServer((req,res)=>{calls.push(req.url);res.setHeader('Content-Type','application/json');res.end(JSON.stringify({fixture:true,path:req.url}));});
 app.listen(0,'127.0.0.1');await once(app,'listening');
 const port=app.address().port,runtime=new LocalPentagiRuntime(session);
 await save();
 try {
  const connection=await runtime.prepare('sk-recorder-offline-fixture-not-a-real-key','openai',async step=>{evidence.stage=step;console.log(step);await save();},undefined,'http://host.docker.internal:'+port+'/fixture');
  assert.equal(connection.isolationVerified,true);
  assert.equal(new URL(connection.server).hostname,'127.0.0.1');
  evidence.checks.push('isolated runtime and temporary API authentication');
  const providers=await new PentagiClient(connection.server,connection.token).providers();
  assert.ok(providers.includes('openai')); // Lists configuration; never starts an AI run.
  evidence.checks.push('configured provider discovery without model calls');
  const probe=String.raw`import urllib.request,urllib.error,socket,time,json
base='http://172.30.0.10:${port}'
def request(path,method='GET'):
 time.sleep(.25)
 try:
  r=urllib.request.urlopen(urllib.request.Request(base+path,method=method),timeout=12)
  return r.status,r.read().decode()
 except urllib.error.HTTPError as e: return e.code,e.read().decode()
code,body=request('/fixture/allowed?secret=do-not-log')
assert code==200 and '"fixture":true' in body,(code,body)
assert request('/outside')[0]==403
assert request('/fixture/delete','DELETE')[0]==405
assert request('/fixture/%2e%2e/outside')[0]==403
for host,port in [('172.30.0.1',2375),('172.17.0.1',2375),('172.30.0.10',${port+1})]:
 try:
  s=socket.create_connection((host,port),1);s.close()
 except OSError: continue
 raise Exception('off-scope reachable')
print(json.dumps({'allowed':200,'outside':403,'delete':405,'encodedTraversal':403,'offScope':'blocked'}))
`;
  evidence.http=JSON.parse(await docker(['exec',engine,'docker','run','--rm','--network',SCOPE_NETWORK,'--cap-drop=ALL','--security-opt=no-new-privileges',LOCAL_WORKER,'python3','-c',probe],{timeout:60000}));
  evidence.checks.push('actual allowed HTTP, forbidden path/method/traversal and engine access');
  const events=await runtime.events();
  assert.equal(events.length,4);assert.equal(events[0].path,'/fixture/allowed');assert.equal(events.filter(e=>e.blocked).length,3);
  assert.ok(!JSON.stringify(events).includes('do-not-log'));assert.equal(calls.length,1);
  evidence.events=events;evidence.checks.push('actual request telemetry without query secrets');
 } catch(error){evidence.error=error.message;throw error;}
 finally{
  evidence.stage='cleanup';await save();
  try {
   await runtime.cleanup();
   for(const kind of ['container','volume','network']){
    const args=[kind,'ls',...(kind==='container'?['-a']:[]),'--filter','label=io.recoder.redteam.session='+session,'--format',kind==='container'?'{{.Names}}':'{{.Name}}'];
    assert.equal(await docker(args),'',kind+' cleanup');
   }
   evidence.cleaned=true;
  }
  catch(error){evidence.cleanupError=error.message;process.exitCode=1;}
  app.closeAllConnections();await new Promise(resolve=>app.close(resolve));
  evidence.completedAt=new Date().toISOString();evidence.stage=evidence.error||evidence.cleanupError?'failed':'passed';await save();
 }
 console.log('PASS: local runtime, target isolation, HTTP telemetry and cleanup; 0 AI calls');
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
