const test=require('node:test'),assert=require('node:assert/strict');
const {progressPublisher}=require('../out/security/managedPentagiWorker');
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
