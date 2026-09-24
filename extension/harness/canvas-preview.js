// Isolated visual/interaction fixture. No request can reach AWS, GitHub or Discord.
const React=require('react');
const {createRoot}=require('react-dom/client');
const snapshot={workspace:'/fixture/board',projectName:'board-app',git:{repository:'team-chain/Re-Coder',branch:'main',dirty:false,connected:true},aws:{ready:false,account:'123456789012',region:'ap-northeast-2'},deployment:{running:false,stage:'idle'},resource:null,topology:null,scan:null,s3:null,warnings:[]};
const connectionsFixture=new URLSearchParams(window.location.search).has('connections');
let fixtureAuth={status:connectionsFixture?'unauthenticated':'authenticated',user:'fixture-user'},discordOnline=!connectionsFixture;
snapshot.git.head='fixture-commit';snapshot.git.initialized=true;
if(connectionsFixture)snapshot.git={repository:'',branch:'',dirty:true,connected:false,head:'',initialized:false};
let currentPlan=null;
const snapshotDelay=Math.min(30000,Math.max(30,Number(new URLSearchParams(window.location.search).get('snapshotDelay'))||30));
const emit=(type,payload,delay=30)=>setTimeout(()=>window.dispatchEvent(new MessageEvent('message',{data:{type,payload}})),delay);
const files=['index.html','app.js','server.js','utils.js','posts.json','package.json','Dockerfile','legacy.js'];
const graph={kind:'project',root:'/fixture/board',nodes:files.map((name,i)=>({id:name,name,module:name.split('.').pop(),layer:'service',in_degree:i===3?4:i===7?0:1,out_degree:1,flags:i===7?['orphan']:i===3?['overloaded']:[]})),edges:[{from:'index.html',to:'app.js'},{from:'app.js',to:'utils.js'},{from:'server.js',to:'utils.js'},{from:'server.js',to:'posts.json'}],findings:[]};
const functions={kind:'file',name:'app.js',path:'app.js',nodes:['render','loadPosts','addPost','savePosts','deletePost','_formatOld'].map((name,i)=>({id:name,name,cls:null,in_degree:i===0?3:i===5?0:1,out_degree:1,flags:i===0?['overloaded']:i===5?['orphan']:[]})),edges:[{from:'render',to:'loadPosts'},{from:'addPost',to:'render'},{from:'savePosts',to:'render'}],findings:[]};
window.__canvasMessages=[];
window.acquireVsCodeApi=()=>({getState:()=>null,setState(){},postMessage(m){
 window.__canvasMessages.push(m);window.dispatchEvent(new CustomEvent('fixture-message',{detail:m.type}));const p=m.payload||{};
 if(m.type==='canvas.snapshot')emit('canvas.snapshotResult',{requestId:p.requestId,snapshot:structuredClone(snapshot)},snapshotDelay);
 if(m.type==='canvas.graph')emit('canvas.graphResult',{requestId:p.requestId,file:p.file,graph:p.file?functions:graph});
 if(m.type.startsWith('canvas.github.')){
  if(m.type==='canvas.github.login')fixtureAuth={status:'authenticated',user:'fixture-user'};
  if(m.type==='canvas.github.connect')snapshot.git={repository:p.repository,branch:'main',dirty:true,connected:true,head:'',initialized:true};
  if(m.type==='canvas.github.sourceControl')snapshot.git={...snapshot.git,head:'fixture-commit',dirty:false};
  emit('canvas.github.result',{requestId:p.requestId,auth:fixtureAuth,git:structuredClone(snapshot.git),repos:fixtureAuth.status==='authenticated'?[{name:'fixture-user/board',private:true}]:[],message:m.type==='canvas.github.sourceControl'?'픽스처: 사용자 커밋 완료 (실제 Git 동작 없음)':''});
 }
 if(m.type==='canvas.prepare'){currentPlan={id:'fixture-plan',config:{...p.config,dir:p.autoDir?'dist':p.config.dir},projectName:'board-app',repository:snapshot.git.repository,branch:'main',commit:'fixture-commit',dirty:false,account:snapshot.aws.account,coreRegion:snapshot.aws.region,preflight:{blocked:false,summary:'Node.js 서버 감지',reasons:[],warnings:[]},targetState:{exists:true,task_definition:'board:8',images:[{image:'board-app:v0',digest:'sha256:fixture-current-image'}],budget:[{name:'fixture-budget',limit:'15',spent:'7.2',unit:'USD',period:'MONTHLY'}],warnings:[]},staticSite:{files:['index.html','app.js'],excluded:'.env 제외'}};emit('canvas.plan',{requestId:p.requestId,...currentPlan});}
 if(m.type==='canvas.execute'){
  if(currentPlan.config.target==='github'){emit('canvas.completed',{requestId:p.requestId,message:'픽스처 GitHub 푸시 완료'});return;}
  if(currentPlan.config.target==='s3'){emit('workspace.deploy.s3.result',{ok:true,result:{url:'https://example.test',bucket:'recoder-site-board',region:'ap-northeast-2',uploaded:['index.html','app.js'],excluded_note:'.env 제외'}});return;}
  snapshot.deployment={running:true,stage:'in_progress',stage_text:'소스·Dockerfile 검사 중',deployment_id:'fixture-deploy',steps:[{key:'source_scan',label:'소스 검사',status:'running'}]};emit('workspace.deploy.result',{deployment_id:'fixture-deploy',message:'픽스처 배포 요청 접수'});
 }
 if(m.type==='workspace.deploy.ecs.status')emit('workspace.deploy.ecs.statusResult',snapshot.deployment);
 if(m.type==='workspace.deploy.s3.dirs')emit(m.type,{suggested:'dist'});
 if(m.type==='canvas.discord.settings')discordOnline=true;
 if(m.type==='canvas.discord.status')discordOnline?emit('canvas.discord.statusResult',connectionsFixture?{}:{active_channel_id:'1',channel_name:'recoder-alerts',guild_name:'ReCoder',connected_clients:1}):emit('canvas.error',{context:m.type,message:'Discord 봇 서버에 연결할 수 없습니다 (127.0.0.1:8765). ReCoder Discord 봇을 실행하거나 연결 설정에서 서버 주소를 확인한 뒤 다시 시도하세요.'});
 if(m.type==='canvas.discord.setChannel')emit('canvas.discord.statusResult',{active_channel_id:p.channelId,channel_name:p.channelId?'recoder-alerts':'',guild_name:'ReCoder',connected_clients:1});
 if(m.type==='canvas.discord.guilds')emit('canvas.discord.guildsResult',{guilds:[{id:'1',name:'ReCoder'}]});
 if(m.type==='canvas.discord.channels')emit('canvas.discord.channelsResult',{channels:[{id:'1',name:'recoder-alerts'}]});
 if(m.type==='canvas.discord.event')emit('canvas.discord.eventResult',{event_id:p.eventId,ok:true});
 if(m.type==='workspace.deploy.preflight')emit('workspace.deploy.preflightResult',{app_kind:'server',summary:'Node.js',evidence:[],recommended_target:'ecs',blocked:false,reasons:[]});
 if(m.type==='aws.status')emit('aws.status',{...snapshot.aws,identity:{account:snapshot.aws.account}});
 if(m.type==='aws.listProfiles')emit('aws.profiles',{profiles:['preview']});
 if(m.type==='aws.role.setup'||m.type==='aws.configure'||m.type==='aws.connect.profile'){
  window.__canvasScenario('connected');
  emit('aws.configure.result',{ok:true,status:{...snapshot.aws,identity:{account:snapshot.aws.account},storage:'assumed_role'}});
  emit('aws.status',{...snapshot.aws,identity:{account:snapshot.aws.account},storage:'assumed_role'});
 }
 if(m.type==='aws.clear'){window.__canvasScenario('locked');emit('aws.clear.result',{ok:true});}
 if(m.type==='runScan')emit('scanResult',{requestId:p.requestId,scan_type:p.scanType,status:'ok',critical_count:0,high_count:0,findings:[],summary:'샘플 검사 완료 · 발견 항목 없음'},1200);
 if(m.type==='aws.onboarding')emit('aws.onboarding.result',{steps:['미리보기에서는 AWS 콘솔을 열지 않습니다. 샘플 프로필로 연결 흐름을 확인하세요.']});
}});
window.__canvasScenario=name=>{
 snapshot.aws.ready=name!=='locked';snapshot.scan=null;snapshot.topology=null;snapshot.deployment={running:false,stage:'idle'};
 snapshot.resource=snapshot.aws.ready?{cluster:'recoder-cluster',service:'board-app-svc',region:'ap-northeast-2',image:'board-app:v1',image_digest:'sha256:fixture',previous_task_definition:'arn:aws:ecs:ap-northeast-2:123456789012:task-definition/board:8'}:null;
 snapshot.s3=snapshot.aws.ready?{bucket:'recoder-site-board',region:'ap-northeast-2'}:null;
 if(['scanning','blocked','deployed'].includes(name)) {
  snapshot.deployment={running:name==='scanning',stage:name==='deployed'?'done':name==='blocked'?'failed':'in_progress',stage_text:name==='scanning'?'이미지 취약점 검사 중':name==='blocked'?'보안 게이트 차단':'배포 완료',deployment_id:'fixture-deploy',steps:[{key:'source_scan',label:'소스·Dockerfile 검사',status:'done'},{key:'image_scan',label:'이미지 검사',status:name==='scanning'?'running':name==='blocked'?'failed':'done'}],service_url:name==='deployed'?'https://example.test':'',error:name==='blocked'?'CRITICAL 취약점이 발견되었습니다.':''};
  snapshot.scan={blocked:name==='blocked',gaps:[],tools:['hadolint','gitleaks','trivy','OPA'].map(tool=>({tool,state:tool==='trivy'&&name==='scanning'?'running':tool==='trivy'&&name==='blocked'?'findings':'passed',detail:''})),findings:name==='blocked'?[{tool:'gitleaks',severity:'critical',title:'시크릿 탐지 (값 숨김)',location:'app.js:12',fix:'키를 제거하고 교체하세요.'}]:[]};
 }
 if(name==='deployed')snapshot.topology={cluster:'recoder-cluster',service:'board-app-svc',region:'ap-northeast-2',desired:2,running:2,observed_at:new Date().toISOString(),truncated:false,tasks:['a12345678','b87654321'].map(id=>({id,status:'RUNNING',health:'HEALTHY',launch_type:'FARGATE',images:[{image:'board-app:v1',digest:'sha256:fixture'}]}))};
 document.querySelector('[data-fixture-refresh]')?.click();
};
const DeploymentCanvas=require('../out/webview-test/components/canvas/DeploymentCanvas.js').default;
const {WorkspaceLayout}=require('../out/webview-test/App.js');
const App=()=>{
 const [view,setView]=React.useState(new URLSearchParams(window.location.search).get('start')==='home'?'home':'hub:deploy');
 const [counts,setCounts]=React.useState({});
 React.useEffect(()=>{const update=e=>setCounts(c=>({...c,[e.detail]:(c[e.detail]||0)+1}));window.addEventListener('fixture-message',update);return()=>window.removeEventListener('fixture-message',update);},[]);
 const routed=new URLSearchParams(window.location.search).get('layout')==='workspace';
 const clean=new URLSearchParams(window.location.search).has('clean');
 React.useEffect(()=>{const style=document.createElement('style');style.textContent=`*{box-sizing:border-box}.rc-workspace{height:calc(100vh - ${clean?24:70}px)!important}`;document.head.append(style);return()=>style.remove();},[clean]);
 return React.createElement('div',{style:{padding:routed?0:24,maxWidth:routed?'none':1500,margin:'auto'}},clean?React.createElement('div',{style:{padding:'4px 16px',fontSize:11,color:'#8493a2'}},'디자인 미리보기 · 샘플 데이터'):React.createElement('div',{style:{marginBottom:16,color:'#a3aec1',display:'flex',flexWrap:'wrap',gap:8}},'격리된 UI 픽스처 · 외부 실행 없음',...['locked','connected','scanning','blocked','deployed'].map(s=>React.createElement('button',{key:s,onClick:()=>{window.__canvasScenario(s);window.dispatchEvent(new Event('fixture-refresh'));}},s))),
  !clean&&React.createElement('output',{'aria-label':'픽스처 요청 통계',style:{display:'block',fontSize:12,color:'#a3aec1'}},`스냅샷 ${counts['canvas.snapshot']||0} · 승인 실행 ${counts['canvas.execute']||0} · 취소 ${counts['canvas.cancel']||0} · 조회 지연 ${snapshotDelay}ms`),
  routed?React.createElement(WorkspaceLayout,{view,onSelectMode:setView,isAiReady:true,isDockerReady:true,isOpsReady:true,diagnostics:null,coreStatus:'ok',showDiagnostics:false,costSummary:null,onToggleDiagnostics(){},postMessage(){}}):React.createElement(DeploymentCanvas,{isAiReady:true,isDockerReady:true,onOpenDocker(){}}));
};
createRoot(document.getElementById('root')).render(React.createElement(App));
window.addEventListener('fixture-refresh',()=>{const buttons=Array.from(document.querySelectorAll('button'));buttons.find(b=>b.textContent==='새로고침')?.click();});
