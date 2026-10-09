const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {DbSchemaChoice,AppCheckWarning,DemoSeedWarning}=require('../out/webview-test/components/DbChoiceCard');
const noop=()=>{};
const diag={code:'DB_SCHEMA_MISMATCH',title:'이 DB에는 다른 앱의 테이블이 있습니다',cause:'shop-postgres 에는 예전 앱의 데이터가 남아 있습니다.',lines:['없는 테이블: carts','products 에 없는 열: stock']};

test('DB 구조가 다르면 조용히 쓰지 않고 [새 DB로 시작] / [그대로 사용] 을 묻는다',()=>{
 const html=renderToStaticMarkup(React.createElement(DbSchemaChoice,{diagnosis:diag,onChoose:noop}));
 assert.match(html,/이 DB에는 다른 앱의 테이블이 있습니다/);assert.match(html,/products 에 없는 열: stock/);
 assert.match(html,/새 DB로 시작 \(기존 DB는 지우지 않고 남겨 둠\)/);assert.match(html,/그대로 사용/);
 const busy=renderToStaticMarkup(React.createElement(DbSchemaChoice,{diagnosis:diag,busy:true,error:'실패',onChoose:noop}));
 assert.equal((busy.match(/disabled=""/g)||[]).length,2);assert.match(busy,/실패/);
});

test('배포 후 조회 API 가 5xx 면 "배포는 됐지만 앱이 오류를 냄" — DB 구조 문제면 새 DB 버튼',()=>{
 const html=renderToStaticMarkup(React.createElement(AppCheckWarning,{check:{path:'/api/products',http_status:500,diagnosis:{...diag,fix:'[새 DB로 시작]'}},onNewDb:noop}));
 assert.match(html,/배포는 됐지만 앱이 오류를 냄/);assert.match(html,/새 DB로 시작/);
 const other=renderToStaticMarkup(React.createElement(AppCheckWarning,{check:{path:'/api/products',http_status:500,diagnosis:{code:'APP_RUNTIME_ERROR',title:'앱 오류',cause:'TypeError'}},onNewDb:noop}));
 assert.doesNotMatch(other,/새 DB로 시작/);
 const bare=renderToStaticMarkup(React.createElement(AppCheckWarning,{check:{path:'/api/products',http_status:502}}));
 assert.match(bare,/\/api\/products 가 502 를 냈습니다/);
});

test('데모 상품 넣기 실패는 결과에 남는다, 성공이면 아무것도 안 그린다',()=>{
 assert.equal(renderToStaticMarkup(React.createElement(DemoSeedWarning,{seed:{ok:true}})),'');
 const html=renderToStaticMarkup(React.createElement(DemoSeedWarning,{seed:{ok:false,script:'backend/init-db.js',message:'a\nError: relation "products" does not exist'}}));
 assert.match(html,/데모 상품을 넣지 못했습니다/);assert.match(html,/backend\/init-db\.js/);assert.match(html,/does not exist/);
});

test('배포 화면이 DB 선택·앱 확인·데모 상품 결과를 실제로 붙이고, 호스트가 db-choice 를 코어로 넘긴다',()=>{
 const ship=fs.readFileSync(path.join(__dirname,'../webview-src/components/ShipMode.tsx'),'utf8');
 for(const s of ['DbSchemaChoice','AppCheckWarning','DemoSeedWarning','"db_schema"','deploy.db.choice','deploy.db.result'])assert.ok(ship.includes(s),s);
 const host=fs.readFileSync(path.join(__dirname,'../src/sidebar/SidebarProvider.ts'),'utf8');
 assert.match(host,/case 'deploy\.db\.choice'/);
 const api=fs.readFileSync(path.join(__dirname,'../src/core/ApiClient.ts'),'utf8');
 assert.match(api,/\/api\/deploy\/db-choice/);
});
