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

test('project change clears a proposal and ignores its late response',()=>{
 const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
 const states=[],effects=[],sent=[];
 let cursor=0,receive,tree;
 const hooks={...React,
  useState(initial){const i=cursor++;if(!(i in states))states[i]=initial;return [states[i],value=>{states[i]=value;}];},
  useRef(initial){const i=cursor++;return states[i]??(states[i]={current:initial});},
  useCallback:fn=>fn,
  useEffect(fn,deps){const i=cursor++,old=states[i];if(!old||deps.some((v,j)=>v!==old[j])){states[i]=deps;effects.push(fn);}},
 };
 const exports={};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../out/webview-test/components/GroundedRepairPanel.js'),'utf8'),{
  exports,URL,require:id=>id==='react'?hooks:{useVSCodeApi:()=>({postMessage:(type,payload)=>sent.push({type,payload}),useMessage:fn=>{receive=fn;}})},
 });
 const render=()=>{cursor=0;tree=exports.GroundedRepairPanel({log:'ETARGET',stage:'build'});effects.splice(0).forEach(fn=>fn());};
 const nodes=(node=tree)=>React.isValidElement(node)?[node,...React.Children.toArray(node.props.children).flatMap(n=>nodes(n))]:[];
 const detail=()=>nodes().find(n=>n.type===exports.RepairDetails);
 const send=()=>{nodes().find(n=>n.type==='button').props.onClick();render();return sent.at(-1).payload.requestId;};
 render();
 const first=send();receive({type:'repair.result',payload:{requestId:first,result:base}});render();
 assert.ok(detail());
 receive({type:'canvas.projectChanged',payload:{workspace:'/new'}});render();
 assert.equal(detail(),undefined);
 const pending=send();receive({type:'canvas.projectChanged',payload:{workspace:'/third'}});render();
 receive({type:'repair.result',payload:{requestId:pending,result:base}});render();
 assert.equal(detail(),undefined);
 assert.equal(nodes().find(n=>n.type==='button').props.disabled,false);
});
