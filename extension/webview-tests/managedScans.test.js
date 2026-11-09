const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {scannerSummary,SCANNER_IMAGES}=require('../out/security/managedScans');
test('secret reports discard the secret, matched source and arbitrary scanner fields',()=>{
 const result=scannerSummary('gitleaks',[{Description:'credential',RuleID:'aws-key',File:'/source/app.ts',StartLine:2,Secret:'NEVER_REPORT_THIS_SECRET',Match:'SECRET_SOURCE',Fingerprint:'SECRET_AGAIN'}]);
 assert.equal(result.findings[0].file,'app.ts');assert.doesNotMatch(JSON.stringify(result),/NEVER_REPORT|SECRET_SOURCE|SECRET_AGAIN/);
});
test('malformed scanner output cannot become a clean result',()=>{
 for(const [tool,result] of [['gitleaks',{}],['hadolint',null],['image',{}],['dependencies',null]])assert.throws(()=>scannerSummary(tool,result));
 assert.equal(scannerSummary('image',{Results:[{Target:'image',Vulnerabilities:[{VulnerabilityID:'CVE-test',PkgName:'openssl',InstalledVersion:'1',Severity:'CRITICAL',FixedVersion:'2'}]}]}).findings[0].severity,'critical');
 assert.equal(scannerSummary('dependencies',{Results:[]}).status,'not_applicable');
 const many=Array.from({length:201},(_,i)=>({code:'DL3001',message:'issue',line:i+1,level:i===200?'error':'warning'}));const capped=scannerSummary('hadolint',many);assert.equal(capped.findings.length,200);assert.equal(capped.blockingCount,1);
});
test('managed scanners are pinned; packaged OPA policy equals existing deploy policy',()=>{
 for(const image of Object.values(SCANNER_IMAGES))assert.match(image,/@sha256:[a-f0-9]{64}$/);
 assert.equal(fs.readFileSync(path.resolve('media/security-audit/deploy.rego'),'utf8'),fs.readFileSync(path.resolve('../policies/recoder/deploy.rego'),'utf8'));
});
test('Trivy omits Results for a valid package-free artifact without implying a clean scan',()=>{
 const result=scannerSummary('dependencies',{SchemaVersion:2,ArtifactName:'/source',ArtifactType:'filesystem'});
 assert.equal(result.status,'not_applicable');assert.equal(result.count,undefined);
 for(const invalid of [{},{SchemaVersion:2},{SchemaVersion:2,ArtifactName:'/source'},{SchemaVersion:2,ArtifactName:'/source',ArtifactType:'filesystem',Results:null}])assert.throws(()=>scannerSummary('dependencies',invalid));
});
