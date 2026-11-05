import * as vscode from 'vscode';
import {spawn} from 'child_process';
import {randomBytes} from 'crypto';
import {promises as fs} from 'fs';
import * as path from 'path';
import {AI_KEY_SECRET} from '../ai/aiKeyEnv';
import {LocalPentagiRuntime, LocalProvider, LocalReport, localTarget, readReport, saveReport, sessionPaths} from './localPentagi';

function alive(pid?:number) {if(!pid) return false; try {process.kill(pid,0);return true;} catch {return false;}}
const providerOptions: Array<vscode.QuickPickItem & {provider:LocalProvider}> = [
    {label:'OpenAI',description:'저장된 키 사용 또는 별도 키 입력',provider:'openai'},
    {label:'Anthropic',description:'저장된 키 사용 또는 별도 키 입력',provider:'anthropic'},
];
const localKeyName = (provider:LocalProvider) => `recoder.pentagi.${provider}Key`;
export class LocalPentagiHost {
    constructor(private root:string, private secrets:vscode.SecretStorage) {}
    private async reports(workspace?:string) {
        await fs.mkdir(this.root,{recursive:true}); const reports:LocalReport[]=[];
        for(const name of await fs.readdir(this.root)) {
            if(!/^rc-rt-[a-f0-9]{24}$/.test(name)) continue;
            const report=await readReport(this.root,name); // Corrupt journal fails closed, never starts a duplicate.
            if(workspace===undefined || report.workspace===workspace) reports.push(report);
        }
        return reports.sort((a,b)=>b.createdAt.localeCompare(a.createdAt));
    }
    async handle(action:string, workspace:string):Promise<Record<string,unknown>> {
        if(!this.root || !this.secrets) throw new Error('검사 환경 저장소를 사용할 수 없습니다.');
        let reports=await this.reports(workspace); let current=reports[0];
        if(action==='localKey') {
            const choice=await vscode.window.showQuickPick(providerOptions,{title:'레드팀 모델 공급자 선택'});
            if(!choice) return {cancelled:true};
            const key=await vscode.window.showInputBox({title:`레드팀용 ${choice.label} API 키 변경`,prompt:'ReCoder의 개발 AI 설정은 바뀌지 않습니다. VS Code 보안 저장소에 저장되어 다음 검사부터 적용됩니다.',password:true,ignoreFocusOut:true});
            if(!key) return {cancelled:true};
            if(key.trim().length>4096 || /[\r\n]/.test(key)) throw new Error('API 키 형식을 확인하세요.');
            await this.secrets.store(localKeyName(choice.provider),key.trim());
        }
        if(action==='localStart') {
            if(reports.some(r=>r.phase!=='removed')) throw new Error('이전 검사 환경을 먼저 중단하거나 정리하세요.');
            if((await this.reports()).some(r=>r.phase!=='removed')) throw new Error('다른 프로젝트의 검사 환경이 남아 있습니다. 해당 프로젝트에서 중단하거나 정리하세요. 로컬 검사는 한 번에 하나만 실행합니다.');
            const input=await vscode.window.showInputBox({title:'내 앱 레드팀 테스트',prompt:'로컬에서 실행 중인 내 테스트 앱 주소와 포트',value:'http://localhost:3000',ignoreFocusOut:true});
            if(!input) return {cancelled:true}; const target=localTarget(input.trim());
            const choice=await vscode.window.showQuickPick(providerOptions,{title:'레드팀 검사 모델 공급자'});
            if(!choice) return {cancelled:true};
            let apiKey=await this.secrets.get(localKeyName(choice.provider)) || await this.secrets.get(AI_KEY_SECRET[choice.provider]);
            if(!apiKey) {
                apiKey=await vscode.window.showInputBox({title:`레드팀용 ${choice.label} API 키`,prompt:'PentAGI 검사 전용입니다. 다음 단계에서 동의하면 VS Code 보안 저장소에 보관합니다. ReCoder 개발 AI 설정은 바뀌지 않습니다.',password:true,ignoreFocusOut:true});
                if(!apiKey) return {cancelled:true};
            }
            if(apiKey.trim().length>4096 || /[\r\n]/.test(apiKey)) throw new Error('API 키 형식을 확인하세요.');
            const approved=await vscode.window.showWarningMessage('PentAGI 레드팀 검사를 시작할까요?',{modal:true,detail:
                `대상: ${target}\n모델: ${choice.label}\n\n내가 소유하거나 명시적으로 검사 권한을 받은 로컬 앱만 검사하세요. PentAGI EULA(https://github.com/vxcontrol/pentagi/blob/main/EULA.md)가 적용됩니다.\n\n공식 PentAGI·PostgreSQL·전용 Docker-in-Docker 이미지를 내려받고 검사 환경을 만듭니다. 중요: 입력한 대상은 localhost로 제한하지만, PentAGI 에이전트의 인터넷·네트워크 접근을 방화벽으로 대상에 한정하지는 않습니다. 프롬프트의 범위 지시는 강제 격리가 아닙니다. 운영·공유망이 아닌, 민감 데이터가 없는 격리된 테스트 환경에서만 진행하세요. Docker Desktop Linux 엔진에 메모리 6GB·CPU 2개, 디스크 여유 공간 25GB가 필요합니다. 전용 엔진은 Linux VM 안에서 privileged로 동작하지만 호스트 Docker 소켓과 프로젝트 소스는 전달하지 않습니다. PentAGI가 앱 응답을 선택한 모델 공급자에 보낼 수 있고, 해당 공급자 사용료가 발생합니다. 설치·버전 확인을 위해 PentAGI가 비식별 설치 요약을 업데이트 서비스에 보낼 수 있습니다.\n\n환경 준비는 최대 60분, 검사는 최대 30분입니다. 결과를 저장한 뒤 전용 컨테이너·볼륨·네트워크를 삭제하고 이미지 캐시는 유지합니다. VS Code를 닫아도 작업은 이어집니다.`},'EULA·범위를 확인했고 검사 시작');
            if(approved!=='EULA·범위를 확인했고 검사 시작') return {cancelled:true};
            // Serialize all windows at the filesystem boundary, after the slow native prompts.
            const lockPath=path.join(this.root,'start.lock');
            let lock;
            try {lock=await fs.open(lockPath,'wx');}
            catch {
                const pid=Number(await fs.readFile(lockPath,'utf8').catch(()=>''));
                if(pid && !alive(pid)) {await fs.unlink(lockPath);lock=await fs.open(lockPath,'wx');}
                else throw new Error('다른 창에서 검사 환경을 준비 중입니다. 잠시 뒤 다시 확인하세요.');
            }
            try {
                await lock.writeFile(String(process.pid));
                reports=await this.reports(workspace);
                if((await this.reports()).some(r=>r.phase!=='removed')) throw new Error('이미 준비하거나 실행 중인 검사가 있습니다.');
                const session='rc-rt-'+randomBytes(12).toString('hex');
                current={session,workspace,target,provider:choice.provider,createdAt:new Date().toISOString(),updatedAt:new Date().toISOString(),phase:'preparing',step:'검사 작업 시작'};
                await saveReport(this.root,current);
                await this.secrets.store(localKeyName(choice.provider),apiKey.trim());
                const env:NodeJS.ProcessEnv={ELECTRON_RUN_AS_NODE:'1'};
                // Avoid inheriting unrelated cloud/model credentials into a detached process.
                for(const key of ['PATH','Path','SystemRoot','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','HOME','LOCALAPPDATA','APPDATA','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG','DOCKER_CERT_PATH','DOCKER_TLS_VERIFY','HTTP_PROXY','HTTPS_PROXY','NO_PROXY']) if(process.env[key]) env[key]=process.env[key];
                try {
                    const child=spawn(process.execPath,[path.join(__dirname,'localPentagiWorker.js')],{detached:true,windowsHide:true,env,stdio:['pipe','ignore','ignore']});
                    await new Promise<void>((resolve,reject)=>{child.once('spawn',resolve);child.once('error',()=>reject(new Error('검사 작업 프로세스를 시작하지 못했습니다.')));});
                    current.pid=child.pid; await saveReport(this.root,current);
                    child.stdin.on('error',()=>{});
                    child.stdin.end(JSON.stringify({root:this.root,session,workspace,target,apiKey:apiKey.trim(),provider:choice.provider}));child.unref();
                } catch {current.phase='cleanup_failed';current.outcome='failed';current.step='작업 시작 실패 · 환경 정리 필요';await saveReport(this.root,current);throw new Error(current.step);}
            } finally {await lock.close();await fs.unlink(lockPath);}
        } else if(action==='localStop') {
            if(current && current.phase!=='removed') await fs.writeFile(sessionPaths(this.root,current.session).cancel,'stop',{mode:0o600});
        } else if(action==='localCleanup') {
            if(current && current.phase!=='removed') {
                if(alive(current.pid)) throw new Error('검사 중단 후 자동 정리가 끝날 때까지 기다리세요.');
                current.outcome??='interrupted'; current.phase='cleaning';await saveReport(this.root,current);
                try {await new LocalPentagiRuntime(current.session).cleanup();current.phase='removed';current.step='검사 환경 삭제 완료';}
                catch {current.phase='cleanup_failed';current.step='Docker를 시작한 뒤 환경 정리를 다시 시도하세요.';}
                await saveReport(this.root,current);
            }
        } else if(action==='localOpenReport') {
            if(current) await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({language:'json',content:JSON.stringify(await readReport(this.root,current.session),null,2)}),{preview:true});
        }
        reports=await this.reports(workspace);current=reports[0];
        // Abnormal OS termination: cleanup is recoverable on next launch; never claim it was removed.
        if(current && !['removed','cleanup_failed'].includes(current.phase) && !alive(current.pid) && Date.now()-Date.parse(current.updatedAt)>30000) {
            current.phase='cleanup_failed';current.outcome='interrupted';current.step='작업이 예기치 않게 종료되었습니다. 환경 정리가 필요합니다.';
            await saveReport(this.root,current);
        }
        return {local:current||null,localHistory:reports.slice(1,20),localMode:true};
    }
}
