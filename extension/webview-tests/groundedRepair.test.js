const test=require('node:test');
const assert=require('node:assert/strict');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');
const {RepairDetails,canApproveRepair,documentLink}=require('../out/webview-test/components/GroundedRepairPanel.js');
const base={id:'r',status:'ready_for_approval',approval_required:true,workspace:'/p',retrieval_mode:'hybrid',llm_calls:1,estimated_cost_usd:0.001,cache_hit:false,sources:[{id:'doc',title:'Docker COPY',url:'https://docs.docker.com/reference/dockerfile/',kind:'excerpt'}],diffs:{Dockerfile:'-bad\n+fixed'},verification:{passed:true}};
test('only verified proposals show the approval action',()=>{
 assert.equal(canApproveRepair(base),true);
 for(const status of ['unresolved','environment_verification_required','verification_unavailable','applied']){
  const result={...base,status};assert.equal(canApproveRepair(result),false);
  assert.ok(!renderToStaticMarkup(React.createElement(RepairDetails,{result,onApprove(){}})).includes('검증된 수정 승인'));
 }
 assert.equal(canApproveRepair({...base,verification:{passed:false}}),false);
});
test('review contains diff, official link and measured cost',()=>{
 const html=renderToStaticMarkup(React.createElement(RepairDetails,{result:base,onApprove(){}}));
 for(const text of ['Dockerfile','+fixed','docs.docker.com','0.00100','검증된 수정 승인'])assert.ok(html.includes(text));
 const unknown=renderToStaticMarkup(React.createElement(RepairDetails,{result:{...base,estimated_cost_usd:null},onApprove(){}}));
 assert.ok(unknown.includes('비용 미확인'));
 assert.equal(documentLink('javascript:alert(1)'),undefined);
 assert.equal(documentLink('https://docs.docker.com.evil.test/'),undefined);
});
