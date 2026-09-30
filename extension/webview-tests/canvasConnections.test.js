const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),http=require('node:http'),Module=require('node:module');
const {execFileSync}=require('node:child_process');
const resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const vscode=require('vscode');
const {CanvasHost,gitContext,githubRepository,publicGit}=require('../out/sidebar/canvasHost');
Module._resolveFilename=resolve;
function fixture(t) {
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-github-'));
 t.after(()=>{assert.ok(path.resolve(dir).startsWith(path.join(os.tmpdir(),'recoder-github-')));fs.rmSync(dir,{recursive:true,force:true});});
 const git=(...args)=>execFileSync('git',['-C',dir,...args],{encoding:'utf8',windowsHide:true,stdio:['ignore','pipe','pipe']}).trim();
 const output=[],connections=[];
 const api={getGithubStatus:async()=>({status:'authenticated',user:'tester'}),listGithubRepos:async()=>({status:'ok',repos:[{name:'tester/repo',private:true}]}),githubConnectRepository:async p=>{connections.push(p);return {status:'ok'};}};
 const host=new CanvasHost(api);vscode.workspace.workspaceFolders=[{uri:{fsPath:dir}}];
 const send=(type,p={})=>host.handle(type,{workspace:dir,requestId:'fixture',...p},(type,payload)=>output.push({type,payload}),()=>'',async()=>assert.fail('Must not deploy'));
 return {dir,git,host,api,send,output,connections};
}

test('first-time connection initializes Git and origin but never commits/uploads project files',async t=>{
 const s=fixture(t);fs.writeFileSync(path.join(s.dir,'app.js'),'console.log("fixture")');
 await s.send('canvas.github.connect',{repository:'https://github.com/tester/repo.git'});
 assert.equal(s.output.at(-1).type,'canvas.github.result');
 const state=s.output.at(-1).payload.git;
 assert.equal(state.initialized,true);assert.equal(state.repository,'tester/repo');assert.equal(state.branch,'main');assert.equal(state.head,'');assert.equal(state.dirty,true);
 assert.equal(s.git('remote','get-url','origin'),'https://github.com/tester/repo.git');
 assert.throws(()=>s.git('rev-parse','--verify','HEAD'));
 assert.deepEqual(s.connections,[{repository:'tester/repo',create:false,private:true}]);
 s.git('add','app.js');s.git('-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','-m','Initial');
 await s.send('canvas.github.status');assert.ok(s.output.at(-1).payload.git.head);
});

test('unborn repositories preserve branch/origin and never expose remote credentials',async t=>{
 const s=fixture(t);s.git('init','--initial-branch=main');s.git('remote','add','origin','https://fixture-secret@github.com/tester/repo.git');
 const git=await gitContext(s.dir);
 assert.equal(git.repository,'tester/repo');assert.equal(git.branch,'main');assert.equal(git.head,'');
 assert.equal(JSON.stringify(publicGit(git)).includes('fixture-secret'),false);
});

test('existing origin and changed workspace cannot be overwritten by connection requests',async t=>{
 const s=fixture(t);s.git('init','--initial-branch=main');s.git('remote','add','origin','git@github.com:tester/keep.git');
 await s.send('canvas.github.connect',{repository:'tester/other',create:true});
 assert.equal(s.output.at(-1).type,'canvas.error');assert.equal(s.connections.length,0);
 assert.equal(s.git('remote','get-url','origin'),'git@github.com:tester/keep.git');
 await s.send('canvas.github.connect',{repository:'tester/other',workspace:'/other'});
 assert.equal(s.output.at(-1).type,'canvas.error');assert.equal(s.connections.length,0);
});

test('GitHub login returns a correlated status even when native authentication is cancelled',async t=>{
 const s=fixture(t);s.api.getGithubStatus=async()=>({status:'unauthenticated'});
 await s.send('canvas.github.login');
 const result=s.output.at(-1);
 assert.equal(result.type,'canvas.github.result');assert.equal(result.payload.requestId,'fixture');assert.match(result.payload.message,/완료되지 않았습니다/);
});

test('repository input rejects tokens, other hosts and traversal',()=>{
 for(const value of ['https://evil.test/a/b','https://secret@github.com/a/b','a/../b','a/..'])assert.throws(()=>githubRepository(value));
 assert.equal(githubRepository('org/repo.git'),'org/repo');
 // 사용자가 흔히 넣는 형식을 모두 받는다.
 for(const value of ['https://github.com/LDK511/lunch-vote','http://github.com/LDK511/lunch-vote/','github.com/LDK511/lunch-vote.git','www.github.com/LDK511/lunch-vote','git@github.com:LDK511/lunch-vote.git',' LDK511/lunch-vote '])assert.equal(githubRepository(value),'LDK511/lunch-vote',value);
 // 새 저장소는 GitHub 웹과 같이 공백·한글을 -로 바꿔 만든다(예전엔 형식 오류).
 assert.equal(githubRepository('LDK511/Recoder Demo',true),'LDK511/Recoder-Demo');
 assert.equal(githubRepository('LDK511/점심 투표 app',true),'LDK511/app');
 assert.throws(()=>githubRepository('LDK511/점심투표',true),/영문·숫자/);
 assert.throws(()=>githubRepository('LDK511/Recoder Demo'),/공백·한글 불가/);
 assert.throws(()=>githubRepository('/lunch-vote',true),/형식/);
});

test('Discord loopback bridge supports status, guild/channel selection and opt-in events',async t=>{
 const s=fixture(t),requests=[];
 const server=http.createServer(async(req,res)=>{
  let raw='';for await(const chunk of req)raw+=chunk;
  requests.push({url:req.url,method:req.method,key:req.headers['x-registration-key'],body:raw?JSON.parse(raw):null});
  res.setHeader('Content-Type','application/json');
  if(req.url.endsWith('/status'))return res.end(JSON.stringify({active_channel_id:'123',channel_name:'alerts'}));
  if(req.url.endsWith('/guilds'))return res.end(JSON.stringify({guilds:[{id:'456',name:'Fixture'}]}));
  if(req.url.endsWith('/channels'))return res.end(JSON.stringify({guild_id:'456',channels:[{id:'123',name:'alerts'}]}));
  if(req.url.endsWith('/events'))return res.end(JSON.stringify({ok:true,event_id:'event-1'}));
  res.end('{}');
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const original=vscode.workspace.getConfiguration;
 vscode.workspace.getConfiguration=()=>({get:key=>({host:'127.0.0.1',httpPort:server.address().port,registrationKey:'fixture-key'})[key]});
 try {
  await s.send('canvas.discord.status',{mode:'bot'});assert.equal(s.output.at(-1).payload.channel_name,'alerts');
  await s.send('canvas.discord.guilds');assert.equal(s.output.at(-1).payload.guilds[0].id,'456');
  await s.send('canvas.discord.channels',{guildId:'456'});assert.equal(s.output.at(-1).payload.channels[0].id,'123');
  await s.send('canvas.discord.setChannel',{channelId:'123'});assert.equal(requests.find(r=>r.method==='PUT').body.channel_id,'123');
  await s.send('canvas.discord.event',{eventId:'event-1'});assert.equal(s.output.at(-1).type,'canvas.error');assert.equal(requests.some(r=>r.url.endsWith('/events')),false);
  await s.send('canvas.discord.event',{eventId:'event-1',enabled:true,title:'Fixture',detail:'Completed'});assert.equal(s.output.at(-1).payload.event_id,'event-1');
  assert.ok(requests.every(r=>r.key==='fixture-key'));
 } finally {vscode.workspace.getConfiguration=original;server.closeAllConnections();await new Promise(r=>server.close(r));}
});

test('offline Discord gives an actionable error instead of hiding its entry point',async t=>{
 const s=fixture(t),original=global.fetch;
 global.fetch=async()=>{throw new TypeError('fetch failed');};
 try {await s.send('canvas.discord.status',{mode:'bot'});assert.equal(s.output.at(-1).type,'canvas.error');assert.match(s.output.at(-1).payload.message,/봇 서버\(127\.0\.0\.1:8765\)가 실행 중이 아닙니다/);assert.match(s.output.at(-1).payload.message,/내 봇 토큰으로 연결/);}
 finally {global.fetch=original;}
});

test('a failed bot lookup keeps an existing webhook connection working',async t=>{
 const {DiscordWebhook}=require('../out/sidebar/discordWebhook');
 const store=new Map(),secrets={get:async k=>store.get(k),store:async(k,v)=>{store.set(k,v);},delete:async k=>{store.delete(k);}};
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-discord-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const calls=[],original=global.fetch,originalInput=vscode.window.showInputBox;
 const hook='https://discord.com/api/webhooks/123456789012345678/'+'a'.repeat(60);
 global.fetch=async(url,options={})=>{
  calls.push({url:String(url),method:options.method||'GET'});
  if(String(url).startsWith('http://127.0.0.1'))throw new TypeError('fetch failed');
  return new Response(JSON.stringify(options.method==='POST'?{id:'m1'}:{type:1,channel_id:'234567890123456789',guild_id:'345678901234567890',name:'alerts'}),{status:200});
 };
 vscode.window.showInputBox=async()=>hook;
 const output=[],host=new CanvasHost({},undefined,new DiscordWebhook(secrets));vscode.workspace.workspaceFolders=[{uri:{fsPath:dir}}];
 const send=(type,p={})=>host.handle(type,{requestId:'fixture',...p},(type,payload)=>output.push({type,payload}),()=>'',async()=>assert.fail('Must not deploy'));
 try {
  await send('canvas.discord.connectWebhook');assert.equal(output.at(-1).payload.channel_name,'alerts');
  await send('canvas.discord.status',{mode:'bot'});assert.equal(output.at(-1).type,'canvas.error');assert.match(output.at(-1).payload.message,/내 봇 토큰으로 연결/);
  await send('canvas.discord.status');assert.equal(output.at(-1).type,'canvas.discord.statusResult');assert.equal(output.at(-1).payload.mode,'webhook');assert.equal(output.at(-1).payload.channel_name,'alerts');
  await send('canvas.discord.event',{enabled:true,eventId:'e1',title:'배포 완료',detail:'ok'});assert.equal(output.at(-1).type,'canvas.discord.eventResult');
  assert.ok(calls.some(c=>c.method==='POST'&&c.url.startsWith('https://discord.com/api/webhooks/')));
 } finally {global.fetch=original;vscode.window.showInputBox=originalInput;}
});
