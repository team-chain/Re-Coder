const test=require('node:test'),assert=require('node:assert/strict'),http=require('node:http'),net=require('node:net');
const {once}=require('node:events'),{spawn}=require('node:child_process'),fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {SCOPE_RELAY,scopeConfig,prepareLocalScope,LOCAL_TOOLBOX,LOCAL_WORKER}=require('../out/security/localRedTeamScope');
test('scope preparation pins toolbox and separates worker, relay and host networks',async()=>{
 const calls=[];
 const run=async a=>{calls.push(a);return a.includes('getent')?'192.168.65.254 host.docker.internal':a.includes('image')&&a.includes('inspect')?'sha256:'+'1'.repeat(64):'';};
 assert.equal(await prepareLocalScope('test-engine','http://host.docker.internal:3000/app',run),'http://172.30.0.10:3000/app');
 const create=calls.find(a=>a.includes('--subnet'));assert.ok(create.includes('--internal'));assert.ok(create.includes('com.docker.network.bridge.gateway_mode_ipv4=isolated'));
 const relay=calls.find(a=>a.includes('recoder-scope-proxy')&&a.includes('run'));
 assert.ok(relay.includes('--cap-drop=ALL'));assert.ok(relay.includes('net.ipv4.ip_forward=0'));assert.ok(relay.includes(LOCAL_WORKER));
 assert.ok(!calls.some(a=>a.includes('-p')||a.includes('--privileged')));
 await assert.rejects(()=>prepareLocalScope('test-engine','http://host.docker.internal:3000',async()=>''),/내용 ID/);
 assert.throws(()=>scopeConfig('http://10.0.0.1:3000'));assert.throws(()=>scopeConfig('http://host.docker.internal:3000?secret=value'));
});

test('toolbox is downloaded by immutable digest inside the dedicated engine and verified before use',async()=>{
 const calls=[];let pulled=false,tagged=false;
 const run=async a=>{calls.push(a);if(a.includes('getent'))return '192.168.65.254 host.docker.internal';
  if(a.includes('pull'))pulled=true;if(a.includes('tag'))tagged=true;
  if(a.includes('image')&&a.includes('inspect')){if(a.includes(LOCAL_TOOLBOX)&&!pulled)throw Error('missing');if(a.includes(LOCAL_WORKER)&&!tagged)throw Error('missing');return 'sha256:'+'1'.repeat(64);}return '';};
 await prepareLocalScope('test-engine','http://host.docker.internal:3000',run);
 assert.ok(calls.some(a=>a[0]==='exec'&&a.includes('pull')&&a.includes(LOCAL_TOOLBOX)));
 assert.ok(calls.every(a=>a[0]==='exec'),'no host daemon changes or host image transfer');
 pulled=false;tagged=false;
 await assert.rejects(()=>prepareLocalScope('test-engine','http://host.docker.internal:3000',async a=>{
  const result=await run(a);return a.includes('inspect')&&a.includes(LOCAL_WORKER)&&tagged?'sha256:'+'2'.repeat(64):result;
 }),/내용 검증/);
});
test('real loopback relay enforces scope, forwards to one app and omits secrets from telemetry',{timeout:30000},async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-scope-'));
 const hits=[];const app=http.createServer((req,res)=>{hits.push({path:req.url,host:req.headers.host});res.end('fixture-app');});
 app.listen(0,'127.0.0.1');await once(app,'listening');
 const free=net.createServer();free.listen(0,'127.0.0.1');await once(free,'listening');const port=free.address().port;await new Promise(r=>free.close(r));
 const config={host:'127.0.0.1',authority:'localhost:'+app.address().port,port:app.address().port,scheme:'http',prefix:'/app',listenPort:port,bind:'127.0.0.1',journal:path.join(root,'events.jsonl')};
 const child=spawn('python',['-I','-u','-c',SCOPE_RELAY,JSON.stringify(config)],{windowsHide:true,stdio:['ignore','ignore','pipe']});
 let errors='';child.stderr.on('data',x=>errors+=x.toString());let spawnError;
 child.on('error',e=>spawnError=e);
 t.after(async()=>{const stopped=child.exitCode!==null||child.signalCode!==null?Promise.resolve():once(child,'close');child.kill();await stopped;app.closeAllConnections();await new Promise(r=>app.close(r));assert.equal(path.dirname(root),os.tmpdir());assert.ok(path.basename(root).startsWith('recoder-scope-'));await fs.rm(root,{recursive:true,force:true});});
 for(let i=0;i<100;i++){
  if(spawnError)throw spawnError;
  const ready=await new Promise(r=>{const socket=net.connect(port,'127.0.0.1');socket.once('connect',()=>{socket.destroy();r(true);});socket.once('error',()=>r(false));});
  if(ready)break;if(i===99)throw new Error('relay did not start: '+errors);await new Promise(r=>setTimeout(r,100));
 }
 async function request(url,method='GET',headers={}){
  await new Promise(r=>setTimeout(r,230));
  return new Promise((resolve,reject)=>{const req=http.request({hostname:'127.0.0.1',port,path:url,method,headers},res=>{let body='';res.on('data',x=>body+=x);res.on('end',()=>resolve({status:res.statusCode,body}));});req.on('error',reject);req.end();});
 }
 assert.equal((await request('/app/search?q=PRIVATE_QUERY','GET',{Host:'unapproved.example',Authorization:'Bearer PRIVATE_TOKEN'})).body,'fixture-app');
 for(const route of ['/outside','/app/%2e%2e/outside','/app/%252e%252e/outside','/app\\..\\outside','http://unapproved.example/app']){
  assert.equal((await request(route)).status,403);
 }
 assert.equal((await request('/app/item','DELETE')).status,405);
 assert.equal(hits.length,1);assert.equal(hits[0].host,config.authority);
 const journal=await fs.readFile(config.journal,'utf8');assert.ok(!journal.includes('PRIVATE_QUERY'));assert.ok(!journal.includes('PRIVATE_TOKEN'));assert.ok(!journal.includes('unapproved.example'));
 const events=journal.trim().split('\n').map(JSON.parse);assert.equal(events.length,7);assert.equal(events[0].path,'/app/search');assert.equal(events[0].blocked,false);assert.ok(events.slice(1).every(e=>e.blocked));
});
