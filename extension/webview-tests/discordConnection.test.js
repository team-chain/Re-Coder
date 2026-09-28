const test=require('node:test'),assert=require('node:assert/strict'),http=require('node:http'),path=require('node:path'),fs=require('node:fs'),os=require('node:os');
const {DiscordConnectionClient,serverUrl,projectId,loginProof}=require('../out/discord/connectionClient');
test('hosted connections require TLS and login redirects cannot leave the configured server',()=>{
 for(const url of ['http://example.com','https://token@example.com','https://example.com/path','https://example.com?token=secret'])assert.throws(()=>serverUrl(url));
 assert.equal(serverUrl('http://127.0.0.1:8765/'),'http://127.0.0.1:8765');
 const client=new DiscordConnectionClient('https://bot.example');
 assert.throws(()=>client.authorizeUrl('https://evil.test/api/v1/connect/authorize/id'));
 assert.equal(client.websocketUrl(),'wss://bot.example/api/v1/connect/ws');
 assert.notEqual(projectId('machine','/one'),projectId('machine','/two'));
 assert.notEqual(projectId('machine','/one'),projectId('other','/one'));
 assert.match(loginProof().challenge,/^[a-f0-9]{64}$/);
});
test('HTTP credentials stay in headers and invalid sessions have actionable errors',async t=>{
 const requests=[],server=http.createServer((req,res)=>{requests.push({url:req.url,token:req.headers.authorization});res.setHeader('Content-Type','application/json');res.statusCode=401;res.end(JSON.stringify({error:'다시 로그인하세요.'}));});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));t.after(()=>new Promise(r=>{server.closeAllConnections();server.close(r);}));
 const client=new DiscordConnectionClient(`http://127.0.0.1:${server.address().port}`);
 await assert.rejects(client.call('status','private-bearer'),e=>e.message.includes('다시 로그인')&&!e.message.includes('private-bearer'));
 assert.equal(requests[0].token,'Bearer private-bearer');assert.ok(!requests[0].url.includes('private-bearer'));
 const offline=new DiscordConnectionClient('https://bot.example',async()=>{throw new Error('secret URL');});
 await assert.rejects(offline.call('status','private-bearer'),/서버가 실행 중인지/);
});
const Module=require('node:module'),resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const vscode=require('vscode');vscode.StatusBarAlignment={Right:2};const {DiscordController}=require('../out/discord/DiscordController'),{BridgeClient}=require('../out/bridge/BridgeClient');
Module._resolveFilename=resolve;
test('login secrets are scoped to server and selected project; expired sessions stay reconnectable',async t=>{
 const prior={fetch:global.fetch,folders:vscode.workspace.workspaceFolders,config:vscode.workspace.getConfiguration,progress:vscode.window.withProgress,trust:vscode.workspace.isTrusted};
 t.after(()=>{global.fetch=prior.fetch;vscode.workspace.workspaceFolders=prior.folders;vscode.workspace.getConfiguration=prior.config;vscode.window.withProgress=prior.progress;vscode.workspace.isTrusted=prior.trust;});
 vscode.workspace.workspaceFolders=[{uri:vscode.Uri.file('/tmp/project-a'),name:'A'},{uri:vscode.Uri.file('/tmp/project-b'),name:'B'}];vscode.workspace.isTrusted=true;vscode.env.machineId='test-machine';
 let base='https://bot.example',expired=false;vscode.workspace.getConfiguration=()=>({get:()=>base});vscode.window.withProgress=async(opts,run)=>run({report(){}},{onCancellationRequested:()=>({dispose(){}})});
 const saved=new Map(),requests=[],context={subscriptions:[],secrets:{get:async k=>saved.get(k),store:async(k,v)=>saved.set(k,v),delete:async k=>saved.delete(k)}};
 global.fetch=async(url,opts)=>{requests.push({url,opts});const route=new URL(url).pathname.split('/').at(-1);let result={},status=200;
 if(route==='start')result={authorize_url:base+'/api/v1/connect/authorize/fixture',attempt:'fixture',confirmation_code:'ABCDEF'};
 if(route==='poll')result={status:'connected',token:'session-secret'};
 if(route==='status'){result=expired?{error:'다시 로그인하세요.'}:{authenticated:true,project_id:projectId('test-machine','/tmp/project-a'),username:'alice'};status=expired?401:200;}
 if(route==='info')result={bot_ready:true,oauth_ready:true};return new Response(JSON.stringify(result),{status});};
 const controller=new DiscordController(context);t.after(()=>controller.dispose());const bridges=[];controller.bridge=async(client,folder,token)=>bridges.push({root:folder.uri.fsPath,token});
 const state=await controller.action('connect',{workspace:'/tmp/project-a'});assert.equal(state.authenticated,true);assert.ok(!JSON.stringify(state).includes('session-secret'));assert.equal(saved.size,1);assert.equal(bridges[0].root,'/tmp/project-a');
 assert.equal((await controller.action('status',{workspace:'/tmp/project-b'})).authenticated,false);expired=true;
 const retry=await controller.action('status',{workspace:'/tmp/project-a'});assert.equal(retry.authenticated,false);assert.equal(retry.projects.length,2);assert.match(retry.connection_error,/다시 로그인/);
 base='https://different.example';assert.equal((await controller.action('status',{workspace:'/tmp/project-a'})).authenticated,false);assert.ok(!requests.filter(r=>r.url.startsWith(base)).some(r=>r.opts.headers.Authorization));
});
test('OAuth bridge keeps burst chunks ordered and writes only the selected project',async t=>{
 const {WebSocketServer}=require('ws');const dir=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-oauth-')),first=path.join(dir,'first'),second=path.join(dir,'second');fs.mkdirSync(first);fs.mkdirSync(second);
 const prior={folders:vscode.workspace.workspaceFolders,editor:vscode.window.showTextDocument,bar:vscode.window.createStatusBarItem,status:vscode.window.setStatusBarMessage,align:vscode.StatusBarAlignment};
 vscode.workspace.workspaceFolders=[{uri:vscode.Uri.file(first)},{uri:vscode.Uri.file(second)}];vscode.window.showTextDocument=async()=>{await new Promise(r=>setTimeout(r,40));throw new Error('fixture uses disk fallback');};vscode.window.createStatusBarItem=()=>({show(){},dispose(){}});vscode.window.setStatusBarMessage=()=>{};vscode.StatusBarAlignment={Right:2};
 const server=new WebSocketServer({port:0,host:'127.0.0.1'});await new Promise(r=>server.on('listening',r));
 const received=new Promise(resolve=>server.once('connection',(socket,request)=>{resolve(request);socket.send(JSON.stringify({type:'start',project_id:'selected',filename:'hello.txt'}));for(let i=0;i<100;i++)socket.send(JSON.stringify({type:'chunk',project_id:'selected',text:`${i}\n`}));socket.send(JSON.stringify({type:'end',project_id:'selected',filename:'hello.txt'}));}));
 const bridge=new BridgeClient({subscriptions:[],secrets:{get:async()=>undefined}},{root:vscode.Uri.file(second),url:`ws://127.0.0.1:${server.address().port}`,token:'scoped',projectId:'selected'});
 t.after(async()=>{bridge.dispose();for(const client of server.clients)client.terminate();await new Promise(r=>server.close(r));vscode.workspace.workspaceFolders=prior.folders;vscode.window.showTextDocument=prior.editor;vscode.window.createStatusBarItem=prior.bar;vscode.window.setStatusBarMessage=prior.status;vscode.StatusBarAlignment=prior.align;fs.rmSync(dir,{recursive:true,force:true});});
 bridge.connect();assert.equal((await received).headers.authorization,'Bearer scoped');let text='';for(let i=0;i<100;i++){await new Promise(r=>setTimeout(r,10));try{text=fs.readFileSync(path.join(second,'hello.txt'),'utf8');}catch{}if(text.endsWith('99\n'))break;}
 assert.equal(text,Array.from({length:100},(_,i)=>`${i}\n`).join(''));assert.equal(fs.existsSync(path.join(first,'hello.txt')),false);
 await bridge._handleMessage({type:'start',project_id:'another-project',filename:'wrong.txt'});assert.equal(fs.existsSync(path.join(second,'wrong.txt')),false);
});
test('default screen offers login and project selection, with visible offline and connected states',()=>{
 const React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),{DiscordPanel}=require('../out/webview-test/components/canvas/DiscordPanel');
 const render=state=>renderToStaticMarkup(React.createElement(DiscordPanel,{state,events:[],enabled:false,onEnabled(){},post(){},guilds:[],channels:[],error:''}));
 const offline=render({mode:'oauth',project_id:'a',project_name:'A',projects:[{id:'a',name:'A'}],connection_error:'봇 서버가 오프라인입니다.'});assert.match(offline,/Discord 연결/);assert.match(offline,/연결 서버 설정/);assert.match(offline,/봇 서버가 오프라인/);
 const ready=render({mode:'oauth',authenticated:true,username:'alice',project_id:'a',canvas_project_id:'a',project_name:'A',projects:[{id:'a',name:'A'}],active_channel_id:'123',development:true});assert.match(ready,/서버에 봇 초대/);assert.match(ready,/테스트 알림 보내기/);assert.match(ready,/recoder develop/);assert.match(ready,/alice/);
 const chat=render({mode:'oauth',authenticated:true,username:'alice',project_id:'a',canvas_project_id:'a',active_channel_id:'123',development:true,plain_chat_ready:true});assert.match(chat,/그냥 채팅으로 요청하세요/);assert.match(chat,/이 채널에 내가 보내는 채팅/);assert.doesNotMatch(chat,/운영자가 Message Content Intent를 켜고/);
 assert.match(ready,/운영자가 Message Content Intent를 켜고/);
});
