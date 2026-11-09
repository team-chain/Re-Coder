const test=require('node:test'),assert=require('node:assert/strict');
const {progressPublisher,finalizeManagedRuntime}=require('../out/security/managedPentagiWorker');
const {managedReport}=require('../out/security/managedReport');

test('every published completion snapshot remains valid while cleanup is pending',async()=>{
 const expected={id:'rc-rt-'+'a'.repeat(24),digest:'a'.repeat(64),mode:'redteam'};
 const report={schema:1,runId:expected.id,sourceSha256:expected.digest,mode:'redteam',status:'completed',stage:'검사',isolationVerified:true,tasks:[{id:'1',title:'Assessment',status:'finished',result:'Reproducible finding and coverage gaps'}]};
 const published=[];let release;
 const barrier=new Promise(resolve=>{release=resolve;});
 const done=finalizeManagedRuntime(report,async()=>{published.push(managedReport(structuredClone(report),expected));},()=>barrier);
 await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(published.map(x=>x.status),['finalizing']);assert.equal(published[0].cleaned,undefined);
 release();await done;
 assert.deepEqual(published.map(x=>x.status),['finalizing','completed']);assert.equal(published[1].cleaned,true);
});
test('cleanup failure never restores a completed assessment status',async()=>{
 const report={status:'completed'},states=[];
 await finalizeManagedRuntime(report,async()=>{states.push(report.status);},async()=>{throw Error('Docker cleanup failed');});
 assert.deepEqual(states,['finalizing','cleanup_failed']);assert.equal(report.cleaned,false);
});
test('quick stage transitions publish before a long operation while identical polls are throttled',async()=>{
 let time=10000,calls=0;const publish=progressPublisher(async()=>{calls++;},()=>time);
 await publish('engine starting','preparing');
 time+=100;await publish('downloading worker image','preparing');
 assert.equal(calls,2,'download stage must be visible before its long await');
 time+=100;await publish('downloading worker image','preparing');assert.equal(calls,2);
 time+=5000;await publish('downloading worker image','preparing');assert.equal(calls,3);
 time+=100;await publish('downloading worker image','failed');assert.equal(calls,4);
});
test('failed publication remains eligible for immediate retry',async()=>{
 let calls=0;const publish=progressPublisher(async()=>{if(++calls===1)throw Error('temporary failure');},()=>10000);
 await assert.rejects(()=>publish('building','running'));
 await publish('building','running');assert.equal(calls,2);
});
