const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {blockingIssues,applyLocked,filesToApply,RemainingIssues,UnusedFilesNote}=require('../out/webview-test/components/codeIssues');
const {DeploySettingsPanel}=require('../out/webview-test/components/DeploySettingsPanel');
const fs=require('node:fs'),path=require('node:path');

const err={code:'NODE_IMPORT_NAME_MISSING',severity:'error',file:'client/src/pages/CartPage.jsx',message:'불러오는 이름을 그 파일이 내보내지 않습니다: `useCart`',fix:'이름을 맞추세요'};

test('남은 오류가 있으면 확인 전까지 적용을 잠그고, 경고·중복은 세지 않는다',()=>{
 assert.equal(blockingIssues([err,err,{...err,severity:'warning'}]).length,1);
 assert.equal(applyLocked([err],false),true);assert.equal(applyLocked([err],true),false);assert.equal(applyLocked([],false),false);assert.equal(applyLocked(undefined,false),false);
 const html=renderToStaticMarkup(React.createElement(RemainingIssues,{issues:[err],acknowledged:false,onAcknowledge(){}}));
 assert.match(html,/빌드·실행 실패 예상 1건/);assert.match(html,/불러오는 이름이 없음/);assert.match(html,/useCart/);assert.match(html,/type="checkbox"/);
 assert.equal(renderToStaticMarkup(React.createElement(RemainingIssues,{issues:[],acknowledged:false,onAcknowledge(){}})),'');
 const src=fs.readFileSync(path.join(__dirname,'../webview-src/components/CodeAgent.tsx'),'utf8');
 assert.match(src,/applyLocked\(turn\.result\?\.consistency_issues/,'파일 하나 적용도 잠근다');
 assert.match(src,/disabled=\{anyPending \|\| allApplied \|\| locked\}/,'모두 적용도 잠근다');
});

test('아무도 쓰지 않는 파일은 모두 적용에서 기본으로 빼고, 고르면 넣는다',()=>{
 const ops=[{file:'a.js'},{file:'src/api/orderApi.js'}];const unused=[{file:'src/api/orderApi.js'}];
 assert.deepEqual(filesToApply(ops,unused,false).map(o=>o.file),['a.js']);
 assert.equal(filesToApply(ops,unused,true).length,2);assert.equal(filesToApply(ops,[],false).length,2);
 const html=renderToStaticMarkup(React.createElement(UnusedFilesNote,{unused,include:false,onInclude(){}}));
 assert.match(html,/아무도 쓰지 않는 파일 1개 — &#x27;모두 적용&#x27; 에서 뺐습니다|아무도 쓰지 않는 파일 1개 — '모두 적용' 에서 뺐습니다/);assert.match(html,/orderApi\.js/);
});

test('데모를 못 쓰는 앱은 왜 못 쓰는지와 방법을 보여 준다',()=>{
 const html=renderToStaticMarkup(React.createElement(DeploySettingsPanel,{settings:[{name:'STRIPE_SECRET_KEY',label:'Stripe 비밀 키',hint:'',status:'missing',source:'',secret:true}],missing:['STRIPE_SECRET_KEY'],
  demo:{available:false,enabled:false,label:'',note:'',unavailable_reason:'이 앱 코드에는 모의 결제 모드가 없어 Stripe 키 없이는 시작할 수 없습니다.'},onSave(){},onDemo(){}}));
 assert.match(html,/data-testid="demo-unavailable"/);assert.match(html,/모의 결제 모드가 없어/);assert.doesNotMatch(html,/demo-button/);
});
