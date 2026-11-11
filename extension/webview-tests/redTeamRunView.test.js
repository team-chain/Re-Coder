const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server');
const {RedTeamRunView}=require('../out/webview-test/components/RedTeamRunView');
const {assessPentagi}=require('../out/security/pentagiAssessment');
test('task animation follows real states, escapes attacker text, and supports SSR without document',()=>{
 const html=renderToStaticMarkup(React.createElement(RedTeamRunView,{active:true,phase:'running',tasks:[{id:'1',title:'<script>alert(1)</script>',status:'running',result:''},{id:'2',title:'Discovery',status:'finished',result:'done'}]}));
 assert.match(html,/작업 1\/2/);assert.match(html,/data-active="true"/);assert.match(html,/data-active="false"/);assert.ok(!html.includes('<script>'));assert.match(html,/prefers-reduced-motion/);assert.match(html,/max-width:700px/);
});
test('terminal failure is not green completed; old unstructured reports are never a clean bill of health',()=>{
 const html=renderToStaticMarkup(React.createElement(RedTeamRunView,{active:false,phase:'removed',outcome:'failed',cleaned:true,tasks:[],assessment:assessPentagi([{result:'No findings.'}])}));
 assert.ok(!html.includes('data-current="true"'));assert.ok(!html.includes('data-done="true"'));assert.match(html,/미검증 범위 5개/);assert.match(html,/완료되지 않은 결과/);assert.match(html,/정리 완료/);
});
