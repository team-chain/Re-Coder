const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {AiPicker}=require('../out/webview-test/components/AiPicker');
const {initialAiSwitch,reduceAiSwitch}=require('../out/webview-test/hooks/useAiSwitch');
const providers=[{id:'anthropic',quality:'claude-sonnet-5-5',fast:'claude-haiku-5-5',connected:true},
 {id:'openai',quality:'gpt-6-sol',fast:'gpt-6-sol',connected:false}];
const props={current:'anthropic',providers,onPick(){},onConnect(){}};
const render=(extra={})=>renderToStaticMarkup(React.createElement(AiPicker,{...props,...extra}));

test('AI picker renders active, disconnected, pending and completed states',()=>{
 const html=render({defaultOpen:true});
 assert.match(html,/role="menu"/);assert.match(html,/aria-checked="true"[^>]+data-provider="anthropic"/);
 assert.match(html,/Sonnet 5.5/);assert.match(html,/Haiku 5.5/);assert.match(html,/키를 연결하면 바로 쓸 수 있어요/);
 assert.match(html,/키 연결/);assert.match(html,/모델 · 비용 설정/);
 assert.match(render({switching:'openai'}),/GPT로 바꾸는 중…/);
 assert.match(render({switching:'anthropic'}),/aria-busy="true"/);
 assert.match(render({justSwitched:true}),/rc-ai-btn done/);
 assert.match(render({disabled:true}),/disabled=""/);
});

test('picker click handlers route saved keys to switch, missing keys to connect, active row to neither',()=>{
 const exports={},picked=[],connected=[];
 const hooks={...React,useState:x=>[x,()=>{}],useRef:()=>({current:null}),useLayoutEffect(){},useEffect(){}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../out/webview-test/components/AiPicker.js'),'utf8'),{
  exports,require:id=>id==='react'?hooks:require(path.join(__dirname,'../out/webview-test/components',id))});
 const flatten=n=>React.isValidElement(n)?[n,...React.Children.toArray(n.props.children).flatMap(flatten)]:[];
 for(const connectedKey of [false,true]) {
  const tree=exports.AiPicker({...props,defaultOpen:true,providers:[providers[0],{...providers[1],connected:connectedKey}],
   onPick:id=>picked.push(id),onConnect:id=>connected.push(id)});
  flatten(tree).filter(n=>n.props.role==='menuitemradio').forEach(n=>n.props.onClick());
 }
 assert.deepEqual(picked,['openai']);assert.deepEqual(connected,['openai']);
});

test('switch state settles cancellation and rollback, and a timeout keeps host mutation locked',()=>{
 const start=()=>reduceAiSwitch(initialAiSwitch,{type:'begin',provider:'anthropic'});
 let s=reduceAiSwitch(start(),{type:'settled',provider:'openai'});
 assert.equal(s.pending,false);assert.equal(s.done,false);assert.equal(s.locked,false);
 s=reduceAiSwitch(start(),{type:'settled',provider:'anthropic'});assert.equal(s.done,true);
 s=reduceAiSwitch(s,{type:'clearDone'});assert.equal(s.done,false);
 s=reduceAiSwitch(start(),{type:'timeout'});assert.equal(s.pending,false);assert.equal(s.locked,true);assert.ok(s.notice);
 s=reduceAiSwitch(s,{type:'settled',provider:'anthropic'});assert.equal(s.locked,false);assert.equal(s.notice,'');
 s=reduceAiSwitch(start(),{type:'error',message:'복구 실패'});assert.equal(s.pending,false);assert.equal(s.notice,'복구 실패');
 s=reduceAiSwitch(s,{type:'activity',activity:{switching:false,target:null,generating:true}});
 assert.equal(s.locked,false);assert.equal(s.generating,true);
 const remote=reduceAiSwitch(initialAiSwitch,{type:'activity',activity:{switching:true,target:'openai',generating:false}});
 assert.equal(remote.target,'openai');assert.equal(remote.pending,true);assert.equal(remote.locked,true);
});
