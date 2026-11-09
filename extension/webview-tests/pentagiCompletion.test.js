const test=require('node:test'),assert=require('node:assert/strict');
const {PentagiClient,pentagiOutcome}=require('../out/security/pentagiClient');
const state=(flow,tasks)=>({flow:{id:'1',status:flow},tasks:tasks.map(([status,result])=>({id:'1',title:'assessment',status,result}))});
test('upstream waiting after successful tasks is completion, not a question or a clean bill of health',()=>{
 assert.equal(pentagiOutcome(state('waiting',[['finished','SQL injection evidence and coverage gaps']])),'finished');
 assert.equal(pentagiOutcome(state('waiting',[['waiting','Scope question']])),'needs_input');
 assert.equal(pentagiOutcome(state('waiting',[['failed','Provider rejected request']])),'failed');
 assert.equal(pentagiOutcome(state('waiting',[])),'needs_input');
 assert.equal(pentagiOutcome(state('finished',[['finished','']])),'needs_input');
 assert.equal(pentagiOutcome(state('running',[['finished','earlier task']])) ,undefined);
 assert.equal(pentagiOutcome(state('waiting',[['finished','earlier'],['running','']])),'needs_input');
});
test('detailed write-up survives without exporting internal thoughts or tool traces',async()=>{
 const client=new PentagiClient('http://127.0.0.1:8443','TOKEN',async()=>Response.json({data:{messageLogs:[
  {id:1,type:'thoughts',message:'private reasoning',result:''},
  {id:2,type:'terminal',message:'raw transcript',result:''},
  {id:3,type:'report',message:'GET /search boolean differential',result:'reproducible evidence'},
  {id:4,type:'done',message:'Coverage gaps',result:''},
  {id:5,type:'ask',message:'Confirm route',result:''}
 ]}}));
 const evidence=await client.evidence('1');
 assert.deepEqual(evidence.map(x=>x.type),['report','done','ask']);assert.equal(evidence[0].id,'3');
 assert.equal(evidence[0].result,'reproducible evidence');assert.ok(!JSON.stringify(evidence).includes('private reasoning'));
});
test('malformed detailed evidence fails rather than silently discarding the write-up',async()=>{
 for(const messageLogs of [null,[{id:3,type:'report',message:5,result:''}],[{id:3,type:'report',message:'x'.repeat(100001),result:''}]]){
  const client=new PentagiClient('http://127.0.0.1:8443','TOKEN',async()=>Response.json({data:{messageLogs}}));
  await assert.rejects(()=>client.evidence('1'),/상세 보고서/);
 }
});
