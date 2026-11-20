import * as vscode from 'vscode';
import {spawn} from 'child_process';
import {randomBytes} from 'crypto';
import {promises as fs} from 'fs';
import * as path from 'path';
import {AI_KEY_SECRET} from '../ai/aiKeyEnv';
import {LocalPentagiRuntime, LocalProvider, LocalReport, localTarget, readReport, saveReport, sessionPaths, localAssessment, docker, engineCacheWarm, clearEngineCache, cacheMarker} from './localPentagi';
import {prewarmPaths, readPrewarm, removePrewarmEngine} from './redTeamPrewarm';
import {assessmentMarkdown} from './pentagiAssessment';
import type {PentagiAssessment} from './pentagiAssessmentTypes';
import {AccessPolicy,accessKey,validateAccessPolicy} from './redTeamAccess';
import {configureAccess} from './redTeamAccessHost';
import { ui } from '../dialogs/inAppDialogs';

function alive(pid?:number) {if(!pid) return false; try {process.kill(pid,0);return true;} catch {return false;}}
const providerOptions: Array<vscode.QuickPickItem & {provider:LocalProvider}> = [
    {label:'OpenAI',description:'저장된 키 사용 또는 별도 키 입력',provider:'openai'},
    {label:'Anthropic',description:'저장된 키 사용 또는 별도 키 입력',provider:'anthropic'},
];
const localKeyName = (provider:LocalProvider) => `recoder.pentagi.${provider}Key`;
//: 이 VS Code 실행 동안 "나중에" 를 고른 경우 다시 묻지 않는다.
let prewarmOfferSnoozed=false;
const PREWARM_OFFER='공격 테스트 도구(17GB)를 미리 받아 둘까요?';
const WORKER_ENV_KEYS=['PATH','Path','SystemRoot','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','HOME','LOCALAPPDATA','APPDATA','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG','DOCKER_CERT_PATH','DOCKER_TLS_VERIFY','HTTP_PROXY','HTTPS_PROXY','NO_PROXY'];
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
    /**
     * 보안 화면 카드용 — 이 프로젝트의 마지막 공격 테스트 결과만 저장 파일에서 읽는다.
     * Docker·PentAGI·네트워크를 건드리지 않고 상태를 바꾸지 않는다(검사 잠금과 겹치지 않게 handle 밖에 둔다).
     */
    async summary(workspace:string):Promise<{local:null|{phase:string;outcome?:string;updatedAt:string;assessment?:PentagiAssessment}}> {
        let current:LocalReport|undefined;
        try {current=(await this.reports(workspace))[0];} catch {return {local:null};}
        if(!current) return {local:null};
        const done=['removed','cleanup_failed'].includes(current.phase);
        return {local:{phase:current.phase,outcome:current.outcome,updatedAt:current.updatedAt,assessment:current.assessment??(done?localAssessment(current):undefined)}};
    }
    /** 분리된 작업 프로세스 — VS Code 를 닫아도 이어진다. 입력은 stdin 으로만 넘긴다. */
    private async spawnWorker(payload:Record<string,unknown>):Promise<number|undefined> {
        const env:NodeJS.ProcessEnv={ELECTRON_RUN_AS_NODE:'1'};
        // Avoid inheriting unrelated cloud/model credentials into a detached process.
        for(const key of WORKER_ENV_KEYS) if(process.env[key]) env[key]=process.env[key];
        const child=spawn(process.execPath,[path.join(__dirname,'localPentagiWorker.js')],{detached:true,windowsHide:true,env,stdio:['pipe','ignore','ignore']});
        await new Promise<void>((resolve,reject)=>{child.once('spawn',resolve);child.once('error',()=>reject(new Error('검사 작업 프로세스를 시작하지 못했습니다.')));});
        child.stdin.on('error',()=>{});
        child.stdin.end(JSON.stringify(payload));child.unref();
        return child.pid;
    }
    private async startPrewarm():Promise<void> {
        if(await readPrewarm(this.root).then(s=>s?.phase==='running'))return;
        if((await this.reports()).some(r=>r.phase!=='removed')) throw new Error('공격 테스트가 진행 중이거나 정리가 필요합니다. 끝난 뒤 미리 받을 수 있습니다.');
        await removePrewarmEngine(docker).catch(()=>{});
        //: 작업 프로세스가 첫 기록을 쓰기 전에 상태를 물어도 "받는 중" 으로 보이게 한다(곧 작업 프로세스 기록으로 바뀐다).
        const file=prewarmPaths(this.root).state;
        await fs.writeFile(file,JSON.stringify({phase:'running',step:'미리 받기 시작',pid:process.pid,updatedAt:new Date().toISOString()}),{mode:0o600});
        try {await this.spawnWorker({mode:'prewarm',root:this.root});}
        catch(error) {await fs.writeFile(file,JSON.stringify({phase:'failed',step:'미리 받기 시작 실패',error:'검사 작업 프로세스를 시작하지 못했습니다.',updatedAt:new Date().toISOString()}),{mode:0o600});throw error;}
    }
    async handle(action:string, workspace:string, options?:Record<string,unknown>):Promise<Record<string,unknown>> {
        if(!this.root || !this.secrets) throw new Error('검사 환경 저장소를 사용할 수 없습니다.');
        await fs.mkdir(this.root,{recursive:true});
        let reports=await this.reports(workspace); let current=reports[0];
        if(action==='localPrewarmOffer') {
            //: 로컬 실행을 마쳐 다음 단계가 공격 테스트일 때 한 번 묻는다. 동의 없이 17GB 를 받지 않는다.
            const dismissed=await fs.stat(path.join(this.root,'prewarm-offer.json')).then(()=>true,()=>false);
            if(prewarmOfferSnoozed||dismissed||(await readPrewarm(this.root))?.phase==='running'||(await this.reports()).some(r=>r.phase!=='removed')||await engineCacheWarm(this.root,docker)) return {skipped:true};
            prewarmOfferSnoozed=true;
            const choice=await ui.showInformationMessage(PREWARM_OFFER,{detail:'받는 동안 다른 작업을 할 수 있고, 다음 테스트가 바로 시작돼요.'},'미리 받기','나중에','다시 묻지 않기');
            if(choice==='다시 묻지 않기') await fs.writeFile(path.join(this.root,'prewarm-offer.json'),JSON.stringify({dismissed:true}),{mode:0o600});
            if(choice!=='미리 받기') return {skipped:true};
            await this.startPrewarm();
        }
        if(action==='localPrewarm') await this.startPrewarm();
        if(action==='localPrewarmStop') {
            if((await readPrewarm(this.root))?.phase==='running') await fs.writeFile(prewarmPaths(this.root).cancel,'stop',{mode:0o600});
        }
        if(action==='localCacheClear') {
            if((await readPrewarm(this.root))?.phase==='running'||(await this.reports()).some(r=>r.phase!=='removed')) throw new Error('공격 테스트나 미리 받기가 끝난 뒤 비울 수 있습니다.');
            const ok=await ui.showWarningMessage('검사 도구(17GB)를 지울까요?',{modal:true,detail:'다음 테스트 때 다시 받아요. 검사 결과는 남아요.'},'지우기');
            if(ok!=='지우기') return {cancelled:true};
            await clearEngineCache(docker);
            await fs.rm(cacheMarker(this.root),{force:true});
        }
        if(action==='localAccessConfigure') {
            if(!await configureAccess(this.secrets,workspace,current?.target))return {cancelled:true};
        }
        if(action==='localAccessClear')await this.secrets.delete(accessKey(workspace));
        if(action==='localKey') {
            const choice=await ui.showQuickPick(providerOptions,{title:'레드팀 모델 공급자 선택'});
            if(!choice) return {cancelled:true};
            const key=await ui.showInputBox({title:`레드팀용 ${choice.label} API 키 변경`,prompt:'ReCoder의 개발 AI 설정은 바뀌지 않습니다. VS Code 보안 저장소에 저장되어 다음 검사부터 적용됩니다.',password:true,ignoreFocusOut:true});
            if(!key) return {cancelled:true};
            if(key.trim().length>4096 || /[\r\n]/.test(key)) throw new Error('API 키 형식을 확인하세요.');
            await this.secrets.store(localKeyName(choice.provider),key.trim());
        }
        if(action==='localStart') {
            if(reports.some(r=>r.phase!=='removed')) throw new Error('이전 검사 환경을 먼저 중단하거나 정리하세요.');
            if((await readPrewarm(this.root))?.phase==='running') throw new Error('검사 도구를 미리 받는 중입니다. 끝나면 바로 시작할 수 있습니다. 지금 시작하려면 미리 받기를 중단하세요.');
            await removePrewarmEngine(docker).catch(()=>{});
            if((await this.reports()).some(r=>r.phase!=='removed')) throw new Error('다른 프로젝트의 검사 환경이 남아 있습니다. 해당 프로젝트에서 중단하거나 정리하세요. 로컬 검사는 한 번에 하나만 실행합니다.');
            //: ReCoder 화면의 시작 카드(대상·모델·동의)를 거친 요청이면 그 값으로 바로 진행한다.
            const form=options&&typeof options.target==='string'&&(options.provider==='openai'||options.provider==='anthropic')&&options.agreed===true
                ?{target:options.target,provider:options.provider as LocalProvider}:undefined;
            const input=form?form.target:await ui.showInputBox({title:'내 앱 레드팀 테스트',prompt:'로컬에서 실행 중인 내 테스트 앱 주소와 포트',value:'http://localhost:3000',ignoreFocusOut:true});
            if(!input) return {cancelled:true}; const target=localTarget(input.trim());
            const storedAccess=await this.secrets.get(accessKey(workspace));
            let accessPolicy:AccessPolicy|undefined;
            try {if(storedAccess)accessPolicy=validateAccessPolicy(JSON.parse(storedAccess),target);}
            catch {throw new Error('권한 검사 설정이 현재 대상과 맞지 않거나 손상되었습니다. 권한 검사 설정을 변경하거나 해제하세요.');}
            const choice=form?providerOptions.find(o=>o.provider===form.provider):await ui.showQuickPick(providerOptions,{title:'레드팀 검사 모델 공급자'});
            if(!choice) return {cancelled:true};
            let apiKey=await this.secrets.get(localKeyName(choice.provider)) || await this.secrets.get(AI_KEY_SECRET[choice.provider]);
            if(!apiKey) {
                apiKey=await ui.showInputBox({title:`레드팀용 ${choice.label} API 키`,prompt:'공격 테스트에만 쓰고 VS Code 보안 저장소에 보관해요.',password:true,ignoreFocusOut:true});
                if(!apiKey) return {cancelled:true};
            }
            if(apiKey.trim().length>4096 || /[\r\n]/.test(apiKey)) throw new Error('API 키 형식을 확인하세요.');
            const approved=form?'EULA·범위를 확인했고 검사 시작':await ui.showWarningMessage('PentAGI 레드팀 검사를 시작할까요?',{modal:true,detail:
                `대상: ${target}\n모델: ${choice.label}\n\n내가 소유하거나 명시적으로 검사 권한을 받은 로컬 앱만 검사하세요. PentAGI EULA(https://github.com/vxcontrol/pentagi/blob/main/EULA.md)가 적용됩니다.\n\n공식 PentAGI·PostgreSQL·전용 Docker-in-Docker 이미지를 내려받고 검사 환경을 만듭니다. 검사 도구는 외부 연결이 없는 네트워크에서 실행하고 승인한 앱의 경로만 HTTP로 중계합니다. 최대 300개 요청, GET·HEAD·OPTIONS·POST만 허용합니다. 민감 데이터가 없는 테스트 앱을 사용하세요. 권한 검사 설정이 있으면 테스트 사용자별 소유 리소스와 비로그인·교차 접근을 GET 요청 6개로 대조합니다. 인증값은 모델에 전달하지 않습니다. Docker Desktop Linux 엔진에 메모리 6GB·CPU 2개, 디스크 여유 공간 50GB(검사 도구를 이미 받아 두었으면 20GB)가 필요합니다. 전용 엔진은 Linux VM 안에서 privileged로 동작하지만 호스트 Docker 소켓과 프로젝트 소스는 전달하지 않습니다. PentAGI가 앱 응답을 선택한 모델 공급자에 보낼 수 있고, 해당 공급자 사용료가 발생합니다. 설치·버전 확인을 위해 PentAGI가 비식별 설치 요약을 업데이트 서비스에 보낼 수 있습니다.\n\n환경 준비는 최대 60분, 검사는 최대 30분입니다. 결과를 저장한 뒤 전용 컨테이너·볼륨·네트워크를 삭제합니다. 검사 도구 이미지(약 17GB)는 다음 검사가 다시 받지 않도록 Docker 안에 보관하며, 보안 화면에서 비울 수 있습니다. VS Code를 닫아도 작업은 이어집니다.`},'EULA·범위를 확인했고 검사 시작');
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
                try {
                    current.pid=await this.spawnWorker({root:this.root,session,workspace,target,apiKey:apiKey.trim(),provider:choice.provider,accessPolicy});
                    await saveReport(this.root,current);
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
            if(current) await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({language:'markdown',content:assessmentMarkdown(localAssessment(current),current)}),{preview:true});
        } else if(action==='localOpenRawReport') {
            if(current) await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({language:'json',content:JSON.stringify(current,null,2)}),{preview:true});
        }
        reports=await this.reports(workspace);current=reports[0];
        // Abnormal OS termination: cleanup is recoverable on next launch; never claim it was removed.
        if(current && !['removed','cleanup_failed'].includes(current.phase) && !alive(current.pid) && Date.now()-Date.parse(current.updatedAt)>30000) {
            current.phase='cleanup_failed';current.outcome='interrupted';current.step='작업이 예기치 않게 종료되었습니다. 환경 정리가 필요합니다.';
            await saveReport(this.root,current);
        }
        const prewarm=await readPrewarm(this.root);
        const engine={warm:await engineCacheWarm(this.root,docker),prewarm:prewarm?{phase:prewarm.phase,step:prewarm.step,error:prewarm.error,updatedAt:prewarm.updatedAt}:null};
        const keys:Record<LocalProvider,boolean>={openai:false,anthropic:false};
        for(const option of providerOptions) keys[option.provider]=!!(await this.secrets.get(localKeyName(option.provider))||await this.secrets.get(AI_KEY_SECRET[option.provider]));
        return {keys,local:current||null,localHistory:reports.slice(1,20),localMode:true,accessConfigured:!!(await this.secrets.get(accessKey(workspace))),engine};
    }
}
