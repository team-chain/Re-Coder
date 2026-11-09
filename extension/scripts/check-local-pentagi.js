/** Opt-in Docker integration check. Never uses a real model key or paid generation. */
const assert=require('node:assert/strict');
const {randomBytes}=require('node:crypto');
const http=require('node:http');
const {LocalPentagiRuntime,docker}=require('../out/security/localPentagi');
const {PentagiClient}=require('../out/security/pentagiClient');
async function check() {
 if(process.env.RECODER_PENTAGI_DOCKER_CHECK!=='1')throw new Error('Set RECODER_PENTAGI_DOCKER_CHECK=1 to create and remove a temporary Docker test environment.');
 const session='rc-rt-'+randomBytes(12).toString('hex');
 const runtime=new LocalPentagiRuntime(session);
 const before=(await docker(['container','ls','-aq'])).split(/\r?\n/).filter(Boolean);
 const server=http.createServer((req,res)=>res.end('recoder-owned-local-fixture'));
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 try {
  const connection=await runtime.prepare('integration-placeholder-not-a-real-api-key','openai',async step=>console.log(step));
  const client=new PentagiClient(connection.server,connection.token);
  assert.ok((await client.providers()).includes('openai'));
  console.log('PASS: private bootstrap and API authentication');
  const target=`http://host.docker.internal:${server.address().port}/`;
  const response=await docker(['exec',session+'-engine','docker','run','--rm','--network','host','busybox:1.37.0','wget','-T','10','-qO-',target],{timeout:120000});
  assert.equal(response,'recoder-owned-local-fixture');console.log('PASS: nested worker reaches local target');
  const flow=await client.start(target,'openai');
  assert.equal((await client.snapshot(flow.id)).flow.id,flow.id);
  await client.finish(flow.id);
  console.log('PASS: actual flow create / snapshot / cancel during preparation');
 } finally {
  server.close();await runtime.cleanup();
  for(const kind of ['container','volume','network']) {
   const ids=await docker([kind,'ls',...(kind==='container'?['-a']:[]),'--filter','label=io.recoder.redteam.session='+session,'-q']);
   assert.equal(ids,'',`${kind} leaked`);
  }
  const after=new Set((await docker(['container','ls','-aq'])).split(/\r?\n/));
  for(const existing of before)assert.ok(after.has(existing),'An existing container disappeared during the check');
  console.log('PASS: owned resources deleted; pre-existing containers retained');
 }
}
check().catch(error=>{console.error(error.message);process.exitCode=1;});
