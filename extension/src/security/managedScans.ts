import {promises as fs} from 'fs';
import * as path from 'path';
import {docker,DockerRun,PENTAGI_IMAGES} from './localPentagi';

export const SCANNER_IMAGES={
    trivy:'aquasec/trivy@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c',
    gitleaks:'zricethezav/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f',
    hadolint:'hadolint/hadolint@sha256:32dac94127fd60b7b7e3fbfc65e1383b9b5e25c9bfd7b8536de7a539fe68a12d',
    opa:'openpolicyagent/opa@sha256:906bfc9904b2693733e7959d71773dd073e6c135cf06f26d955abe94603e82b3',
};
export interface ScanSummary {tool:string;status:'completed'|'failed'|'not_applicable';findings:Array<{title:string;severity:string;file?:string;line?:number;fix?:string}>;message?:string;count?:number;blockingCount?:number;criticalCount?:number;highCount?:number}
export function scannerSummary(tool:string,data:any,maxFindings=200):ScanSummary {
    const findings:ScanSummary['findings']=[];
    if(tool==='gitleaks'){
        if(!Array.isArray(data)||data.some(f=>!f||typeof f.File!=='string'||!Number.isInteger(f.StartLine)))throw new Error('gitleaks 응답 오류');
        for(const f of data)findings.push({title:String(f.Description||f.RuleID||'시크릿 후보'),severity:'high',file:String(f.File||'').replace(/^\/source\//,''),line:Number(f.StartLine)||undefined,fix:'시크릿을 제거하고 해당 자격증명을 회전하세요.'});
    }else if(tool==='hadolint'){
        if(!Array.isArray(data)||data.some(f=>!f||typeof f.code!=='string'||typeof f.message!=='string'||!['error','warning','info','style'].includes(f.level)))throw new Error('hadolint 응답 오류');
        for(const f of data)findings.push({title:`${f.code}: ${f.message}`,severity:f.level==='error'?'high':'medium',file:'Dockerfile',line:Number(f.line)||undefined});
    }else{
        // Trivy omits Results when no supported packages are present. A valid
        // artifact header distinguishes that case from a missing/broken report.
        if(data&&data.Results===undefined&&data.SchemaVersion===2&&typeof data.ArtifactName==='string'&&data.ArtifactName&&['filesystem','container_image'].includes(data.ArtifactType))data={...data,Results:[]};
        if(!data||!Array.isArray(data.Results))throw new Error('Trivy가 검사 대상을 확인하지 못했습니다.');
        if(!data.Results.length)return {tool,status:'not_applicable',findings:[],message:'분석 가능한 패키지 목록이 없습니다. 취약점 검사 통과를 의미하지 않습니다.'};
        for(const r of data.Results)for(const f of r.Vulnerabilities||[])findings.push({title:`${f.VulnerabilityID} · ${f.PkgName} ${f.InstalledVersion}`,severity:String(f.Severity||'unknown').toLowerCase(),file:String(r.Target||''),fix:f.FixedVersion?`수정 버전: ${f.FixedVersion}`:'수정 버전 미확인'});
    }
    return {tool,status:'completed',count:findings.length,blockingCount:findings.filter(f=>f.severity==='high'||f.severity==='critical').length,criticalCount:findings.filter(f=>f.severity==='critical').length,highCount:findings.filter(f=>f.severity==='high').length,findings:findings.slice(0,maxFindings)};
}
export async function runManagedScans(id:string,progress:(stage:string)=>Promise<void>,signal:AbortSignal,exportImage?:string):Promise<{checks:ScanSummary[];policy?:unknown}> {
    const run:DockerRun=(args,options)=>docker(args,{...options,signal});
    const checks:ScanSummary[]=[],source=path.resolve('source'),scratch=await fs.mkdtemp(path.resolve('recoder-scan-'));
    const engine=id+'-scan-engine',volume=id+'-scan-data';let engineCreated=false,volumeCreated=false;
    const label=['--label',`io.recoder.redteam.session=${id}`];
    const base=['run','--rm',...label,'--network','none','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit','128','--memory','1536m','--cpus','2','--read-only','--tmpfs','/tmp:rw,noexec,nosuid,size=128m'];
    const mount=(src:string,dst:string,ro=true)=>['--mount',`type=bind,src=${src},dst=${dst}${ro?',readonly':''}`];
    const checked=async(tool:string,action:()=>Promise<ScanSummary>)=>{await progress(tool+' 검사');try{checks.push(await action());}catch{if(signal.aborted)throw new Error('검사 취소');checks.push({tool,status:'failed',findings:[],message:'검사를 완료하지 못했습니다. 통과로 처리하지 않습니다.'});}};
    try {
        for(const image of Object.values(SCANNER_IMAGES)){await progress('검사 도구 준비');await run(['pull',image],{timeout:600000});}
        await fs.writeFile(path.join(scratch,'gitleaks.toml'),'[extend]\nuseDefault = true\n');
        await checked('gitleaks',async()=>{
            // Fixed config + explicit redaction: target .gitleaks.toml cannot disable checks.
            const raw=await run([...base,...mount(source,'/source'),...mount(scratch,'/results',false),SCANNER_IMAGES.gitleaks,'dir','/source','--config','/results/gitleaks.toml','--redact=100','--exit-code','0','--report-format','json','--report-path','/results/secrets.json','--no-banner'],{timeout:300000});void raw;
            return scannerSummary('gitleaks',JSON.parse(await fs.readFile(path.join(scratch,'secrets.json'),'utf8')));
        });
        const hasDockerfile=!!await fs.stat(path.join(source,'Dockerfile')).catch(()=>null);
        if(hasDockerfile)await checked('hadolint',async()=>scannerSummary('hadolint',JSON.parse(await run([...base,...mount(source,'/source'),SCANNER_IMAGES.hadolint,'hadolint','--format','json','--no-fail','/source/Dockerfile']))));
        else checks.push({tool:'hadolint',status:'not_applicable',findings:[],message:'Dockerfile 없음'});
        // The deploy builder must stop before executing project code on source scan failure.
        if(exportImage&&(!hasDockerfile||checks.some(c=>c.status!=='completed'||(c.blockingCount??1)>0)))throw new Error('시크릿·Dockerfile 검사를 통과하지 못해 이미지 빌드를 중단했습니다.');
        await fs.mkdir(path.join(scratch,'cache'));
        await progress('취약점 DB 다운로드');
        await run(['run','--rm',...label,'--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit','128','--memory','1536m',...mount(path.join(scratch,'cache'),'/cache',false),SCANNER_IMAGES.trivy,'image','--cache-dir','/cache','--download-db-only','--no-progress'],{timeout:600000});
        const trivy=[...base,...mount(path.join(scratch,'cache'),'/cache',false),...mount(source,'/source'),...mount(scratch,'/results',false),SCANNER_IMAGES.trivy];
        const flags=['--cache-dir','/cache','--skip-db-update','--skip-java-db-update','--offline-scan','--scanners','vuln','--format','json','--no-progress','--exit-code','0'];
        await checked('dependencies',async()=>scannerSummary('dependencies',JSON.parse(await run([...trivy,'fs',...flags,'/source'],{timeout:600000}))));
        let imageReport:any,sbomPresent=false;
        if(hasDockerfile)await checked('image',async()=>{
            await progress('격리된 엔진에서 이미지 빌드');await run(['pull',PENTAGI_IMAGES.engine],{timeout:600000});
            await run(['volume','create',...label,volume]);volumeCreated=true;
            await run(['run','-d','--name',engine,...label,'--privileged','--memory','3g','--cpus','2','--pids-limit','2048','-e','DOCKER_TLS_CERTDIR=','-v',volume+':/var/lib/docker',PENTAGI_IMAGES.engine,'dockerd','--host=unix:///var/run/docker.sock']);engineCreated=true;
            let ready=false;for(let i=0;i<30;i++){try{await run(['exec',engine,'docker','info'],{timeout:5000});ready=true;break;}catch{await new Promise(r=>setTimeout(r,2000));}}if(!ready)throw new Error('검사 엔진 시작 실패');
            for(const cidr of ['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','169.254.0.0/16','127.0.0.0/8'])await run(['exec',engine,'iptables','-I','DOCKER-USER','-i','docker0','-d',cidr,'-j','REJECT']);
            await run(['cp',source+path.sep+'.',engine+':/source']);
            await run(['exec',engine,'docker','build','--platform=linux/amd64','--network=default','-t','recoder-scan','/source'],{timeout:900000});
            // DinD remounts /tmp inside its own mount namespace. docker cp from
            // the outer engine cannot see files written there by docker exec.
            const archive='/var/lib/docker/recoder-image.tar';
            await run(['exec',engine,'docker','save','-o',archive,'recoder-scan'],{timeout:180000});
            const size=Number(await run(['exec',engine,'stat','-c','%s',archive]));if(!Number.isFinite(size)||size<=0||size>1024**3)throw new Error('검사 이미지 크기가 올바르지 않거나 1GB를 초과합니다.');
            await run(['cp',engine+':'+archive,path.join(scratch,'image.tar')],{timeout:180000});
            imageReport=JSON.parse(await run([...trivy,'image',...flags,'--input','/results/image.tar'],{timeout:600000}));
            await run([...trivy,'image','--cache-dir','/cache','--skip-db-update','--skip-java-db-update','--offline-scan','--format','cyclonedx','--output','/results/sbom.json','--input','/results/image.tar'],{timeout:600000});
            const sbom=JSON.parse(await fs.readFile(path.join(scratch,'sbom.json'),'utf8'));
            if(sbom.bomFormat!=='CycloneDX'||!sbom.specVersion)throw new Error('SBOM 생성 확인 실패');
            await fs.copyFile(path.join(scratch,'sbom.json'),path.resolve('sbom.json'));sbomPresent=true;
            return scannerSummary('image',imageReport,exportImage?5000:200);
        });else checks.push({tool:'image',status:'not_applicable',findings:[],message:'Dockerfile 없음 · 이미지 검사 미실행'});
        // Evaluate the shipped deployment policy against this exact scan, never production metadata guesses.
        const input={environment:'security-preview',sbom:{present:sbomPresent},trivy:{critical_count:(imageReport?.Results||[]).flatMap((r:any)=>r.Vulnerabilities||[]).filter((f:any)=>f.Severity==='CRITICAL').length},
            gitleaks:{passed:checks.some(c=>c.tool==='gitleaks'&&c.status==='completed'&&c.count===0)},hadolint:{passed:checks.some(c=>c.tool==='hadolint'&&c.status==='completed'&&c.blockingCount===0)}};
        const policyInput=path.join(scratch,'policy-input.json'),policyFile=path.join(scratch,'policy.rego');
        await fs.writeFile(policyInput,JSON.stringify(input));
        await fs.copyFile(path.join(__dirname,'policy.rego'),policyFile);
        // CodeBuild creates root-owned private directories; OPA runs as UID 1000.
        // Mount only these non-secret files, readable inside the sandbox, instead
        // of exposing the private scratch directory or changing its permissions.
        await fs.chmod(policyInput,0o444);await fs.chmod(policyFile,0o444);
        await progress('OPA 정책 평가');
        const policy=JSON.parse(await run([...base,...mount(policyFile,'/policy.rego'),...mount(policyInput,'/policy-input.json'),SCANNER_IMAGES.opa,'eval','--format','json','--data','/policy.rego','--input','/policy-input.json','data.recoder.deploy.allow']));
        const value=policy.result?.[0]?.expressions?.[0]?.value;if(!['allow','deny'].includes(value?.decision))throw new Error('정책 평가 결과 오류');
        if(exportImage){
            if(value.decision!=='allow'||!sbomPresent||checks.some(c=>c.status==='failed')||!checks.some(c=>c.tool==='image'&&c.status==='completed'&&c.criticalCount===0))throw new Error('이미지 검사·SBOM·정책을 통과하지 못해 업로드하지 않습니다.');
            await fs.copyFile(path.join(scratch,'image.tar'),exportImage);
        }
        return {checks,policy:{...value,scope:'검사 사본 정책 평가 · 실제 배포 허용 아님'}};
    }finally{
        // These exact names were created here; never enumerate/delete unrelated workloads.
        if(engineCreated)await docker(['rm','-f','-v',engine]);
        if(volumeCreated)await docker(['volume','rm',volume]);
        await fs.rm(scratch,{recursive:true,force:true});
    }
}
