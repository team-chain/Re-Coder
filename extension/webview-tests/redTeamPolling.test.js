const test=require('node:test'),assert=require('node:assert/strict');
const {redTeamPollDelay,REDTEAM_STATUS_TIMEOUT}=require('../out/webview-test/components/redTeamPolling');
test('temporary status failures have bounded backoff; mutations and approvals are never replayed',()=>{
 for(const action of ['localStatus','status','managedStatus']){
  assert.deepEqual([1,2,3,4].map(n=>redTeamPollDelay(action,n)),[3000,6000,12000,undefined]);
 }
 for(const action of ['start','localStart','managedStart','stop','localStop','managedSetup','configure'])assert.equal(redTeamPollDelay(action,1),undefined);
 assert.equal(redTeamPollDelay('status',0),undefined);assert.equal(REDTEAM_STATUS_TIMEOUT,45000);
});
