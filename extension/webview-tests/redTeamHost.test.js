const test=require('node:test'),assert=require('node:assert/strict'),Module=require('node:module');
const mock={workspace:{isTrusted:true},window:{}};
const load=Module._load;Module._load=function(name,...args){return name==='vscode'?mock:load.call(this,name,...args);};
const {RedTeamHost}=require('../out/security/redTeamHost');
Module._load=load;
const {PentagiClient}=require('../out/security/pentagiClient');
function setup(){const data=new Map();return {data,host:new RedTeamHost({get:async()=>JSON.stringify({server:'https://pentagi.example',token:'NEVER_ECHO'}),store:async()=>{}},{get:(key,fallback)=>data.get(key)??fallback,update:async(key,value)=>data.set(key,JSON.parse(JSON.stringify(value)))})};}
test('corrupt secret storage never leaks its content through JSON errors',async()=>{
 const host=new RedTeamHost({get:async()=>'{NEVER_ECHO_TOKEN',store:async()=>{}},{get:()=>[],update:async()=>{}});
 await assert.rejects(()=>host.handle('status','project'),e=>e.message.includes('연결 정보')&&!e.message.includes('NEVER_ECHO'));
});
test('cancelled native approval never starts a flow',async t=>{
 const {host,data}=setup();let starts=0;
 t.mock.method(PentagiClient.prototype,'providers',async()=>['openai']);t.mock.method(PentagiClient.prototype,'start',async()=>{starts++;});
 mock.window.showQuickPick=async()=> 'openai';mock.window.showInputBox=async()=> 'http://host.docker.internal:3000';mock.window.showWarningMessage=async()=>undefined;
 assert.equal((await host.handle('start','project')).cancelled,true);assert.equal(starts,0);assert.equal(data.size,0);
});
test('uncertain creation is persisted, cannot retry, credentials stay out of state and response',async t=>{
 const {host,data}=setup();let starts=0;
 t.mock.method(PentagiClient.prototype,'providers',async()=>['openai']);t.mock.method(PentagiClient.prototype,'start',async()=>{starts++;throw new Error('timeout');});
 mock.window.showQuickPick=async()=> 'openai';mock.window.showInputBox=async()=> 'http://host.docker.internal:3000';mock.window.showWarningMessage=async()=> '범위를 확인했고 실행';
 await assert.rejects(()=>host.handle('start','project'),/timeout/);await assert.rejects(()=>host.handle('start','project'),/미확인|확인되지 않은/);
 assert.equal(starts,1);const state=await host.handle('status','project');assert.equal(state.runs[0].uncertain,true);
 assert.ok(!JSON.stringify([...data,state]).includes('NEVER_ECHO'));
});
test('stop failure does not turn an active flow into a completed flow',async t=>{
 const {host}=setup();
 t.mock.method(PentagiClient.prototype,'providers',async()=>['openai']);t.mock.method(PentagiClient.prototype,'start',async()=>({id:'1',status:'running'}));
 t.mock.method(PentagiClient.prototype,'snapshot',async()=>({flow:{id:'1',status:'running'},tasks:[]}));
 t.mock.method(PentagiClient.prototype,'stop',async()=>{throw new Error('stop failed');});
 mock.window.showQuickPick=async()=> 'openai';mock.window.showInputBox=async()=> 'http://host.docker.internal:3000';mock.window.showWarningMessage=async()=> '범위를 확인했고 실행';
 await host.handle('start','project');await assert.rejects(()=>host.handle('stop','project'));
 assert.equal((await host.handle('status','project')).runs[0].terminal,false);
});
