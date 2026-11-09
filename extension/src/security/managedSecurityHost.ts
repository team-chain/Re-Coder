import * as vscode from 'vscode';
import {createHash,randomBytes} from 'crypto';
import {CoreManager} from '../core/CoreManager';
import {AwsRequestError} from '../cloud/managedAwsClient';
import {ManagedJobs,ManagedRun} from './managedJobs';
import {projectBundle} from './managedBundle';
import {validateManagedInput} from './managedPentagiWorker';
import {ManagedMode,ManagedReport} from './managedReport';

export class ManagedSecurityHost {
    private busy=false;
    constructor(private core:CoreManager,private state:vscode.Memento,private runtimeDir:string){}
    async handle(action:string,workspace:string,mode:ManagedMode='redteam'):Promise<Record<string,unknown>> {
        if(!vscode.workspace.isTrusted||!workspace)throw new Error('신뢰할 수 있는 프로젝트를 먼저 여세요.');
        if(!['redteam','audit','scans'].includes(mode))throw new Error('검사 종류가 올바르지 않습니다.');
        if(action==='managedAuditStart')mode='audit';else if(action==='managedScansStart')mode='scans';else if(action==='managedStart')mode='redteam';
        if(this.busy)throw new Error('이전 요청이 끝난 뒤 다시 시도하세요.');this.busy=true;
        try {
            if(action==='managedPermissions')await vscode.commands.executeCommand('recoder.managedAwsPermissions');
            const connection=await this.core.getManagedAwsConnection();
            if(!connection)return {managed:true,connected:false};
            const {metadata,client}=connection, jobs=new ManagedJobs(client,metadata.account);
            const key='security.managed.'+createHash('sha256').update(`${metadata.account}:${metadata.region}:${workspace}`).digest('hex');
            let runs=this.state.get<ManagedRun[]>(key,[]);if(!Array.isArray(runs))throw new Error('검사 이력을 읽지 못했습니다.');
            let setup:{ready:boolean;stackStatus:string};
            try{setup=await jobs.status();}
            catch(e){if(e instanceof AwsRequestError&&/AccessDenied|Unauthorized/.test(e.code))return {managed:true,connected:true,account:metadata.account,region:metadata.region,ready:false,stackStatus:'PERMISSION_REQUIRED',runs,permissionMessage:'연결된 AWS 사용자에 관리형 실행 권한이 없습니다. AWS 권한 보기에서 이 계정·리전에 맞춘 설치용·실행용 정책을 확인하세요.'};throw e;}
            if(action==='managedReport'){
                const selected=await vscode.window.showQuickPick(runs.filter(r=>(r.mode||'redteam')===mode).map(r=>({label:`${r.createdAt} · ${r.mode||'redteam'} · ${r.status}`,description:r.id,run:r})),{title:'보안 검사 보고서'});
                if(!selected)return {cancelled:true};
                const result=await jobs.refresh(selected.run);
                if(!result.report)throw new Error('저장된 보고서가 없거나 보관 기한이 지났습니다.');
                await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({language:'json',content:JSON.stringify({sourceSha256:selected.run.digest,run:result.run,report:result.report},null,2)}),{preview:true});
            }
            if(action==='managedSetup'&&!setup.ready){
                if(setup.stackStatus!=='NOT_CREATED')throw new Error(`검사 환경 상태: ${setup.stackStatus}. 준비가 끝난 뒤 상태를 확인하세요.`);
                const accepted=await vscode.window.showWarningMessage('AWS에 관리형 검사 환경을 준비할까요?',{modal:true,detail:
                    `계정 ${metadata.account} · ${metadata.region}\n\n검사 전용 CodeBuild·IAM 역할·비공개 S3 저장소·DynamoDB 작업 잠금·로그를 만듭니다. 작업마다 CodeBuild·Bedrock 사용료와 저장·요청 비용이 발생합니다. 동시 작업 1개, 작업당 최대 60분입니다. 소스 사본은 1일, 결과·로그는 7일 뒤 만료합니다. 저장소는 스택 삭제 시에도 남습니다. 다른 배포 리소스의 권한은 작업 역할에 주지 않습니다.`},'비용·권한 확인 후 준비');
                if(accepted!=='비용·권한 확인 후 준비')return {cancelled:true};await jobs.provision();setup={ready:false,stackStatus:'CREATE_IN_PROGRESS'};
            }
            if(action==='managedStart'||action==='managedAuditStart'||action==='managedScansStart'){
                if(!setup.ready)throw new Error('관리형 검사 환경을 먼저 준비하세요.');
                if(runs.some(r=>!r.terminal))throw new Error('진행 중이거나 실행 여부가 확인되지 않은 작업이 있습니다. 먼저 상태를 확인하세요.');
                const mode=action==='managedAuditStart'?'audit':action==='managedScansStart'?'scans':'redteam';
                const portText=mode!=='redteam'?'3000':await vscode.window.showInputBox({title:'검사 사본의 앱 포트',value:'3000',prompt:'Dockerfile로 실행할 앱의 컨테이너 포트',validateInput:v=>/^\d+$/.test(v)&&+v>0&&+v<=65535?null:'1~65535 포트 번호를 입력하세요.'});if(!portText)return {cancelled:true};
                const model=mode==='scans'?'사용 안 함':await vscode.window.showInputBox({title:'검사용 Bedrock 모델',value:'global.anthropic.claude-haiku-4-5-20251001-v1:0',prompt:'연결된 계정에서 사용할 수 있는 Claude 모델 또는 inference profile ID'});if(!model)return {cancelled:true};
                const request=validateManagedInput({id:'rc-rt-'+randomBytes(12).toString('hex'),mode,port:Number(portText),model:model.trim()});
                const bundle=await projectBundle(workspace,this.runtimeDir,request);
                await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({language:'plaintext',content:`AWS 업로드 검토 · ${metadata.account} · ${metadata.region}\nSHA256 ${bundle.digest}\n${bundle.files.length}개 파일 · ${bundle.bytes} bytes\n.env·키 파일·숨김 폴더·node_modules는 제외합니다. 소스 안에 직접 쓴 비밀값이 없는지 검토하세요.\n\n${bundle.files.join('\n')}`}),{preview:true});
                const scope=mode==='audit'?'소스를 내 AWS 저장소와 Bedrock에 전송해 읽기 전용 감사를 합니다. 최대 100개 파일·160KB 범위를 읽고 제외 범위를 보고합니다. 최대 20회 모델 호출이며 실행 검증을 하지 않습니다.':mode==='scans'?'소스를 내 AWS 저장소에 올려 시크릿·의존성·Dockerfile을 검사합니다. Dockerfile이 있으면 격리된 엔진에서 빌드하고 이미지를 검사합니다. 외부 의존성 다운로드가 발생하며 운영 DB·환경변수·자격증명은 전달하지 않습니다. AI 모델은 사용하지 않습니다.':'소스를 내 AWS 저장소에 올리고 Dockerfile의 빌드 명령을 실행합니다. 앱 실행과 검사는 격리된 사본만 대상으로 합니다. 운영 DB·환경변수·자격증명은 전달하지 않습니다. 앱 응답 일부는 Amazon Bedrock으로 전송됩니다.';
                const accepted=await vscode.window.showWarningMessage(mode==='audit'?'이 프로젝트 사본으로 AI 코드 감사를 시작할까요?':mode==='scans'?'이 프로젝트 사본으로 기본 보안 검사를 시작할까요?':'이 프로젝트 사본으로 레드팀 검사를 시작할까요?',{modal:true,detail:
                    `${metadata.account} · ${metadata.region}\n${bundle.files.length}개 파일 · ${(bundle.bytes/1024/1024).toFixed(1)}MB\n모델: ${request.model||'사용 안 함'}\n\n${scope} 컴퓨팅 및 선택한 모델 비용이 발생하며 달러 기준 자동 상한은 없습니다. VS Code를 닫아도 최대 60분간 작업이 이어질 수 있습니다.\n${mode==='redteam'?'PentAGI EULA: https://github.com/vxcontrol/pentagi/blob/main/EULA.md':mode==='audit'?'Cloudflare security-audit MIT 기반 ReCoder 어댑터 · 전체 감사 인증이 아닙니다.':''}`},'범위·업로드·비용에 동의하고 검사');
                if(accepted!=='범위·업로드·비용에 동의하고 검사')return {cancelled:true};
                const run:ManagedRun={id:request.id,mode,submission:'uploading',createdAt:new Date().toISOString(),digest:bundle.digest,files:bundle.files.length,bytes:bundle.bytes,status:'SUBMITTING',uncertain:true};
                runs=[run,...runs].slice(0,20);await this.state.update(key,runs);
                try {run.buildId=await jobs.start(run,bundle,async()=>{run.submission='starting';await this.state.update(key,runs);});run.uncertain=false;run.status='IN_PROGRESS';}
                finally{await this.state.update(key,runs);}
            }
            let report:ManagedReport|undefined,reportError:string|undefined;
            if(setup.ready){
                const activeIndex=runs.findIndex(r=>!r.terminal);
                if(activeIndex>=0){
                    const active=runs[activeIndex];if(action==='managedStop'){await jobs.stop(active);active.stopRequested=true;await this.state.update(key,runs);}
                    const current=await jobs.refresh(active,false);runs[activeIndex]=current.run;
                    if(current.run.terminal)await jobs.deleteSource(current.run).catch(()=>{});
                }
                const index=runs.findIndex(r=>(r.mode||'redteam')===mode);
                if(index>=0){const current=await jobs.refresh(runs[index],false);runs[index]=current.run;if(current.run.terminal)await jobs.deleteSource(current.run).catch(()=>{});}
                // An unreadable report must not lose an authoritative terminal compute status.
                await this.state.update(key,runs);
                if(index>=0){try{report=await jobs.readReport(runs[index]);}catch{reportError='보고서를 읽거나 검증하지 못했습니다. 검사 통과로 처리하지 않습니다. 상태 확인으로 다시 읽을 수 있습니다.';}}
            }
            return {managed:true,connected:true,account:metadata.account,region:metadata.region,...setup,runs:runs.filter(r=>(r.mode||'redteam')===mode),activeRun:runs.find(r=>!r.terminal),report,reportError};
        } finally {this.busy=false;}
    }
}
