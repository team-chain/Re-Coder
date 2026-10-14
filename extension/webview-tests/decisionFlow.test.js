const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {collapseChanged,insertFollowups,isConfirmOnly,pendingFollowup}=require('../out/webview-test/components/decisionFlow');

const opt=(key,rec=false)=>({key,label:key,summary:'',pros:[],cons:[],recommended:rec});
const starter={id:'commerce-foundation',question:'시작 방식',impact:'',options:[opt('reviewed',true),opt('custom')]};
const payment={id:'commerce-payment',question:'결제 시작',impact:'',options:[opt('mock',true),opt('keys')]};
const base=()=>({decisions:[starter],selections:{'commerce-foundation':'reviewed'},step:0,followups:{'commerce-foundation':{reviewed:[payment],custom:'ai'}},expanded:{}});

test('기반을 고르면 결제 시작 결정을 이어서 끼워 넣고 다음으로 넘어간다',()=>{
 const s=base();
 const p=pendingFollowup(s);
 assert.deepEqual(p,[payment]);
 const n=insertFollowups(s,'commerce-foundation',p);
 assert.deepEqual(n.decisions.map(d=>d.id),['commerce-foundation','commerce-payment']);
 assert.equal(n.step,1);assert.equal(n.selections['commerce-payment'],'mock');
 assert.equal(pendingFollowup({...n,step:0}),null,'같은 선택으로는 다시 끼우지 않는다');
});

test('AI 자유 생성을 고르면 AI 에게 받을 차례가 되고, 받은 결정을 끼우되 확인 카드·선택지 1개짜리는 버린다',()=>{
 const s={...base(),selections:{'commerce-foundation':'custom'}};
 assert.equal(pendingFollowup(s),'ai');
 const ai=[{id:'storage',question:'DB',impact:'',options:[opt('pg',true),opt('mongo')]},{id:'auth',question:'로그인',impact:'',options:[opt('email')]},{id:'__confirm__',question:'?',impact:'',options:[opt('proceed'),opt('cancel')]},{id:'commerce-foundation',question:'겹침',impact:'',options:[opt('a'),opt('b')]}];
 const n=insertFollowups(s,'commerce-foundation',ai);
 assert.deepEqual(n.decisions.map(d=>d.id),['commerce-foundation','storage','commerce-foundation-2']);
 assert.equal(n.step,1);
});

test('앞으로 돌아가 선택을 바꾸면 예전 선택으로 끼운 결정을 뺀다',()=>{
 let n=insertFollowups(base(),'commerce-foundation',[payment]);
 n={...n,step:0,selections:{...n.selections,'commerce-foundation':'custom'}};
 const c=collapseChanged(n);
 assert.deepEqual(c.decisions.map(d=>d.id),['commerce-foundation']);
 assert.equal(c.selections['commerce-payment'],undefined);
 assert.equal(pendingFollowup(c),'ai');
});

test('고를 갈림길이 없는 요청은 확인 창으로 보인다',()=>{
 assert.equal(isConfirmOnly([{id:'__confirm__',question:'q',impact:'',options:[opt('proceed',true),opt('cancel')]}]),true);
 assert.equal(isConfirmOnly([starter]),false);
 const src=fs.readFileSync(path.join(__dirname,'../webview-src/components/CodeAgent.tsx'),'utf8');
 for(const s of ['data-testid="decision-confirm"','고를 설계 갈림길이 없는 요청이라 확인만 받습니다','진행 →','code.planFollowup','code.followupResult','AI 가 이어서 물을 설계 결정을 만드는 중'])assert.ok(src.includes(s),s);
 const host=fs.readFileSync(path.join(__dirname,'../src/sidebar/SidebarProvider.ts'),'utf8');
 assert.match(host,/case 'code\.planFollowup':/);assert.match(host,/followups: plan\.followups/);
 const api=fs.readFileSync(path.join(__dirname,'../src/core/ApiClient.ts'),'utf8');
 assert.match(api,/after_starter: opts\?\.afterStarter/);
});
