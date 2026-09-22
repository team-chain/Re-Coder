const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {executionRoleReducer:reduce,initialExecutionRole:initial,ExecutionRoleView} = require('../out/webview-test/components/EcsExecutionRole.js');
const plan = {status:'missing',account_id:'123456789012',caller_arn:'arn:aws:iam::123456789012:user/reviewer',role_arn:'arn:aws:iam::123456789012:role/ecsTaskExecutionRole',policy_arn:'arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy',trust_policy:{Statement:[]},proposal_id:'reviewed-plan',message:'역할이 없습니다.'};
const render = (state,disabled=false) => renderToStaticMarkup(React.createElement(ExecutionRoleView,{state,disabled,onPreview(){},onApprove(){},onCancel(){}}));

test('role creation requires a preview; initial and existing views have no create approval',()=>{
  assert.match(render(initial),/실행 역할 확인/);assert.doesNotMatch(render(initial),/승인하고 역할 생성/);
  const review=render({...initial,plan});
  for(const value of [plan.account_id,plan.role_arn,'AmazonECSTaskExecutionRolePolicy','승인하고 역할 생성 (Level 4)','취소']) assert.ok(review.includes(value));
  for(const status of ['exists','created']) assert.doesNotMatch(render({...initial,plan:{...plan,status}}),/승인하고 역할 생성/);
});

test('pending role requests lock buttons and reject duplicate state transitions',()=>{
  const state=reduce({...initial,plan},{type:'start',requestId:'apply-1',operation:'apply'});
  assert.match(render(state),/역할 생성 중/);
  assert.equal((render(state).match(/disabled=""/g)||[]).length,3);
  assert.equal(reduce(state,{type:'start',requestId:'apply-2',operation:'apply'}),state);
  assert.equal(reduce(state,{type:'cancel'}),state);
});

test('late responses cannot replace the current review or unlock its request',()=>{
  const state=reduce(initial,{type:'start',requestId:'current',operation:'preview'});
  assert.equal(reduce(state,{type:'result',requestId:'old',plan}),state);
  assert.equal(reduce(state,{type:'error',requestId:'old',message:'late'}),state);
  const done=reduce(state,{type:'result',requestId:'current',plan});
  assert.equal(done.plan,plan);assert.equal(done.pending,'');
  assert.equal(reduce(done,{type:'error',requestId:'current',message:'duplicate'}),done);
});

test('failed or expired approval clears the old proposal and preserves the explanation',()=>{
  const pending=reduce({...initial,plan},{type:'start',requestId:'r',operation:'apply'});
  const result=reduce(pending,{type:'error',requestId:'r',message:'역할 생성 완료, 정책 연결 실패'});
  assert.equal(result.plan,null);assert.equal(result.pending,'');
  assert.match(render(result),/역할 생성 완료, 정책 연결 실패/);
  assert.doesNotMatch(render(result),/승인하고 역할 생성/);
});

test('malformed responses cannot produce approval; cancel clears the review',()=>{
  const pending=reduce(initial,{type:'start',requestId:'r',operation:'preview'});
  const invalid=reduce(pending,{type:'result',requestId:'r',plan:{...plan,proposal_id:undefined}});
  assert.equal(invalid.plan,null);assert.ok(invalid.error);
  assert.deepEqual(reduce({...initial,plan},{type:'cancel'}),initial);
});

test('host calls only the requested role API and echoes IDs; errors stay on the role card',async()=>{
  const Module=require('node:module'),path=require('node:path');const resolve=Module._resolveFilename;
  Module._resolveFilename=function(request,...args){return request==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,request,...args);};
  try{
    const vscode=require('vscode'); const {SidebarProvider}=require('../out/sidebar/SidebarProvider.js');
    const calls=[];let fail=false;
    const api={previewEcsExecutionRole:async r=>{calls.push(['preview',r]);return plan;},applyEcsExecutionRole:async(...args)=>{calls.push(['apply',...args]);if(fail)throw new Error('AccessDenied');return {...plan,status:'created'};}};
    const provider=new SidebarProvider(vscode.Uri.file('/tmp/extension'),api,{},{});
    const messages=[];provider.postMessage=(type,payload)=>messages.push({type,payload});
    await provider.handleMessage({type:'aws.executionRole.preview',payload:{requestId:'p',region:' ap-northeast-2 '}});
    assert.deepEqual(calls,[['preview','ap-northeast-2']]);
    assert.equal(messages.at(-1).payload.requestId,'p');
    await provider.handleMessage({type:'aws.executionRole.apply',payload:{requestId:'bad',proposalId:'id'}});
    assert.equal(calls.length,1);assert.equal(messages.at(-1).type,'aws.executionRole.error');
    await provider.handleMessage({type:'aws.executionRole.apply',payload:{requestId:'a',proposalId:'id',approved:true}});
    assert.deepEqual(calls.at(-1),['apply','id',true]);
    assert.equal(messages.at(-1).payload.result.status,'created');
    fail=true;
    await provider.handleMessage({type:'aws.executionRole.apply',payload:{requestId:'e',proposalId:'id2',approved:true}});
    assert.equal(messages.at(-1).type,'aws.executionRole.error');
    assert.equal(messages.at(-1).payload.requestId,'e');assert.match(messages.at(-1).payload.message,/AccessDenied/);
  } finally {Module._resolveFilename=resolve;}
});
