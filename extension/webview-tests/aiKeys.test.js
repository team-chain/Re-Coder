const test=require('node:test'),assert=require('node:assert/strict');
const path=require('node:path'),Module=require('node:module');
const resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const vscode=require('vscode');
const ai=require('../out/ai/aiKeys');
Module._resolveFilename=resolve;

function context(){
 const secrets=new Map(),state=new Map();
 return {secrets:{get:async k=>secrets.get(k),store:async(k,v)=>{secrets.set(k,v);},delete:async k=>{secrets.delete(k);}},
  globalState:{get:(k,d)=>state.has(k)?state.get(k):d,update:async(k,v)=>{v===undefined?state.delete(k):state.set(k,v);}},secrets_:secrets,state_:state};
}
const anthropicKey='sk-ant-api03-'+'x'.repeat(40), openaiKey='sk-proj-'+'y'.repeat(40);

test('without a chosen key the Core keeps the AWS/gateway path',async()=>{
 const c=context();
 assert.deepEqual(await ai.aiKeyEnv(c),{});
 await c.secrets.store('recoder.ai.openaiKey',openaiKey); // 키만 있고 선택이 없으면 쓰지 않는다
 assert.deepEqual(await ai.aiKeyEnv(c),{});
});
test('a stored key is passed to the Core with the chosen provider only',async()=>{
 const c=context();
 await ai.storeAiKey(c,'anthropic',`  ${anthropicKey}  `);
 assert.deepEqual(await ai.aiKeyEnv(c),{RECODER_AI_PROVIDER:'anthropic',RECODER_ANTHROPIC_API_KEY:anthropicKey});
 const original=vscode.workspace.getConfiguration;
 vscode.workspace.getConfiguration=()=>({get:key=>key==='anthropicModel'?'claude-opus-5-5':''});
 try {const env=await ai.aiKeyEnv(c);assert.equal(env.RECODER_ANTHROPIC_MODEL,'claude-opus-5-5');assert.equal(env.RECODER_ANTHROPIC_FAST_MODEL,'claude-opus-5-5');}
 finally {vscode.workspace.getConfiguration=original;}
 await ai.clearAiKeys(c);
 assert.deepEqual(await ai.aiKeyEnv(c),{});assert.equal(c.secrets_.size,0);
});
test('key format is checked before it is stored',()=>{
 assert.equal(ai.validateApiKey('anthropic',anthropicKey),undefined);
 assert.equal(ai.validateApiKey('openai',openaiKey),undefined);
 assert.match(ai.validateApiKey('anthropic',openaiKey),/sk-ant-/);
 assert.match(ai.validateApiKey('openai','key with space '+'z'.repeat(30)),/공백/);
 assert.match(ai.validateApiKey('openai','sk-short'),/길이/);
});
test('the palette flow stores the key and asks the caller to restart the Core',async()=>{
 const c=context(),originalInput=vscode.window.showInputBox,originalInfo=vscode.window.showInformationMessage;
 let prompt;vscode.window.showInputBox=async o=>{prompt=o;return openaiKey;};vscode.window.showInformationMessage=async()=>undefined;
 try {
  assert.equal(await ai.runAiConnectCommand(c,'openai'),true);
  assert.equal(prompt.password,true);assert.equal(prompt.validateInput('bad-'+'q'.repeat(30)),'OpenAI API 키는 sk- 로 시작합니다.');
  assert.equal(await ai.currentAiProvider(c),'openai');
  vscode.window.showInputBox=async()=>undefined;
  assert.equal(await ai.runAiConnectCommand(c,'anthropic'),false);
  assert.equal(await ai.currentAiProvider(c),'openai');
 } finally {vscode.window.showInputBox=originalInput;vscode.window.showInformationMessage=originalInfo;}
});
