import * as vscode from 'vscode';
import {promises as fs} from 'fs';
import * as path from 'path';
import {createHash,randomBytes} from 'crypto';
import {CoreManager} from '../core/CoreManager';
import {ManagedAwsClient,AwsRequestError} from '../cloud/managedAwsClient';
import {ManagedJobs,ManagedRun} from '../security/managedJobs';
import {bundleSources,zipFiles,Bundle} from '../security/managedBundle';
import {frontendRequest,frontendManifest,FrontendRequest,FrontendManifest} from '../security/managedFrontendWorker';
import {buildFrontend,FrontendProject,BuildOutcome} from './frontendBuild';

const hash=(x:Buffer|string)=>createHash('sha256').update(x).digest('hex');
export async function frontendBundle(workspace:string,runtime:string,request:FrontendRequest):Promise<Bundle>{
    frontendRequest(request);
    const source=await bundleSources(workspace),files=source.files.map(f=>f.name.slice(7));
    for(const name of ['managedFrontendWorker.js','localPentagi.js','pentagiClient.js'])source.files.push({name:'runtime/'+name,data:await fs.readFile(path.join(runtime,name))});
    source.files.push({name:'runtime/request.json',data:Buffer.from(JSON.stringify(request))});
    const data=zipFiles(source.files);return {data,digest:hash(data),files,bytes:source.bytes};
}
/** Download into a fresh private directory; never overwrite the developer's dist or source. */
export async function downloadFrontend(client:ManagedAwsClient,bucket:string,run:ManagedRun,workspace:string):Promise<string>{
    if(!/^rc-rt-[a-f0-9]{24}$/.test(run.id)||!/^[a-f0-9]{64}$/.test(run.digest))throw new Error('빌드 식별자가 올바르지 않습니다.');
    if(run.status!=='SUCCEEDED'||!run.terminal)throw new Error('성공한 빌드만 배포에 사용할 수 있습니다.');
    const manifest:FrontendManifest=frontendManifest(JSON.parse(await client.object(bucket,`reports/${run.id}/report.json`,'GET')),run.id,run.digest);
    const root=await fs.realpath(workspace),cache=path.join(root,'.recoder');
    await fs.mkdir(cache,{recursive:true});
    if((await fs.lstat(cache)).isSymbolicLink()||await fs.realpath(cache)!==cache)throw new Error('프로젝트 캐시가 외부 경로를 가리킵니다.');
    const destination=await fs.mkdtemp(path.join(cache,'cloud-build-'));
    for(let i=0;i<manifest.files.length;i++){
        const file=manifest.files[i],data=await client.objectBytes(bucket,`reports/${run.id}/files/${i}`);
        if(data.length!==file.size||hash(data)!==file.sha256)throw new Error('빌드 산출물의 크기 또는 SHA256이 다릅니다. 배포를 중단했습니다.');
        const target=path.join(destination,...file.path.split('/'));
        await fs.mkdir(path.dirname(target),{recursive:true});
        const parent=await fs.realpath(path.dirname(target));
        if(parent!==destination&&!parent.startsWith(destination+path.sep))throw new Error('빌드 산출물 경로가 변경되었습니다.');
        await fs.writeFile(target,data,{flag:'wx'});
    }
    return path.relative(root,destination).split(path.sep).join('/');
}

export class ManagedFrontendHost {
    private busy=false;
    constructor(private core:CoreManager,private state:vscode.Memento,private runtime:string){}
    async build(workspace:string,project:FrontendProject,progress:(message:string)=>void,region:string):Promise<BuildOutcome>{
        if(!vscode.workspace.isTrusted)throw new Error('신뢰할 수 있는 프로젝트에서 빌드하세요.');
        if(this.busy)throw new Error('현재 화면 빌드가 끝난 뒤 다시 시도하세요.');this.busy=true;
        try{
            let connection;
            try{connection=await this.core.getManagedAwsConnection();}
            catch(error){
                const local=await vscode.window.showWarningMessage('AWS 연결 상태를 확인하지 못했습니다. 기존 로컬 빌드는 사용할 수 있습니다.','내 PC에서 빌드');
                if(local!=='내 PC에서 빌드')throw error;
                return buildFrontend(workspace,project,progress);
            }
            if(!connection)return buildFrontend(workspace,project,progress);
            const choice=await vscode.window.showQuickPick([
                {label:'AWS에서 빌드',description:'내 PC에 Node.js·Docker 설치 없이 실행',cloud:true},
                {label:'내 PC에서 빌드',description:'기존 npm / Docker 실행 사용',cloud:false},
            ],{title:'배포할 화면 빌드'});
            if(!choice)throw new Error('빌드를 취소했습니다.');
            if(!choice.cloud)return buildFrontend(workspace,project,progress);
            const {metadata,client}=connection;
            if(region&&metadata.region!==region)throw new Error(`배포 리전(${region})과 연결된 AWS 리전(${metadata.region})을 맞춰 주세요.`);
            const jobs=new ManagedJobs(client,metadata.account,'build');
            const key='deploy.managed-build.'+hash(`${metadata.account}:${metadata.region}:${workspace}`);
            let prior=this.state.get<ManagedRun>(key);
            if(prior&&!prior.terminal){
                const refreshed=await jobs.refresh(prior,false);prior=refreshed.run;await this.state.update(key,prior);
                if(!prior.terminal){
                    const action=await vscode.window.showWarningMessage('이 프로젝트의 이전 빌드가 진행 중이거나 실행 여부를 확인 중입니다.',{modal:true,detail:'중복 빌드를 만들지 않습니다. 중단 요청 후 실제 종료를 확인한 다음 다시 시도하세요.'},'이전 빌드 중단 요청');
                    if(action==='이전 빌드 중단 요청')await jobs.stop(prior);
                    throw new Error('이전 빌드가 종료된 후 다시 배포를 준비하세요.');
                }
                await jobs.deleteSource(prior).catch(()=>{});
            }
            let setup=await jobs.status();
            if(!setup.ready&&setup.stackStatus==='NOT_CREATED'){
                const approved=await vscode.window.showWarningMessage('내 AWS에 화면 빌드 환경을 만들까요?',{modal:true,detail:`${metadata.account} · ${metadata.region}\n전용 CodeBuild·IAM·비공개 S3·작업 잠금·로그를 만듭니다. 빌드 사용료와 저장·요청 비용이 발생합니다. 실행은 최대 60분, 동시 1개입니다. 소스는 1일, 결과·로그는 7일 후 만료되며 스택 삭제 시 저장소는 보존됩니다.`},'환경 만들기');
                if(approved!=='환경 만들기')throw new Error('빌드 환경 설치를 취소했습니다.');
                await jobs.provision();setup={ready:false,stackStatus:'CREATE_IN_PROGRESS'};
            }
            return await vscode.window.withProgress({location:vscode.ProgressLocation.Notification,title:'ReCoder · AWS 화면 빌드',cancellable:true},async(notification,cancel)=>{
                const stage=(message:string)=>{progress(message);notification.report({message});};
                let attempts=0;
                while(!setup.ready){
                    if(cancel.isCancellationRequested)throw new Error('대기를 취소했습니다. AWS에서 이미 시작한 환경 설치는 계속 진행됩니다.');
                    if(!/^(CREATE|UPDATE)_IN_PROGRESS$/.test(setup.stackStatus)||attempts++>=120)throw new Error(`빌드 환경 상태: ${setup.stackStatus}. CloudFormation 이벤트를 확인하세요.`);
                    stage('AWS 빌드 환경 준비 중');await new Promise(r=>setTimeout(r,5000));setup=await jobs.status();
                }
                if(cancel.isCancellationRequested)throw new Error('빌드를 취소했습니다.');
                const request=frontendRequest({id:'rc-rt-'+randomBytes(12).toString('hex'),projectDir:project.projectDir,outDir:project.outDir,script:project.script||'build'});
                stage('업로드할 프로젝트 사본 준비');const bundle=await frontendBundle(workspace,this.runtime,request);
                await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({language:'plaintext',content:`AWS 화면 빌드 업로드 검토\n${metadata.account} · ${metadata.region}\nSHA256 ${bundle.digest}\n${bundle.files.length}개 파일\n.env·숨김 설정·키 파일·node_modules 제외. 직접 작성된 비밀값은 제외를 보장하지 않습니다.\n\n${bundle.files.join('\n')}`}),{preview:true});
                const approved=await vscode.window.showWarningMessage('이 프로젝트 사본을 AWS에서 빌드할까요?',{modal:true,detail:`${metadata.account} · ${metadata.region}\n${bundle.files.length}개 파일 · ${(bundle.bytes/1024/1024).toFixed(1)}MB\n${project.projectDir||'루트'}: npm run ${request.script}\n\n의존성과 npm 스크립트를 독립 컨테이너에서 실행합니다. AWS 자격증명·로컬 .env·비공개 npm 인증은 전달하지 않습니다. 공개 API 주소 등 필수 설정은 소스의 공개 설정으로 준비하세요. CodeBuild 사용료가 발생하며 확장을 닫아도 최대 60분까지 실행될 수 있습니다. 이 단계는 빌드만 수행하며 실제 배포는 다음 화면에서 승인합니다.`},'업로드·비용 확인 후 빌드');
                if(approved!=='업로드·비용 확인 후 빌드'||cancel.isCancellationRequested)throw new Error('빌드를 취소했습니다.');
                let run:ManagedRun={id:request.id,createdAt:new Date().toISOString(),digest:bundle.digest,files:bundle.files.length,bytes:bundle.bytes,status:'SUBMITTING',uncertain:true,submission:'uploading'};
                await this.state.update(key,run);
                try{run.buildId=await jobs.start(run,bundle,async()=>{run.submission='starting';await this.state.update(key,run);});run.status='IN_PROGRESS';run.uncertain=false;}
                finally{await this.state.update(key,run);}
                stage('AWS 작업 시작');let stopRequested=false;
                const deadline=Date.now()+70*60*1000;
                while(!run.terminal){
                    if(cancel.isCancellationRequested&&!stopRequested){await jobs.stop(run);stopRequested=true;stage('빌드 중단 확인 중');}
                    if(Date.now()>deadline)throw new Error('빌드 확인 시간이 초과되었습니다. 다시 준비하면 기존 작업 상태를 확인합니다.');
                    await new Promise(r=>setTimeout(r,5000));run=(await jobs.refresh(run,false)).run;await this.state.update(key,run);
                    if(!stopRequested){
                        let message=run.stage||run.status;
                        try{const p=JSON.parse(await client.object(jobs.bucket,`reports/${run.id}/progress.json`,'GET'));if(p.runId===run.id&&p.sourceSha256===run.digest&&typeof p.stage==='string')message=p.stage.slice(0,200);}catch{/* authoritative AWS state remains visible */}
                        stage(message);
                    }
                }
                await jobs.deleteSource(run).catch(()=>{});
                if(run.status!=='SUCCEEDED')throw new Error(`AWS 화면 빌드가 완료되지 않았습니다 (${run.status}). 기존 배포는 변경되지 않았습니다.`);
                if(cancel.isCancellationRequested)throw new Error('배포 준비를 취소했습니다.');
                stage('빌드 결과 다운로드 및 SHA256 검증');const outDir=await downloadFrontend(client,jobs.bucket,run,workspace);
                return {ok:true,via:'managed',output:'AWS 빌드 및 산출물 검증 완료',outDir};
            });
        }catch(e){
            if(e instanceof AwsRequestError&&/AccessDenied|Unauthorized/.test(e.code))throw new Error('AWS 관리형 빌드 권한이 없습니다. ReCoder: 관리형 AWS 권한 보기 명령에서 설치용·실행용 정책을 확인하세요.');
            throw e;
        }finally{this.busy=false;}
    }
}
