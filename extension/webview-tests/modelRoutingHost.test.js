// 2.0.36 확장 호스트: 작업별 AI 공급자 메시지 — 키 값은 화면에 보내지 않고, 비교는 비용 확인을 받은 뒤에만 시작한다.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const Module = require('node:module');

const resolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...args) {
  return request === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : resolve.call(this, request, ...args);
};
const vscode = require('vscode');
const { SidebarProvider } = require('../out/sidebar/SidebarProvider.js');
Module._resolveFilename = resolve;

const KEY = 'sk-' + 'k'.repeat(40);

function fakeApi(calls) {
  return {
    getModelRouting: async () => ({ quality: { provider: 'anthropic', model: 'm' }, economy: { spec: 'quality', provider: 'anthropic', model: 'm', separate: false, scrubbed: false, note: '' }, economy_operations: [], choices: [] }),
    getModelCosts: async (days) => { calls.push(['costs', days]); return { calls: 0, total_cost_usd: 0, unknown_cost_calls: 0, economy_saved_usd: 0, groups: [], days, persisted: true }; },
    getModelCompare: async (id) => { calls.push(['get', id]); return id ? { id, status: 'running', sides: [] } : null; },
    startModelCompare: async (...args) => { calls.push(['start', ...args]); return { id: 'abcdef123456', status: 'running', sides: [] }; },
  };
}

async function send(sidebar, type, payload) {
  const sent = [];
  const original = sidebar.postMessage.bind(sidebar);
  sidebar.postMessage = (t, p) => { sent.push({ type: t, payload: p }); };
  try { await sidebar.handleMessage({ type, payload }); } finally { sidebar.postMessage = original; }
  return sent;
}

function secrets() {
  const m = new Map([['recoder.ai.compatibleKey', KEY]]);
  return { get: async (k) => m.get(k), store: async (k, v) => { m.set(k, v); }, delete: async (k) => { m.delete(k); }, onDidChange: () => ({ dispose() {} }) };
}

test('models.load sends routing, costs and which keys exist — never the key itself', async () => {
  const calls = [];
  const state = new Map();
  const sidebar = new SidebarProvider(vscode.Uri.file('/tmp'), fakeApi(calls), {}, {}, undefined,
    { get: (k, d) => (state.has(k) ? state.get(k) : d), update: async (k, v) => { state.set(k, v); }, keys: () => [] }, secrets());
  const sent = await send(sidebar, 'models.load', {});
  const msg = sent.find((m) => m.type === 'models.state');
  assert.ok(msg, JSON.stringify(sent));
  assert.deepEqual(msg.payload.keys, { anthropic: false, openai: false, compatible: true });
  assert.ok(!JSON.stringify(msg.payload).includes(KEY));
  assert.deepEqual(calls[0], ['costs', 30]);
});

test('models.compare.start needs the cost confirmation; poll ignores bad ids', async () => {
  const calls = [];
  const sidebar = new SidebarProvider(vscode.Uri.file('/tmp'), fakeApi(calls), {}, {});
  const original = vscode.window.showWarningMessage;
  try {
    let asked = '';
    vscode.window.showWarningMessage = async (text, opts) => { asked = text; assert.equal(opts.modal, true); return undefined; };
    let sent = await send(sidebar, 'models.compare.start', { instruction: '할 일 앱', providers: ['quality', 'economy'] });
    assert.match(asked, /두 번/);
    assert.ok(!calls.some((c) => c[0] === 'start'), '확인 없이 시작하지 않는다');
    assert.ok(sent.some((m) => m.type === 'models.compareError'));

    vscode.window.showWarningMessage = async () => '비교 시작';
    sent = await send(sidebar, 'models.compare.start', { instruction: '  할 일 앱  ', providers: ['quality', 'economy'] });
    const start = calls.find((c) => c[0] === 'start');
    assert.equal(start[1], '할 일 앱');
    assert.deepEqual(start[3], ['quality', 'economy']);
    assert.ok(sent.some((m) => m.type === 'models.compare' && m.payload.id === 'abcdef123456'));

    sent = await send(sidebar, 'models.compare.start', { instruction: '', providers: ['quality'] });
    assert.ok(sent.some((m) => m.type === 'models.compareError' && m.payload.message));

    await send(sidebar, 'models.compare.poll', { id: '../../etc' });
    assert.ok(!calls.some((c) => c[0] === 'get' && c[1] === '../../etc'));
    sent = await send(sidebar, 'models.compare.poll', { id: 'abcdef123456' });
    assert.ok(sent.some((m) => m.type === 'models.compare'));
  } finally { vscode.window.showWarningMessage = original; }
});

test('모델 저장은 확인 통과한 모델과 허용된 설정 키만 저장하고 Core를 재시작한다', async()=>{
 const updates=[],commands=[];
 const original={cfg:vscode.workspace.getConfiguration,execute:vscode.commands.executeCommand};
 const api=fakeApi([]);
 api.getModelRouting=async()=>({quality:{provider:'openai',model:'q'},configurable:true,
  catalog:[{model:'q',ready:true,checked:true},{model:'f',ready:true,checked:true},{model:'failed',ready:false,checked:true},
   {model:'old',ready:true,checked:true,retirement:'2026-12-11'}]});
 vscode.workspace.getConfiguration=()=>({get:(k,d)=>d,update:async(k,v)=>updates.push([k,v])});
 vscode.commands.executeCommand=async name=>commands.push(name);
 const sidebar=new SidebarProvider(vscode.Uri.file('/tmp'),api,{},{});
 try {
  for(const model of ['failed','old','unverified']) {
   const out=await send(sidebar,'models.setModels',{tier:'fast',model});
   assert.ok(out.some(m=>m.type==='models.error'));
  }
  assert.equal(updates.length,0);
  await send(sidebar,'models.setModels',{tier:'fast',model:'f'});
  assert.deepEqual(updates,[['openaiFastModel','f']]);assert.equal(commands.at(-1),'recoder.restartCore');
  await send(sidebar,'models.setToggle',{key:'lockSensitive',value:false});
  assert.deepEqual(updates.at(-1),['lockSensitive',false]);
  const count=updates.length;
  await send(sidebar,'models.setToggle',{key:'openaiKey',value:true});
  await send(sidebar,'models.setToggle',{key:'lockSensitive',value:'false'});
  assert.equal(updates.length,count);
 } finally {vscode.workspace.getConfiguration=original.cfg;vscode.commands.executeCommand=original.execute;}
});
test('모델 확인 버튼은 검사 API 결과와 설정값을 반환한다',async()=>{
 const calls=[],api=fakeApi(calls);
 const routing=await api.getModelRouting();
 api.checkModels=async()=>{calls.push(['check']);return {...routing,checked_at:123,catalog:[{model:'m',ready:true,checked:true}]};};
 const sidebar=new SidebarProvider(vscode.Uri.file('/tmp'),api,{},{});
 const out=await send(sidebar,'models.check');
 assert.ok(calls.some(c=>c[0]==='check'));
 const state=out.find(m=>m.type==='models.state').payload;
 assert.equal(state.routing.checked_at,123);assert.equal(state.settings.lockSensitive,true);
 assert.equal(state.settings.autoRecommend,true);assert.deepEqual(state.settings.prices,{});
});

function switchHarness({claude=true}={}) {
 const state=new Map([['recoder.ai.provider','openai']]);
 const keys=new Map([['recoder.ai.openaiKey',KEY],...(claude?[['recoder.ai.anthropicKey','sk-ant-'+KEY]]:[])]);
 const storage={get:async k=>keys.get(k),store:async(k,v)=>{keys.set(k,v);},delete:async k=>{keys.delete(k);}};
 const global={get:(k,d)=>state.has(k)?state.get(k):d,update:async(k,v)=>{state.set(k,v);}};
 const api=fakeApi([]);
 api.getModelRouting=async()=>({quality:{provider:state.get('recoder.ai.provider'),model:'m'},choices:[],
  provider_defaults:{anthropic:{model:'claude-sonnet-5-5',fast_model:'claude-haiku-5-5'},openai:{model:'gpt-6-sol',fast_model:'gpt-6-sol'}}});
 const side=new SidebarProvider(vscode.Uri.file('/tmp'),api,{}, {},undefined,global,storage);
 const a=[],b=[];
 side._view={webview:{postMessage:m=>a.push(m)}};
 side._workspacePanelWebview={postMessage:m=>b.push(m)};
 side._workspaceReady=true;
 return {side,state,keys,a,b,api};
}

test('switch broadcasts defaults and preserves keys, without paid checks or key prompts',async()=>{
 const h=switchHarness(),original=vscode.commands.executeCommand;
 const before=[...h.keys];let restarts=0;
 vscode.commands.executeCommand=async name=>{assert.equal(name,'recoder.restartCore');restarts++;return true;};
 try {
  await h.side.handleMessage({type:'ai.switch',payload:{provider:'anthropic'}});
  assert.equal(restarts,1);assert.equal(h.state.get('recoder.ai.provider'),'anthropic');
  for(const messages of [h.a,h.b]) {
   const model=messages.find(m=>m.type==='models.state');assert.ok(model,JSON.stringify(messages));
   assert.equal(model.payload.settings.providers.anthropic.fastModel,'claude-haiku-5-5');
   assert.equal(model.payload.activity.switching,false);
   assert.ok(!JSON.stringify(messages).includes(KEY));
  }
  assert.deepEqual([...h.keys],before);
  await h.side.changeAiProvider('anthropic');assert.equal(restarts,1);
  await h.side.changeAiProvider('openai');assert.equal(restarts,2);
 } finally {vscode.commands.executeCommand=original;}
});

test('model defaults follow configured fast > configured complex > Core defaults',async()=>{
 const h=switchHarness(),original=vscode.workspace.getConfiguration;
 vscode.workspace.getConfiguration=()=>({get:(k,d)=>({anthropicModel:' custom ',openaiFastModel:' fast '}[k]??d)});
 try {
  await h.side.handleMessage({type:'models.load'});
  assert.deepEqual(h.a.find(m=>m.type==='models.state').payload.settings.providers,{
   anthropic:{model:'custom',fastModel:'custom'},openai:{model:'gpt-6-sol',fastModel:'fast'}});
 } finally {vscode.workspace.getConfiguration=original;}
});

test('missing key opens secure input; Esc settles both views without selecting or restarting',async()=>{
 const h=switchHarness({claude:false});const original={input:vscode.window.showInputBox,execute:vscode.commands.executeCommand};
 let prompts=0;vscode.window.showInputBox=async o=>{assert.equal(o.password,true);prompts++;return undefined;};
 vscode.commands.executeCommand=async()=>{throw new Error('must not restart');};
 try {
  assert.equal(await h.side.changeAiProvider('anthropic'),false);assert.equal(prompts,1);
  assert.equal(h.state.get('recoder.ai.provider'),'openai');
  assert.ok(h.a.some(m=>m.type==='models.state'));assert.ok(h.b.some(m=>m.type==='models.state'));
  vscode.window.showInputBox=async()=> 'sk-ant-'+KEY;
  vscode.commands.executeCommand=async()=>true;
  assert.equal(await h.side.changeAiProvider('anthropic'),true);
  assert.equal(h.state.get('recoder.ai.provider'),'anthropic');
 } finally {vscode.window.showInputBox=original.input;vscode.commands.executeCommand=original.execute;}
});

for(const rollback of [true,false]) test(`restart failure restores selection and reports rollback ${rollback}`,async()=>{
 const h=switchHarness(),original=vscode.commands.executeCommand;let count=0;
 vscode.commands.executeCommand=async()=>++count===1?false:rollback;
 try {
  assert.equal(await h.side.changeAiProvider('anthropic'),false);
  assert.equal(h.state.get('recoder.ai.provider'),'openai');assert.equal(count,2);
  const err=h.a.find(m=>m.type==='ai.switchError').payload.message;
  assert.match(err,rollback ? /되돌렸습니다/ : /재연결도 실패/);
  assert.ok(!JSON.stringify(h.a).includes(KEY));
 } finally {vscode.commands.executeCommand=original;}
});

test('host guards all generation phases, releases counters on failure, and rejects new generation during switch',async()=>{
 const h=switchHarness(),original=vscode.commands.executeCommand;let restarts=0;
 vscode.commands.executeCommand=async()=>{restarts++;return true;};
 try {
  for(const type of ['code.plan','code.planFollowup','code.spec','code.generate']) {
   let release;const gate=new Promise(resolve=>release=resolve);
   const dispatch=h.side.dispatchMessage;
   h.side.dispatchMessage=async()=>{await gate;throw new Error('test failure');};
   const work=h.side.handleMessage({type,payload:{requestId:'qa'}});
   assert.equal(await h.side.changeAiProvider('anthropic'),false);
   assert.match(h.a.findLast(m=>m.type==='ai.switchError').payload.message,/코드 생성/);
   assert.equal(restarts,0);release();await work;h.side.dispatchMessage=dispatch;
   assert.equal(h.side._codeRequests,0);
  }
  let release;const gate=new Promise(resolve=>release=resolve);
  vscode.commands.executeCommand=async()=>{restarts++;await gate;return true;};
  const switching=h.side.changeAiProvider('anthropic');
  while(!restarts) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(await h.side.changeAiProvider('openai'),false); // duplicate ignored
  await h.side.handleMessage({type:'code.generate',payload:{requestId:'blocked'}});
  assert.ok(h.a.some(m=>m.type==='code.error'&&m.payload.requestId==='blocked'&&/AI 전환/.test(m.payload.message)));
  release();await switching;assert.equal(restarts,1);
 } finally {vscode.commands.executeCommand=original;}
});

test('a load started before switching cannot overwrite the final provider',async()=>{
 const h=switchHarness(),original=vscode.commands.executeCommand;
 const route=h.api.getModelRouting;let release;
 const stale=await route();h.api.getModelRouting=()=>new Promise(resolve=>{release=()=>resolve(stale);});
 const load=h.side.handleMessage({type:'models.load'});
 h.api.getModelRouting=route;vscode.commands.executeCommand=async()=>true;
 try {
  await h.side.changeAiProvider('anthropic');release();await load;
  const states=h.a.filter(m=>m.type==='models.state');
  assert.equal(states.length,1);assert.equal(states[0].payload.routing.quality.provider,'anthropic');
  await h.side.handleMessage({type:'ai.switch',payload:{provider:'invalid'}});
  assert.match(h.a.findLast(m=>m.type==='ai.switchError').payload.message,/알 수 없는/);
 } finally {vscode.commands.executeCommand=original;}
});
