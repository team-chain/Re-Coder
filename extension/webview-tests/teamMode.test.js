const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {buildRoster,reduceTeam,emptyTeamView,MAX_DEV_AGENTS,formatElapsed}=require('../out/webview-test/components/teamState');
const {TeamBoard,TeamComposer}=require('../out/webview-test/components/TeamBoard');
const {ANIMAL_KINDS,pickAnimal}=require('../out/webview-test/components/teamAnimals');

const seq=(...xs)=>{let i=0;return()=>xs[i++%xs.length];};

test('팀 구성: 설계 1 + 개발 N + 검토 1, 동물은 겹치지 않게 무작위, 기존 배정은 유지',()=>{
 const r=buildRoster(3,seq(0.1,0.5,0.9,0.3,0.7));
 assert.deepEqual(r.map(m=>m.role),['planner','dev','dev','dev','review']);
 assert.equal(new Set(r.map(m=>m.animal)).size,5);
 assert.ok(r.every(m=>ANIMAL_KINDS.includes(m.animal)));
 const more=buildRoster(4,Math.random,r);
 assert.deepEqual(more.slice(0,4).map(m=>m.animal),r.slice(0,4).map(m=>m.animal)); // 이미 나온 동물은 그대로
 assert.equal(buildRoster(99).filter(m=>m.role==='dev').length,MAX_DEV_AGENTS);
 assert.equal(buildRoster(0).filter(m=>m.role==='dev').length,1);
 assert.notEqual(pickAnimal(['dog','cat','rabbit','squirrel','penguin']),'dog');
});

test('진행 이벤트를 화면 상태로: 설계 → 동시 작업 → 이어 쓰기 → 즉시 교정 → 완료',()=>{
 let v=emptyTeamView();
 const ev=[
  {step:'planning',message:'설계 중',job_id:'abcdef123456'},
  {step:'planned',total:3,files:[{file:'package.json',layer:0},{file:'src/pay.js',layer:1},{file:'public/i.html',layer:2}],summary:'쇼핑몰',done_count:0},
  {step:'wave',layer:0},{step:'file_start',agent:'agent-1',file:'package.json'},{step:'file_done',agent:'agent-1',file:'package.json',done_count:1,total:3},
  {step:'wave',layer:1},{step:'file_start',agent:'agent-2',file:'src/pay.js'},{step:'file_part',agent:'agent-2',file:'src/pay.js',part:3,lines:420},
  {step:'fixing',agent:'agent-2',file:'src/pay.js',message:'src/pay.js 고치는 중 — 하드코딩된 비밀값(generic_secret_assignment) 3번째 줄'},
  {step:'retry',agent:'agent-3',message:'일시적 오류로 3초 뒤 다시 시도합니다 (1/5)'},
 ];
 for(const e of ev)v=reduceTeam(v,e);
 assert.equal(v.jobId,'abcdef123456');assert.equal(v.total,3);assert.equal(v.done,1);assert.equal(v.layer,1);
 assert.equal(v.agents['agent-1'].done,1);assert.equal(v.agents['agent-2'].state,'fixing');assert.equal(v.agents['agent-2'].part,3);
 assert.equal(v.fixes,1);assert.equal(v.secretFixes,1);assert.equal(v.retries,1);
 assert.equal(v.files.find(f=>f.file==='src/pay.js').state,'fixing');
 v=reduceTeam(v,{step:'file_done',agent:'agent-2',file:'src/pay.js',done_count:2,total:3});
 v=reduceTeam(v,{step:'consistency',message:'전체 점검'});assert.equal(v.phase,'checking');
 v=reduceTeam(v,{step:'done'});assert.equal(v.phase,'done');assert.equal(v.done,3);
 assert.ok(v.log.length<=5);
 assert.equal(formatElapsed(125.4),'2:05');
});

test('보드: 에이전트별 동물·역할·하고 있는 일, 일시 정지면 [이어서 만들기]',()=>{
 const roster=buildRoster(2,seq(0,0.2,0.4,0.6));
 let v=emptyTeamView();
 for(const e of [{step:'planned',total:4,files:[{file:'a.js',layer:0},{file:'b.js',layer:1},{file:'c.js',layer:1},{file:'d.html',layer:2}]},
   {step:'file_start',agent:'agent-1',file:'b.js'},{step:'file_part',agent:'agent-2',file:'c.js',part:2},{step:'file_done',agent:'agent-1',file:'a.js',done_count:1,total:4}])v=reduceTeam(v,e);
 const html=renderToStaticMarkup(React.createElement(TeamBoard,{roster,view:v}));
 assert.match(html,/팀 작업/);assert.match(html,/파일 1\/4/);assert.match(html,/c\.js · 2조각/);assert.match(html,/설계/);assert.match(html,/검토/);
 assert.match(html,/<svg/);assert.doesNotMatch(html,/이어서 만들기/);
 const paused=renderToStaticMarkup(React.createElement(TeamBoard,{roster,view:v,paused:{message:'AI 사용 한도',done:1,total:4},onResume:()=>{}}));
 assert.match(paused,/일시 정지/);assert.match(paused,/이어서 만들기 \(1\/4\)/);
});

test('구성 막대: 팀 모드 켜면 팀원과 [＋ 개발 에이전트], 최대 인원이면 추가 불가',()=>{
 const off=renderToStaticMarkup(React.createElement(TeamComposer,{enabled:false,roster:buildRoster(2),onToggle(){},onAdd(){},onRemove(){}}));
 assert.match(off,/팀 모드/);assert.doesNotMatch(off,/개발 에이전트/);
 const full=renderToStaticMarkup(React.createElement(TeamComposer,{enabled:true,roster:buildRoster(MAX_DEV_AGENTS),onToggle(){},onAdd(){},onRemove(){}}));
 assert.match(full,/＋ 개발 에이전트/);assert.match(full,/<button type="button" disabled="" aria-label="개발 에이전트 추가">/);
 assert.match(full,/효과가 작을 수 있어요/);
});
