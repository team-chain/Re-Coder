// Discord 운영 요청 E2E 의 VS Code 쪽 — 실제 DiscordController·BridgeClient(빌드 결과)로 봇 서버에 붙는다.
// stdin 첫 줄: {"url":..., "token":...}. 요청이 오면 VS Code 승인 화면이 하는 일(승인 → 결과)을 그대로 보낸다.
const path=require('node:path'),fs=require('node:fs'),os=require('node:os'),readline=require('node:readline');
const Module=require('node:module'),resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'vscode-mock.js'):resolve.call(this,name,...args);};
const vscode=require('vscode');vscode.StatusBarAlignment={Right:2};vscode.window.setStatusBarMessage=()=>({dispose(){}});
const {DiscordController}=require('../out/discord/DiscordController');
const {statusSummary}=require('../out/discord/opsStatus');
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'shop-'));
vscode.workspace.workspaceFolders=[{uri:vscode.Uri.file(dir),name:'shop',index:0}];vscode.workspace.isTrusted=true;vscode.env.machineId='e2e-machine';
const controller=new DiscordController({subscriptions:[],secrets:{get:async()=>undefined}});
const folder=vscode.workspace.workspaceFolders[0];
controller.statusProvider=async()=>statusSummary('shop',{deployment:{deployment_id:'d1',stage:'done',finished_at:new Date(Date.now()-120000).toISOString(),service_url:'https://shop.example'},resource:{cluster:'demo',service:'web'}});
controller.onOpsChanged(request=>{
 if(!request)return;
 const say=(m)=>process.stdout.write(JSON.stringify(m)+'\n');
 say({event:'vscode-request',action:request.action,requester:request.requester});
 setTimeout(async()=>{
  if(request.action==='rollback'){await controller.action('opsReply',{opsId:request.requestId,state:'declined',ok:false,title:'롤백 요청을 거절했어요',detail:'VS Code에서 요청을 거절했어요.'});return;}
  await controller.action('opsReply',{opsId:request.requestId,state:'accepted',ok:true,title:'VS Code에서 배포를 승인했어요',detail:'S3 정적 사이트로 배포를 시작합니다.'});
  setTimeout(()=>controller.action('opsReply',{opsId:request.requestId,state:'done',ok:true,title:'S3 배포 완료',detail:'shop-bucket',url:'https://shop.example'}),150);
 },150);
});
process.stdout.write(JSON.stringify({event:'project',id:controller.id(folder)})+'\n');
readline.createInterface({input:process.stdin}).once('line',async line=>{
 const {url,token}=JSON.parse(line);
 await controller.bridge({base:'e2e',websocketUrl:()=>url},folder,token);
 process.stdout.write(JSON.stringify({event:'bridge'})+'\n');
});
