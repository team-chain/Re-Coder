// 3.0.2 화면 개편 — 홈(벤토) · 허브 탭 · 위 막대 · 아래 요청창 · 글자 기호/캐릭터 없음.
const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {HomeView,securitySummary,previewable,isOk}=require('../out/webview-test/components/shell/HomeBento');
const {HubTabs,HUB_TABS,HUB_ENTRY}=require('../out/webview-test/components/Hubs');
const {WorkspaceLayout}=require('../out/webview-test/App.js');
const {parseNumstat,parseFirstHunk}=require('../out/sidebar/homeChanges.js');
const fs=require('node:fs'),path=require('node:path');

const NOW=Date.parse('2026-10-24T08:00:00Z');
const snap={workspace:'/w/app',projectName:'temp-app',git:{repository:'me/app',branch:'main',dirty:true,connected:true,head:'a1b2c3d4',initialized:true,hasOrigin:true},
 aws:{ready:true,region:'ap-northeast-2',account:'1'},deployment:{running:false,stage:'idle'},resource:null,
 topology:{cluster:'c',service:'app',region:'r',desired:2,running:2,task_definition:'td',observed_at:'',truncated:false,tasks:[]},
 scan:{blocked:false,gaps:[],tools:[{tool:'trivy',state:'ok',detail:''},{tool:'gitleaks',state:'ok',detail:''}],findings:[]},s3:null,warnings:[]};
const hist=[{key:'ecs:1',source:'ecs',deployment_id:'1',target:'c / app',region:'ap-northeast-2',status:'succeeded',status_text:'배포 완료',started_at:new Date(NOW-7200e3).toISOString(),service_url:'https://app.example.com'}];
const mon={key:'k',service:'app',region:'r',cluster:'c',metrics:true,service_state:{status:'ACTIVE',running:2,desired:2,revision:'app:4',task_definition:'x'},
 series:{requests:[1,2,30,2,3],errors:[0,0,4,0,0],blocked:[0,0,0,0,0]},now:{rpm:42,error_rate:0.004,points:5},watcher:{installed:true},events:[],
 offers:[{event:'e',title:'오류 급증',at:'',target:'arn:aws:ecs:r:1:task-definition/app:3',from_td:'arn:aws:ecs:r:1:task-definition/app:4',label:'',expires:0}],alerts:[],gaps:[]};
const changes={files:[{path:'src/a.ts',added:3,removed:1,binary:false}],added:3,removed:1,untracked:0,snippet:{file:'src/a.ts',lines:[{kind:'del',text:'old'},{kind:'add',text:'new'}]}};
const EMOJI=/[\u{1F300}-\u{1FAFF}☀-➿]/u;

test('3.0.2 홈: 실제 데이터로 여섯 칸(미리보기 · 요청 · 보안 · 승인 · 배포 경로 · 바뀐 코드)',()=>{
 const html=renderToStaticMarkup(React.createElement(HomeView,{data:{snapshot:snap,history:hist,monitor:mon,changes,now:NOW},go(){},open(){}}));
 for(const id of ['home-app','home-traffic','home-security','home-approvals','home-path','home-changes']) assert.match(html,new RegExp(`data-testid="${id}"`));
 assert.match(html,/<iframe[^>]*sandbox="allow-scripts"[^>]*src="https:\/\/app\.example\.com"|<iframe[^>]*src="https:\/\/app\.example\.com"[^>]*sandbox="allow-scripts"/);
 assert.match(html,/app:3 으로 되돌리기/);assert.match(html,/승인 대기 · 1/);
 assert.match(html,/aria-label="Amazon ECS"/);assert.match(html,/aria-label="GitHub"/);
 assert.match(html,/\+ new/);assert.match(html,/2시간 전/);
 assert.doesNotMatch(html,EMOJI);assert.doesNotMatch(html,/[✓✕✗]/);
});

test('3.0.2 홈: 데이터가 없으면 무엇을 하면 되는지 한 줄 + 버튼 하나',()=>{
 const html=renderToStaticMarkup(React.createElement(HomeView,{data:{snapshot:{...snap,aws:{ready:false,region:'',account:''},topology:null,scan:null},history:[],monitor:null,changes:{files:[],added:0,removed:0,untracked:0,snippet:null},now:NOW},go(){},open(){}}));
 assert.match(html,/아직 배포한 버전이 없습니다/);assert.match(html,/배포 캔버스/);
 assert.match(html,/AWS 를 연결하고 배포하면/);assert.match(html,/검사 전/);assert.match(html,/커밋하지 않은 변경이 없습니다/);
 assert.doesNotMatch(html,/<iframe/);
});

test('3.0.2 홈 보조 함수: 미리보기 주소 · 보안 요약 · 성공 판정',()=>{
 assert.equal(previewable('https://a.com'),true);assert.equal(previewable('http://localhost:3000'),true);
 assert.equal(previewable('http://x.elb.amazonaws.com'),false,'https 웹뷰 안에서 http 사이트는 막힌다');assert.equal(previewable('javascript:alert(1)'),false);
 assert.equal(securitySummary(null).label,'검사 전');
 assert.equal(securitySummary({...snap,scan:{...snap.scan,blocked:true}}).tone,'bad');
 assert.equal(securitySummary({...snap,scan:{...snap.scan,findings:[{tool:'gitleaks',severity:'high',rule:'',title:'',location:'',fix:''}]}}).secrets,1);
 assert.equal(isOk({status:'success'}),true);assert.equal(isOk({status:'failed'}),false);
});

test('3.0.2 허브 탭: 위쪽 탭은 첫 화면으로, 아래 줄에 허브의 모든 기능',()=>{
 assert.deepEqual(HUB_ENTRY,{develop:'code',deploy:'hub:deploy',security:'hub:security',monitor:'hub:monitor'});
 const html=renderToStaticMarkup(React.createElement(HubTabs,{hub:'develop',current:'map',onGo(){}}));
 for(const t of HUB_TABS.develop) assert.ok(html.includes(`>${t.label}<`),t.label);
 assert.match(html,/aria-current="page"[^>]*>아키텍처</);
 const docker=renderToStaticMarkup(React.createElement(HubTabs,{hub:'deploy',current:'hub:deploy',ctx:{isAiReady:true,isDockerReady:false,isOpsReady:true},onGo(){}}));
 assert.match(docker,/Docker 필요 · 자동 조치/,'Docker 자동 조치는 Deploy 에서 늘 보인다');
});

test('3.0.2 위 막대 · 아래 요청창: 홈과 네 허브, 코드 생성 화면에는 요청창이 없다',()=>{
 const render=view=>renderToStaticMarkup(React.createElement(WorkspaceLayout,{view,diagnostics:null,coreStatus:'ok',showDiagnostics:false,isAiReady:true,isDockerReady:true,isOpsReady:true,costSummary:null,onSelectMode(){},onToggleDiagnostics(){},postMessage(){}}));
 const home=render('home');
 assert.match(home,/aria-label="작업 선택"/);for(const t of ['홈','Develop','Deploy','Security','Operate']) assert.ok(home.includes(`>${t}<`)||home.includes(`>${t}<span`),t);
 assert.match(home,/aria-label="ReCoder 에 요청"/);
 assert.doesNotMatch(render('code'),/aria-label="ReCoder 에 요청"/);
 assert.doesNotMatch(home,EMOJI);
});

test('3.0.2 바뀐 코드 읽기: numstat · 첫 변경 부분만',()=>{
 assert.deepEqual(parseNumstat('3\t1\tsrc/a.ts\n-\t-\timg.png\n2\t0\tsrc/{old => new}.ts\n'),[
  {path:'src/a.ts',added:3,removed:1,binary:false},{path:'img.png',added:0,removed:0,binary:true},{path:'src/new.ts',added:2,removed:0,binary:false}]);
 const hunk=parseFirstHunk('diff --git a/x b/x\n@@ -1,3 +1,3 @@\n ctx\n-old\n+new\n@@ -9 +9 @@\n+later\n');
 assert.deepEqual(hunk.map(l=>l.kind),['ctx','del','add']);
});

test('3.0.2 웹뷰에 캐릭터 · 글자 기호가 없다(로고만)',()=>{
 const root=path.join(__dirname,'../webview-src');const bad=[];
 const walk=d=>{for(const f of fs.readdirSync(d)){const p=path.join(d,f);if(fs.statSync(p).isDirectory())walk(p);else if(/\.tsx?$/.test(f)){
  fs.readFileSync(p,'utf8').split('\n').forEach((l,i)=>{const t=l.trim();if(t.startsWith('//')||t.startsWith('*')||t.startsWith('/*'))return;if(/[✓✕✗]/.test(l))bad.push(`${f}:${i+1}`);});}}};
 walk(root);assert.deepEqual(bad,[]);
});
