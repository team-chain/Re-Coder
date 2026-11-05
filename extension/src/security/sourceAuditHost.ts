import * as vscode from 'vscode';
import {promises as fs} from 'fs';
import * as path from 'path';
import {createHash,randomBytes} from 'crypto';
import {ApiClient} from '../core/ApiClient';
import {AUDIT_REVISION,auditFiles,auditSource,AuditResult} from './managedAudit';

interface AuditState {phase:string;stage:string;createdAt?:string;finishedAt?:string;calls?:number;provider?:string;model?:string;stale?:boolean;error?:string;report?:AuditResult}
const digest=(value:unknown)=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
const ACTIVE=new Set(['preparing','running','stopping']);
/** Additional read-only feature; shares Develop's Core client and existing credentials. */
export class SourceAuditHost {
    private states=new Map<string,AuditState>();
    private active?:{id:string;workspace:string;stop:boolean};
    private writes:Promise<unknown>=Promise.resolve();
    constructor(private api:Pick<ApiClient,'securityAuditStep'>,private memory:vscode.Memento,private assets:string){}
    private key(workspace:string){return 'security.sourceAudit.'+digest(workspace);}
    private async save(workspace:string,state:AuditState){
        this.states.set(workspace,state);
        const snapshot=JSON.parse(JSON.stringify(state));
        this.writes=this.writes.catch(()=>{}).then(()=>this.memory.update(this.key(workspace),snapshot));
        await this.writes;
    }
    async handle(action:string,workspace:string):Promise<AuditState>{
        if(!workspace||!vscode.workspace.isTrusted)throw new Error('신뢰할 수 있는 프로젝트를 먼저 여세요.');
        if(!['status','start','stop'].includes(action))throw new Error('지원하지 않는 감사 요청입니다.');
        if(!this.states.has(workspace)){
            const previous=this.memory.get<AuditState>(this.key(workspace));
            const state=previous&&typeof previous.phase==='string'?previous:{phase:'idle',stage:'현재 AI 연결로 소스를 검사합니다.'};
            this.states.set(workspace,ACTIVE.has(state.phase)?{...state,phase:'interrupted',stage:'이전 감사가 중단됐습니다. 다시 검토 후 실행하세요.',report:undefined}:state);
        }
        if(action==='stop'&&this.active?.workspace===workspace){
            this.active.stop=true;
            await this.save(workspace,{...this.states.get(workspace)!,phase:'stopping',stage:'현재 AI 응답을 기다린 뒤 중단합니다. 추가 요청은 보내지 않습니다.'});
        }
        if(action!=='start')return this.states.get(workspace)!;
        if(this.active)throw new Error('진행 중인 AI 감사를 먼저 완료하거나 중단하세요.');
        const job={id:randomBytes(12).toString('hex'),workspace,stop:false};this.active=job;
        const start={phase:'preparing',stage:'감사할 소스와 제외 범위 확인',createdAt:new Date().toISOString()};
        let launched=false;
        try{
            await this.save(workspace,start);
            const sources=await auditFiles(workspace),sourceDigest=digest(sources.files);
            const revision=(await fs.readFile(path.join(this.assets,'REVISION'),'utf8')).trim();
            if(revision!==AUDIT_REVISION)throw new Error('감사 문서 버전이 일치하지 않습니다. 확장을 업데이트하세요.');
            const guide=await fs.readFile(path.join(this.assets,'SKILL.md'),'utf8');
            const approved=await vscode.window.showInformationMessage('현재 AI 연결로 코드 감사를 진행할까요?',{modal:true,detail:
                `${sources.files.length}개 파일 · 제외 ${sources.skipped.length}개\n${sources.files.slice(0,30).map(f=>f.path).join('\n')}${sources.files.length>30?'\n…':''}\n\nDevelop에서 사용하는 AI에 선택된 소스가 전달됩니다. 파일을 변경하거나 앱을 실행하지 않습니다. 최대 20회 모델 호출·15분이며 기존 모델 사용료가 발생할 수 있습니다. .env·키 파일·의존성·빌드 출력은 제외합니다. 하드코딩한 모든 비밀값을 자동으로 찾는다고 보장하지는 않습니다.`},'검토한 파일 감사');
            if(approved!=='검토한 파일 감사'||job.stop){await this.save(workspace,{...start,phase:'cancelled',stage:'감사를 취소했습니다.'});this.active=undefined;return this.states.get(workspace)!;}
            await this.save(workspace,{...start,phase:'running',stage:'소스 구조 분석 시작',calls:0});
            launched=true;void this.run(job,sources,sourceDigest,guide).catch(()=>{
                this.states.set(workspace,{...start,phase:'failed',stage:'감사 기록을 저장하지 못했습니다. 다시 상태를 확인하세요.'});
                if(this.active===job)this.active=undefined;
            });
        }catch(error){
            const state={...start,phase:'failed',stage:'감사 준비 실패',error:error instanceof Error?error.message:'감사 준비 실패'};
            try{await this.save(workspace,state);}catch{this.states.set(workspace,{...state,error:'감사 기록을 저장하지 못했습니다. 저장 공간을 확인하세요.'});}
        }finally{if(!launched&&this.active===job)this.active=undefined;}
        return this.states.get(workspace)!;
    }
    private async run(job:{id:string;workspace:string;stop:boolean},sources:Awaited<ReturnType<typeof auditFiles>>,sourceDigest:string,guide:string){
        let calls=0,inputTokens=0,outputTokens=0,provider='',model='';const deadline=Date.now()+15*60*1000;
        const check=()=>{if(job.stop||this.active!==job)throw new Error('AUDIT_CANCELLED');if(Date.now()>deadline)throw new Error('감사 시간 제한에 도달했습니다.');};
        try{
            const report=await auditSource(sources.files,sources.skipped,async(role,instructions,data)=>{
                check();calls++;await this.save(job.workspace,{...this.states.get(job.workspace)!,calls});check();
                const reply=await this.api.securityAuditStep(role,instructions,data);check();
                provider=reply.provider;model=reply.model;inputTokens+=reply.inputTokens||0;outputTokens+=reply.outputTokens||0;
                return reply.result;
            },async stage=>{check();await this.save(job.workspace,{...this.states.get(job.workspace)!,phase:'running',stage,calls,provider,model});},guide);
            check();let stale=true;
            try{stale=digest((await auditFiles(job.workspace)).files)!==sourceDigest;}catch{/* source changed or became unreadable */}
            check();await this.save(job.workspace,{...this.states.get(job.workspace)!,phase:'completed',stage:'감사 완료 · 소스 근거와 수정 방향을 확인하세요.',finishedAt:new Date().toISOString(),calls,provider,model,stale,report:{...report,inputTokens,outputTokens}});
        }catch(error){
            const cancelled=job.stop;
            await this.save(job.workspace,{...this.states.get(job.workspace)!,phase:cancelled?'cancelled':'failed',stage:cancelled?'감사를 중단했습니다.':'감사를 완료하지 못했습니다.',error:cancelled?undefined:error instanceof Error?error.message:'AI 감사 실패',report:undefined,calls});
        }finally{if(this.active===job)this.active=undefined;}
    }
}
