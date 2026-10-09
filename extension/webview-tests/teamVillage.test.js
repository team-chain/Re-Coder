const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {buildRoster,reduceTeam,emptyTeamView,recordOf,agentName}=require('../out/webview-test/components/teamState');
const {TeamVillage,villageLayout}=require('../out/webview-test/components/TeamVillage');
const {TeamBoard}=require('../out/webview-test/components/TeamBoard');

const FILES=[{file:'package.json',layer:0},{file:'src/db.js',layer:0},{file:'src/routes/a.js',layer:1},{file:'src/routes/b.js',layer:1},{file:'src/routes/c.js',layer:1},{file:'public/index.html',layer:2},{file:'public/app.js',layer:2}];
const run=(evs)=>evs.reduce((v,e)=>reduceTeam(v,e),emptyTeamView());

test('진행 기록은 실제 이벤트를 누가 무엇을 했는지로 적는다 — 고치는 것은 작성한 개발 슬롯',()=>{
 const v=run([{step:'planning'},{step:'planned',total:7,files:FILES},{step:'wave',layer:0,agents:1},
  {step:'file_start',agent:'agent-1',file:'package.json'},{step:'fixing',agent:'agent-1',file:'package.json',message:'package.json 고치는 중 — JSON 문법 오류'},
  {step:'file_done',agent:'agent-1',file:'package.json',lines:24,done_count:1,total:7},{step:'waiting',agent:'agent-2',seconds:61}]);
 const t=v.records.map(r=>`${r.who}|${r.text}`);
 assert.ok(t.includes('설계|작업 7개로 나눔 — 공통 기반 2 → 기능 3 → 화면 2'));
 assert.ok(t.some(x=>x.startsWith('|공통 기반 2개 — 다른 파일이 기대므로 한 명이 순서대로')));
 assert.ok(t.includes('개발 1|package.json 작성 시작'));
 assert.ok(t.includes('개발 1|자동 검사에서 문제 발견 → 고치는 중 (JSON 문법 오류)'),t.join('\n'));
 assert.ok(t.includes('개발 1|package.json 완료 (24줄)'));
 assert.ok(t.some(x=>x.startsWith('개발 2|분당 호출 한도 — 61초 대기')));
 assert.ok(!t.some(x=>x.startsWith('검토|')&&x.includes('고치는 중')),'파일마다 고치는 것은 검토 에이전트가 아니다');
 assert.deepEqual(agentName('agent-3'),{who:'개발 3',role:'dev'});
 assert.equal(recordOf({step:'file_part',agent:'agent-1',part:2},[]),null);
 const many=Array.from({length:40},(_,i)=>({step:'file_start',agent:'agent-1',file:`f${i}.js`}));
 assert.equal(run(many).records.length,30);
});

test('마을 배치: 작업대가 겹치지 않고 폭 안에 들어간다(개발 1~6명, 폭 300~1200)',()=>{
 for(const W of [300,520,760,1200])for(let n=1;n<=6;n++){
  const L=villageLayout(W,n);
  assert.equal(L.desks.length,n);
  for(let i=1;i<n;i++)assert.ok(L.desks[i].x>=L.desks[i-1].x+L.desks[i-1].w,`${W}/${n}`);
  if(W>=520)assert.ok(L.desks[n-1].x+L.desks[n-1].w<=W-8,`${W}/${n} 오른쪽 넘침`);
  for(const q of L.qcols)assert.ok(q.x>=L.queue.x&&q.x+q.w<=L.queue.x+L.queue.w+0.5);
 }
});

test('마을: 남은 카드·잠긴 단계·작업 중 말풍선·자동 검사 교정은 작성자 작업대에',()=>{
 const roster=buildRoster(2);
 let v=run([{step:'planned',total:7,files:FILES},{step:'wave',layer:0,agents:1},
  {step:'file_start',agent:'agent-1',file:'package.json'},{step:'file_done',agent:'agent-1',file:'package.json',done_count:1},
  {step:'file_start',agent:'agent-1',file:'src/db.js'},{step:'fixing',agent:'agent-1',file:'src/db.js',message:'src/db.js 고치는 중 — 하드코딩된 비밀값'}]);
 const html=renderToStaticMarkup(React.createElement(TeamVillage,{roster,view:v,width:900}));
 assert.match(html,/data-testid="team-village"/);
 assert.equal((html.match(/<img src="data:image\/png/g)||[]).length,roster.length);
 assert.equal((html.match(/class="cd"/g)||[]).length,5,'대기 카드 = 아직 시작 안 한 파일 수');
 assert.equal((html.match(/class="qc locked"/g)||[]).length,2,'공통 기반 중에는 기능·화면 칸이 잠김');
 assert.match(html,/class="dk fix"/);assert.match(html,/자동 검사 → 고치는 중/);
 assert.match(html,/공통 기반 먼저 — 대기/,'다른 개발 리코더는 공통 기반을 기다린다');
 assert.doesNotMatch(html,/전체 점검 — 빌드로 확인/);
 v=run([{step:'planned',total:7,files:FILES},...FILES.map((f,i)=>({step:'file_done',agent:'agent-1',file:f.file,done_count:i+1})),{step:'consistency',message:'전체 점검'}]);
 const chk=renderToStaticMarkup(React.createElement(TeamVillage,{roster,view:v,width:900}));
 assert.match(chk,/전체 점검 — 빌드로 확인/);assert.doesNotMatch(chk,/class="qc locked"/);assert.match(chk,/<b>2\/2<\/b>/);
});

test('보드: 폭을 모르는 첫 렌더(서버)는 한 줄 보기, 진행 기록 마지막 3줄',()=>{
 const v=run([{step:'planned',total:7,files:FILES},{step:'file_start',agent:'agent-2',file:'src/routes/a.js'}]);
 const html=renderToStaticMarkup(React.createElement(TeamBoard,{roster:buildRoster(2),view:v}));
 assert.doesNotMatch(html,/team-village/);assert.match(html,/개발 2 · src\/routes\/a\.js 작성 시작/);
 assert.match(html,/마지막에 전체 점검/);
});
