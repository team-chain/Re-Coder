const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {buildRoster,reduceTeam,emptyTeamView,recordOf,agentName}=require('../out/webview-test/components/teamState');
const {AgentVillage,cardOf,stagesOf,displayName}=require('../out/webview-test/components/AgentVillage');
const {TeamBoard,boardMetrics,boardStatus}=require('../out/webview-test/components/TeamBoard');
const V=(props)=>renderToStaticMarkup(React.createElement(AgentVillage,props));

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

test('전체 점검이 완성 파일을 고쳐도 완료 수·단계 완료 표시는 줄지 않고 "다듬는 중" 만 붙는다',()=>{
 const {polishingCount}=require('../out/webview-test/components/teamState');
 const built=[{step:'planned',total:7,files:FILES},...FILES.map((f,i)=>({step:'file_done',agent:'agent-1',file:f.file,done_count:i+1}))];
 let v=run([...built,{step:'consistency',message:'전체 점검'},{step:'fixing',agent:'review',file:'src/db.js',message:'전체 점검 — src/db.js 고치는 중'},
  {step:'fixing',agent:'review',file:'public/app.js',message:'전체 점검 — public/app.js 고치는 중'}]);
 assert.equal(v.files.filter(f=>f.state==='done').length,7,'완료로 센다');
 assert.equal(polishingCount(v),2);assert.equal(polishingCount(v,0),1);
 const html=V({roster:buildRoster(2),view:v,width:900});
 assert.match(html,/rc-av-stage done"><svg[^>]*aria-label="완료"[\s\S]*?공통 기반 2\/2/);assert.match(html,/rc-av-stage done"><svg[\s\S]*?화면 2\/2/);
 assert.deepEqual(stagesOf(v).map(x=>x.state),['done','done','done','now'],'다듬는 중에도 단계 ✓ 는 그대로');
 const board=renderToStaticMarkup(React.createElement(TeamBoard,{roster:buildRoster(2),view:v}));
 assert.match(board,/2개 다듬는 중/);assert.match(board,/파일 7\/7/);
 v=reduceTeam(v,{step:'done'});assert.equal(polishingCount(v),0,'끝나면 표시를 지운다');
 //: 아직 쓰는 중인 파일의 교정은 예전처럼 "고치는 중"
 const w=run([{step:'planned',total:7,files:FILES},{step:'file_start',agent:'agent-1',file:'package.json'},{step:'fixing',agent:'agent-1',file:'package.json'}]);
 assert.equal(w.files.find(f=>f.file==='package.json').state,'fixing');
});

test('파일마다 맡은 개발 슬롯을 기억한다(완성 카드를 맡은 작업대에서 날린다)',()=>{
 const v=run([{step:'planned',total:7,files:FILES},{step:'file_start',agent:'agent-2',file:'src/routes/a.js'},{step:'file_start',agent:'agent-2',file:'src/routes/b.js'},
  {step:'file_done',agent:'agent-2',file:'src/routes/a.js'},{step:'fixing',agent:'review',file:'src/routes/a.js'}]);
 assert.equal(v.files.find(f=>f.file==='src/routes/a.js').by,'agent-2','검토의 다듬기가 맡은 사람을 바꾸지 않는다');
 assert.equal(v.files.find(f=>f.file==='src/routes/b.js').by,'agent-2');
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
 const html=V({roster:buildRoster(2),view:v,width:900});
 assert.match(html,/공통 기반 0\/2 · 못 만듦 1/,'실패한 파일은 완료 수에 넣지 않는다');
 const panel=renderToStaticMarkup(React.createElement(PausePanel,{paused:{message:'m',done:50,total:51,failed:[{file:'client/src/styles/pages.css',kind:'format',reason:'응답 형식이 맞지 않음'}]},onResume(){}}));
 for(const w of ['만들지 못한 파일 1개 — 나머지 50/51개는 저장했습니다','client/src/styles/pages.css','3번 시도해도 응답 형식이 맞지 않음','>이 파일 다시 쓰기<','>이 파일 빼고 결과 받기<'])assert.ok(panel.includes(w),w);
 const plain=renderToStaticMarkup(React.createElement(PausePanel,{paused:{message:'AI 사용 한도',done:4,total:9},onResume(){}}));
 assert.match(plain,/AI 사용 한도/);assert.match(plain,/이어서 만들기 \(4\/9\)/);assert.doesNotMatch(plain,/빼고 결과/);
 const src=require('node:fs').readFileSync(require('node:path').join(__dirname,'../webview-src/components/CodeAgent.tsx'),'utf8');
 assert.match(src,/skipFailed: true/);assert.match(src,/<PausePanel standalone/);
});

test('이어 받기 기록 — 새로 받기·이어서·맥락 줄이기·자동 작성 문서',()=>{
 assert.equal(recordOf({step:'file_split',agent:'agent-1',file:'src/a.js',message:'src/a.js 을(를) 글자 그대로 이어 받습니다'},[]).text,'a.js 이(가) 길어 끊기는 만큼 이어 받습니다');
 assert.equal(recordOf({step:'file_split',agent:'agent-1',file:'src/a.js',message:'src/a.js 을(를) 글자 그대로 이어 받습니다 (맥락을 줄여서)'},[]).text,'a.js 이(가) 길어 끊기는 만큼 이어 받습니다 (맥락을 줄여서)');
 assert.equal(recordOf({step:'file_split',agent:'agent-1',file:'src/a.js',message:'src/a.js 쓰던 내용에 이어서 씁니다'},[]).text,'a.js 쓰던 내용에 이어서 씁니다');
 const r=recordOf({step:'file_fallback',agent:'agent-4',file:'README.md',message:'README.md — AI 가 끝내 못 써서 설계로 기본 문서를 자동 작성했습니다'},[]);
 assert.equal(r.tone,'warn');assert.match(r.text,/자동 작성/);
});

test('ReCoder 가 넣은 결제 모듈(고정 파일)은 설계 직후 완료로 표시하고 그 사실을 기록한다',()=>{
 const files=[{file:'backend/src/payments/stripe.ts',layer:0},...FILES];
 const v=run([{step:'planning'},{step:'planned',total:8,files},
  {step:'file_done',agent:'planner',file:'backend/src/payments/stripe.ts',lines:170,message:'backend/src/payments/stripe.ts — ReCoder 결제 모듈(검증된 고정 파일)을 넣었습니다'}]);
 assert.equal(v.files.find(f=>f.file==='backend/src/payments/stripe.ts').state,'done');
 assert.match(v.records.at(-1).text,/ReCoder 결제 모듈/);
 assert.equal(recordOf({step:'file_done',agent:'agent-1',file:'src/a.js',lines:3,message:'src/a.js 완료'},[]).text,'src/a.js 완료 (3줄)');
});

test('2.0.23 보안 검사가 막은 수상한 패키지는 실제 이벤트로만 기록·표시된다',()=>{
 let v=run([{step:'planning'},{step:'planned',total:7,files:FILES},{step:'consistency',message:'전체 점검'}]);
 assert.equal(v.securityBlocks,0);
 const review=(view)=>{const r=buildRoster(2);return cardOf(r,r.find(m=>m.role==='review'),view,false);};
 assert.doesNotMatch(review(v).meta[0],/차단/);
 v=reduceTeam(v,{step:'security_block',agent:'review',file:'package.json',message:'수상한 패키지 차단 — tiny-pay-sdk-kr'});
 assert.equal(v.securityBlocks,1);
 assert.ok(v.records.some(r=>r.who==='검토'&&r.text==='수상한 패키지 차단 — tiny-pay-sdk-kr'&&r.tone==='warn'));
 assert.match(review(v).meta[0],/차단 1/);
});

test('2.0.24 공용 게시판: 서버 경로가 붙고, 화면 리코더는 게시판을 보고 시작한다',()=>{
 const roster=buildRoster(2);
 let v=run([{step:'planned',total:7,files:FILES},{step:'wave',layer:-1,agents:2},
  {step:'file_start',agent:'agent-1',file:'src/routes/a.js'},
  {step:'board_post',agent:'agent-1',file:'src/routes/a.js',items:[{method:'GET',path:'/api/a',shape:'{ items, total }',auth:false},{method:'POST',path:'/api/a',shape:null,auth:true}],message:'게시판에 API 2개 공유'},
  {step:'file_done',agent:'agent-1',file:'src/routes/a.js',done_count:1},
  {step:'board_read',agent:'agent-2',file:'public/app.js'},{step:'file_start',agent:'agent-2',file:'public/app.js'}]);
 assert.equal(v.board.length,2);assert.equal(v.lastPost.agent,'agent-1');assert.equal(v.boardReaders['agent-2'],'public/app.js');
 const t=v.records.map(r=>`${r.who}|${r.text}`);
 assert.ok(t.includes('개발 1|게시판에 API 2개 공유 — a.js'));assert.ok(t.includes('개발 2|게시판 확인 후 시작 — app.js'));
 const html=V({roster,view:v,width:900});
 assert.match(html,/data-testid="village-board"/);assert.match(html,/<em>GET<\/em>\/api\/a/);assert.match(html,/<em>POST<\/em>\/api\/a/);
 // 같은 경로를 다시 붙이면 새 것으로 바뀐다(중복 메모 없음)
 v=reduceTeam(v,{step:'board_post',agent:'agent-1',file:'src/routes/a.js',items:[{method:'GET',path:'/api/a',shape:'{ items }'}]});
 assert.equal(v.board.filter(b=>b.path==='/api/a'&&b.method==='GET').length,1);assert.equal(v.board.find(b=>b.method==='GET').shape,'{ items }');
 const empty=V({roster,view:run([{step:'planned',total:7,files:FILES}]),width:900});
 assert.doesNotMatch(empty,/village-board/,'경로가 없으면 게시판을 그리지 않는다');
});

test('2.0.24 의존 관문: 검토 카드에 통과·수정 수, 되돌린 파일은 작성자 카드가 고치는 중',()=>{
 const roster=buildRoster(2);
 let v=run([{step:'planned',total:7,files:FILES},{step:'wave',layer:-1,agents:2},
  {step:'file_start',agent:'agent-2',file:'src/routes/b.js'},
  {step:'gate_check',agent:'review',by:'agent-2',file:'src/routes/b.js',ok:true,fixed:['a: x 내보내기','b: TYPE'],returned:false,message:'관문 통과 — 자동 수정 2건'}]);
 assert.deepEqual(v.gates,{passed:1,fixed:2,returned:0});
 const rv=(view)=>cardOf(roster,roster.find(m=>m.role==='review'),view,false);
 assert.match(rv(v).meta[0],/통과 1 · 수정 2/);assert.equal(rv(v).state,'관문 확인');
 v=reduceTeam(v,{step:'gate_check',agent:'review',by:'agent-2',file:'src/routes/b.js',ok:false,fixed:[],returned:true,message:'관문 — 되돌림'});
 assert.equal(v.agents['agent-2'].state,'fixing');assert.equal(v.files.find(f=>f.file==='src/routes/b.js').state,'fixing');
 const dev2=cardOf(roster,roster.find(m=>m.id==='agent-2'),v,false);
 assert.equal(dev2.state,'고치는 중');assert.equal(dev2.file,'src/routes/b.js');assert.equal(v.gates.returned,1);
 assert.ok(v.records.some(r=>r.who==='검토'&&r.tone==='warn'&&r.text.includes('작성자에게 한 번 돌려보냄')));
 // 마지막 전체 점검이 시작되면 관문 자리 대신 모두 모인다
 v=reduceTeam(v,{step:'consistency',message:'전체 점검'});
 assert.equal(rv(v).state,'전체 점검');assert.equal(boardStatus(v,false),'전체 점검 중');
});


// ── 2.0.45 AI Village ─────────────────────────────────────────────────────

test('2.0.45 카드: 리코더마다 상태·파일·일한 시간·AI 호출·비용 — 값은 실제 이벤트에서만',()=>{
 const roster=buildRoster(2,Math.random,[],true);
 assert.deepEqual(roster.map(m=>m.role),['planner','dev','dev','review','test']);
 let v=run([{step:'planned',total:7,files:FILES,elapsed:3},{step:'wave',layer:0,agents:1,elapsed:3},
  {step:'file_start',agent:'agent-1',file:'package.json',elapsed:4},
  {step:'usage',agent:'agent-1',model:'us.anthropic.claude-sonnet-4-5-20250929-v1:0',input_tokens:1000,output_tokens:500,cost_usd:0.0105},
  {step:'usage',agent:'agent-1',model:'us.anthropic.claude-sonnet-4-5-20250929-v1:0',input_tokens:10,output_tokens:5},
  {step:'file_done',agent:'agent-1',file:'package.json',lines:20,done_count:1,elapsed:34,preview:'{\n  "name": "x"\n}'},
  {step:'file_start',agent:'agent-1',file:'src/db.js',elapsed:35}]);
 const dev=cardOf(roster,roster[1],v,false);
 assert.equal(dev.name,'개발 1');assert.equal(dev.state,'작성 중');assert.equal(dev.file,'src/db.js');assert.equal(dev.role,'공통 기반');
 assert.match(dev.meta[1],/0:31 · 2회 · \$0\.01/,'일한 시간(첫 움직임 → 마지막) · 호출 · 단가를 아는 호출만 더한 비용');
 assert.equal(v.usage['agent-1'].unknownCost,1,'단가를 모르는 호출은 따로 센다');
 const idle=cardOf(roster,roster[2],v,false);assert.equal(idle.state,'대기');assert.match(idle.meta[1],/—/);
 assert.equal(cardOf(roster,roster[4],v,false).state,'대기','테스트는 전체 점검 뒤 시작');
 assert.equal(displayName(roster,roster[2]),'개발 2');
 //: 화면 — 카드 5장, 오른쪽 자세히(코드 탭: 완성 파일 앞부분), 모델 이름은 사람이 읽는 이름
 const html=V({roster,view:v,width:1000});
 assert.equal((html.match(/data-agent="/g)||[]).length,5);
 assert.match(html,/class="rc-av split"/);assert.match(html,/data-testid="village-inspector"/);
 assert.match(html,/모델 · Claude Sonnet 4\.5/);
 assert.match(html,/src\/db\.js 작성 중 — 파일이 완성되면 내용이 보입니다/,'작성 중인 파일은 지어내지 않는다');
 assert.ok(!html.includes('NaN'));
 const narrow=V({roster,view:v,width:0});assert.match(narrow,/class="rc-av stack"/,'폭을 모르면(서버 렌더) 위아래로 쌓는다');
});

test('2.0.45 테스트 리코더: 시험 작성 → 실행 → 결과, 실패면 빨간 상태와 조건별 판정',()=>{
 const roster=buildRoster(1,Math.random,[],true);
 const base=[{step:'planned',total:7,files:FILES},...FILES.map((f,i)=>({step:'file_done',agent:'agent-1',file:f.file,done_count:i+1})),{step:'consistency',message:'점검'}];
 let v=run([...base,{step:'acceptance',agent:'test',phase:'writing',message:'완성 기준 시험 작성'}]);
 const t=()=>cardOf(roster,roster.find(m=>m.role==='test'),v,false);
 assert.equal(t().state,'시험 작성 중');assert.equal(boardStatus(v,false),'테스트 중');
 v=reduceTeam(v,{step:'acceptance',agent:'test',phase:'written',checks:5,unverifiable:1});
 assert.equal(t().meta[0],'시험 5개');
 v=reduceTeam(v,{step:'acceptance',agent:'test',phase:'result',round:1,passed:3,failed:1,unverifiable:1,total:5,
  criteria:[{id:'AC-1',verdict:'passed'},{id:'AC-2',verdict:'failed'},{id:'AC-3',verdict:'unverifiable'}]});
 assert.equal(t().state,'실패 1');assert.equal(t().tone,'x');assert.equal(t().meta[0],'3/5 통과 · 확인 불가 1');
 assert.deepEqual(stagesOf(v).slice(-2),[{label:'전체 점검',state:'done'},{label:'테스트 3/5',state:'now'}]);
 v=reduceTeam(v,{step:'acceptance',agent:'test',phase:'result',round:2,passed:4,failed:0,unverifiable:1,total:5,criteria:[{id:'AC-2',verdict:'passed'}]});
 assert.equal(t().state,'통과');assert.equal(stagesOf(v).at(-1).state,'done');
});

test('2.0.45 다시 보기: 기록한 이벤트를 그 시점까지 다시 접는다 · 이어서 만들기는 시계를 이어 붙인다',()=>{
 const {viewAt,totalUsage}=require('../out/webview-test/components/teamState');
 let v=run([{step:'planned',total:7,files:FILES,elapsed:2},{step:'file_start',agent:'agent-1',file:'package.json',elapsed:3},
  {step:'file_done',agent:'agent-1',file:'package.json',done_count:1,elapsed:20},{step:'file_start',agent:'agent-1',file:'src/db.js',elapsed:21},
  {step:'usage',agent:'agent-1',input_tokens:5,output_tokens:5,cost_usd:0.5}]);
 assert.equal(v.events.length,5,'사용량도 다시 보기에 남는다');assert.ok(!v.events.some(e=>'preview' in e));
 const at10=viewAt(v,10);
 assert.equal(at10.done,0);assert.equal(at10.agents['agent-1'].file,'package.json');assert.equal(at10.elapsed,10);
 assert.equal(totalUsage(at10).costUsd,0,'그 시점까지의 사용량');
 assert.equal(viewAt(v,25).agents['agent-1'].file,'src/db.js');assert.equal(totalUsage(viewAt(v,25)).costUsd,0.5);
 //: 멈춘 뒤 이어서 만들면 새 요청의 시계는 0 부터 — 앞의 시간에 이어 붙인다(뒤로 가지 않는다).
 v={...v,clockBase:v.elapsed};
 v=reduceTeam(v,{step:'resumed',elapsed:1});
 assert.equal(v.elapsed,22);assert.equal(v.events.at(-1).elapsed,22);
 const html=V({roster:buildRoster(1),view:v,width:900});
 assert.match(html,/aria-label="다시 볼 시점"/);assert.match(html,/0:22 \/ 0:22/);
});

test('2.0.45 팀 머리: 상태·파일·고친 문제·AI 호출·비용·시간, 끝난 뒤에는 접힌 "다시 보기"',()=>{
 let v=run([{step:'planned',total:7,files:FILES,elapsed:1},{step:'file_done',agent:'agent-1',file:'package.json',done_count:1,elapsed:5},
  {step:'fixing',agent:'agent-1',file:'src/db.js',message:'고치는 중'},
  {step:'usage',agent:'agent-1',cost_usd:0.25},{step:'usage',agent:'planner'}]);
 assert.deepEqual(boardMetrics(v),['파일 1/7','자동으로 고친 문제 1','AI 2회','$0.25+','0:05']);
 const live=renderToStaticMarkup(React.createElement(TeamBoard,{roster:buildRoster(2),view:v}));
 assert.match(live,/data-testid="agent-village"/);assert.match(live,/만드는 중/);
 const done=renderToStaticMarkup(React.createElement(TeamBoard,{roster:buildRoster(2),view:{...v,phase:'done'},collapsible:true,files:{'package.json':'{}'}}));
 assert.match(done,/팀 작업 다시 보기/);assert.match(done,/aria-expanded="false"/);assert.doesNotMatch(done,/agent-village/,'접혀 있으면 마을을 그리지 않는다');
});

test('2.0.45 코드 탭: 결과가 오면 완성 파일 전체, 그 전에는 코어가 비밀값을 가린 앞부분만',()=>{
 const roster=buildRoster(1);
 const v=run([{step:'planned',total:7,files:FILES},{step:'file_start',agent:'agent-1',file:'src/db.js'},
  {step:'file_done',agent:'agent-1',file:'src/db.js',done_count:1,preview:'const url = process.env.DB_URL;'}]);
 const pre=V({roster,view:v,width:900});
 assert.match(pre,/data-testid="village-code"[^>]*>const url = process\.env\.DB_URL;/);assert.match(pre,/앞 40줄 · 비밀값 모양은 가림/);
 const full=V({roster,view:v,width:900,files:{'src/db.js':'line1\nline2'}});
 assert.match(full,/line1\nline2/);assert.doesNotMatch(full,/앞 40줄/);
});

test('2.0.45 테스트 결과의 total(완성 기준 수)은 파일 수를 덮어쓰지 않는다 · 완료 수는 완성 파일보다 작지 않다',()=>{
 let v=run([{step:'planned',total:7,files:FILES},{step:'file_done',agent:'agent-1',file:'package.json'},{step:'file_done',agent:'agent-1',file:'src/db.js'}]);
 assert.equal(v.done,2,'done_count 가 없어도 완성 파일 수');
 v=reduceTeam(v,{step:'acceptance',agent:'test',phase:'result',passed:3,failed:0,unverifiable:1,total:4,criteria:[]});
 assert.equal(v.total,7);assert.equal(v.test.total,4);
 assert.deepEqual(boardMetrics({...v,elapsed:0}).slice(0,1),['파일 2/7']);
});
