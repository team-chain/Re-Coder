// 4.0.0: 여러 서비스(compose) 로컬 실행 결과 — 단일 앱 "응답 확인 중" 화면으로 보이지 않게.
const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {StackDeployResult,isStackDeploy}=require('../out/webview-test/components/StackDeployResult.js');
test('a running stack with failed checks shows the real address, counts and causes',()=>{
 const result={status:'success',method:'local_docker_compose',url:'http://localhost:3000',deployment_id:'x',
  message:'서비스 6개 · DB 4개 실행 중 — http://localhost:3000 · 검사 30/32 통과 · 일부 검사 실패: a',
  verification:{checks:[...Array(30)].map((_,i)=>({name:'c'+i,ok:true})).concat([{name:'a',ok:false},{name:'b',ok:false}]),
   failures:[{title:'POST gateway/api/auth/signup → 400 (기대 201)',detail:'계약의 응답 코드'}]}};
 assert.equal(isStackDeploy(result),true);assert.equal(isStackDeploy({status:'success'}),false);
 const html=renderToStaticMarkup(React.createElement(StackDeployResult,{result}));
 assert.match(html,/실행 중 · 확인 필요 — 서비스 6개 · DB 4개 실행 중/);
 assert.match(html,/http:\/\/localhost:3000/);assert.match(html,/검사 30\/32 통과/);assert.match(html,/auth\/signup/);
 assert.doesNotMatch(html,/아직 배포 완료가 아닙니다/);
});
test('a clean stack run reads as complete',()=>{
 const html=renderToStaticMarkup(React.createElement(StackDeployResult,{result:{status:'success',method:'local_docker_compose',url:'http://localhost:3000',message:'서비스 3개 · DB 2개 실행 중 — http://localhost:3000 · 검사 10/10 통과',verification:{checks:[{name:'a',ok:true}],failures:[]}}}));
 assert.match(html,/실행 완료 — 서비스 3개 · DB 2개 실행 중/);
});
test('the address button opens the page that answers, not an API root that 404s',()=>{
 let opened='';const result={status:'success',method:'local_docker_compose',url:'http://localhost:3000',open_url:'http://localhost:3000/health',message:'서비스 1개',verification:{checks:[],failures:[]}};
 const el=StackDeployResult({result,onOpen:u=>{opened=u;}});
 const find=n=>!n||typeof n!=='object'?null:(n.type==='button'&&n.props?.onClick?n:[].concat(n.props?.children||[]).map(find).find(Boolean));
 find(el).props.onClick();assert.equal(opened,'http://localhost:3000/health');
});
