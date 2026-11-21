'use strict';
// Real Docker regression; synthetic source only, no AWS or paid model calls.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const {randomBytes}=require('node:crypto');
const {docker}=require('../out/security/localPentagi');

async function main(){
 if(process.env.RECODER_MANAGED_SCANNERS_CHECK!=='1')throw Error('Set RECODER_MANAGED_SCANNERS_CHECK=1 to create and remove a temporary Docker scanner environment.');
 const extension=path.resolve(__dirname,'..');
 const endpoint=process.env.DOCKER_HOST||await docker(['context','inspect','--format','{{(index .Endpoints "docker").Host}}']);
 assert.match(endpoint,/^(unix:\/\/|npipe:\/\/)/,'A local Docker engine is required');
 const scratch=await fs.mkdtemp(path.join(os.tmpdir(),'recoder-managed-scanners-'));
 const id='rc-rt-'+randomBytes(12).toString('hex');
 const previousCwd=process.cwd();
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20*60*1000);
 const evidence={id,stages:[],source:'synthetic fixture',cloudOrModelCalls:false};
 try{
  await fs.mkdir(path.join(scratch,'source'));await fs.mkdir(path.join(scratch,'runtime'));
  await fs.writeFile(path.join(scratch,'source','Dockerfile'),'FROM python:3.12-alpine\nWORKDIR /app\nCOPY app.py /app/app.py\nUSER 65534:65534\nCMD ["python", "/app/app.py"]\n');
  await fs.writeFile(path.join(scratch,'source','app.py'),'print("recoder-owned-fixture")\n');
  //: managedScans 가 불러오는 모듈이 늘어도 빠지지 않게 out/security 의 스크립트를 모두 복사한다(목록을 손으로 두면 새 모듈에서 'Cannot find module').
  for(const file of (await fs.readdir(path.join(extension,'out/security'))).filter(f=>f.endsWith('.js')))await fs.copyFile(path.join(extension,'out/security',file),path.join(scratch,'runtime',file));
  await fs.copyFile(path.join(extension,'media/security-audit/deploy.rego'),path.join(scratch,'runtime/policy.rego'));
  process.chdir(scratch);
  const {runManagedScans}=require(path.join(scratch,'runtime/managedScans.js'));
  evidence.result=await runManagedScans(id,async stage=>{evidence.stages.push(stage);console.log(stage);},controller.signal);
  const checks=new Map(evidence.result.checks.map(c=>[c.tool,c]));
  assert.equal(checks.get('gitleaks')?.status,'completed');assert.equal(checks.get('gitleaks').count,0);
  assert.equal(checks.get('hadolint')?.status,'completed');assert.equal(checks.get('hadolint').blockingCount,0);
  assert.equal(checks.get('dependencies')?.status,'not_applicable','A package-free fixture must not be a failed or clean dependency scan');
  assert.equal(checks.get('image')?.status,'completed');
  assert.ok(['allow','deny'].includes(evidence.result.policy?.decision));
  const sbom=JSON.parse(await fs.readFile(path.join(scratch,'sbom.json'),'utf8'));
  assert.equal(sbom.bomFormat,'CycloneDX');assert.ok(sbom.specVersion);
  evidence.passed=true;
 }catch(error){evidence.passed=false;evidence.error=error.message;throw error;}
 finally{
  clearTimeout(timer);process.chdir(previousCwd);
  const names=await docker(['container','ls','-a','--filter','label=io.recoder.redteam.session='+id,'--format','{{.Names}}']);
  if(names.split(/\r?\n/).includes(id+'-scan-engine'))await docker(['rm','-f','-v',id+'-scan-engine']);
  const volumes=await docker(['volume','ls','--filter','label=io.recoder.redteam.session='+id,'--format','{{.Name}}']);
  if(volumes.split(/\r?\n/).includes(id+'-scan-data'))await docker(['volume','rm',id+'-scan-data']);
  evidence.remainingContainers=await docker(['container','ls','-aq','--filter','label=io.recoder.redteam.session='+id]);
  evidence.remainingVolumes=await docker(['volume','ls','-q','--filter','label=io.recoder.redteam.session='+id]);
  await fs.mkdir(path.join(extension,'.test-artifacts'),{recursive:true});
  await fs.writeFile(path.join(extension,'.test-artifacts/managed-scanners-live.json'),JSON.stringify(evidence,null,2));
  await fs.rm(scratch,{recursive:true,force:true});
  assert.equal(evidence.remainingContainers,'');assert.equal(evidence.remainingVolumes,'');
 }
 console.log('PASS: actual managed scanners, image build, SBOM, OPA and owned-resource cleanup');
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
