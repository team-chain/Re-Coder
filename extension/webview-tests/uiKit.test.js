// 2.0.46 새 화면 부품(kit · 배포 설정 시트 · GitHub 시트 · 퀵 휠 카드) 검사.
const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const kit=require('../out/webview-test/components/ui/kit');
const {Icon,ICON_NAMES}=require('../out/webview-test/components/ui/icons');
const {EcsSheet,S3Sheet,deployedTag,nextTag}=require('../out/webview-test/components/canvas/TargetSheets');
const {DEFAULT_PROTECTION,monthlyExtra,paidSummary}=require('../out/webview-test/components/EcsProtection');
const {GitHubPanel}=require('../out/webview-test/components/canvas/GitHubPanel');
const h=React.createElement,r=(el)=>renderToStaticMarkup(el);
const EMOJI=/[\u{1F300}-\u{1FAFF}☀-➿]/u;

test('kit: 선택자는 모두 .rk 아래(캔버스 버튼 규칙에 덮이지 않게)',()=>{
 const rules=kit.KIT_CSS.replace(/@media[^{]*\{/g,'').split('}').map(s=>s.split('{')[0].trim()).filter(Boolean);
 for(const sel of rules) for(const part of sel.split(',')) assert.match(part.trim(),/^\.rk(\b|[ .:[])/,part);
});

test('kit: KvRow 는 값과 「변경」을 보이고, 편집기가 없으면 버튼도 없다',()=>{
 const html=r(h(kit.Kit,null,h(kit.KvRow,{label:'이름',value:'temp-app',note:'지난 배포 v3',editor:()=>null,testId:'kv'})));
 assert.match(html,/temp-app/);assert.match(html,/이름 변경/);assert.match(html,/지난 배포 v3/);
 assert.doesNotMatch(r(h(kit.KvRow,{label:'이름',value:'x'})),/변경/);
});

test('kit: Segmented 는 라디오 그룹이고 고른 값이 checked',()=>{
 const html=r(h(kit.Segmented,{name:'s',value:'b',label:'강도',options:[{value:'a',label:'A'},{value:'b',label:'B'}],onChange(){}}));
 assert.match(html,/role="radiogroup"/);assert.match(html,/checked="" value="b"/);assert.doesNotMatch(html,/checked="" value="a"/);
});

test('kit: 지역 이름',()=>{assert.equal(kit.regionName('ap-northeast-2'),'서울');assert.equal(kit.regionName('xx-1'),'');});

test('icons: 모든 아이콘은 선 SVG, 모르는 이름은 file 모양으로',()=>{
 for(const n of ICON_NAMES){const s=r(h(Icon,{name:n}));assert.match(s,/stroke="currentColor"/);assert.match(s,new RegExp(`data-icon="${n}"`));}
 assert.match(r(h(Icon,{name:'nope'})),/<path d="M7 3h7l5 5v13H7zM14 3v5h5"/);
});

test('버전: 지난 배포가 v3 이면 다음은 v4',()=>{
 assert.equal(deployedTag('123.dkr.ecr/app:v3'),'v3');assert.equal(nextTag('123.dkr.ecr/app:v3'),'v4');
 assert.equal(nextTag('app:latest'),'');assert.equal(nextTag(''),'');assert.equal(deployedTag('app@sha256:abc'),'');
});

test('예상 추가 요금: HTTPS 만 $18, 대규모 방패 $25, DB +$15',()=>{
 assert.equal(monthlyExtra(DEFAULT_PROTECTION),0);
 assert.equal(monthlyExtra({...DEFAULT_PROTECTION,https:true}),18);
 assert.equal(monthlyExtra({...DEFAULT_PROTECTION,https:true,edge:'alb_waf'}),25);
 assert.equal(monthlyExtra({...DEFAULT_PROTECTION,database:'rds'}),15);
 assert.ok(paidSummary({...DEFAULT_PROTECTION,https:true}).includes('HTTPS 주소'));
});

const paidTag=(x)=>x.match(/<details[^>]*data-testid="ecs-paid"[^>]*>/)[0];
const config={ecs_service:'temp-app',aws_region:'ap-northeast-2',tag:'v4',cpu:'256',memory:'512',container_port:'3000',dir:''};
const common={config,update(){},running:false,busy:false,ready:true,onPrepare(){}};

test('ECS 시트: 미리 채운 값 · 보호 두 줄 · 유료는 접힘 · 요금 한 줄 · 버튼 하나',()=>{
 const html=r(h(EcsSheet,{...common,protection:DEFAULT_PROTECTION,onProtection(){},deployedImage:'app:v3',advanced:h('i',null,'ADV')}));
 assert.match(html,/ECS 로 배포/);assert.match(html,/temp-app/);assert.match(html,/서울/);assert.match(html,/지난 배포 v3/);
 assert.match(html,/기본 방패/);assert.match(html,/공격·장애 감지 알림/);
 assert.doesNotMatch(paidTag(html),/open/);
 assert.match(html,/data-testid="ecs-extra">없음 · Fargate 실행비만/);
 assert.equal((html.match(/data-testid="ecs-review"/g)||[]).length,1);
 assert.doesNotMatch(html,EMOJI);
 const on=r(h(EcsSheet,{...common,protection:{...DEFAULT_PROTECTION,https:true},onProtection(){},advanced:null}));
 assert.match(paidTag(on),/open/);assert.match(on,/월 약 \$18\+ · Fargate 실행비 별도/);
});

test('S3 시트: 폴더 자동 찾기 · 요금 한 줄',()=>{
 const html=r(h(S3Sheet,{...common,autoDir:true}));
 assert.match(html,/S3 로 배포/);assert.match(html,/자동으로 찾기/);assert.match(html,/거의 없음/);assert.doesNotMatch(html,EMOJI);
});

test('GitHub 시트: 처음엔 로그인 버튼 하나만',()=>{
 const html=r(h(GitHubPanel,{workspace:'/w',git:{branch:'main',dirty:false},onChanged(){},onReview(){},onWorkflow(){},disabled:false}));
 assert.match(html,/GitHub 에 푸시/);assert.match(html,/data-testid="github-primary"/);assert.doesNotMatch(html,EMOJI);
});
