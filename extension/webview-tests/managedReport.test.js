const test=require('node:test'),assert=require('node:assert/strict');
const {managedReport}=require('../out/security/managedReport');
const expected={id:'rc-rt-'+'a'.repeat(24),digest:'a'.repeat(64),mode:'scans'};
const base=()=>({schema:1,runId:expected.id,sourceSha256:expected.digest,mode:'scans',status:'completed',stage:'검사 완료',tasks:[],scans:{
 checks:['gitleaks','hadolint','dependencies','image'].map(tool=>({tool,status:'completed',count:0,blockingCount:0,findings:[]})),policy:{decision:'allow',reason:'검사 범위 충족',scope:'사본'}}});
test('managed reports bind the exact run, source and mode before rendering',()=>{
 assert.equal(managedReport(base(),expected).status,'completed');
 for(const change of [{runId:'another'},{sourceSha256:'different'},{mode:'audit'},{tasks:[null]},{scans:{checks:[]}}])assert.throws(()=>managedReport({...base(),...change},expected));
});
test('completed checks require complete counts, known policy decisions and all four results',()=>{
 for(const mutate of [r=>r.scans.checks.pop(),r=>r.scans.checks[0].status='failed',r=>r.scans.policy.decision='maybe',r=>delete r.scans.checks[0].count,r=>r.scans.checks[0].blockingCount=1,r=>r.scans.checks[1].tool='gitleaks']){
  const r=base();mutate(r);assert.throws(()=>managedReport(r,expected));
 }
 const r=base();r.scans.checks[3]={tool:'image',status:'not_applicable',findings:[],message:'Dockerfile 없음'};
 assert.equal(managedReport(r,expected).scans.checks[3].status,'not_applicable');
});
test('audit navigation accepts only reviewed relative source files; runtime claims require isolation and cleanup',()=>{
 const audit={...base(),mode:'audit',scans:undefined,audit:{scope:'partial',files:['src/app.ts'],skipped:[],calls:9,inputTokens:2,outputTokens:1,needsValidation:[],findings:[{title:'검토 항목',severity:'high',file:'src/app.ts',line:2,impact:'검토',verification:'소스 재검토'}]}};
 assert.equal(managedReport(audit,{...expected,mode:'audit'}).audit.findings.length,1);
 for(const file of ['../outside','C:/secret','/etc/passwd','file://secret','src/missing.ts'])assert.throws(()=>managedReport({...audit,audit:{...audit.audit,findings:[{...audit.audit.findings[0],file}]}},{...expected,mode:'audit'}));
 const redteam={...base(),mode:'redteam',scans:undefined};
 assert.throws(()=>managedReport(redteam,{...expected,mode:'redteam'}));
 assert.equal(managedReport({...redteam,isolationVerified:true,cleaned:true},{...expected,mode:'redteam'}).status,'completed');
});
