const test=require('node:test'),assert=require('node:assert/strict');
const path=require('node:path'),Module=require('node:module'),fs=require('node:fs');
const resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const vscode=require('vscode'),{CanvasHost}=require('../out/sidebar/canvasHost');
const {DiscordBotToken,normalizeBotToken,botInviteUrl,BOT_INVITE_PERMISSIONS}=require('../out/sidebar/discordBotToken');
const {DiscordWebhook}=require('../out/sidebar/discordWebhook');
Module._resolveFilename=resolve;

const TOKEN='MTIzNDU2Nzg5MDEyMzQ1Njc4.GAbCdE.'+'x'.repeat(38);
const BOT_ID='123456789012345678',GUILD='345678901234567890',CHANNEL='234567890123456789';

function discordApi({status=200,userBot=true}={}){
 const saved=new Map(),calls=[];
 const secrets={get:async k=>saved.get(k),store:async(k,v)=>saved.set(k,v),delete:async k=>saved.delete(k)};
 const routes={
  'GET /users/@me':{id:BOT_ID,username:'my-deploy-bot',bot:userBot},
  'GET /users/@me/guilds':[{id:GUILD,name:'우리 팀'}],
  [`GET /guilds/${GUILD}/channels`]:[{id:'1',type:2,name:'voice',position:0},{id:CHANNEL,type:0,name:'deploy',position:2},{id:'999999999999999999',type:0,name:'general',position:1},{id:'4',type:4,name:'category'}],
  [`GET /channels/${CHANNEL}`]:{id:CHANNEL,type:0,name:'deploy',guild_id:GUILD},
  [`GET /guilds/${GUILD}`]:{id:GUILD,name:'우리 팀'},
  [`POST /channels/${CHANNEL}/messages`]:{id:'message-id'},
 };
 const request=async(address,options)=>{
  calls.push({address,options});
  const route=`${options.method} ${address.replace('https://discord.com/api/v10','')}`;
  if(status!==200) return new Response('{}',{status});
  if(!(route in routes)) return new Response('{}',{status:404});
  return new Response(JSON.stringify(routes[route]),{status:200});
 };
 return {saved,calls,secrets,service:new DiscordBotToken(secrets,request,'1.1.13')};
}

test('봇 토큰 형식 검사: 공백·웹후크 URL·사용자 토큰 형식을 거른다',()=>{
 assert.equal(normalizeBotToken(`Bot ${TOKEN}`),TOKEN);
 assert.throws(()=>normalizeBotToken(''),/붙여넣으세요/);
 assert.throws(()=>normalizeBotToken('https://discord.com/api/webhooks/1/abc'),/웹후크/);
 assert.throws(()=>normalizeBotToken('abc def'),/공백/);
 assert.throws(()=>normalizeBotToken('short.token'),/형식/);
});

test('내 봇 토큰으로 연결 → 서버·텍스트 채널 선택 → 알림 전송 (봇 서버 없이)',async()=>{
 const d=discordApi(),ws='C:\\Users\\me\\TEMP';
 assert.deepEqual(await d.service.status(ws),{mode:'token',active_channel_id:'',bot_user:''});
 assert.deepEqual(await d.service.connect(TOKEN),{bot_user:'my-deploy-bot',bot_id:BOT_ID});
 assert.equal(d.calls[0].options.headers.Authorization,`Bot ${TOKEN}`);
 assert.deepEqual(await d.service.guilds(),[{id:GUILD,name:'우리 팀'}]);
 assert.deepEqual((await d.service.channels(GUILD)).map(c=>c.name),['general','deploy']);
 const state=await d.service.setChannel(ws,CHANNEL);
 assert.equal(state.channel_name,'deploy');assert.equal(state.guild_name,'우리 팀');assert.equal(state.bot_user,'my-deploy-bot');
 await d.service.send(ws,'배포 성공','temp:latest');
 const post=d.calls.at(-1);
 assert.equal(post.address,`https://discord.com/api/v10/channels/${CHANNEL}/messages`);
 assert.deepEqual(JSON.parse(post.options.body).allowed_mentions,{parse:[]});
 assert.equal(JSON.stringify(state).includes(TOKEN),false);
 await d.service.disconnect(ws);
 assert.equal(await d.service.hasToken(),false);
});

test('오류 문구에 토큰이 새지 않고, 원인별 안내를 준다',async()=>{
 const bad=discordApi({status:401});
 await assert.rejects(bad.service.connect(TOKEN),e=>/Reset Token/.test(e.message)&&!e.message.includes(TOKEN));
 assert.equal(await bad.service.hasToken(),false);
 const user=discordApi({userBot:false});
 await assert.rejects(user.service.connect(TOKEN),/봇의 토큰/);
 const forbidden=discordApi();await forbidden.service.connect(TOKEN);
 const f403=new DiscordBotToken(forbidden.secrets,async()=>new Response('{}',{status:403}));
 await assert.rejects(f403.channels(GUILD),/권한/);
 const offline=new DiscordBotToken(forbidden.secrets,async()=>{throw new TypeError(`fetch failed ${TOKEN}`);});
 await assert.rejects(offline.guilds(),e=>/인터넷/.test(e.message)&&!e.message.includes(TOKEN));
});

test('초대 링크는 알림에 필요한 최소 권한만 요청한다',()=>{
 const url=botInviteUrl(BOT_ID);
 assert.equal(BOT_INVITE_PERMISSIONS,19456);
 assert.ok(url.startsWith('https://discord.com/oauth2/authorize?client_id=123456789012345678&permissions=19456&scope=bot'));
 assert.throws(()=>botInviteUrl('abc'));
});

test('캔버스: 토큰 방식은 8765 봇 서버를 부르지 않고 채널로 이벤트를 보낸다',async t=>{
 const d=discordApi(),ws=fs.mkdtempSync(path.join(require('node:os').tmpdir(),'recoder-discord-'));
 t.after(()=>fs.rmSync(ws,{recursive:true,force:true}));
 vscode.workspace.workspaceFolders=[{uri:{fsPath:ws}}];
 const origFetch=global.fetch;global.fetch=async()=>assert.fail('ReCoder 봇 서버(8765)를 부르면 안 된다');
 t.after(()=>{global.fetch=origFetch;});
 const origInput=vscode.window.showInputBox;vscode.window.showInputBox=async()=>TOKEN;t.after(()=>{vscode.window.showInputBox=origInput;});
 const host=new CanvasHost({},undefined,new DiscordWebhook(d.secrets,async()=>assert.fail('webhook 호출 없음')),undefined,d.service);
 const out=[];const send=(type,p={})=>host.handle(type,{requestId:'r',...p},(type,payload)=>out.push({type,payload}),()=>'',async()=>assert.fail('no deploy'));
 await send('canvas.discord.connectToken',{mode:'token'});
 assert.equal(out.find(o=>o.type==='canvas.discord.statusResult').payload.bot_user,'my-deploy-bot');
 assert.deepEqual(out.find(o=>o.type==='canvas.discord.guildsResult').payload.guilds,[{id:GUILD,name:'우리 팀'}]);
 out.length=0;await send('canvas.discord.status');
 assert.equal(out[0].payload.mode,'token');
 await send('canvas.discord.channels',{guildId:GUILD});
 await send('canvas.discord.setChannel',{channelId:CHANNEL});
 assert.equal(out.filter(o=>o.type==='canvas.discord.statusResult').at(-1).payload.active_channel_id,CHANNEL);
 await send('canvas.discord.event',{enabled:true,eventId:'e1',title:'배포 성공',detail:'ok'});
 assert.equal(out.at(-1).type,'canvas.discord.eventResult');
 assert.equal(d.calls.at(-1).options.method,'POST');
 assert.equal(out.some(o=>JSON.stringify(o.payload).includes(TOKEN)),false);
 await send('canvas.discord.disconnectToken');
 assert.equal(out.at(-1).payload.mode,'webhook');
});
