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
test('remote waiting with completed tasks is terminal but an unstructured report is not a pass',async t=>{
 const {host}=setup();
 t.mock.method(PentagiClient.prototype,'providers',async()=>['openai']);t.mock.method(PentagiClient.prototype,'start',async()=>({id:'1',status:'created'}));
 t.mock.method(PentagiClient.prototype,'snapshot',async()=>({flow:{id:'1',status:'waiting'},tasks:[{id:'1',title:'test',status:'finished',result:'No issues.'}]}));
 t.mock.method(PentagiClient.prototype,'evidence',async()=>[]);
 mock.window.showQuickPick=async()=> 'openai';mock.window.showInputBox=async()=> 'http://host.docker.internal:3000';mock.window.showWarningMessage=async()=> '범위를 확인했고 실행';
 await host.handle('start','project');const result=await host.handle('status','project');
 assert.equal(result.runs[0].terminal,true);assert.equal(result.assessment.status,'incomplete');assert.equal(result.assessment.checks.filter(c=>c.status==='not_tested').length,5);
});
test('connections remain workspace scoped while another workspace changes server',async t=>{
 const data=new Map(),secrets=new Map([['recoder.pentagi.connection.v1',JSON.stringify({server:'https://a.example',token:'A_FAKE'})]]);
 const host=new RedTeamHost({get:async k=>secrets.get(k),store:async(k,v)=>secrets.set(k,v)},{get:(k,f)=>data.get(k)??f,update:async(k,v)=>data.set(k,structuredClone(v))});
 t.mock.method(PentagiClient.prototype,'providers',async()=>['openai']);
 t.mock.method(PentagiClient.prototype,'start',async()=>({id:'1',status:'running'}));
 let stoppedServer; t.mock.method(PentagiClient.prototype,'stop',async function(){stoppedServer=this.base;});
 t.mock.method(PentagiClient.prototype,'snapshot',async()=>({flow:{id:'1',status:'waiting'},tasks:[]}));
 t.mock.method(PentagiClient.prototype,'evidence',async()=>[]);
 mock.window.showQuickPick=async()=> 'openai';mock.window.showInputBox=async()=> 'http://host.docker.internal:3000';mock.window.showWarningMessage=async()=> '범위를 확인했고 실행';
 await host.handle('start','A');
 const answers=['https://b.example','B_FAKE'];mock.window.showInputBox=async()=>answers.shift();
 await host.handle('configure','B');
 const result=await host.handle('stop','A');
 assert.equal(stoppedServer,'https://a.example');assert.equal(result.runs[0].terminal,true);
 assert.equal(JSON.stringify([...data]).includes('A_FAKE'),false);
 assert.equal((await host.handle('status','B')).server,'https://b.example');
});
test('legacy orphan can reconnect to its original server and rotate expired credentials',async t=>{
 const data=new Map(),secrets=new Map([['recoder.pentagi.connection.v1',JSON.stringify({server:'https://wrong.example',token:'OLD_FAKE'})]]);
 const key='security.redteam.'+require('node:crypto').createHash('sha256').update('A').digest('hex');
 data.set(key,[{id:'9',server:'https://original.example',target:'http://localhost:3000',createdAt:'',terminal:false}]);
 const host=new RedTeamHost({get:async k=>secrets.get(k),store:async(k,v)=>secrets.set(k,v)},{get:(k,f)=>data.get(k)??f,update:async(k,v)=>data.set(k,structuredClone(v))});
 t.mock.method(PentagiClient.prototype,'providers',async()=>['openai']);
 await assert.rejects(()=>host.handle('status','A'),/다시 연결/);
 let answers=['https://different.example'];mock.window.showInputBox=async()=>answers.shift();
 await assert.rejects(()=>host.handle('configure','A'),/원래 서버/);
 answers=['https://original.example','NEW_FAKE'];
 const result=await host.handle('configure','A');
 assert.equal(result.server,'https://original.example');assert.equal(result.runs[0].terminal,false);
});
