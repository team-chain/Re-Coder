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
 assert.equal((html.match(/class="cd( dim)?"/g)||[]).length,5,'대기 카드 = 아직 시작 안 한 파일 수');
 assert.equal((html.match(/class="qc locked"/g)||[]).length,2,'공통 기반 중에는 기능·화면 칸이 잠김');assert.equal((html.match(/class="lk"/g)||[]).length,2);
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

test('전체 점검이 완성 파일을 고쳐도 완료 수·단계 ✓ 는 줄지 않고 "다듬는 중" 만 붙는다',()=>{
 const {polishingCount}=require('../out/webview-test/components/teamState');
 const built=[{step:'planned',total:7,files:FILES},...FILES.map((f,i)=>({step:'file_done',agent:'agent-1',file:f.file,done_count:i+1}))];
 let v=run([...built,{step:'consistency',message:'전체 점검'},{step:'fixing',agent:'review',file:'src/db.js',message:'전체 점검 — src/db.js 고치는 중'},
  {step:'fixing',agent:'review',file:'public/app.js',message:'전체 점검 — public/app.js 고치는 중'}]);
 assert.equal(v.files.filter(f=>f.state==='done').length,7,'완료로 센다');
 assert.equal(polishingCount(v),2);assert.equal(polishingCount(v,0),1);
 const html=renderToStaticMarkup(React.createElement(TeamVillage,{roster:buildRoster(2),view:v,width:900}));
 assert.match(html,/<b>2\/2<\/b>/);assert.match(html,/1개 다듬는 중/);
 const board=renderToStaticMarkup(React.createElement(TeamBoard,{roster:buildRoster(2),view:v}));
 assert.match(board,/✓ 공통 기반 2/);assert.match(board,/2개 다듬는 중/);
 v=reduceTeam(v,{step:'done'});assert.equal(polishingCount(v),0,'끝나면 표시를 지운다');
 //: 아직 쓰는 중인 파일의 교정은 예전처럼 "고치는 중"
 const w=run([{step:'planned',total:7,files:FILES},{step:'file_start',agent:'agent-1',file:'package.json'},{step:'fixing',agent:'agent-1',file:'package.json'}]);
 assert.equal(w.files.find(f=>f.file==='package.json').state,'fixing');
});

test('마을: 말풍선은 사람마다 따로 — 이웃은 높이를 엇갈리고 폭은 작업대 간격 안, 이름표와 완료 수는 두 줄',()=>{
 const {WAIT_TEXT}=require('../out/webview-test/components/TeamVillage');
 const roster=buildRoster(6);
 const v=run([{step:'planned',total:7,files:FILES},{step:'wave',layer:0,agents:1},{step:'file_start',agent:'agent-1',file:'package.json'},
  {step:'file_done',agent:'agent-1',file:'package.json',done_count:1},{step:'file_start',agent:'agent-1',file:'src/db.js'}]);
 for(const W of [520,760,1200]){
  const html=renderToStaticMarkup(React.createElement(TeamVillage,{roster,view:v,width:W}));
  const bubbles=html.match(/class="bb[^"]*"[^>]*>[^<]*/g)||[];
  assert.equal(bubbles.length,6,'작성 중 1 + 기다리는 5명 각자');
  for(const t of WAIT_TEXT.slice(0,5))assert.ok(html.includes(`>${t}<`),t);
  assert.equal(bubbles.filter(b=>/class="bb[^"]* up"/.test(b)).length,3,'이웃끼리 높이를 엇갈린다');
  const L=villageLayout(W,6);const pitch=L.desks[1].x-L.desks[0].x;
  assert.ok(L.bubbleMax<=2*pitch-10,'엇갈린 두 줄이면 이웃과 겹치지 않는 폭');
  assert.match(html,/<div class="lb"><span>개발 1 작업대<\/span><b>완료 1<\/b><\/div>/);
  for(let i=1;i<6;i++)assert.ok(L.desks[i].x-L.desks[i-1].x>=L.desks[i-1].w);
  const agentTop=L.desks[0].y-58,agentH=55;
  assert.ok(agentTop+agentH-(58+26)-18>=L.queue.y+L.queue.h-4,'윗줄 말풍선도 대기열을 가리지 않는다');
  assert.ok(L.desks[0].y+L.desks[0].h+30<=L.shelf.y,'이름표 두 줄이 선반과 겹치지 않는다');
 }
});

test('마을: 선반 칸 위치 — 넘치면 숫자만 올라간다',()=>{
 const {shelfSlot}=require('../out/webview-test/components/TeamVillage');
 const seg={x:0,y:0,w:200,h:58};
 assert.deepEqual(shelfSlot(seg,0),{x:8,y:20});assert.deepEqual(shelfSlot(seg,9),{x:8,y:29});
 assert.equal(shelfSlot(seg,36),null);assert.equal(shelfSlot(seg,-1),null);
});

test('마을: 가장자리 말풍선은 안쪽으로 밀어 잘리지 않는다',()=>{
 const {bubbleShift,bubbleWidth}=require('../out/webview-test/components/TeamVillage');
 assert.equal(bubbleShift(400,100,800),0);
 assert.equal(bubbleShift(30,100,800),24);assert.equal(bubbleShift(790,100,800),-44);
 assert.ok(bubbleWidth('공통 기반 먼저 — 대기',300)<150);assert.equal(bubbleWidth('아주 긴 말풍선 문구가 여기에 들어갑니다 아주 길게',80),80);
});

test('파일마다 맡은 개발 슬롯을 기억한다(완성 카드를 맡은 작업대에서 날린다)',()=>{
 const v=run([{step:'planned',total:7,files:FILES},{step:'file_start',agent:'agent-2',file:'src/routes/a.js'},{step:'file_start',agent:'agent-2',file:'src/routes/b.js'},
  {step:'file_done',agent:'agent-2',file:'src/routes/a.js'},{step:'fixing',agent:'review',file:'src/routes/a.js'}]);
 assert.equal(v.files.find(f=>f.file==='src/routes/a.js').by,'agent-2','검토의 다듬기가 맡은 사람을 바꾸지 않는다');
 assert.equal(v.files.find(f=>f.file==='src/routes/b.js').by,'agent-2');
});

test('마을: 카드를 집으러 선 자리는 작업대 앞 리코더와 겹치지 않는다',()=>{
 for(const W of [520,760,1200]){const L=villageLayout(W,6);const standTop=L.queue.y+L.queue.h-8,agentH=55,deskAgentTop=L.desks[0].y-58;
  assert.ok(standTop+agentH<=deskAgentTop+2,`${W}`);}
});

test('재시도·실패·멈춤은 진행 기록에 이유와 함께 남고, 실패한 파일은 완료로 세지 않는다',()=>{
 const {PausePanel}=require('../out/webview-test/components/pausePanel');
 const v=run([{step:'planned',total:7,files:FILES},{step:'file_start',agent:'agent-1',file:'src/db.js'},
  {step:'part_retry',agent:'agent-1',file:'src/db.js',kind:'truncation',lines:80,message:'db.js — 응답이 잘려 80줄씩으로 줄여 다시 씁니다'},
  {step:'file_retry',agent:'agent-1',file:'src/db.js',kind:'format',attempt:2,message:'src/db.js — 응답 형식이 맞지 않음 → 처음부터 80줄씩 다시 씁니다 (2/3)'},
  {step:'file_failed',agent:'agent-1',file:'src/db.js',kind:'format',message:'src/db.js — 3번 시도해도 응답 형식이 맞지 않음 · 나머지를 먼저 만듭니다'},
  {step:'paused',kind:'format',message:'멈춤 — src/db.js — 3번 시도해도 응답 형식이 맞지 않음'}]);
 const t=v.records.map(r=>`${r.who}|${r.text}|${r.tone||''}`);
 for(const w of ['개발 1|db.js — 응답이 잘려 80줄씩으로 줄여 다시 씁니다|warn','개발 1|src/db.js — 응답 형식이 맞지 않음 → 처음부터 80줄씩 다시 씁니다 (2/3)|warn',
   '|멈춤 — src/db.js — 3번 시도해도 응답 형식이 맞지 않음|warn'])assert.ok(t.includes(w),w+'\n'+t.join('\n'));
 assert.equal(v.files.find(f=>f.file==='src/db.js').state,'failed');assert.equal(v.agents['agent-1'].state,'idle');
 const html=renderToStaticMarkup(React.createElement(TeamVillage,{roster:buildRoster(2),view:v,width:900}));
 assert.match(html,/1개 못 만듦/);assert.doesNotMatch(html,/<b>1\/2<\/b>/,'실패한 파일은 선반 완료 수에 넣지 않는다');
 const panel=renderToStaticMarkup(React.createElement(PausePanel,{paused:{message:'m',done:50,total:51,failed:[{file:'client/src/styles/pages.css',kind:'format',reason:'응답 형식이 맞지 않음'}]},onResume(){}}));
 for(const w of ['만들지 못한 파일 1개 — 나머지 50/51개는 저장했습니다','client/src/styles/pages.css','3번 시도해도 응답 형식이 맞지 않음','>이 파일 다시 쓰기<','>이 파일 빼고 결과 받기<'])assert.ok(panel.includes(w),w);
 const plain=renderToStaticMarkup(React.createElement(PausePanel,{paused:{message:'AI 사용 한도',done:4,total:9},onResume(){}}));
 assert.match(plain,/AI 사용 한도/);assert.match(plain,/이어서 만들기 \(4\/9\)/);assert.doesNotMatch(plain,/빼고 결과/);
 const src=require('node:fs').readFileSync(require('node:path').join(__dirname,'../webview-src/components/CodeAgent.tsx'),'utf8');
 assert.match(src,/skipFailed: true/);assert.match(src,/<PausePanel standalone/);
});

test('나눠 쓰기 기록은 실제 조각 크기를 쓴다(다시 쓰기는 40줄)',()=>{
 assert.equal(recordOf({step:'file_split',agent:'agent-1',file:'src/a.js',lines:40,message:'src/a.js 이(가) 커서 40줄씩 나눠 씁니다'},[]).text,'a.js 가 커서 40줄씩 이어 씁니다');
 assert.equal(recordOf({step:'file_split',agent:'agent-1',file:'src/a.js',lines:150,message:'src/a.js 이어 쓰기를 이어서 합니다'},[]).text,'a.js 쓰던 조각에 이어서 씁니다');
});
