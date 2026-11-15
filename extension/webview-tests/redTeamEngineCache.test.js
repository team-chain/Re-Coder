// 공격 테스트 준비 시간 단축: 전용 엔진의 도구 이미지만 세션 사이에 남긴다(4.0.0).
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {LocalPentagiRuntime,ENGINE_CACHE,ENGINE_CACHE_LABEL,PENTAGI_IMAGES,engineCacheWarm,clearEngineCache}=require('../out/security/localPentagi');
const {LOCAL_TOOLBOX,LOCAL_WORKER}=require('../out/security/localRedTeamScope');
const {runPrewarm,readPrewarm,prewarmPaths,PREWARM_ENGINE}=require('../out/security/redTeamPrewarm');
const {ensureRedTeamCapacity}=require('../out/security/redTeamCapacity');
const {EngineCacheRow}=require('../out/webview-test/components/RedTeamPanel.js');
const id='rc-rt-0123456789abcdef01234567',TOOL='sha256:'+'1'.repeat(64),OTHER='sha256:'+'9'.repeat(64);

function fakeDocker({cacheExists=false,cacheUsers=[],images=[TOOL,OTHER]}={}) {
 const calls=[];
 const run=async(args,options={})=>{
  calls.push({args,options});
  if(args[0]==='context')return 'npipe:////./pipe/docker_engine';
  if(args[0]==='info')return 'linux|8589934592|4';
  if(args[0]==='volume'&&args[1]==='ls')return cacheExists?ENGINE_CACHE:'';
  if(args[0]==='container'&&args[1]==='ls'&&args.some(a=>a.startsWith('volume=')))return cacheUsers.join('\n');
  if(args.includes('getent'))return '192.168.65.254 host.docker.internal';
  if(args.includes('image')&&args.includes('ls'))return images.join('\n');
  if(args.includes('image')&&args.includes('inspect'))return TOOL;
  if(args.includes('psql')&&options.input.startsWith('SELECT'))return '1';
  if(args[0]==='port')return '127.0.0.1:49152';
  return '';
 };
 return {calls,run};
}
function mockFetch(t){t.mock.method(global,'fetch',async url=>url.endsWith('/info')?new Response('{}'):url.endsWith('/auth/login')?new Response('{}',{headers:{'set-cookie':'auth=session; HttpOnly'}}):Response.json({data:{token:'temporary-token'}}));}

test('local run mounts the kept tool store, resets it to the pinned toolbox before scoping, and keeps isolation',async t=>{
 mockFetch(t);const {calls,run}=fakeDocker();
 const connection=await new LocalPentagiRuntime(id,run).prepare('KEY','openai',async()=>{},undefined,'http://host.docker.internal:3000/');
 assert.equal(connection.engineCache,ENGINE_CACHE);
 const create=calls.find(c=>c.args[0]==='volume'&&c.args[1]==='create'&&c.args.includes(ENGINE_CACHE));
 assert.ok(create.args.includes(`${ENGINE_CACHE_LABEL}=engine`),'kept volume is labeled as cache, never as a session resource');
 assert.ok(!create.args.includes('io.recoder.redteam.session='+id));
 assert.ok(!calls.some(c=>c.args[0]==='volume'&&c.args.includes(id+'-engine-data')),'no per-session engine store when the cache is used');
 const engine=calls.findIndex(c=>c.args[0]==='run'&&c.args.includes(id+'-engine'));
 assert.ok(calls[engine].args.includes(`${ENGINE_CACHE}:/var/lib/docker`));
 assert.ok(calls[engine].args.includes('--storage-driver=overlay2'));
 const reset=calls.findIndex(c=>c.args[0]==='exec'&&c.args[2]==='sh'&&c.args[4].includes('docker rm -f'));
 const removeOther=calls.findIndex(c=>c.args.includes('rm')&&c.args.includes(OTHER));
 const scope=calls.findIndex(c=>c.args.includes('--subnet'));
 assert.ok(engine<reset&&reset<removeOther&&removeOther<scope,'old workers, networks, volumes and foreign images go before the run');
 assert.ok(!calls.some(c=>c.args.includes('rm')&&c.args.includes(TOOL)),'pinned toolbox stays');
 assert.ok(calls.every(c=>c.args[0]==='exec'||!c.args.some(a=>a==='prune'||a==='rmi')&&!(c.args[0]==='image'&&c.args[1]!=='inspect')&&c.args[0]!=='system'),'host images and host daemon are never pruned');
 assert.ok(calls.some(c=>c.args.includes('tag')&&c.args.includes(LOCAL_WORKER))||calls.some(c=>c.args.includes(LOCAL_WORKER)),'worker tag is still verified');
});

test('a tool store in use by another engine is not shared — the run falls back to its own disposable store',async t=>{
 mockFetch(t);const {calls,run}=fakeDocker({cacheExists:true,cacheUsers:[PREWARM_ENGINE]});
 const connection=await new LocalPentagiRuntime(id,run).prepare('KEY','openai',async()=>{},undefined,'http://host.docker.internal:3000/');
 assert.equal(connection.engineCache,undefined);
 const engine=calls.find(c=>c.args[0]==='run'&&c.args.includes(id+'-engine'));
 assert.ok(engine.args.includes(`${id}-engine-data:/var/lib/docker`));
 assert.ok(!calls.some(c=>c.args[0]==='exec'&&c.args[2]==='sh'),'nothing to reset in a fresh store');
});

test('managed runs never use the local tool store',async t=>{
 mockFetch(t);const {calls,run}=fakeDocker();
 await new LocalPentagiRuntime(id,run).prepare('','bedrock',async()=>{},{configPath:'/tmp/x.yml',workerImage:'w@sha256:'+'1'.repeat(64),bedrockEnv:{},prepareTarget:async()=>{}});
 assert.ok(!calls.some(c=>c.args.includes(ENGINE_CACHE)));
});

test('cleanup empties the engine (not its images), stops dockerd cleanly and never deletes the kept store',async()=>{
 const calls=[];const run=async args=>{calls.push(args);if(args[1]==='ls')return [id+'-engine',id+'-db-data',ENGINE_CACHE].join('\n');return '';};
 await new LocalPentagiRuntime(id,run).cleanup();
 const reset=calls.findIndex(a=>a[0]==='exec'&&a[2]==='sh'),stop=calls.findIndex(a=>a[0]==='stop'),rm=calls.findIndex(a=>a[0]==='container'&&a[1]==='rm'&&a.at(-1)===id+'-engine');
 assert.ok(reset>=0&&reset<stop&&stop<rm);
 assert.ok(!calls[reset][4].includes('image'),'cleanup keeps images');
 assert.ok(!calls.some(a=>a.includes(ENGINE_CACHE)));
});

test('pre-warm pulls only the pinned toolbox into the kept store with no ports, sockets, keys or target, then removes its engine',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-prewarm-'));
 t.after(()=>fs.rm(root,{recursive:true,force:true}));
 t.mock.method(require('../out/security/redTeamCapacity'),'ensureRedTeamCapacity',async()=>{});
 let pulled=false;const {calls,run:base}=fakeDocker({images:[TOOL]});
 const names=new Set();
 const run=async(args,o)=>{
  if(args[0]==='run')names.add(PREWARM_ENGINE);
  if(args[0]==='container'&&args[1]==='rm')names.delete(args.at(-1));
  if(args[0]==='container'&&args[1]==='ls'&&args.some(a=>a.startsWith('label=io.recoder.redteam.prewarm'))){calls.push({args,options:o});return [...names].join('\n');}
  if(args.includes('pull')&&args.includes(LOCAL_TOOLBOX))pulled=true;
  if(args.includes('inspect')&&args.includes(LOCAL_TOOLBOX)&&!pulled){calls.push({args,options:o});throw new Error('missing');}
  return base(args,o);
 };
 const state=await runPrewarm(root,run);
 assert.equal(state.phase,'done',state.error);
 const engine=calls.find(c=>c.args[0]==='run'&&c.args.includes(PREWARM_ENGINE)).args;
 assert.ok(engine.includes(`${ENGINE_CACHE}:/var/lib/docker`));assert.ok(engine.includes(PENTAGI_IMAGES.engine));
 assert.ok(!engine.includes('-p')&&!engine.some(a=>a.includes('tcp://')||a.includes('docker.sock:')),'no published port, TCP API or host socket');
 assert.ok(calls.some(c=>c.args[0]==='exec'&&c.args.includes('pull')&&c.args.includes(LOCAL_TOOLBOX)));
 assert.ok(!calls.some(c=>c.args[0]==='pull'&&c.args.includes(LOCAL_TOOLBOX)),'toolbox never lands on the host daemon');
 assert.ok(!names.has(PREWARM_ENGINE),'pre-warm engine is removed');
 assert.equal(JSON.parse(await fs.readFile(path.join(root,'engine-cache.json'),'utf8')).toolboxId,TOOL);
 assert.equal((await readPrewarm(root)).phase,'done');
 assert.equal(await engineCacheWarm(root,async a=>a[1]==='ls'&&a[0]==='volume'?ENGINE_CACHE:''),true);
 assert.equal(await engineCacheWarm(root,async()=>''),false,'marker alone is not enough once the volume is gone');
});

test('pre-warm stops on request and still removes its engine',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-prewarm-'));
 t.after(()=>fs.rm(root,{recursive:true,force:true}));
 t.mock.method(require('../out/security/redTeamCapacity'),'ensureRedTeamCapacity',async()=>{});
 const removed=[];const {run:base}=fakeDocker();let started=false;
 const run=async(args,o)=>{
  if(args[0]==='run'){started=true;await fs.writeFile(prewarmPaths(root).cancel,'stop');}
  if(args[0]==='container'&&args[1]==='ls'&&args.some(a=>a.startsWith('label=io.recoder.redteam.prewarm')))return started?PREWARM_ENGINE:'';
  if(args[0]==='container'&&args[1]==='rm')removed.push(args.at(-1));
  return base(args,o);
 };
 const state=await runPrewarm(root,run);
 assert.equal(state.phase,'cancelled');assert.deepEqual(removed,[PREWARM_ENGINE]);
 await assert.rejects(fs.stat(path.join(root,'engine-cache.json')));
});

test('a dead pre-warm process is never shown as still downloading',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-prewarm-'));
 t.after(()=>fs.rm(root,{recursive:true,force:true}));
 await fs.writeFile(prewarmPaths(root).state,JSON.stringify({phase:'running',step:'x',pid:2**22+12345,updatedAt:''}));
 assert.equal((await readPrewarm(root)).phase,'failed');
});

test('clearing the store refuses while an engine uses it',async()=>{
 await assert.rejects(()=>clearEngineCache(async a=>a[0]==='volume'?ENGINE_CACHE:'engine'),/진행 중/);
 const removed=[];assert.equal(await clearEngineCache(async a=>{if(a[1]==='rm')removed.push(a.at(-1));return a[0]==='volume'&&a[1]==='ls'?ENGINE_CACHE:'';}),true);
 assert.deepEqual(removed,[ENGINE_CACHE]);
});

test('with the toolbox already kept, the free-space floor drops from 50GB to 20GB',async()=>{
 const root=path.resolve('r'),io={readFile:async()=>{throw 0;},realpath:async()=>'/disk',statfs:async p=>({bsize:4096,bavail:Math.floor((p===root?1:30)*1024**3/4096)})};
 await assert.rejects(()=>ensureRedTeamCapacity(root,{LOCALAPPDATA:'/l'},'win32',io),/50GB/);
 await ensureRedTeamCapacity(root,{LOCALAPPDATA:'/l'},'win32',io,true);
});

test('tool store row tells the user what to expect in each state',()=>{
 const html=e=>renderToStaticMarkup(React.createElement(EngineCacheRow,{busy:false,blocked:false,send:()=>{},engine:e}));
 assert.match(html({warm:false,prewarm:null}),/17GB.*미리 받기/s);
 assert.match(html({warm:false,prewarm:{phase:'running',step:'검사 도구 설치'}}),/받는 중 · 검사 도구 설치.*중단/s);
 assert.match(html({warm:true,prewarm:{phase:'done',step:''}}),/준비됨 · 바로 시작/);
});
