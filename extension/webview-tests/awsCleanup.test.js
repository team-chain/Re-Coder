const test=require('node:test'),assert=require('node:assert/strict');
const path=require('node:path'),Module=require('node:module');
const resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const {validateConfig}=require('../out/sidebar/canvasHost');
Module._resolveFilename=resolve;
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {EcsProtection,protectionRequest,DEFAULT_PROTECTION}=require('../out/webview-test/components/EcsProtection');
const {CleanupGroup,DeleteApproval,selectedIds,AwsCleanupPanel}=require('../out/webview-test/components/AwsCleanupPanel');

const I=(id,extra={})=>({id,kind:id.split(':')[0],label:'라벨',name:id.split(':')[1],service:'recoder-shop',cluster:'recoder-cluster',tagged:true,deletable:true,detail:'d',cost:'c',note:'',...extra});

test('2.0.26 대규모 방패: 비용 확인 없이는 요청에 싣지 않고, 켜면 한도·창·확인을 함께 보낸다',()=>{
 assert.deepEqual(protectionRequest(DEFAULT_PROTECTION),{shield:true,shield_level:'normal',alerts:true,alert_email:''});
 assert.throws(()=>protectionRequest({...DEFAULT_PROTECTION,edge:'alb_waf'}),/비용 확인/);
 const on=protectionRequest({...DEFAULT_PROTECTION,edge:'alb_waf',waf_rate_limit:1000,edge_cost_approved:true});
 assert.equal(on.edge,'alb_waf');assert.equal(on.waf_rate_limit,1000);assert.equal(on.waf_window,300);assert.equal(on.edge_cost_approved,true);
 const html=renderToStaticMarkup(React.createElement(EcsProtection,{value:{...DEFAULT_PROTECTION,edge:'alb_waf'},onChange(){}}));
 assert.match(html,/대규모 방패/);assert.match(html,/월 25달러/);assert.match(html,/비용이 드는 것을 확인했습니다/);assert.match(html,/2,000회 \(기본\)/);
 const off=renderToStaticMarkup(React.createElement(EcsProtection,{value:DEFAULT_PROTECTION,onChange(){}}));
 assert.match(off,/대규모 방패/);assert.doesNotMatch(off,/비용이 드는 것을/);
});

test('2.0.26 캔버스: 대규모 방패는 비용 확인·한도·창을 검사한다',()=>{
 const base={target:'ecs',image_name:'shop',tag:'v1',aws_region:'us-east-1',ecs_cluster:'recoder-cluster',ecs_service:'shop',task_family:'recoder-task',container_port:3000,cpu:'256',memory:'512',environment:'staging',dir:''};
 assert.equal(validateConfig({...base,edge:'none'}).edge,undefined);
 assert.throws(()=>validateConfig({...base,edge:'alb_waf'}),/비용 확인/);
 assert.throws(()=>validateConfig({...base,edge:'cdn',edge_cost_approved:true}),/대규모 방패/);
 assert.throws(()=>validateConfig({...base,edge:'alb_waf',edge_cost_approved:true,waf_rate_limit:5}),/한도/);
 assert.throws(()=>validateConfig({...base,edge:'alb_waf',edge_cost_approved:true,waf_window:30}),/창/);
 const c=validateConfig({...base,edge:'alb_waf',edge_cost_approved:true,waf_rate_limit:500,waf_window:60});
 assert.equal(c.edge,'alb_waf');assert.equal(c.waf_rate_limit,500);assert.equal(c.waf_window,60);assert.equal(c.edge_cost_approved,true);
});

test('2.0.26 정리: 태그 확인된 항목만 고를 수 있고, 승인 카드에 지울 것을 다시 보여 준다',()=>{
 const plan={region:'us-east-1',groups:[{service:'recoder-shop',items:[I('ecs_service:recoder-shop'),I('alb:recoder-shop-1a2b'),I('ecr:recoder-shop')]}],untagged:[I('ecr:recoder-old',{tagged:false,deletable:false,note:'직접'})],
  summary:{running_tasks:1,load_balancers:1,web_acls:0,image_mb:12,tagged:3,untagged:1},gaps:[]};
 assert.deepEqual(selectedIds(plan,{'alb:recoder-shop-1a2b':true,'ecr:recoder-old':true,'ecr:recoder-shop':false}),['alb:recoder-shop-1a2b']);
 assert.deepEqual(selectedIds(null,{x:true}),[]);
 const g=renderToStaticMarkup(React.createElement(CleanupGroup,{group:plan.groups[0],picked:{},onPick(){},onStop(){},disabled:false}));
 assert.equal((g.match(/>중지<\/button>/g)||[]).length,1);assert.match(g,/recoder-shop-1a2b/);assert.doesNotMatch(g,/checked=""/);
 const a=renderToStaticMarkup(React.createElement(DeleteApproval,{items:[plan.groups[0].items[1]],busy:false,onApprove(){},onCancel(){}}));
 assert.match(a,/1개 완전 삭제/);assert.match(a,/되돌릴 수 없습니다/);assert.match(a,/삭제 승인/);
 const panel=renderToStaticMarkup(React.createElement(AwsCleanupPanel,{defaultRegion:'ap-northeast-2'}));
 assert.match(panel,/AWS 리소스 정리/);assert.match(panel,/목록 불러오기/);assert.match(panel,/selected="" value="ap-northeast-2"|value="ap-northeast-2" selected=""/);
 assert.doesNotMatch(panel,/완전 삭제…/);
});
