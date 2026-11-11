const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {ComposeVerification}=require('../out/webview-test/components/CodeAgent');

test('3.1 다중 서비스 검증 — 묶음별 통과 수와 실패 목록',()=>{
 const v={kind:'compose',status:'failed',passed:false,output:'x',
  checks:[{name:'db:orders-db',ok:true},{name:'service:orders',ok:true},{name:'service:gateway',ok:false},{name:'link:gateway->orders',ok:false},
          {name:'schema:orders-db',ok:true},{name:'test:orders-GET-/api/orders-noauth',ok:false},{name:'test:orders-health',ok:true}],
  failures:[{kind:'service_down',service:'gateway',title:'gateway 계속 재시작'},{kind:'test',service:'orders',title:'GET orders/api/orders → 200 (기대 401|403)'}]};
 const html=renderToStaticMarkup(React.createElement(ComposeVerification,{v}));
 assert.match(html,/다중 서비스 검증 미통과 · DB 1\/1 · 서비스 1\/2 · 연결 0\/1 · 테이블 1\/1 · 통합 테스트 1\/2/);
 assert.match(html,/실패 2건 보기/);assert.match(html,/gateway 계속 재시작/);
 const ok=renderToStaticMarkup(React.createElement(ComposeVerification,{v:{kind:'compose',status:'passed',passed:true,checks:[{name:'service:a',ok:true}],failures:[]}}));
 assert.match(ok,/다중 서비스 검증 통과 · 서비스 1\/1/);assert.doesNotMatch(ok,/실패/);
 const na=renderToStaticMarkup(React.createElement(ComposeVerification,{v:{kind:'compose',status:'unavailable',passed:false,output:'Docker 가 없습니다'}}));
 assert.match(na,/다중 서비스 미검증 · Docker 가 없습니다/);
});
