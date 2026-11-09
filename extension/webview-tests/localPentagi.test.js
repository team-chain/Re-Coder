const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {LocalPentagiRuntime,localTarget,sessionPaths,saveReport,readReport,redactReport,PENTAGI_IMAGES}=require('../out/security/localPentagi');
const id='rc-rt-0123456789abcdef01234567';
test('local targets normalize host access and reject remote, ambiguous or secret-bearing URLs',()=>{
 assert.equal(localTarget('http://localhost:3000/app'),'http://host.docker.internal:3000/app');
 assert.equal(localTarget('http://[::1]:5173'),'http://host.docker.internal:5173/');
 for(const u of ['https://example.com:443','http://192.168.1.2:3000','http://localhost','http://localhost:3000?token=x','http://user:password@localhost:3000'])assert.throws(()=>localTarget(u));
 for(const name of ['../outside','rc-rt-*',id+'/../other'])assert.throws(()=>sessionPaths(os.tmpdir(),name));
});
test('cleanup removes only exact labeled session resources, leaves unrelated and same-prefix resources alone',async()=>{
 const calls=[],resources={container:new Set([id+'-app',id+'-engine',id+'-unexpected','existing-project']),volume:new Set([id+'-engine-data',id+'-db-data',id+'-unrelated']),network:new Set([id+'-runtime',id+'-database','default'])};
 const run=async args=>{calls.push(args);if(args[1]==='ls'){assert.ok(args.includes('label=io.recoder.redteam.session='+id));return [...resources[args[0]]].join('\n');}if(args[1]==='rm')resources[args[0]].delete(args.at(-1));return '';};
 const runtime=new LocalPentagiRuntime(id,run);await runtime.cleanup();await runtime.cleanup();
 assert.deepEqual([...resources.container],[id+'-unexpected','existing-project']);
 assert.deepEqual([...resources.volume],[id+'-unrelated']);assert.deepEqual([...resources.network],['default']);
 assert.ok(calls.every(args=>!args.includes('prune')&&!args.includes('image')));
 assert.ok(calls.filter(args=>args[1]==='rm').every(args=>args.at(-1).startsWith(id+'-')));
});
test('cleanup does not claim success if Docker refuses to delete the owned engine',async()=>{
 const removed=[];const runtime=new LocalPentagiRuntime(id,async args=>{if(args[1]==='ls')return id+'-engine';if(args[1]==='rm'){removed.push(args.at(-1));throw new Error('in use');}return '';});
 await assert.rejects(()=>runtime.cleanup(),/in use/);assert.deepEqual(removed,[id+'-engine']);
});
test('preparation bootstraps privately, pins images, keeps credentials off CLI and never mounts host Docker',async t=>{
 const calls=[],http=[];
 t.mock.method(global,'fetch',async(url,init={})=>{http.push({url,init});if(url.endsWith('/info'))return new Response('{}');if(url.endsWith('/auth/login'))return new Response('{}',{headers:{'set-cookie':'auth=session; HttpOnly'}});return Response.json({data:{token:'temporary-token'}});});
 const runtime=new LocalPentagiRuntime(id,async(args,options={})=>{calls.push({args,options});if(args[0]==='context')return 'npipe:////./pipe/docker_engine';if(args[0]==='info')return 'linux|8589934592|4';if(args.includes('psql')&&options.input.startsWith('SELECT'))return '1';if(args[0]==='port')return '127.0.0.1:49152';return '';});
 const connection=await runtime.prepare('PRIVATE_API_KEY','openai',async()=>{});
 assert.equal(connection.server,'http://127.0.0.1:49152');assert.equal(connection.token,'temporary-token');
 assert.ok(Object.values(PENTAGI_IMAGES).every(x=>/@sha256:[a-f0-9]{64}$/.test(x)));
 const app=calls.find(c=>c.args[0]==='create'&&c.args.includes(id+'-app'));
 const bootstrap=calls.find(c=>c.args[0]==='create'&&c.args.includes(id+'-bootstrap'));
 assert.ok(app.args.includes('127.0.0.1::8443'));assert.ok(!bootstrap.args.includes('-p'));
 assert.ok(app.args.includes('DOCKER_HOST=tcp://engine:2375'));assert.ok(app.args.includes('DOCKER_NETWORK=host'));
 assert.equal(app.options.env.OPEN_AI_KEY,'PRIVATE_API_KEY');
 assert.ok(!JSON.stringify(calls.map(c=>c.args)).includes('PRIVATE_API_KEY'));
 assert.ok(!calls.some(c=>c.args.some(a=>a.includes('/var/run/docker.sock:'))));
 const rotate=calls.findIndex(c=>c.options.input?.includes('password_change_required=false'));
 assert.ok(rotate>0&&rotate<calls.indexOf(app));assert.equal(http.at(-1).init.headers.Cookie,'auth=session');
});
test('saved reports survive environment deletion and are scoped to a validated session directory',async()=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-redteam-test-'));
 const report={session:id,workspace:'project',target:'http://host.docker.internal:3000/',createdAt:new Date().toISOString(),phase:'saving',step:'결과 저장',tasks:[{id:'1',title:'Headers',result:'Evidence stays available'}]};
 try {await saveReport(root,report);report.phase='removed';await saveReport(root,report);assert.equal((await readReport(root,id)).tasks[0].result,'Evidence stays available');}
 finally {assert.equal(path.dirname(root),os.tmpdir());assert.ok(path.basename(root).startsWith('recoder-redteam-test-'));await fs.rm(root,{recursive:true,force:true});}
});
test('upstream task output cannot persist the supplied model key or temporary API token',()=>{
 const report={session:id,error:'KEY_PRIVATE',tasks:[{result:'key KEY_PRIVATE and token TEMP_TOKEN',title:'Evidence'}]};
 const cleaned=redactReport(report,['KEY_PRIVATE','TEMP_TOKEN']);
 assert.ok(!JSON.stringify(cleaned).includes('KEY_PRIVATE'));assert.ok(!JSON.stringify(cleaned).includes('TEMP_TOKEN'));
 assert.equal(cleaned.tasks[0].title,'Evidence');assert.equal(report.error,'KEY_PRIVATE');
});
test('remote Docker contexts are rejected before any resource creation',async()=>{
 const calls=[];const runtime=new LocalPentagiRuntime(id,async args=>{calls.push(args);return 'ssh://remote-server';});
 await assert.rejects(()=>runtime.prepare('key','openai',async()=>{}),/로컬 Docker/);assert.equal(calls.length,1);
});
test('preparation rejects Docker allocations below the supported memory and CPU floor',async()=>{
 const calls=[];const runtime=new LocalPentagiRuntime(id,async args=>{calls.push(args);if(args[0]==='context')return 'npipe:////./pipe/docker_engine';if(args[0]==='info')return 'linux|4294967296|2';return '';});
 await assert.rejects(()=>runtime.prepare('key','openai',async()=>{}),/최소 메모리 6GB/);
 assert.equal(calls.filter(a=>a[0]==='pull'||a[0]==='network').length,0);
});
test('Anthropic preparation injects only the selected provider secret',async t=>{
 const calls=[];
 t.mock.method(global,'fetch',async(url,init={})=>{
  if(url.endsWith('/info'))return new Response('{}');
  if(url.endsWith('/auth/login'))return new Response('{}',{headers:{'set-cookie':'auth=session; HttpOnly'}});
  return Response.json({data:{token:'temporary-token'}});
 });
 const runtime=new LocalPentagiRuntime(id,async(args,options={})=>{
  calls.push({args,options});
  if(args[0]==='context')return 'npipe:////./pipe/docker_engine';
  if(args[0]==='info')return 'linux|8589934592|4';
  if(args.includes('psql')&&options.input.startsWith('SELECT'))return '1';
  if(args[0]==='port')return '127.0.0.1:49152';
  return '';
 });
 await runtime.prepare('ANTHROPIC_PRIVATE_KEY','anthropic',async()=>{});
 const app=calls.find(c=>c.args[0]==='create'&&c.args.includes(id+'-app'));
 assert.ok(app.args.includes('ANTHROPIC_API_KEY'));assert.equal(app.options.env.ANTHROPIC_API_KEY,'ANTHROPIC_PRIVATE_KEY');
 assert.equal(app.options.env.OPEN_AI_KEY,undefined);assert.ok(!JSON.stringify(calls.map(c=>c.args)).includes('ANTHROPIC_PRIVATE_KEY'));
});
