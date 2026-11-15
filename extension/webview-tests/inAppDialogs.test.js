// 4.0.0: Windows 기본 팝업·위쪽 입력줄 대신 ReCoder 화면 안 대화창, 주소·포트는 고르기·자동 감지.
const test=require('node:test'),assert=require('node:assert/strict'),Module=require('node:module');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const native={calls:[]};
const mock={workspace:{isTrusted:true},QuickPickItemKind:{Separator:-1},window:{
 showWarningMessage:async(...a)=>{native.calls.push(['warn',...a]);return native.answer;},
 showInformationMessage:async(...a)=>{native.calls.push(['info',...a]);return native.answer;},
 showErrorMessage:async(...a)=>{native.calls.push(['error',...a]);return native.answer;},
 showInputBox:async(...a)=>{native.calls.push(['input',...a]);return native.answer;},
 showQuickPick:async(...a)=>{native.calls.push(['pick',...a]);return native.answer;},
}};
const original=Module._load;
Module._load=function(name,...args){return name==='vscode'?mock:original.call(this,name,...args);};
const dialogs=require('../out/ui/dialogs');
const {LocalPentagiHost}=require('../out/security/localPentagiHost');
const {dockerfilePort}=require('../out/security/managedSecurityHost');
Module._load=original;
const {shortText,DialogView}=require('../out/webview-test/components/ui/Dialogs.js');
const {RedTeamStart,defaultTarget}=require('../out/webview-test/components/RedTeamStart.js');

/** 화면 흉내 — 받은 질문을 기록하고 정해 둔 답을 보낸다. */
function screen({ack=true,reply}={}){
 const asked=[];
 const webview={postMessage:async msg=>{
  if(msg.type!=='ui.ask')return true;asked.push(msg.payload);
  if(ack)setTimeout(()=>{dialogs.handleDialogMessage({type:'ui.shown',payload:{id:msg.payload.id}});const a=reply?.(msg.payload,asked.length);if(a)setTimeout(()=>dialogs.handleDialogMessage({type:'ui.answer',payload:{id:msg.payload.id,...a}}),5);},1);
  return true;}};
 return {webview,asked};
}
test.beforeEach(()=>{dialogs.resetDialogs();native.calls=[];native.answer=undefined;});

test('a modal confirm is asked inside the visible ReCoder screen, not as a Windows dialog',async()=>{
 const s=screen({reply:()=>({index:0})});dialogs.registerDialogWebview(s.webview,()=>true);
 const picked=await dialogs.ui.showWarningMessage('검사 도구(17GB)를 지울까요?',{modal:true,detail:'다음 테스트 때 다시 받아요.'},'지우기');
 assert.equal(picked,'지우기');assert.equal(native.calls.length,0);
 assert.equal(s.asked[0].kind,'confirm');assert.equal(s.asked[0].items[0].label,'지우기');assert.equal(s.asked[0].cancelLabel,'취소');
});
test('cancelling returns undefined like VS Code; message items keep their object identity',async()=>{
 const item={title:'계속'};const s=screen({reply:()=>({cancelled:true})});dialogs.registerDialogWebview(s.webview,()=>true);
 assert.equal(await dialogs.ui.showWarningMessage('계속할까요?',{modal:true},item),undefined);
 const s2=screen({reply:()=>({index:0})});dialogs.resetDialogs();dialogs.registerDialogWebview(s2.webview,()=>true);
 assert.equal(await dialogs.ui.showWarningMessage('계속할까요?',{modal:true},item),item);
});
test('falls back to the VS Code dialog when no ReCoder screen is visible or it does not answer',async()=>{
 dialogs.registerDialogWebview(screen().webview,()=>false);native.answer='확인';
 assert.equal(await dialogs.ui.showWarningMessage('x',{modal:true},'확인'),'확인');assert.equal(native.calls.length,1);
 dialogs.resetDialogs();native.calls=[];
 const silent=screen({ack:false});dialogs.registerDialogWebview(silent.webview,()=>true);
 const t0=Date.now();await dialogs.ui.showInputBox({title:'키'});
 assert.ok(Date.now()-t0>=dialogs.SHOWN_TIMEOUT_MS-50);assert.equal(native.calls[0][0],'input');
});
test('input validation runs in the extension and re-asks inside the same dialog',async()=>{
 const s=screen({reply:(_q,n)=>({value:n===1?'12':'ap-northeast-2'})});dialogs.registerDialogWebview(s.webview,()=>true);
 const v=await dialogs.ui.showInputBox({title:'리전',validateInput:x=>/^[a-z]{2}-[a-z]+-\d$/.test(x)?null:'예: ap-northeast-2'});
 assert.equal(v,'ap-northeast-2');assert.equal(s.asked.length,2);assert.equal(s.asked[1].error,'예: ap-northeast-2');assert.equal(s.asked[1].id,s.asked[0].id);
});
test('quick picks become an in-screen list and return the original item',async()=>{
 const items=[{label:'OpenAI',provider:'openai'},{label:'Anthropic',provider:'anthropic'}];
 const s=screen({reply:()=>({index:1})});dialogs.registerDialogWebview(s.webview,()=>true);
 assert.equal(await dialogs.ui.showQuickPick(items,{title:'모델'}),items[1]);assert.equal(s.asked[0].kind,'pick');
});
test('closing the screen while a question is open does not hang the caller',async()=>{
 const s=screen();const reg=dialogs.registerDialogWebview(s.webview,()=>true);
 const p=dialogs.ui.showWarningMessage('x',{modal:true},'예');await new Promise(r=>setTimeout(r,20));reg.dispose();
 assert.equal(await p,undefined);
});

test('red-team start from the in-screen card asks nothing but a missing key',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-dialog-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const host=new LocalPentagiHost(root,{get:async()=>undefined,store:async()=>{},delete:async()=>{}});
 const r=await host.handle('localStart','project',{target:'http://localhost:8080',provider:'anthropic',agreed:true});
 assert.equal(r.cancelled,true);
 assert.deepEqual(native.calls.map(c=>c[0]),['input'],'only the API key is asked');
 assert.match(native.calls[0][1].title,/Anthropic API 키/);
});
test('a start request without explicit agreement still goes through the full confirmation',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-dialog-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const host=new LocalPentagiHost(root,{get:async()=>'KEY',store:async()=>{},delete:async()=>{}});
 native.answer=undefined;
 await host.handle('localStart','project',{target:'http://localhost:8080',provider:'anthropic',agreed:'yes'});
 assert.equal(native.calls[0][0],'input','falls back to asking the address');
});

test('the start card picks the running public app and never auto-picks another project',()=>{
 const apps={apps:[{name:'orders',url:'http://localhost:8081',reachable:true},{name:'gateway',url:'http://localhost:8080',public:true,reachable:true}],others:[]};
 assert.equal(defaultTarget(apps),'http://localhost:8080');
 assert.equal(defaultTarget({apps:[],others:[{name:'x',url:'http://localhost:3000',reachable:true}]}),'');
 const html=renderToStaticMarkup(React.createElement(RedTeamStart,{keys:{anthropic:true},onStart(){},onCancel(){},initialApps:apps}));
 assert.match(html,/gateway<\/b><code>localhost:8080/);assert.match(html,/aria-checked="true"[^>]*><span class="dot"><\/span><b>gateway/);
 assert.match(html,/저장된 키 사용/);assert.doesNotMatch(html,/placeholder="http:\/\/localhost:3000"/,'no address typing by default');
 const empty=renderToStaticMarkup(React.createElement(RedTeamStart,{onStart(){},onCancel(){},onGoLocal(){},initialApps:{apps:[],others:[]}}));
 assert.match(empty,/실행 중인 앱이 없어요/);assert.match(empty,/로컬 실행하러 가기/);
});
test('dialogs keep text short: one-line title, the rest folded',()=>{
 assert.deepEqual(shortText('검사 도구를 미리 받아 둘까요? 받는 동안 다른 작업을 할 수 있고, 한 번 받아 두면 다음 검사부터 다시 받지 않습니다.'),{head:'검사 도구를 미리 받아 둘까요?',rest:'받는 동안 다른 작업을 할 수 있고, 한 번 받아 두면 다음 검사부터 다시 받지 않습니다.'});
 assert.deepEqual(shortText('짧은 질문인가요?'),{head:'짧은 질문인가요?',rest:''});
 const long='가'.repeat(200);
 const html=renderToStaticMarkup(React.createElement(DialogView,{request:{id:'1',kind:'confirm',message:'시작할까요?',detail:long,items:[{label:'시작'}]},onAnswer(){}}));
 assert.match(html,/<details class="rc-dlg-more"><summary>자세히 ›<\/summary>/);
});
test('the managed check reads the app port from the Dockerfile instead of asking',async t=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-port-'));t.after(()=>fs.rm(dir,{recursive:true,force:true}));
 await fs.writeFile(path.join(dir,'Dockerfile'),'FROM node:22\nEXPOSE 8080\nCMD ["node","a.js"]\n');
 assert.equal(await dockerfilePort(dir),8080);
 await fs.writeFile(path.join(dir,'Dockerfile'),'FROM x\nEXPOSE 80\nEXPOSE 443\n');
 assert.equal(await dockerfilePort(dir),undefined,'ambiguous → ask');
});
