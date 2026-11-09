/** Runs only in the ReCoder CodeBuild project. No application code runs with this role. */
import {promises as fs} from 'fs';
import * as path from 'path';
import {createHash} from 'crypto';
import {spawn} from 'child_process';
import {docker} from './localPentagi';
import {runManagedScans} from './managedScans';
import {containerReport,sourceManifest} from './managedContainerContract';

const hash=(data:Buffer)=>createHash('sha256').update(data).digest('hex');
function aws(args:string[]):Promise<string>{
    return new Promise((resolve,reject)=>{
        const child=spawn('aws',[...args,'--no-cli-pager'],{shell:false,stdio:['ignore','pipe','pipe']});let output='',done=false;
        const finish=(error?:Error)=>{if(done)return;done=true;clearTimeout(timer);error?reject(error):resolve(output.trim());};
        const timer=setTimeout(()=>{child.kill();finish(new Error('AWS 이미지 저장 시간이 초과되었습니다.'));},120000);
        child.stdout.on('data',chunk=>{output+=chunk;if(output.length>4*1024*1024){child.kill();finish(new Error('AWS 응답 크기 초과'));}});
        child.stderr.resume();child.on('error',()=>finish(new Error('AWS 이미지 저장 도구를 실행하지 못했습니다.')));
        child.on('close',code=>finish(code===0?undefined:new Error(`AWS ${args[0]} 작업 실패 (${code}).`)));
    });
}
export async function runContainerWorker(runtime=__dirname,dependencies={scan:runManagedScans,docker,aws}){
    const {scan,docker:runDocker,aws:runAws}=dependencies;
    const request=JSON.parse(await fs.readFile(path.join(runtime,'request.json'),'utf8'));
    const region=process.env.AWS_REGION||'',bucket=process.env.RECODER_REPORT_BUCKET||'',id=process.env.RECODER_RUN_ID||'',digest=process.env.RECODER_SOURCE_SHA256||'',buildId=process.env.CODEBUILD_BUILD_ID||'';
    const match=/^recoder-container-(\d{12})-([a-z0-9-]+)$/.exec(bucket);
    if(!match||match[2]!==region||!/^rc-rt-[a-f0-9]{24}$/.test(id)||request.id!==id||!/^[a-f0-9]{64}$/.test(digest)||!/^recoder-container-v1:[a-f0-9-]{36}$/.test(buildId))throw new Error('관리형 컨테이너 실행 환경을 확인하지 못했습니다.');
    const sources=sourceManifest(request.sourceFiles),account=match[1];
    const registry=`${account}.dkr.ecr.${region}.amazonaws.com`,repo='recoder-managed-images-v1',tagged=`${registry}/${repo}:${id}`;
    const report:any={schema:1,runId:id,buildId,sourceSha256:digest,status:'running',sourceFiles:sources,stage:'이미지 빌드 준비'};
    const put=(file:string,key:string)=>runAws(['s3api','put-object','--bucket',bucket,'--key',`reports/${id}/${key}`,'--body',path.resolve(file),'--server-side-encryption','AES256']);
    const progress=async(stage:string)=>{report.stage=stage;await fs.writeFile('report.json',JSON.stringify(report));await put('report.json','progress.json');};
    const archive=path.resolve('verified-image.tar');
    try{
        // Bind the result to the exact source snapshot approved in VS Code.
        for(const file of sources){
            const full=path.resolve('source',...file.path.split('/')),stat=await fs.lstat(full);
            if(!stat.isFile()||stat.isSymbolicLink()||stat.size!==file.size||hash(await fs.readFile(full))!==file.sha256)throw new Error('업로드한 소스 사본이 변경되었습니다.');
        }
        Object.assign(report,await scan(id,progress,new AbortController().signal,archive));
        const sbom=await fs.readFile('sbom.json');if(sbom.length>4*1024*1024)throw new Error('SBOM 크기가 검증 한도(4MB)를 초과했습니다.');
        const components=JSON.parse(sbom.toString('utf8'));
        if(components.bomFormat!=='CycloneDX'||typeof components.specVersion!=='string'||!Array.isArray(components.components))throw new Error('유효한 CycloneDX SBOM이 아닙니다.');
        report.sbomSha256=hash(sbom);
        const pendingDigest='sha256:'+'0'.repeat(64);
        containerReport({...report,status:'completed',imageDigest:pendingDigest,image:`${registry}/${repo}@${pendingDigest}`},{run_id:id,build_id:buildId,source_sha256:digest},account,region);
        // No RUN/ENTRYPOINT from the image is executed by this credential-bearing controller.
        await progress('검사한 이미지를 내 ECR에 저장');
        await runDocker(['load','-i',archive],{timeout:180000});
        await runDocker(['tag','recoder-scan',tagged]);
        const password=await runAws(['ecr','get-login-password','--region',region]);
        await runDocker(['login','--username','AWS','--password-stdin',registry],{input:password});
        await runDocker(['push',tagged],{timeout:600000});
        const images=JSON.parse(await runAws(['ecr','describe-images','--region',region,'--repository-name',repo,'--image-ids',`imageTag=${id}`]));
        const details=images.imageDetails;if(!Array.isArray(details)||details.length!==1)throw new Error('저장한 이미지를 하나로 확인하지 못했습니다.');
        report.imageDigest=details[0].imageDigest;report.image=`${registry}/${repo}@${report.imageDigest}`;report.status='completed';
        containerReport(report,{run_id:id,build_id:buildId,source_sha256:digest},account,region);
        await put('sbom.json','sbom.json');report.stage='이미지·보안 검사·SBOM 검증 완료';
    }catch(e){report.status='failed';delete report.image;report.stage=e instanceof Error?e.message:'이미지 빌드를 완료하지 못했습니다.';process.exitCode=1;}
    finally{
        await runDocker(['logout',registry]).catch(()=>{});
        await fs.writeFile('report.json',JSON.stringify(report));await put('report.json','report.json');
        await fs.unlink(archive).catch(()=>{});
    }
}
if(require.main===module)runContainerWorker().catch(()=>{console.error('관리형 컨테이너 빌드를 완료하지 못했습니다.');process.exitCode=1;});
