const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {AcceptancePanel,acceptanceBadge,acceptanceStory}=require('../out/webview-test/components/AcceptancePanel');

const criteria=[{id:'AC-1',text:'제목 없이 쓰면 400',kind:'api'},{id:'AC-2',text:'좋아요 두 번이면 2',kind:'api'},{id:'AC-3',text:'목록 맨 위에 보인다',kind:'screen',check:'작성 후 첫 줄'}];
const acceptance={status:'passed',passed:2,failed:0,unverifiable:1,total:3,checks:3,rounds:2,first:{passed:1,failed:1},criteria:[
 {id:'AC-1',text:'제목 없이 쓰면 400',verdict:'passed',ms:42,checks:1,retries:0,history:['passed','passed'],evidence:[{method:'POST',path:'/api/posts',status:400,response:'{"error":"title"}'}]},
 {id:'AC-2',text:'좋아요 두 번이면 2',verdict:'passed',ms:1530,checks:1,retries:1,history:['failed','passed']},
 {id:'AC-3',text:'목록 맨 위에 보인다',verdict:'unverifiable',reason:'화면 조건',retries:0,history:['unverifiable','unverifiable']}]};

test('배지·요약: 통과 수, 고친 과정, 확인 불가, 시험 안 함',()=>{
 assert.deepEqual(acceptanceBadge(acceptance,3),{text:'2/3 통과 · 확인 불가 1',tone:'mid'});
 assert.deepEqual(acceptanceBadge({...acceptance,unverifiable:0,passed:3},3),{text:'3/3 통과',tone:'ok'});
 assert.deepEqual(acceptanceBadge({...acceptance,failed:1,passed:1},3),{text:'1/3 통과 · 실패 1',tone:'bad'});
 assert.deepEqual(acceptanceBadge({status:'skipped',note:'테스트를 끈 채 만들었습니다.'},3),{text:'자동 시험 안 함 · 조건 3개',tone:'mid'});
 assert.deepEqual(acceptanceBadge(null,2).tone,'mid');
 assert.match(acceptanceStory(acceptance),/처음 1\/3 통과 → 실패를 고친 뒤 2\/3/);
 assert.equal(acceptanceStory({status:'skipped',note:'기존 프로젝트에 더한 코드는 아직 자동으로 시험하지 않습니다.'}),'기존 프로젝트에 더한 코드는 아직 자동으로 시험하지 않습니다.');
});

test('조건마다 판정 아이콘·시간·재시도, 실패는 펼쳐서 요청·응답 근거',()=>{
 const html=renderToStaticMarkup(React.createElement(AcceptancePanel,{spec:{criteria,file:'docs/spec/SPEC-1.md',acceptance}}));
 assert.match(html,/완성 기준 테스트/);assert.match(html,/data-testid="acceptance-badge">2\/3 통과 · 확인 불가 1</);
 assert.match(html,/data-criterion="AC-1" data-verdict="passed"/);assert.match(html,/42ms/);assert.match(html,/1\.5초/);
 assert.match(html,/고친 뒤 · 재시도 1/);assert.match(html,/data-verdict="unverifiable"/);assert.match(html,/기록: docs\/spec\/SPEC-1\.md/);
 const failed={...acceptance,status:'failed',passed:1,failed:1,criteria:[acceptance.criteria[0],{id:'AC-2',verdict:'failed',history:['failed','failed'],retries:1,
  failure:{step:2,message:'likes 1 (기대 2)',request:'GET /api/posts/7',file:'server/src/routes/likes.js'},
  evidence:[{method:'POST',path:'/api/posts/7/like',status:200,response:'{"ok":true}'},{method:'GET',path:'/api/posts/7',status:200,response:'{"likes":1}',problem:'likes 1 (기대 2)'}]}]};
 const bad=renderToStaticMarkup(React.createElement(AcceptancePanel,{spec:{criteria,acceptance:failed}}));
 assert.match(bad,/data-criterion="AC-2" data-verdict="failed"><button type="button" aria-expanded="true"/,'실패는 처음부터 펼친다');
 assert.match(bad,/2단계 · GET \/api\/posts\/7 — likes 1 \(기대 2\)/);assert.match(bad,/원인 후보: server\/src\/routes\/likes\.js/);
 assert.match(bad,/data-testid="acceptance-evidence"/);assert.match(bad,/1차 실패/);assert.match(bad,/2차 실패/);
});

test('시험을 못 돌렸으면 조건 목록과 이유만 — 통과로 보이지 않는다',()=>{
 const html=renderToStaticMarkup(React.createElement(AcceptancePanel,{spec:{criteria,acceptance:{status:'skipped',note:'테스트를 끈 채 만들었습니다.',total:3,passed:0,failed:0,unverifiable:0,criteria:[]}}}));
 assert.match(html,/자동 시험 안 함/);assert.match(html,/테스트를 끈 채 만들었습니다\./);
 assert.doesNotMatch(html,/data-verdict="passed"/);assert.equal((html.match(/data-verdict="unverifiable"/g)||[]).length,3);
 const old=renderToStaticMarkup(React.createElement(AcceptancePanel,{spec:{criteria}}));
 assert.match(old,/이 결과는 테스트하지 않았습니다/,'예전 코어(결과 없음)');
});
