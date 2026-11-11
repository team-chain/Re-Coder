const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {ContractPanel,contractHeadline}=require('../out/webview-test/components/ContractPanel');

const result={available:true,summary:{error:1,warning:1,info:1},
 services:[{name:'gateway',kind:'api',language:'node',databases:[],depends_on:['orders']},{name:'orders',kind:'api',language:'python',databases:['orders-db','cache'],depends_on:[]}],
 databases:[{name:'orders-db',engine:'postgres'},{name:'cache',engine:'redis'}],
 edges:[{from:'gateway',to:'orders',kind:'service',status:'ok',file:'services/gateway/src/server.js',line:3},
        {from:'gateway',to:'orders-db',kind:'db',status:'violation',file:'services/gateway/src/db.js',line:1},
        {from:'orders',to:'orders-db',kind:'db',status:'ok',file:'services/orders/app/main.py',line:4},
        {from:'orders',to:'cache',kind:'db',status:'unused'}],
 findings:[{code:'CONTRACT_DB_FOREIGN',severity:'error',service:'gateway',file:'services/gateway/src/db.js',line:1,message:'gateway 가 계약상 orders 의 DB orders-db 에 직접 접속합니다',fix:'orders 의 API 로 받으세요'},
           {code:'CONTRACT_DB_UNUSED',severity:'warning',service:'orders',file:'services/orders',line:0,message:'계약상 orders 가 쓰는 DB cache 를 코드가 읽지 않습니다',fix:'CACHE_URL 로 접속'},
           {code:'CONTRACT_ENDPOINT_EXTRA',severity:'info',service:'gateway',file:'services/gateway/src/server.js',line:9,message:'계약에 없는 API GET /api/debug',fix:'계약에 더하세요'}]};

test('3.1 계약 대조 패널 — 연결 상태 색·근거 파일·요약',()=>{
 const html=renderToStaticMarkup(React.createElement(ContractPanel,{result}));
 assert.match(html,/계약 대조 — 설계도와 실제 코드/);
 assert.match(html,/data-testid="contract-headline"[^>]*>계약 위반 1건 — 고치기 전에는 다중 서비스 배포가 막힙니다/);
 assert.match(html,/data-edge="gateway-&gt;orders-db" data-status="violation"/);
 assert.match(html,/data-edge="orders-&gt;cache" data-status="unused"/);
 assert.match(html,/services\/gateway\/src\/db\.js:1/);
 assert.doesNotMatch(html,/services\/orders:0/);
 assert.equal(contractHeadline({...result,summary:{error:0,warning:2,info:0}}),'계약 위반 없음 · 확인할 것 2건');
 assert.equal(contractHeadline({...result,summary:{error:0,warning:0,info:3}}),'설계도와 코드가 일치합니다');
 assert.equal(renderToStaticMarkup(React.createElement(ContractPanel,{result:{available:false}})),'');
});
