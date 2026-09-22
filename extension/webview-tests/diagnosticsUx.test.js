const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {updateHealing} = require('../out/webview-test/hooks/useSelfHeal.js');
const {HubHome,HubPage} = require('../out/webview-test/components/Hubs.js');
const {DiagnosticsPanel} = require('../out/webview-test/components/DiagnosticsPanel.js');
const ctx={isAiReady:true,isDockerReady:false,isOpsReady:false};

test('one diagnostic result cannot unlock another pending action',()=>{
 let state=updateHealing({},'selfHeal',{key:'docker_ready',pending:true,message:'starting'});
 state=updateHealing(state,'selfHeal',{key:'aws_deploy_ready',message:'connected'});
 assert.equal(state.docker_ready.pending,true);
 assert.equal(updateHealing(state,'diagnosticsUpdate',{docker_ready:'not_ready'}),state);
 state=updateHealing(state,'selfHeal',{key:'docker_ready',pending:true,message:'waiting'});
 assert.equal(state.docker_ready.pending,true);
 state=updateHealing(state,'selfHeal',{key:'docker_ready',failed:true,message:'not installed'});
 assert.equal(state.docker_ready.pending,false);
 assert.equal(state.docker_ready.failed,true);
});

test('finishing setup or error releases only its own button and preserves outcome',()=>{
 let state=updateHealing({},'selfHeal',{key:'ai_ready',pending:true,message:'checking'});
 state=updateHealing(state,'selfHeal',{key:'docker_ready',pending:true,message:'starting'});
 state=updateHealing(state,'selfHeal.finished',{key:'ai_ready'});
 assert.equal(state.ai_ready.pending,false);
 assert.equal(state.docker_ready.pending,true);
 state=updateHealing(state,'selfHeal',{key:'docker_ready',message:'ready'});
 state=updateHealing(state,'selfHeal.finished',{key:'docker_ready'});
 assert.equal(state.docker_ready.message,'ready');
});

test('both home and deploy hub offer Docker action instead of a dead badge',()=>{
 for(const Component of [HubHome,HubPage]){
  const html=renderToStaticMarkup(React.createElement(Component,{hub:'deploy',ctx,onSelect(){},onOpen(){},onHome(){},onHub(){}}));
  assert.match(html,/<button[^>]*>Docker 필요 · 자동 조치<\/button>/);
  assert.ok(!/<button[^>]*>(?:(?!<\/button>)[\s\S])*<button/.test(html),'nested button');
 }
 const html=renderToStaticMarkup(React.createElement(HubHome,{ctx:{...ctx,isDockerReady:true},onSelect(){}}));
 assert.ok(!html.includes('Docker 필요 · 자동 조치'));
});

test('diagnostics describes probe evidence without claiming every task uses one model',()=>{
 const html=renderToStaticMarkup(React.createElement(DiagnosticsPanel,{diagnostics:{core_ready:'ready',ai_ready:'ready',docker_ready:'ready',aws_deploy_ready:'ready',ops_ready:'not_ready',resolved_model_id:'global.test'}}));
 assert.ok(html.includes('연결 검증 모델'));
 assert.ok(html.includes('작업 종류와 폴백'));
});

test('Docker starts with a pending event and concurrent requests share one attempt',async()=>{
 const Module=require('node:module'),path=require('node:path');const resolve=Module._resolveFilename;
 Module._resolveFilename=function(request,...args){return request==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,request,...args);};
 try{
  const {SidebarProvider}=require('../out/sidebar/SidebarProvider.js');const vscode=require('vscode');
  let release,calls=0;const result=new Promise(r=>release=r);
  const provider=new SidebarProvider(vscode.Uri.file('/tmp/test-extension'),{ensureDocker:()=>{calls++;return result;}},{},{});
  const messages=[];provider.postMessage=(type,payload)=>messages.push({type,payload});
  const first=provider.healDocker('fix'),second=provider.healDocker('diagnostics');
  assert.equal(calls,1);assert.equal(messages[0].payload.pending,true);
  release({ready:true,attempted:true,message:'ready'});
  assert.equal(await first,true);assert.equal(await second,true);
  assert.equal(messages.at(-1).payload.failed,false);
  assert.ok(messages.at(-1).payload.message.includes('ready'));
 }finally{Module._resolveFilename=resolve;}
});
