import {createHash} from 'crypto';
import type {PentagiAssessment,AssessmentFinding,AssessmentCheck} from './pentagiAssessmentTypes';

export interface AccessIdentity {label:'A'|'B';kind:'bearer'|'cookie';credential:string;path:string;marker:string}
export interface AccessPolicy {schema:1;target:string;users:[AccessIdentity,AccessIdentity]}
export interface AccessObservation {role:'A'|'B'|'anonymous';path:string;status:number;markerFound:boolean;responseHash:string}
export interface AccessResult {status:'verified'|'blocked';reason:string;observations:AccessObservation[];findings:AssessmentFinding[]}
export function accessKey(workspace:string) {return 'recoder.redteam.access.v1.'+createHash('sha256').update(workspace).digest('hex');}
export function accessTarget(target:string):string {
    const u=new URL(target);
    if(!['http:','https:'].includes(u.protocol)||!['localhost','127.0.0.1','[::1]','host.docker.internal'].includes(u.hostname)||!u.port||u.username||u.password||u.search||u.hash)throw new Error('권한 검사는 승인한 로컬 앱과 포트에서만 실행합니다.');
    u.hostname='127.0.0.1';return u.toString();
}
export function validateAccessPolicy(value:unknown,target?:string):AccessPolicy {
    const p=value as AccessPolicy;
    if(!p||p.schema!==1||!Array.isArray(p.users)||p.users.length!==2)throw new Error('권한 검사 설정 형식을 확인하세요.');
    const base=new URL(accessTarget(p.target));
    if(target&&accessTarget(target)!==base.toString())throw new Error('저장된 권한 검사 대상이 다릅니다. 현재 앱에 맞게 다시 설정하세요.');
    for(const [i,u] of p.users.entries()) {
        if(!u||u.label!==['A','B'][i]||!['bearer','cookie'].includes(u.kind)||typeof u.credential!=='string'||u.credential.trim().length<6||u.credential.length>4096||/[\r\n]/.test(u.credential))throw new Error('테스트 사용자 인증값을 확인하세요.');
        if(typeof u.marker!=='string'||u.marker.length<6||u.marker.length>128||/[\r\n]/.test(u.marker))throw new Error('테스트 데이터의 고유 표식은 6~128자로 입력하세요.');
        if(typeof u.path!=='string'||!u.path.startsWith('/')||u.path.startsWith('//')||u.path.length>2048||/[%\\#\r\n]/.test(u.path))throw new Error('권한 검사 경로는 인코딩되지 않은 앱 내부 경로여야 합니다.');
        const url=new URL(u.path,base),prefix=base.pathname.replace(/\/+$/,'');
        if(url.origin!==base.origin||u.path.split(/[/?]/).some(v=>v==='.'||v==='..')||(prefix&&url.pathname!==prefix&&!url.pathname.startsWith(prefix+'/')))throw new Error('권한 검사 경로가 승인한 범위 밖입니다.');
    }
    if(p.users[0].credential===p.users[1].credential||p.users[0].path===p.users[1].path||p.users[0].marker===p.users[1].marker)throw new Error('사용자 A·B의 인증값, 소유 리소스 경로, 고유 표식은 서로 달라야 합니다.');
    return p;
}
/** A small, explicit read-only access matrix. No credentials are sent to the LLM or report.
 * The owner-positive control must succeed before anonymous/cross-user access is classified.
 */
export async function runAccessChecks(policy:AccessPolicy,signal?:AbortSignal,onProgress?:(step:string)=>Promise<void>,transport:typeof fetch=fetch):Promise<AccessResult> {
    const p=validateAccessPolicy(policy),base=accessTarget(p.target);
    const result:AccessResult={status:'blocked',reason:'검사 준비',observations:[],findings:[]};
    const observe=async(role:AccessIdentity|undefined,resource:AccessIdentity)=>{
        if(signal?.aborted)throw new Error();
        const controller=new AbortController(),abort=()=>controller.abort();
        signal?.addEventListener('abort',abort,{once:true});
        const timer=setTimeout(abort,10000);
        try {
            const headers:Record<string,string>={Accept:'application/json, text/plain'};
            if(role)headers[role.kind==='cookie'?'Cookie':'Authorization']=role.kind==='cookie'?role.credential:'Bearer '+role.credential;
            const response=await transport(new URL(resource.path,base),{method:'GET',headers,redirect:'error',signal:controller.signal});
            const chunks:Uint8Array[]=[];let bytes=0;const reader=response.body?.getReader();
            if(reader){try{while(true){const x=await reader.read();if(x.done)break;bytes+=x.value.byteLength;if(bytes>65536||controller.signal.aborted)throw new Error();chunks.push(x.value);}}finally{await reader.cancel().catch(()=>{});}}
            const body=Buffer.concat(chunks).toString('utf8');
            const observation:AccessObservation={role:role?.label||'anonymous',path:resource.path,status:response.status,markerFound:body.includes(resource.marker),responseHash:createHash('sha256').update(body).digest('hex')};
            result.observations.push(observation);return observation;
        } finally {clearTimeout(timer);signal?.removeEventListener('abort',abort);}
    };
    const description=(o:AccessObservation)=>`HTTP ${o.status} · 소유 리소스 표식 ${o.markerFound?'확인':'없음'} · SHA256 ${o.responseHash}`;
    try {
        const controls:AccessObservation[]=[];
        for(const user of p.users){await onProgress?.(`권한 검사 · 사용자 ${user.label} 로그인 확인`);controls.push(await observe(user,user));}
        if(controls.some(o=>o.status<200||o.status>=300||!o.markerFound)){result.reason='사용자별 소유 리소스 확인에 실패했습니다. 만료된 로그인 또는 경로·표식을 확인하세요.';return result;}
        for(const [i,owner] of p.users.entries()){
            for(const actor of [undefined,p.users[1-i]]){
                await onProgress?.(`권한 검사 · ${actor?'사용자 '+actor.label:'비로그인'} → 사용자 ${owner.label} 리소스`);
                const observed=await observe(actor,owner);
                if(observed.status>=200&&observed.status<300&&observed.markerFound) {
                    result.findings.push({title:actor?`사용자 ${actor.label}가 사용자 ${owner.label}의 리소스를 조회했습니다.`:'로그인 없이 보호 리소스를 조회했습니다.',
                        kind:actor?'authorization':'authentication',route:new URL(owner.path,base).pathname,
                        observation:'승인된 접근 규칙상 거부되어야 하는 GET 응답에서 소유자의 고유 표식을 확인했습니다.',
                        remediation:actor?'로그인 사용자와 리소스 소유자를 서버에서 대조하세요.':'보호된 경로에 서버 인증 검사를 적용하세요.',
                        request:`[${actor?.label||'anonymous'}] GET ${owner.path}`,response:description(observed),
                        controlRequest:`[${owner.label}] GET ${owner.path}`,controlResponse:description(controls[i]),evidence:'http_observed'});
                } else if(![401,403,404].includes(observed.status)||observed.markerFound){
                    result.reason='접근 거부를 확정할 수 없는 응답이 있습니다. 부분 결과만 보관합니다.';return result;
                }
            }
        }
        result.status='verified';result.reason='승인된 두 소유 리소스의 정상·비로그인·교차 접근 6개 응답을 대조했습니다. 다른 경로와 관리자 권한은 이 결과에 포함되지 않습니다.';
    } catch {result.reason=signal?.aborted?'사용자가 검사를 중단했습니다.':'요청 실패·리디렉션·시간 또는 응답 크기 제한으로 권한 검사를 완료하지 못했습니다.';}
    return result;
}
export function withAccessResult(assessment:PentagiAssessment,access?:AccessResult):PentagiAssessment {
    if(!access)return assessment;
    const checks=assessment.checks.map((c):AssessmentCheck=>['authentication','authorization'].includes(c.kind)?{...c,status:access.status==='verified'?'tested':'blocked',reason:access.reason,requests:access.observations.map(o=>`[${o.role}] GET ${o.path}`)}:c);
    const warnings=assessment.warnings.filter(w=>!w.startsWith('인증·권한 검사는 승인된 테스트 계정')&&w!=='미실행·차단된 검사 범위가 있습니다.');
    if(checks.some(c=>c.status!=='tested'))warnings.push('미실행·차단된 검사 범위가 있습니다.');
    return {...assessment,checks,status:checks.every(c=>c.status==='tested')&&warnings.length===0?'review_required':'incomplete',
        findings:[...assessment.findings.filter(f=>!['authentication','authorization'].includes(f.kind)),...access.findings],
        warnings,limitations:[...new Set([...assessment.limitations,access.reason])]};
}
