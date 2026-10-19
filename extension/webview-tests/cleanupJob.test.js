const test=require('node:test'),assert=require('node:assert/strict');
const path=require('node:path'),Module=require('node:module');
const resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const {waitForCleanupJob}=require('../out/core/ApiClient');
Module._resolveFilename=resolve;
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {cleanupProgressText}=require('../out/webview-test/components/AwsCleanupPanel');

const job=(status,extra={})=>({id:'abc123abc123',region:'us-east-1',status,count:2,progress:{done:0,total:2},result:null,error:'',...extra});
const nosleep=async()=>{};

test('2.0.31 정리: 뒤에서 도는 작업을 끝날 때까지 보고, 진행을 알린다',async()=>{
 const seq=[job('running',{progress:{done:0,total:2,current:'로드밸런서 a'}}),job('running',{progress:{done:1,total:2,current:'관리형 DB(RDS) b'}}),
  job('done',{result:{region:'us-east-1',deleted:2,failed:0,gaps:[],results:[]}})];
 const seen=[];
 const result=await waitForCleanupJob(async()=>seq.shift(),{onProgress:p=>seen.push(p.current),sleep:nosleep});
 assert.equal(result.deleted,2);assert.deepEqual(seen,['로드밸런서 a','관리형 DB(RDS) b']);
});

test('2.0.31 정리: 실패한 작업은 코어의 이유로, 조회가 잠깐 실패하면 이어서 기다린다',async()=>{
 await assert.rejects(waitForCleanupJob(async()=>job('failed',{error:'AWS 에 연결하지 못했습니다: ExpiredToken'}),{sleep:nosleep}),/ExpiredToken/);
 let n=0;
 const flaky=async()=>{n+=1;if(n<3){throw new Error('busy');}return job('done',{result:{region:'r',deleted:1,failed:0,gaps:[],results:[]}});};
 assert.equal((await waitForCleanupJob(flaky,{sleep:nosleep})).deleted,1);
 await assert.rejects(waitForCleanupJob(async()=>{throw new Error('코어 꺼짐');},{sleep:nosleep}),/코어 꺼짐/);
 await assert.rejects(waitForCleanupJob(async()=>job('running'),{sleep:nosleep,intervalMs:1000,maxWaitMs:3000}),/오래 걸리고/);
});

test('2.0.31 정리: 진행 문장',()=>{
 assert.match(cleanupProgressText({done:1,total:3,current:'관리형 DB(RDS) x'}),/지우는 중 2\/3 — 관리형 DB\(RDS\) x/);
 assert.match(cleanupProgressText({}),/지우는 중 1\/1/);
 assert.ok(React&&renderToStaticMarkup);
});

test('2.0.31 HTTPS 주소: 잠금과 WAF 위치를 알려 준다',()=>{
 const {EcsProtection,DEFAULT_PROTECTION}=require('../out/webview-test/components/EcsProtection');
 const waf=renderToStaticMarkup(React.createElement(EcsProtection,{value:{...DEFAULT_PROTECTION,https:true,edge:'alb_waf'},onChange(){}}));
 assert.match(waf,/비밀 머리글로 잠금/);assert.match(waf,/WAF 는 CloudFront 에/);
 const plain=renderToStaticMarkup(React.createElement(EcsProtection,{value:{...DEFAULT_PROTECTION,https:true},onChange(){}}));
 assert.match(plain,/비밀 머리글로 잠금/);assert.doesNotMatch(plain,/WAF 는 CloudFront 에/);
});
