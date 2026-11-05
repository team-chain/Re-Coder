/** Validate untrusted worker output before it can drive status or file navigation. */
import type {ManagedFinding,ManagedMode,ManagedReport} from './managedReportTypes';
export type {ManagedMode,ManagedReport} from './managedReportTypes';
const fail=():never=>{throw new Error('검사 보고서의 형식 또는 실행 정보가 올바르지 않습니다. 통과로 처리하지 않습니다.');};
const object=(v:any):any=>v&&typeof v==='object'&&!Array.isArray(v)?v:fail();
const text=(v:any,max=4000):string=>typeof v==='string'&&v.length<=max?v:fail();
const number=(v:any,max=10000000):number=>Number.isSafeInteger(v)&&v>=0&&v<=max?v:fail();
const list=(v:any,max:number):any[]=>Array.isArray(v)&&v.length<=max?v:fail();
const choice=<T extends string>(v:any,values:readonly T[]):T=>values.includes(v)?v:fail();
const relative=(v:any):string=>{const s=text(v,1024);return s&&!s.includes('\\')&&!s.includes(':')&&!s.startsWith('/')&&!s.split('/').some(p=>!p||p==='.'||p==='..')&&!/[\x00-\x1f]/.test(s)?s:fail();};
function finding(v:any):ManagedFinding {
    object(v);return {title:text(v.title,1000),severity:choice(v.severity,['critical','high','medium','low','unknown']),
        ...(v.file===undefined?{}:{file:text(v.file,1024)}),...(v.line===undefined?{}:{line:number(v.line)||fail()}),...(v.fix===undefined?{}:{fix:text(v.fix)})};
}
export function managedReport(raw:unknown,expected:{id:string;digest:string;mode?:ManagedMode}):ManagedReport {
    const v=object(raw);
    if(v.schema!==1||v.runId!==expected.id||v.sourceSha256!==expected.digest||v.mode!==(expected.mode||'redteam'))fail();
    const result:ManagedReport={schema:1,runId:v.runId,sourceSha256:v.sourceSha256,mode:v.mode,
        status:choice(v.status,['preparing','running','completed','incomplete','finalizing','cancelled','failed','needs_input','timed_out','cleanup_failed']),stage:text(v.stage,300),
        tasks:list(v.tasks,500).map(t=>{object(t);return {id:text(t.id,200),title:text(t.title,2000),status:text(t.status,100),result:text(t.result,100000)};})};
    if(v.error!==undefined)result.error=text(v.error);
    for(const key of ['cleaned','isolationVerified'] as const)if(v[key]!==undefined){if(typeof v[key]!=='boolean')fail();result[key]=v[key];}
    if(v.scans!==undefined){
        if(v.mode!=='scans')fail();object(v.scans);const seen=new Set<string>();
        const checks=list(v.scans.checks,4).map(c=>{object(c);const tool=choice(c.tool,['gitleaks','hadolint','dependencies','image']);if(seen.has(tool))fail();seen.add(tool);
            const findings=list(c.findings,200).map(finding),status=choice(c.status,['completed','failed','not_applicable']);
            const count=c.count===undefined?undefined:number(c.count),blockingCount=c.blockingCount===undefined?undefined:number(c.blockingCount);
            if(status==='completed'&&(count===undefined||count<findings.length||blockingCount===undefined||blockingCount>count))fail();
            return {tool,status,findings,count,blockingCount,...(c.message===undefined?{}:{message:text(c.message)})};});
        result.scans={checks};
        if(v.scans.policy!==undefined){const p=object(v.scans.policy);result.scans.policy={decision:choice(p.decision,['allow','deny']),reason:text(p.reason),scope:text(p.scope)};}
        if(v.status==='completed'&&(checks.length!==4||checks.some(c=>c.status==='failed')||!result.scans.policy))fail();
    }
    if(v.audit!==undefined){
        if(v.mode!=='audit')fail();const a=object(v.audit),files=list(a.files,100).map(relative);
        result.audit={scope:text(a.scope),files,skipped:list(a.skipped,4000).map(relative),calls:number(a.calls,20),inputTokens:number(a.inputTokens),outputTokens:number(a.outputTokens),
            findings:list(a.findings,6).map(f=>{const found=finding(f);if(!found.file||!found.line||!files.includes(relative(found.file)))fail();return {...found,file:found.file!,line:found.line!,impact:text(f.impact),verification:text(f.verification)};}),
            needsValidation:list(a.needsValidation,100).map(c=>({title:text(object(c).title,1000),reason:text(c.reason)}))};
    }
    if(v.status==='completed'&&((v.mode==='scans'&&!result.scans)||(v.mode==='audit'&&!result.audit)||(v.mode==='redteam'&&(!result.cleaned||!result.isolationVerified))))fail();
    return result;
}
