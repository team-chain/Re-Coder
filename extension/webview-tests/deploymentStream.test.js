const test=require('node:test'),assert=require('node:assert/strict');
const {readDeploymentStream}=require('../out/core/deploymentStream');
const {ApiClient}=require('../out/core/ApiClient');
const {renderToStaticMarkup}=require('react-dom/server'),React=require('react');
const {DeploymentActivity}=require('../out/webview-test/components/DeploymentActivity');
function response(text){const bytes=new TextEncoder().encode(text);return new Response(new ReadableStream({start(c){for(const b of bytes)c.enqueue(Uint8Array.of(b));c.close();}}));}
test('SSE handles split Korean UTF-8, CRLF, heartbeat and terminal result',async()=>{
 const events=[];
 const result=await readDeploymentStream(response(': heartbeat\r\n\r\ndata: {"step":"build","message":"이미지 빌드"}\r\n\r\ndata: {"step":"done","result":{"status":"pending"}}\r\n\r\n'),e=>events.push(e));
 assert.equal(events[0].message,'이미지 빌드');assert.equal(result.status,'pending');
});
test('SSE error and early EOF never count as success',async()=>{
 await assert.rejects(readDeploymentStream(response('data: {"step":"error","message":"검사 차단"}\n\n'),()=>{}),/검사 차단/);
 await assert.rejects(readDeploymentStream(response('data: {"step":"build"}\n\n'),()=>{}),/연결이 끊겼습니다/);
});
test('local execution retries only a rejected token, never an opened/failed stream',async t=>{
 const old=global.fetch;t.after(()=>global.fetch=old);let calls=0,refresh=0;
 const api=new ApiClient({getSessionToken:()=>refresh?'fresh':'old',getPort:()=>12345,refreshToken:async()=>{refresh++;}});
 global.fetch=async(url,options)=>{calls++;assert.match(url,/execute\/stream$/);assert.equal(JSON.parse(options.body).approved,true);return calls===1?new Response('',{status:401}):response('data: {"step":"done","result":{"status":"success"}}\n\n');};
 assert.equal((await api.executeDeploymentStream('p',true,()=>{})).status,'success');assert.equal(calls,2);assert.equal(refresh,1);
 calls=0;global.fetch=async()=>{calls++;return response('data: {"step":"build"}\n\n');};
 await assert.rejects(api.executeDeploymentStream('p',true,()=>{}),/연결이 끊겼습니다/);assert.equal(calls,1);
});
test('visible S3 upload count and ratio, Docker pending health never shown as completed',()=>{
 const render=(target,event)=>renderToStaticMarkup(React.createElement(DeploymentActivity,{target,event}));
 assert.match(render('s3',{step:'upload',done_count:2,total:4,key:'app.js'}),/50%/);
 assert.match(render('s3',{step:'upload',done_count:2,total:4}),/파일 업로드 2\/4/);
 assert.match(render('docker',{step:'build',message:'이미지 빌드'}),/이미지 빌드/);
 const pending=render('docker',{step:'done',result:{status:'pending'}});assert.match(pending,/헬스 확인 대기/);assert.doesNotMatch(pending,/배포 완료/);
});
