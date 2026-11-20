const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server');
const {securityModel,impactOf,attackRows}=require('../out/webview-test/components/securityRisks');
const {SecurityDashboard}=require('../out/webview-test/components/SecurityDashboard');
const {RedTeamTrafficView,outcomeOf}=require('../out/webview-test/components/RedTeamTrafficView');

const ok=(findings=[],extra={})=>({status:'ok',findings,critical_count:0,high_count:0,medium_count:0,...extra});
const code={score:100,findings:[]};
const clean={trivy:ok(),hadolint:ok(),gitleaks:ok()};

test('verdict: nothing run is "검사 전", never safe',()=>{
 const m=securityModel({scans:{},scanning:false});
 assert.equal(m.verdict,'idle');assert.equal(m.title,'검사 전');assert.equal(m.risks.length,0);
});
test('verdict: running wins over results',()=>{
 assert.equal(securityModel({scans:clean,scanning:true,code}).verdict,'running');
 assert.equal(securityModel({scans:clean,scanning:false,code,codeRunning:true}).verdict,'running');
});
test('verdict: safe only when every basic check really ran clean',()=>{
 assert.equal(securityModel({scans:clean,scanning:false,code}).verdict,'safe');
 //: 코드 검사를 안 했으면 안전이라 하지 않는다.
 assert.equal(securityModel({scans:clean,scanning:false}).verdict,'caution');
 //: 실패·미실행 검사는 통과가 아니다.
 for(const status of ['error','not_run','unverified']){
  const m=securityModel({scans:{...clean,gitleaks:{status}},scanning:false,code});
  assert.equal(m.verdict,'caution',status);assert.match(m.reason,/확인하지 못한/);
 }
 assert.equal(securityModel({scans:clean,scanning:false,codeError:'fetch failed'}).verdict,'caution');
 //: 이미지가 아직 없음(해당 없음)은 막지 않는다.
 assert.equal(securityModel({scans:{...clean,trivy:{status:'not_run',reason_code:'image_not_found'}},scanning:false,code}).verdict,'safe');
});
test('secrets and critical CVEs are 위험, high is 주의, medium is 낮음',()=>{
 const m=securityModel({scanning:false,code,scans:{...clean,
  gitleaks:ok([{severity:'CRITICAL',rule_id:'aws-key',file:'.env.example',line:3}]),
  trivy:ok([{severity:'CRITICAL',package:'busybox',installed:'1.37.0',fixed:'',title:'denial of service',id:'CVE-1',target:'shop-web (alpine 3.22)'},
            {severity:'HIGH',package:'openssl',installed:'3.5.4',fixed:'3.5.5',title:'out-of-bounds read',id:'CVE-2'},
            {severity:'HIGH',package:'openssl',installed:'3.5.4',fixed:'3.5.5',title:'timing side-channel',id:'CVE-3'}]),
  hadolint:ok([{severity:'MEDIUM',file:'Dockerfile',line:3,code:'DL3018',message:'Pin versions'}])}});
 assert.equal(m.verdict,'danger');assert.equal(m.title,'위험');
 assert.deepEqual(m.tally,{danger:2,caution:1,low:1});
 const [first]=m.risks;assert.equal(first.level,'danger');
 const busybox=m.risks.find(r=>r.where.startsWith('busybox'));
 assert.equal(busybox.title,'서버 멈춤');assert.equal(busybox.fix,'wait');assert.match(busybox.where,/shop-web/);
 const openssl=m.risks.find(r=>r.where.startsWith('openssl'));
 assert.equal(openssl.count,2,'같은 패키지의 CVE 는 한 줄로');assert.equal(openssl.fix,'auto');
 const secret=m.risks.find(r=>r.source==='secret');assert.equal(secret.title,'키 노출');assert.equal(secret.fix,'manual');
 assert.equal(m.steps.find(s=>s.key==='gitleaks').state,'bad');assert.equal(m.steps.find(s=>s.key==='hadolint').state,'ok');
});
test('plain-language impact never invents: unknown titles stay generic',()=>{
 assert.equal(impactOf('Remote Code Execution in foo'),'서버 장악');
 assert.equal(impactOf('Regular expression denial of service'),'서버 멈춤');
 assert.equal(impactOf('',['CWE-89']),'DB 공격');
 assert.equal(impactOf('Cross-site Scripting (XSS)'),'화면 조작');
 assert.equal(impactOf('something unusual'),'알려진 취약점');
});
test('code findings route to the AI fix flow with readable titles',()=>{
 const m=securityModel({scans:clean,scanning:false,code:{score:70,findings:[{rule:'SQL_INJECTION',label:'SQL 인젝션',file:'app.js',line:2,message:'x',severity:'high',fix:''}]}});
 assert.equal(m.risks[0].title,'DB 공격');assert.equal(m.risks[0].fix,'code');assert.equal(m.risks[0].level,'caution');
});
test('attack rows: tested without findings = 막음, unverifiable checks are never 막음',()=>{
 assert.ok(attackRows(undefined).every(r=>r.state==='untested'));
 const a={schema:1,status:'review_required',findings:[{kind:'sql_injection',evidence:'agent_reported',title:'t',route:'/s',observation:'o'}],warnings:[],limitations:[],
  checks:[{kind:'discovery',status:'tested',reason:'',requests:['GET /']},{kind:'sql_injection',status:'tested',reason:'',requests:['GET /s','GET /s?a','GET /s?b']},
   {kind:'authentication',status:'blocked',reason:'',requests:['POST /login']},{kind:'authorization',status:'not_tested',reason:'',requests:[]},{kind:'input_handling',status:'not_tested',reason:'',requests:[]}]};
 const rows=Object.fromEntries(attackRows(a).map(r=>[r.kind,r.state]));
 assert.deepEqual(rows,{discovery:'blocked',sql_injection:'review',authentication:'unverified',authorization:'untested',input_handling:'untested'});
 const m=securityModel({scans:clean,scanning:false,code,attack:a});
 assert.equal(m.steps.find(s=>s.key==='attack').state,'warn');
 assert.ok(m.risks.some(r=>r.source==='attack'&&r.level==='caution'));
});
test('dashboard renders verdict, risk list, attack card and every existing entry point',()=>{
 const html=renderToStaticMarkup(React.createElement(SecurityDashboard));
 for(const label of ['검사 전','보안 검사 시작','레드팀 테스트','공격 테스트','키 노출','배포 후 감시','검사 결과 상세','AI 감사','다른 실행 방식','prefers-reduced-motion','max-width:620px'])assert.ok(html.includes(label),label);
 assert.doesNotMatch(html,/안전<\/h1>/,'never shows 안전 before any check');
});
test('attack stage: one probe per real request, none when inactive, no request numbers invented',()=>{
 const events=[{id:1,method:'GET',path:'/',status:200,durationMs:10,blocked:false},{id:2,method:'POST',path:'/login',status:401,durationMs:58,blocked:false}];
 const live=renderToStaticMarkup(React.createElement(RedTeamTrafficView,{events,active:true,target:'http://host.docker.internal:3000/',caption:'로그인 우회 진행 중'}));
 assert.equal((live.match(/rt-packet/g)||[]).length,1,'latest request only marked once');
 assert.match(live,/마지막 요청 #2/);assert.match(live,/58 ms/);assert.match(live,/localhost:3000/);assert.match(live,/로그인 우회 진행 중/);
 assert.match(live,/거부 <b>1<\/b>/);assert.match(live,/응답 <b>1<\/b>/);
 const idle=renderToStaticMarkup(React.createElement(RedTeamTrafficView,{events,active:false}));
 assert.equal((idle.match(/class="rt-probe/g)||[]).length,0);assert.doesNotMatch(idle,/class="rt-scanline"/);assert.match(live,/class="rt-scanline"/);
 const managed=renderToStaticMarkup(React.createElement(RedTeamTrafficView,{events:[],active:true,traffic:false}));
 assert.doesNotMatch(managed,/요청 <b>/,'no request counter without real traffic data');
 assert.equal(outcomeOf({status:403,blocked:false}),'refused');assert.equal(outcomeOf({status:502,blocked:false}),'error');assert.equal(outcomeOf({status:200,blocked:true}),'limited');
});

const {fixFor}=require('../out/webview-test/components/securityRisks');
const {shieldPoint}=require('../out/webview-test/components/RedTeamTrafficView');
test('fix button only when a real automatic fix matches that row',()=>{
 const m=securityModel({scanning:false,code,scans:{...clean,
  trivy:ok([{severity:'HIGH',package:'axios',installed:'0.19.2',fixed:'0.28.0',title:'SSRF',class:'lang-pkgs',target:'app/package-lock.json'},
            {severity:'CRITICAL',package:'busybox',installed:'1.37.0-r12',fixed:'1.37.0-r13',title:'dos',class:'os-pkgs',target:'shop (alpine 3.22)'},
            {severity:'HIGH',package:'left-pad',installed:'1.0.0',fixed:'1.0.1',title:'x',class:'lang-pkgs'}]),
  hadolint:ok([{severity:'MEDIUM',file:'Dockerfile',line:3,code:'DL3018',message:'Pin versions'}]),
  gitleaks:ok([{severity:'CRITICAL',rule_id:'generic',file:'src/config.js',line:2}])}});
 const proposals=[{id:'a',tool:'trivy',title:'axios 0.19.2 → 0.28.0',detail:'package.json',files:['package.json'],auto:true},
  {id:'b',tool:'trivy',title:'베이스 이미지 OS 패키지 1개 패치',detail:'apk upgrade',files:['Dockerfile'],auto:true,rebuild:true},
  {id:'c',tool:'hadolint',title:'OS 패키지 버전은 기본 이미지 태그로 고정(이 줄만 예외) (DL3018)',detail:'',files:['Dockerfile'],auto:true},
  {id:'d',tool:'gitleaks',title:'키를 .env 로 옮기기',detail:'',files:['src/config.js'],auto:true}];
 const by=w=>m.risks.find(r=>r.where.startsWith(w));
 assert.equal(fixFor(by('axios'),proposals).proposal.id,'a');
 assert.equal(fixFor(by('busybox'),proposals).proposal.id,'b','OS 패키지는 베이스 이미지 패치로');
 assert.equal(fixFor(by('left-pad'),proposals),null,'수정안이 없으면 고치기를 띄우지 않는다');
 assert.equal(fixFor(m.risks.find(r=>r.source==='dockerfile'),proposals).proposal.id,'c');
 assert.equal(fixFor(m.risks.find(r=>r.source==='secret'),proposals).proposal.id,'d');
 assert.equal(fixFor(by('axios'),[{...proposals[0],auto:false}]).kind,'manual');
 assert.equal(fixFor(by('axios'),[{...proposals[0],title:'axios-retry 1 → 2'}]),null,'이름이 겹치는 다른 패키지와 섞지 않는다');
});
test('watch step is green only with real records, and skipped checks are said out loud',()=>{
 const st=w=>securityModel({scans:clean,scanning:false,code,watch:w}).steps.find(s=>s.key==='watch').state;
 assert.equal(st(undefined),'idle');assert.equal(st({result:{}}),'idle');
 assert.equal(st({result:{status:'not_run',message:'x'}}),'idle');assert.equal(st({result:{status:'ok',checked:2}}),'ok');
 assert.equal(st({result:{web:{checked_at:'t',known:{a:{id:'CVE',name:'x',version:'1'}}}}}),'warn');
 assert.equal(st({result:{web:{checked_at:'t',known:{a:{id:'CVE',name:'x',version:'1',fixed_at:'t'}}}}}),'ok');
 assert.equal(st({error:'x'}),'warn');
 const m=securityModel({scans:{...clean,trivy:{status:'not_run',reason_code:'image_not_found'}},scanning:false,code});
 assert.equal(m.verdict,'safe');assert.ok(m.skipped.some(t=>/이미지 검사는 건너뛰었어요/.test(t)));
});
test('attack impacts land exactly on the drawn rounded shield',()=>{
 const CX=320,CY=128,RX=176,RY=104,CR=26;
 for(let deg=0;deg<360;deg+=7){
  const [x,y]=shieldPoint(deg*Math.PI/180),dx=Math.abs(x-CX),dy=Math.abs(y-CY);
  let dist;
  if(dx>RX-CR&&dy>RY-CR)dist=Math.hypot(dx-(RX-CR),dy-(RY-CR))-CR;else dist=Math.max(dx-RX,dy-RY);
  assert.ok(Math.abs(dist)<0.01,`${deg}° off by ${dist}`);
 }
});

//: Core 실제 경로로 만든 결과 — hadolint 2.15.1 실제 출력(--format json, 표준 입력) + Trivy JSON v2 형식 보고서를
//: InfraAgent._filter_trivy_results → deploy._normalise_scan_result → security_fix.plan 에 그대로 통과시킨 것.
test('Core pipeline output: plain titles, honest locations, and fix buttons only where a fix really applies',()=>{
 const n=JSON.parse(JSON.stringify(require('./fixtures/core-scan-pipeline.json')));const proposals=n._proposals;delete n._proposals;
 const m=securityModel({scans:n,scanning:false,code:{score:100,findings:[]}});
 assert.equal(m.verdict,'danger');assert.deepEqual(m.tally,{danger:2,caution:5,low:1});
 const row=w=>m.risks.find(r=>r.where.startsWith(w));
 assert.equal(row('busybox').fix,'wait');assert.equal(fixFor(row('busybox'),proposals),null,'고친 버전 없는 CVE 에 고치기 없음');
 assert.equal(fixFor(row('libssl3'),proposals).proposal.title.startsWith('베이스 이미지 OS 패키지'),true);
 assert.equal(row('axios').where,'axios · 앱 의존성');assert.equal(row('axios').title,'내부망 접근');
 assert.equal(row('cross-spawn').where,'cross-spawn · 이미지의 npm');assert.equal(fixFor(row('cross-spawn'),proposals).kind,'manual');
 assert.ok(m.risks.every(r=>!r.where.includes('Node.js')),'Trivy 분류명을 위치로 쓰지 않는다');
 assert.ok(m.risks.filter(r=>r.source==='dockerfile').every(r=>r.where.startsWith('Dockerfile:')),'표준 입력의 "-" 를 Dockerfile 로');
 assert.equal(fixFor(m.risks.find(r=>r.source==='secret'),proposals).kind,'auto');
 assert.equal(m.risks.find(r=>r.source==='secret').details[0].title,n.gitleaks.findings[0].secret_type);
});
