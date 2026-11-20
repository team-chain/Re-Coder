// Discord → VS Code 배포·롤백·상태 요청: 규칙(화면 결과와 잇기), 상태 요약, 실제 WebSocket 으로 주고받기.
const test=require('node:test'),assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs'),os=require('node:os');
const {opsFor,opsOutcome}=require('../out/webview-test/components/canvas/discordOps');
const {statusSummary,ago}=require('../out/discord/opsStatus');

const op=(over={})=>({requestId:'req-123456789',action:'deploy',requester:'alice',projectName:'shop',workspace:'/w/shop',receivedAt:0,accepted:false,...over});

test('requests attach to the open project only, regardless of slashes and case',()=>{
 const list=[op({workspace:'C:\\Users\\me\\Shop\\'})];
 assert.equal(opsFor(list,'c:/users/me/shop')?.requestId,'req-123456789');
 assert.equal(opsFor(list,'c:/users/me/other'),undefined);
 assert.equal(opsFor(list,undefined),undefined);
});

test('deploy results answer Discord only after VS Code approval; rollback results always do',()=>{
 assert.equal(opsOutcome(op(),'배포 완료','x'),null,'승인 전 다른 배포 결과를 요청 결과로 보내지 않는다');
 assert.equal(opsOutcome(op(),'배포 시작','x').state,'accepted');
 const accepted=op({accepted:true});
 assert.deepEqual(opsOutcome(accepted,'S3 배포 완료','bucket'),{ok:true,state:'done',title:'S3 배포 완료',detail:'bucket'});
 assert.equal(opsOutcome(accepted,'보안 게이트 차단','x').state,'failed');
 assert.equal(opsOutcome(accepted,'배포 상태 변경','x'),null);
 assert.equal(opsOutcome(accepted,'Docker 실행','x'),null);
 const rb=op({action:'rollback'});
 assert.deepEqual(opsOutcome(rb,'롤백 결과','이전 버전으로 되돌렸습니다'),{ok:true,state:'done',title:'롤백 완료',detail:'이전 버전으로 되돌렸습니다'});
 assert.equal(opsOutcome(rb,'롤백 결과','롤백 실패: 권한 없음').state,'failed');
 assert.equal(opsOutcome(rb,'배포 완료','x'),null);
});

test('status summary covers running, done, failed, S3-only and empty projects',()=>{
 const now=Date.parse('2026-11-20T10:00:00Z');
 assert.equal(ago('2026-11-20T09:58:00Z',now),'2분 전');assert.equal(ago('',now),'');assert.equal(ago('2026-11-17T10:00:00Z',now),'3일 전');
 const running=statusSummary('shop',{deployment:{running:true,stage_text:'이미지 빌드 중'},resource:{cluster:'c',service:'s'}},now);
 assert.equal(running.title,'shop · 배포 진행 중');assert.match(running.detail,/ECS c \/ s/);assert.match(running.detail,/이미지 빌드 중/);
 const done=statusSummary('shop',{deployment:{deployment_id:'d',stage:'done',finished_at:'2026-11-20T09:00:00Z',service_url:'http://x.example'},resource:{cluster:'c',service:'s'}},now);
 assert.equal(done.title,'shop · 마지막 배포 완료');assert.equal(done.url,'http://x.example');assert.match(done.detail,/1시간 전/);
 const failed=statusSummary('shop',{deployment:{deployment_id:'d',stage:'failed',error:'권한 없음'},scan:{blocked:true}},now);
 assert.equal(failed.ok,false);assert.match(failed.detail,/권한 없음/);assert.match(failed.detail,/보안 게이트/);
 assert.match(statusSummary('shop',{deployment:{},s3:{bucket:'b',region:'r'}},now).detail,/버킷 b \(r\)/);
 assert.match(statusSummary('shop',null,now).title,/아직 배포 기록이 없어요/);
});

const Module=require('node:module'),resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const vscode=require('vscode');vscode.StatusBarAlignment={Right:2};
const {DiscordController,OPS_TIMING}=require('../out/discord/DiscordController');
Module._resolveFilename=resolve;

test('a Discord request reaches VS Code, waits for approval, and every answer goes back over the same socket',async t=>{
 const {WebSocketServer}=require('ws');
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-ops-'));
 const prior={folders:vscode.workspace.workspaceFolders,trust:vscode.workspace.isTrusted,timing:{...OPS_TIMING}};
 vscode.workspace.workspaceFolders=[{uri:vscode.Uri.file(dir),name:'shop',index:0}];vscode.workspace.isTrusted=true;vscode.env.machineId='m';const status0=vscode.window.setStatusBarMessage;vscode.window.setStatusBarMessage=()=>({dispose(){}});t.after(()=>{vscode.window.setStatusBarMessage=status0;});
 const server=new WebSocketServer({port:0,host:'127.0.0.1'});await new Promise(r=>server.on('listening',r));
 const inbox=[];let socket;const connected=new Promise(r=>server.on('connection',s=>{socket=s;s.on('message',m=>inbox.push(JSON.parse(String(m))));r();}));
 const next=async(type='ops.result')=>{for(let i=0;i<200;i++){const at=inbox.findIndex(m=>m.type===type);if(at>=0)return inbox.splice(at,1)[0];await new Promise(r=>setTimeout(r,10));}throw new Error('no '+type);};
 const controller=new DiscordController({subscriptions:[],secrets:{get:async()=>undefined}});
 t.after(async()=>{controller.dispose();for(const c of server.clients)c.terminate();await new Promise(r=>server.close(r));vscode.workspace.workspaceFolders=prior.folders;vscode.workspace.isTrusted=prior.trust;Object.assign(OPS_TIMING,prior.timing);});
 const events=[];controller.onOpsChanged(e=>events.push(e));
 controller.statusProvider=async workspace=>({ok:true,state:'status',title:`${path.basename(workspace)} · 마지막 배포 완료`,detail:'x'.repeat(5000),url:'javascript:alert(1)'});
 const folder=vscode.workspace.workspaceFolders[0],pid=controller.id(folder);
 await controller.bridge({base:'test',websocketUrl:()=>`ws://127.0.0.1:${server.address().port}`},folder,'token');
 await connected;await new Promise(r=>setTimeout(r,50));
 const send=m=>socket.send(JSON.stringify({project_id:pid,...m}));

 // 상태: 승인 없이 바로 답하고, 4KB 소켓 한도와 안전한 주소만 지킨다.
 send({type:'ops.request',request_id:'status-0001',action:'status',requester:'bob'});
 const status=await next();assert.equal(status.request_id,'status-0001');assert.equal(status.title,`${path.basename(dir)} · 마지막 배포 완료`);
 assert.ok(Buffer.byteLength(JSON.stringify(status))<4096);assert.equal(status.url,undefined);

 // 잘못된 요청은 무시한다.
 send({type:'ops.request',request_id:'x',action:'deploy'});send({type:'ops.request',request_id:'bad-action-1',action:'rm -rf'});
 send({type:'ops.request',request_id:'other-proj-1',action:'deploy',project_id:'someone-else'});
 await new Promise(r=>setTimeout(r,80));assert.equal(controller.pendingOps().length,0);

 // 배포: VS Code 에 요청이 뜨고, 같은 프로젝트의 두 번째 요청은 바로 거절한다.
 send({type:'ops.request',request_id:'deploy-0001',action:'deploy',requester:'alice'});
 for(let i=0;i<100&&!controller.pendingOps().length;i++)await new Promise(r=>setTimeout(r,10));
 const [pending]=controller.pendingOps();assert.equal(pending.requester,'alice');assert.equal(pending.workspace,dir);assert.equal(pending.accepted,false);
 assert.equal(events.at(-1).requestId,'deploy-0001');
 send({type:'ops.request',request_id:'deploy-0002',action:'deploy',requester:'carol'});
 const dup=await next();assert.equal(dup.request_id,'deploy-0002');assert.equal(dup.state,'declined');

 assert.equal((await controller.action('opsReply',{opsId:'deploy-0001',state:'accepted',ok:true,title:'VS Code에서 배포를 승인했어요'})).replied,true);
 const accepted=await next();assert.equal(accepted.state,'accepted');assert.equal(controller.pendingOps()[0].accepted,true);
 await controller.action('opsReply',{opsId:'deploy-0001',state:'accepted',ok:true,title:'again'});
 await new Promise(r=>setTimeout(r,50));assert.equal(inbox.filter(m=>m.type==='ops.result').length,0,'승인 알림은 한 번만');
 await controller.action('opsReply',{opsId:'deploy-0001',state:'done',ok:true,title:'S3 배포 완료',detail:'bucket',url:'https://shop.example'});
 const done=await next();assert.deepEqual([done.state,done.ok,done.url],['done',true,'https://shop.example']);
 assert.equal(controller.pendingOps().length,0);
 assert.equal((await controller.action('opsReply',{opsId:'deploy-0001',state:'done',title:'late'})).replied,false);
 await assert.rejects(controller.action('opsReply',{opsId:'deploy-0001',state:'expired'}),/알 수 없는 응답/);

 // 승인하지 않고 두면 닫았다고 알린다.
 OPS_TIMING.approvalMs=60;
 send({type:'ops.request',request_id:'rollback-001',action:'rollback',requester:'alice'});
 const expired=await next();assert.equal(expired.request_id,'rollback-001');assert.equal(expired.state,'expired');assert.match(expired.title,/롤백 요청을 닫았어요/);
 assert.equal(controller.pendingOps().length,0);
});
