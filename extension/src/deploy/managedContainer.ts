import * as vscode from 'vscode';
import {promises as fs} from 'fs';
import * as path from 'path';
import {createHash,randomBytes} from 'crypto';
import {CoreManager} from '../core/CoreManager';
import {AwsRequestError} from '../cloud/managedAwsClient';
import {ManagedJobs,ManagedRun} from '../security/managedJobs';
import {bundleSources,zipFiles,Bundle} from '../security/managedBundle';
import {containerReport,sourceManifest,ManagedContainerReference} from '../security/managedContainerContract';

const hash=(x:Buffer|string)=>createHash('sha256').update(x).digest('hex');
export async function containerBundle(workspace:string,runtime:string,id:string):Promise<Bundle>{
    if(!/^rc-rt-[a-f0-9]{24}$/.test(id))throw new Error('이미지 빌드 식별자 오류');
    const source=await bundleSources(workspace),files=source.files.map(f=>f.name.slice(7));
    const sourceFiles=sourceManifest(source.files.map(f=>({path:f.name.slice(7),size:f.data.length,sha256:hash(f.data)})));
    for(const name of ['managedContainerWorker.js','managedContainerContract.js','managedScans.js','localPentagi.js','pentagiClient.js'])source.files.push({name:'runtime/'+name,data:await fs.readFile(path.join(runtime,name))});
    source.files.push({name:'runtime/policy.rego',data:await fs.readFile(path.resolve(runtime,'../../media/security-audit/deploy.rego'))});
    source.files.push({name:'runtime/request.json',data:Buffer.from(JSON.stringify({id,sourceFiles}))});
    const data=zipFiles(source.files);return {data,digest:hash(data),files,bytes:source.bytes};
}
/** The host retains this reference in its approval plan; the webview cannot supply scan evidence. */
export class ManagedContainerHost {
    private busy=false;
    constructor(private core:CoreManager,private state:vscode.Memento,private runtime:string){}
    async build(workspace:string,region:string,progress:(message:string)=>void):Promise<ManagedContainerReference|undefined>{
        if(!vscode.workspace.isTrusted)throw new Error('신뢰할 수 있는 프로젝트에서 배포하세요.');
        if(this.busy)throw new Error('현재 이미지 빌드가 끝난 뒤 다시 시도하세요.');this.busy=true;
        try{
            let connection;
            try{connection=await this.core.getManagedAwsConnection();}
            catch(error){
                const local=await vscode.window.showWarningMessage('AWS 연결 상태를 확인하지 못했습니다. 기존 로컬 이미지 준비는 사용할 수 있습니다.','내 PC의 Docker 사용');
                if(local!=='내 PC의 Docker 사용')throw error;
                return;
            }
            if(!connection)return;
            const choice=await vscode.window.showQuickPick([
                {label:'AWS에서 이미지 준비',description:'Docker 설치 없이 빌드·보안 검사·SBOM 생성',cloud:true},
                {label:'내 PC의 Docker 사용',description:'기존 로컬 빌드 및 검사 사용',cloud:false},
            ],{title:'ECS에 배포할 이미지 준비'});
            if(!choice)throw new Error('이미지 준비를 취소했습니다.');if(!choice.cloud)return;
            const {metadata,client}=connection;
            if(metadata.region!==region)throw new Error('연결된 AWS 리전과 배포 리전을 맞춰 주세요.');
            const jobs=new ManagedJobs(client,metadata.account,'container');
            // Validate the snapshot before creating any AWS resources.
            progress('업로드할 이미지 소스 준비');const id='rc-rt-'+randomBytes(12).toString('hex');
            const bundle=await containerBundle(workspace,this.runtime,id);
            const key='deploy.managed-container.'+hash(`${metadata.account}:${region}:${workspace}`);
            let prior=this.state.get<ManagedRun>(key);
            if(prior&&!prior.terminal){
                prior=(await jobs.refresh(prior,false)).run;await this.state.update(key,prior);
                if(!prior.terminal){
                    const action=await vscode.window.showWarningMessage('이 프로젝트의 이전 이미지 준비가 아직 끝나지 않았습니다.',{modal:true,detail:'확장을 다시 열어도 같은 AWS 작업을 확인합니다. 중단 요청은 실제 종료까지 기다려야 합니다.'},'이전 작업 중단 요청');
                    if(action==='이전 작업 중단 요청')await jobs.stop(prior);
                    throw new Error('이전 이미지 준비가 종료된 뒤 다시 시도하세요.');
                }
                await jobs.deleteSource(prior).catch(()=>{});
            }
            let setup=await jobs.status();
            if(!setup.ready&&setup.stackStatus==='NOT_CREATED'){
                const approved=await vscode.window.showWarningMessage('내 AWS에 컨테이너 빌드 환경을 만들까요?',{modal:true,detail:`${metadata.account} · ${region}\n전용 CodeBuild·IAM·비공개 S3·ECR·작업 잠금·로그를 만듭니다. 빌드 사용료와 저장·요청 비용이 발생합니다. 동시 1개·최대 60분입니다. 소스는 1일, 검사 결과·로그는 7일 후 만료됩니다. ECR 이미지는 롤백을 위해 자동 삭제하지 않으며, 스택을 삭제해도 저장소는 보존됩니다.`},'환경 만들기');
                if(approved!=='환경 만들기')throw new Error('이미지 빌드 환경 설치를 취소했습니다.');
                await jobs.provision();setup={ready:false,stackStatus:'CREATE_IN_PROGRESS'};
            }
            return await vscode.window.withProgress({location:vscode.ProgressLocation.Notification,title:'ReCoder · AWS 이미지 준비',cancellable:true},async(notification,cancel)=>{
                const stage=(message:string)=>{progress(message);notification.report({message});};let attempts=0;
                while(!setup.ready){
                    if(cancel.isCancellationRequested)throw new Error('대기를 취소했습니다. 이미 시작한 AWS 환경 설치는 계속 진행됩니다.');
                    if(!/^(CREATE|UPDATE)_IN_PROGRESS$/.test(setup.stackStatus)||attempts++>=120)throw new Error(`이미지 환경 상태: ${setup.stackStatus}. CloudFormation 이벤트를 확인하세요.`);
                    stage('AWS 이미지 빌드 환경 준비 중');await new Promise(r=>setTimeout(r,5000));setup=await jobs.status();
                }
                if(cancel.isCancellationRequested)throw new Error('이미지 준비를 취소했습니다.');
                await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({language:'plaintext',content:`AWS 이미지 빌드 업로드 검토\n${metadata.account} · ${region}\nSHA256 ${bundle.digest}\n.env·키 파일·node_modules 제외. 소스에 직접 작성된 비밀값의 제외는 보장하지 않습니다.\n\n${bundle.files.join('\n')}`}),{preview:true});
                const approved=await vscode.window.showWarningMessage('이 프로젝트 사본으로 이미지를 준비할까요?',{modal:true,detail:`${bundle.files.length}개 파일 · ${(bundle.bytes/1024/1024).toFixed(1)}MB\n소스 검사 → 격리 빌드 → 이미지 검사 → SBOM·정책 확인 → 내 ECR 저장 순서로 실행합니다. 프로젝트에 AWS 자격증명이나 로컬 .env를 전달하지 않습니다. CodeBuild·ECR 비용이 발생하며 창을 닫아도 최대 60분간 실행됩니다. 실제 ECS 배포는 다음 승인 단계에서 진행합니다.`},'업로드·비용 확인 후 준비');
                if(approved!=='업로드·비용 확인 후 준비'||cancel.isCancellationRequested)throw new Error('이미지 준비를 취소했습니다.');
                let run:ManagedRun={id,createdAt:new Date().toISOString(),digest:bundle.digest,files:bundle.files.length,bytes:bundle.bytes,status:'SUBMITTING',uncertain:true,submission:'uploading'};
                await this.state.update(key,run);
                try{run.buildId=await jobs.start(run,bundle,async()=>{run.submission='starting';await this.state.update(key,run);});run.status='IN_PROGRESS';run.uncertain=false;}
                finally{await this.state.update(key,run);}
                stage('AWS 이미지 작업 시작');let stopRequested=false;const deadline=Date.now()+70*60*1000;
                while(!run.terminal){
                    if(cancel.isCancellationRequested&&!stopRequested){await jobs.stop(run);stopRequested=true;stage('이미지 작업 중단 확인 중');}
                    if(Date.now()>deadline)throw new Error('이미지 준비 확인 시간이 초과되었습니다. 다시 준비하면 이전 작업부터 확인합니다.');
                    await new Promise(r=>setTimeout(r,5000));run=(await jobs.refresh(run,false)).run;await this.state.update(key,run);
                    if(!stopRequested){
                        let message=run.stage||run.status;
                        try{const p=JSON.parse(await client.object(jobs.bucket,`reports/${run.id}/progress.json`,'GET'));if(p.runId===run.id&&p.sourceSha256===run.digest&&typeof p.stage==='string')message=p.stage.slice(0,200);}catch{}
                        stage(message);
                    }
                }
                await jobs.deleteSource(run).catch(()=>{});
                if(run.status!=='SUCCEEDED'){
                    let reason='';
                    try{const failed=JSON.parse(await client.object(jobs.bucket,`reports/${run.id}/report.json`,'GET'));if(failed.runId===run.id&&failed.sourceSha256===run.digest&&failed.buildId===run.buildId&&typeof failed.stage==='string')reason=' '+failed.stage.slice(0,200);}catch{}
                    throw new Error(`AWS 이미지 준비가 완료되지 않았습니다 (${run.status}).${reason} 기존 서비스는 변경되지 않았습니다.`);
                }
                if(cancel.isCancellationRequested)throw new Error('이미지 준비를 취소했습니다.');
                const ref={run_id:run.id,build_id:run.buildId!,source_sha256:run.digest};
                containerReport(JSON.parse(await client.object(jobs.bucket,`reports/${run.id}/report.json`,'GET')),ref,metadata.account,region);
                stage('이미지 검사 완료 · 배포 내용 확인');return ref;
            });
        }catch(e){
            if(e instanceof AwsRequestError&&/AccessDenied|Unauthorized/.test(e.code))throw new Error('AWS 컨테이너 빌드 권한이 없습니다. ReCoder: 관리형 AWS 권한 보기 명령에서 설치용·실행용 정책을 확인하세요.');
            throw e;
        }finally{this.busy=false;}
    }
}
