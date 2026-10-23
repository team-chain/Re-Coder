const test=require('node:test'),assert=require('node:assert/strict');
const path=require('node:path'),Module=require('node:module');
const resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const {validateConfig}=require('../out/sidebar/canvasHost');
Module._resolveFilename=resolve;
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {EcsProtection,protectionRequest,DEFAULT_PROTECTION}=require('../out/webview-test/components/EcsProtection');

test('2.0.27 HTTPS 주소: 로드밸런서를 함께 켜고 비용 확인·도메인 형식을 검사한다',()=>{
 assert.throws(()=>protectionRequest({...DEFAULT_PROTECTION,https:true}),/HTTPS 주소는/);
 const on=protectionRequest({...DEFAULT_PROTECTION,https:true,edge_cost_approved:true,custom_domain:' Shop.Example.com '});
 assert.equal(on.https,true);assert.equal(on.edge,'alb');assert.equal(on.custom_domain,'shop.example.com');assert.equal(on.edge_cost_approved,true);
 assert.throws(()=>protectionRequest({...DEFAULT_PROTECTION,https:true,edge_cost_approved:true,custom_domain:'bad_domain'}),/도메인/);
 const withWaf=protectionRequest({...DEFAULT_PROTECTION,edge:'alb_waf',https:true,edge_cost_approved:true});
 assert.equal(withWaf.edge,'alb_waf');
 const html=renderToStaticMarkup(React.createElement(EcsProtection,{value:{...DEFAULT_PROTECTION,https:true},onChange(){}}));
 assert.match(html,/HTTPS 주소/);assert.match(html,/CloudFront/);assert.match(html,/내 도메인/);assert.match(html,/로드밸런서만\(WAF 없이\)/);
});

test('2.0.27 관리형 DB: 비용 확인이 있어야 요청에 싣는다',()=>{
 assert.throws(()=>protectionRequest({...DEFAULT_PROTECTION,database:'rds'}),/관리형 DB/);
 const on=protectionRequest({...DEFAULT_PROTECTION,database:'rds',db_cost_approved:true});
 assert.equal(on.database,'rds');assert.equal(on.db_cost_approved,true);assert.equal(on.edge,undefined);
 assert.equal(protectionRequest(DEFAULT_PROTECTION).database,undefined);
 const html=renderToStaticMarkup(React.createElement(EcsProtection,{value:{...DEFAULT_PROTECTION,database:'rds'},onChange(){}}));
 assert.match(html,/관리형 DB/);assert.match(html,/RDS PostgreSQL/);assert.match(html,/월 15달러/);assert.match(html,/schema\.sql/);
});

test('2.0.27 캔버스: HTTPS·도메인·관리형 DB 를 검사한다',()=>{
 const base={target:'ecs',image_name:'shop',tag:'v1',aws_region:'us-east-1',ecs_cluster:'recoder-cluster',ecs_service:'shop',task_family:'recoder-task',container_port:3000,cpu:'256',memory:'512',environment:'staging',dir:''};
 assert.throws(()=>validateConfig({...base,https:true}),/비용 확인/);
 const c=validateConfig({...base,https:true,edge_cost_approved:true,custom_domain:'shop.example.com'});
 assert.equal(c.https,true);assert.equal(c.edge,'alb');assert.equal(c.custom_domain,'shop.example.com');
 assert.throws(()=>validateConfig({...base,custom_domain:'shop.example.com'}),/HTTPS/);
 assert.throws(()=>validateConfig({...base,https:true,edge_cost_approved:true,custom_domain:'x'}),/도메인/);
 assert.throws(()=>validateConfig({...base,database:'rds'}),/관리형 DB/);
 assert.throws(()=>validateConfig({...base,database:'mysql',db_cost_approved:true}),/DB 설정/);
 assert.equal(validateConfig({...base,database:'rds',db_cost_approved:true}).database,'rds');
});
