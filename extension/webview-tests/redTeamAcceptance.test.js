const test=require('node:test'),assert=require('node:assert/strict');
const {checkJournal}=require('../harness/redteam-sql-fixture');
test('acceptance requires responses actually captured by the fixture; fabricated model evidence cannot pass',()=>{
 const requests=['GET /search?q=public','GET /search?q=%27%20OR%201%3D1--','GET /search?q=%27%20AND%201%3D2--'];
 const contract={schema:1,checks:[{kind:'sql_injection',status:'tested',reason:'Fixture differential',requests}],findings:[{kind:'sql_injection',title:'SQL',route:'/search',request:requests[1],response:'[{"name":"synthetic-marker-unique"}]',controlRequest:requests[2],controlResponse:'[]',observation:'Only true condition returns marker.',remediation:'Use placeholders.'}],limitations:[]};
 const report={target:'http://localhost:19371/',outcome:'finished',phase:'removed',tasks:[{result:'```recoder-report\n'+JSON.stringify(contract)+'\n```'}]};
 const journal=[{request:requests[1],response:contract.findings[0].response},{request:requests[2],response:'[]'},...requests.slice(1).map(request=>({request:request.replace('/search','/safe'),response:'[]'}))];
 assert.equal(checkJournal(report,journal).passed,true);
 assert.equal(checkJournal(report,[]).passed,false);
 assert.equal(checkJournal(report,[{...journal[0],response:'[]'},journal[1]]).passed,false);
 assert.equal(checkJournal({...report,phase:'cleanup_failed'},journal).passed,false);
 assert.equal(checkJournal(report,journal.slice(0,2)).passed,false);
});
