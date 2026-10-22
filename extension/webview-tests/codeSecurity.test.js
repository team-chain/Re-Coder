const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {SecurityScoreNote}=require('../out/webview-test/components/codeIssues');
const {OpsAlertCard}=require('../out/webview-test/components/OpsAlertsPanel');

test('2.0.28 생성 결과의 AI 코드 보안 점수: 점수·통과 규칙·확인할 곳과 고치는 법',()=>{
 const report={score:78,rules:[{rule:'SQL_INJECTION',label:'SQL 인젝션',applicable:true,passed:false,count:1,severity:'high'},{rule:'CORS_WILDCARD',label:'CORS',applicable:true,passed:true,count:0},{rule:'XSS_HTML',label:'XSS',applicable:false,passed:true,count:0}],
  findings:[{rule:'SQL_INJECTION',label:'SQL 인젝션',file:'backend/src/routes/products.js',line:12,message:'사용자 입력이 SQL 문자열에 그대로 들어갑니다',severity:'high',fix:'자리표시자를 쓰세요'}]};
 const html=renderToStaticMarkup(React.createElement(SecurityScoreNote,{report}));
 assert.match(html,/AI 코드 보안 점검/);assert.match(html,/78점/);assert.match(html,/규칙 2개 중 1개 통과/);
 assert.match(html,/products\.js:12/);assert.match(html,/고치는 법: 자리표시자를 쓰세요/);
 assert.equal(renderToStaticMarkup(React.createElement(SecurityScoreNote,{report:{score:null,rules:[],findings:[]}})),'');
 assert.equal(renderToStaticMarkup(React.createElement(SecurityScoreNote,{report:null})),'');
 const clean=renderToStaticMarkup(React.createElement(SecurityScoreNote,{report:{score:100,rules:[],findings:[]}}));
 assert.match(clean,/보이지 않습니다/);
});

test('2.0.28 새 취약점 알림: 줄바꿈 상세와 올리기 제안 여러 개',()=>{
 const alert={id:'v1',at:'2026-10-14T00:00:00Z',kind:'vuln',severity:'critical',title:'새 취약점 2건',detail:'배포 이후 새로 발표된 취약점입니다.\n· lodash 4.17.15 → 4.17.21 — 높음 CVE-2026-1111',region:'us-east-1',cluster:'c',service:'shop',status:'open',
  suggestions:[{action:'upgrade',value:{ecosystem:'npm',name:'lodash',from:'4.17.15',to:'4.17.21'},label:'lodash 4.17.21 로 올리기',detail:'d'},{action:'upgrade',value:{ecosystem:'npm',name:'qs',from:'6.0.0',to:'6.11.0'},label:'qs 6.11.0 로 올리기',detail:'d'}]};
 const html=renderToStaticMarkup(React.createElement(OpsAlertCard,{alert,onChoose(){}}));
 assert.match(html,/새 취약점 2건/);assert.match(html,/pre-line/);assert.match(html,/lodash 4\.17\.21 로 올리기<\/button>/);assert.match(html,/qs 6\.11\.0 로 올리기<\/button>/);
});
