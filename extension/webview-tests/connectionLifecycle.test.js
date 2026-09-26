const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const Module = require('node:module');
const resolve = Module._resolveFilename;
Module._resolveFilename = function(name, ...args) { return name === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : resolve.call(this, name, ...args); };
const vscode = require('vscode');
const { SidebarProvider } = require('../out/sidebar/SidebarProvider');
Module._resolveFilename = resolve;
const ready = {core_ready:'ok', ai_ready:'ok', docker_ready:'ok', aws_deploy_ready:'ok', ops_ready:'ok', issues:[]};
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function fixture() {
  const events = [], messages = [];
  let instance = 'one', healthCallback;
  const core = {
    ensureRunning: async () => { events.push('ensure'); await delay(5); },
    refreshToken: async () => { events.push('token'); },
    coreInstanceKey: () => instance,
  };
  const api = {runDiagnostics: async () => { events.push('diagnose'); await delay(10); return ready; }};
  const polling = {start(fn) { events.push('poll'); healthCallback = fn; }, stop() {}, getLastHealth: () => ({status:'ok'})};
  const provider = new SidebarProvider(vscode.Uri.file(__dirname), api, core, polling);
  provider.healAwsConnection = async () => {events.push('credentials'); return false;};
  provider._selfHealFromDiagnostics = async () => false;
  provider.refreshCost = async () => {};
  provider.postMessage = (type, payload) => messages.push({type, payload});
  return {provider, api, events, messages, setInstance(value) {instance=value;}, health() {healthCallback({status:'ok'});} };
}

test('sidebar and workspace startup share credentials and a diagnostic before any feature click', async () => {
  const f = fixture();
  await Promise.all([f.provider.ensureConnection(), f.provider.ensureConnection(), f.provider.ensureConnection()]);
  await Promise.all([f.provider.runDiagnosticsShared(false), f.provider.runDiagnosticsShared(false)]);
  assert.equal(f.events.filter(e => e === 'credentials').length, 1);
  assert.equal(f.events.filter(e => e === 'diagnose').length, 1);
  assert.ok(f.events.indexOf('token') < f.events.indexOf('credentials'));
  assert.ok(f.events.indexOf('credentials') < f.events.indexOf('diagnose'));
  await f.provider.runDiagnosticsShared(false);
  assert.equal(f.events.filter(e => e === 'diagnose').length, 1);
  await f.provider.runDiagnosticsShared(true);
  assert.equal(f.events.filter(e => e === 'diagnose').length, 2);
});

test('slow diagnosis does not delay a chat request and selected references survive its proposal', async () => {
  const f = fixture();
  let finish;
  f.api.runDiagnostics = () => new Promise(resolve => {finish=resolve;});
  f.api.chat = async (...args) => {
    f.events.push('chat');
    assert.deepEqual(args[3], [{path:'app.ts',content:'reference'}]);
    return {reply:'fixture',model:'fixture',action:{type:'code.plan',instruction:'fix',target_folder:'',target_source:'workspace',files:[]}};
  };
  const diagnostics = f.provider.runDiagnosticsShared(false);
  await delay(20);
  const messages=[];
  await f.provider.handleMessage({type:'chat.send',payload:{id:'first',message:'fix',targetFolder:'src',contextFiles:[{path:'app.ts',content:'reference'}]}},{postMessage: m => messages.push(m)});
  assert.equal(messages.at(-1).type,'chat.response');
  assert.match(messages.at(-1).payload.action.target_folder, /src$/);
  finish(ready); await diagnostics;
});

test('failed diagnostics leave Core health untouched and do not retry paid probes on every poll', async () => {
  const f=fixture();
  f.api.runDiagnostics=async()=>{f.events.push('failed');throw new Error('AI timeout');};
  await f.provider.runDiagnosticsShared(false);
  assert.ok(f.messages.some(m=>m.type==='diagnostics.error'));
  assert.ok(!f.messages.some(m=>m.type==='core.error' || (m.type==='healthUpdate' && m.payload.status==='down')));
  f.provider._lastDiagnosticsAttempt=0;
  f.health(); await f.provider.runDiagnosticsShared(false);
  assert.equal(f.events.filter(e=>e==='failed').length,1);
  f.api.runDiagnostics=async()=>ready;
  await f.provider.runDiagnosticsShared(true);
  assert.equal(f.provider._lastDiagnosticsError,'');
});

test('repair runs one follow-up check and a new Core instance gets new credentials', async () => {
  const f=fixture(); let heals=0;
  f.provider._selfHealFromDiagnostics=async()=>++heals===1;
  await f.provider.runDiagnosticsShared(false);
  assert.equal(f.events.filter(e=>e==='diagnose').length,2);
  f.setInstance('two'); await f.provider.ensureConnection(); await f.provider.runDiagnosticsShared(false);
  assert.equal(f.events.filter(e=>e==='credentials').length,2);
  assert.equal(f.events.filter(e=>e==='diagnose').length,3);
  f.provider._pollingActive=false;
  await f.provider.ensureConnection();
  assert.equal(f.events.filter(e=>e==='poll').length,2, 'reopened launcher resumes health polling');
});

test('generic operation errors do not downgrade live Core health, including before initial connection', () => {
  const fs=require('node:fs'), vm=require('node:vm');
  let state, receive;
  const exports={};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../out/webview-test/hooks/usePolling.js'),'utf8'),{
    exports, Date, require(id) {
      if(id==='react')return {useState(initial){state=initial;return [state,update=>{state=typeof update==='function'?update(state):update;}];},useCallback:fn=>fn,useEffect(){}};
      return {useVSCodeApi:()=>({postMessage(){},useMessage(fn){receive=fn;}})};
    },
  });
  exports.usePolling();
  receive({type:'core.error',payload:{message:'startup failed'}});
  assert.equal(state.coreHealth.status,'down');
  receive({type:'healthUpdate',payload:{status:'ok',version:'fixture'}});
  for(const type of ['errorMessage','chat.error','diagnostics.error'])receive({type,payload:{message:'operation failed'}});
  assert.equal(state.coreHealth.status,'ok');assert.equal(state.isConnected,true);
  receive({type:'core.stopped'});
  assert.equal(state.coreHealth.status,'down');
});

test('approval preserves selected reference files when handed to the result engine', async () => {
  const f=fixture(), messages=[];
  const files=[{path:'fixture.ts',content:'original reference'}];
  const original=vscode.workspace.workspaceFolders;
  vscode.workspace.workspaceFolders=[{uri:vscode.Uri.file(__dirname)}];
  try {
    await f.provider.handleMessage({type:'chat.approveAction',payload:{id:'approve',instruction:'fix',targetFolder:'',contextFiles:files}},{postMessage:m=>messages.push(m)});
    const accepted=messages.find(m=>m.type==='chat.actionAccepted');
    assert.deepEqual(accepted.payload.contextFiles,files);
  } finally {vscode.workspace.workspaceFolders=original;}
});

test('cancelling a design reports its request identity to the shared conversation', async () => {
  const f=fixture(), messages=[];
  await f.provider.handleMessage({type:'code.cancelPlan',payload:{requestId:42}},{postMessage:m=>messages.push(m)});
  assert.equal(messages[0].type,'code.error');
  assert.equal(messages[0].payload.requestId,42);
  assert.match(messages[0].payload.message,/취소/);
  assert.equal(f.events.length,0, 'cancel never generates code or probes AI');
});
