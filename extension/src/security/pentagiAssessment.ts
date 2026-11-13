/** A report contract, not a second LLM verdict. Never equate task completion with safety. */
export const CHECKS = ['discovery','sql_injection','authentication','authorization','input_handling'] as const;
import type {CheckKind,AssessmentCheck,AssessmentFinding,PentagiAssessment} from './pentagiAssessmentTypes';
export type {CheckKind,AssessmentCheck,AssessmentFinding,PentagiAssessment} from './pentagiAssessmentTypes';

/** FIRST CVSS v3.1 base equations and integer Roundup (Appendix A).
 * https://www.first.org/cvss/v3.1/specification-document#7-1-Base-Metrics-Equations
 * CVSS is owned by FIRST.Org, Inc. and used by permission. Only base vectors are supported.
 */
export function cvss31(vector:string):number|undefined {
    if(typeof vector!=='string'||!vector.startsWith('CVSS:3.1/'))return;
    const parts=vector.slice(9).split('/'), m:Record<string,string>={};
    const allowed:Record<string,string[]>={AV:['N','A','L','P'],AC:['L','H'],PR:['N','L','H'],UI:['N','R'],S:['U','C'],C:['N','L','H'],I:['N','L','H'],A:['N','L','H']};
    if(parts.length!==8)return;
    for(const p of parts){const [k,v,...extra]=p.split(':');if(extra.length||m[k]||!allowed[k]?.includes(v))return;m[k]=v;}
    if(Object.keys(m).length!==8)return;
    const impactValue={N:0,L:0.22,H:0.56};
    const iss=1-(1-impactValue[m.C])*(1-impactValue[m.I])*(1-impactValue[m.A]);
    const impact=m.S==='U'?6.42*iss:7.52*(iss-0.029)-3.25*(iss-0.02)**15;
    if(impact<=0)return 0;
    const pr=m.PR==='N'?0.85:m.PR==='L'?(m.S==='U'?0.62:0.68):(m.S==='U'?0.27:0.5);
    const exploit=8.22*({N:0.85,A:0.62,L:0.55,P:0.2}[m.AV])*({L:0.77,H:0.44}[m.AC])*pr*({N:0.85,R:0.62}[m.UI]);
    const value=Math.min((m.S==='C'?1.08:1)*(impact+exploit),10), integer=Math.round(value*100000);
    return integer%10000===0?integer/100000:(Math.floor(integer/10000)+1)/10;
}

export const REPORT_CONTRACT = `Before concluding, enumerate in-scope routes and parameters from links, forms and available OpenAPI descriptions. Prioritize every discovered search/filter/id input, including safe negative controls. A normal value, empty value or random punctuation is NOT a SQL injection test: use a minimal non-destructive paired boolean test and a negative control; if a data-read finding needs confirmation, use only a synthetic marker, never extract real records. No stacked writes, time-delay or destructive payloads. Do not claim authentication/authorization was tested without supplied test identities and an expected access policy: mark it blocked. Treat routes that could not be inspected as not_tested. Do not conclude no vulnerabilities from the landing page alone.
Finish with exactly one fenced recoder-report JSON block in the final task result:
{"schema":1,"checks":[{"kind":"discovery|sql_injection|authentication|authorization|input_handling","status":"tested|blocked|not_tested","reason":"what was and was not checked","requests":["METHOD /path?parameter=value"]}],"findings":[{"title":"short title","kind":"sql_injection|authentication|authorization|input_handling","route":"/path","observation":"only behavior demonstrated by responses, no guesses about source or wider impact","remediation":"smallest valid fix","request":"exact URL-encoded request","response":"minimal actual response excerpt","controlRequest":"different baseline or negative-control request","controlResponse":"actual control response excerpt","cvssVector":"optional complete CVSS:3.1 base vector"}],"limitations":["unverified scope"]}.
Include all five check kinds once. Preserve baseline, probe and negative control evidence. For POST, include the request line, Content-Type, a blank line and the exact body, using JSON-escaped newlines. Never include credentials. The finding request and control must occur in that check's requests list, with the same method and route. Do not invent responses or fill unavailable evidence with examples. Do not put numeric CVSS scores in prose: ReCoder calculates from the vector. Put missing evidence and unsupported impact in limitations. A finding and a no-finding conclusion both require coverage; finishing tools alone does not mean passing security.`;

const str=(v:unknown,max=4000):v is string=>typeof v==='string'&&!!v.trim()&&v.length<=max;
export function redactDiagnostic(text:string):string {
    return text.replace(/\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g,'[REDACTED]')
        .replace(/\b(?:sk-[a-zA-Z0-9_-]{16,}|gh[pousr]_[a-zA-Z0-9]{20,})\b/g,'[REDACTED]')
        .replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+=*/gi,'Bearer [REDACTED]')
        .replace(/((?:password|secret|token|api[_-]?key|set-cookie|cookie)\s*["']?\s*[:=]\s*["']?)[^\s"'<>,;]+/gi,'$1[REDACTED]');
}
function inScope(route:string,target?:string):boolean {
    try {
        if(!route.startsWith('/')||route.startsWith('//')||/[\r\n\\]/.test(route))return false;
        const base=new URL(target||'http://scope.invalid/'),u=new URL(route,base),prefix=base.pathname.replace(/\/$/,'');
        const decoded=decodeURIComponent(u.pathname),scope=decodeURIComponent(prefix);
        if(/[\\\r\n%]/.test(decoded)||decoded.split('/').some(p=>p==='.'||p==='..'))return false;
        return u.origin===base.origin&&(!scope||decoded===scope||decoded.startsWith(scope+'/'));
    }catch{return false;}
}
function requestInfo(request:string,target?:string):{method:string;path:string}|undefined {
    const first=request.split(/\r?\n/,1)[0],match=/^(GET|HEAD|OPTIONS|POST) (\/\S*?)(?: HTTP\/1\.[01])?$/.exec(first);
    if(!match||!inScope(match[2],target))return;
    return {method:match[1],path:new URL(match[2],'http://scope.invalid').pathname};
}
const omitted=(kind:CheckKind):AssessmentCheck=>({kind,status:'not_tested',reason:'구조화된 검사 근거가 없습니다.',requests:[]});
/** Read only bounded final reports. Raw model text stays separate and is explicitly unverified. */
export function assessPentagi(tasks:Array<{result:string}>=[],evidence:Array<{message:string;result:string}>=[],target?:string):PentagiAssessment {
    const warnings:string[]=[], findings:AssessmentFinding[]=[], checks=new Map<CheckKind,AssessmentCheck>();
    const result:PentagiAssessment={schema:1,status:'incomplete',checks:CHECKS.map(omitted),findings,warnings,limitations:['AI가 보고한 HTTP 근거입니다. ReCoder가 공격 성공을 독립적으로 확정한 결과가 아니며, 최종 검토가 필요합니다.']};
    const texts=[...(Array.isArray(evidence)?evidence:[]).slice(-50).flatMap(e=>[e?.message,e?.result]),...(Array.isArray(tasks)?tasks:[]).slice(-100).map(t=>t?.result)].filter(t=>typeof t==='string'&&t.length<=100000);
    let report:any;
    for(const text of texts){
        // Only a closing fence on its own line ends the block. HTTP evidence can
        // legitimately contain backticks inside a JSON string.
        const blocks=[...text.matchAll(/^ {0,3}```recoder-report[ \t]*\r?\n([\s\S]*?)^ {0,3}```[ \t]*\r?$/gm)];
        for(const block of blocks){try{const x=JSON.parse(block[1]);if(x?.schema===1)report=x;}catch{warnings.push('보고서 JSON을 읽지 못했습니다. 원문을 확인하세요.');}}
    }
    if(!report){warnings.push('검사 범위와 대조 근거를 담은 최종 보고서가 없습니다. 발견 0건을 안전 판정으로 해석하지 마세요.');return result;}
    if(!Array.isArray(report.checks)||report.checks.length>20||!Array.isArray(report.findings)||report.findings.length>50||!Array.isArray(report.limitations)||report.limitations.length>30){warnings.push('보고서 형식 또는 크기가 올바르지 않습니다.');return result;}
    const duplicate=new Set<CheckKind>();
    for(const c of report.checks){
        if(!c||!CHECKS.includes(c.kind)||!['tested','blocked','not_tested'].includes(c.status)||!str(c.reason)||!Array.isArray(c.requests)||c.requests.length>60||c.requests.some((r:unknown)=>!str(r,4000))){warnings.push('검사 범위 항목을 검증하지 못했습니다.');continue;}
        if(checks.has(c.kind)){duplicate.add(c.kind);continue;}
        const requests:string[]=[...new Set<string>(c.requests.filter((r:string)=>!!requestInfo(r,target)))];
        let status=c.status==='tested'&&requests.length===0?'not_tested':c.status;
        if(c.requests.some((r:string)=>!requestInfo(r,target))){status='not_tested';warnings.push('승인 범위 밖이거나 읽을 수 없는 요청이 있어 해당 검사를 미검증으로 표시합니다.');}
        if(c.status==='tested'&&c.kind==='sql_injection'){
            const decoded=requests.map((r:string)=>{try{return decodeURIComponent(r.replace(/\+/g,' '));}catch{return '';}});
            if(requests.length<3||decoded.filter((r:string)=>/\b(?:AND|OR)\s+[^\s]+\s*=|\bUNION\s+(?:ALL\s+)?SELECT\b/i.test(r)).length<2){
                status='not_tested';warnings.push('SQL 검사에 정상·변형·대조 요청이 부족합니다. 일반 문자 입력만으로 검사 완료로 표시하지 않습니다.');
            }
        }
        // This adapter receives no trusted identity/access policy contract. Agent prose
        // alone must not assert that another user's protected resources were checked.
        if(c.status==='tested'&&['authentication','authorization'].includes(c.kind)){
            status='blocked';warnings.push('인증·권한 검사는 승인된 테스트 계정과 기대 접근 규칙을 확인할 수 없어 미검증으로 남깁니다.');
        }
        checks.set(c.kind,{kind:c.kind,status,reason:c.reason,requests});
    }
    for(const kind of duplicate){checks.set(kind,omitted(kind));warnings.push(`${kind}: 중복된 검사 범위는 미검증으로 처리했습니다.`);}
    const roots=new Set<string>();
    for(const f of report.findings){
        if(!f||!CHECKS.includes(f.kind)||f.kind==='discovery'||!['title','route','observation','remediation','request','response','controlRequest','controlResponse'].every(k=>str(f[k],k==='title'?300:4000))||!inScope(f.route,target)||f.request===f.controlRequest||f.response===f.controlResponse){warnings.push('요청·대조 응답이 불충분한 발견 사항을 후보 목록에 넣지 않았습니다.');continue;}
        const request=requestInfo(f.request,target),control=requestInfo(f.controlRequest,target),check=checks.get(f.kind);
        if(!request||!control||request.method!==control.method||request.path!==control.path||request.path!==new URL(f.route,'http://scope.invalid').pathname||check?.status!=='tested'||!check.requests.includes(f.request)||!check.requests.includes(f.controlRequest)){warnings.push('범위 밖이거나 검사·대조 요청의 연결 근거가 부족한 발견 사항을 제외했습니다.');continue;}
        const key=f.kind+' '+f.request+' '+f.controlRequest;if(roots.has(key)){warnings.push(`${f.route}: 같은 요청·대조 쌍의 중복 항목을 합쳤습니다.`);continue;}roots.add(key);
        const item:AssessmentFinding={title:f.title,kind:f.kind,route:f.route,observation:f.observation,remediation:f.remediation,request:f.request,response:f.response,controlRequest:f.controlRequest,controlResponse:f.controlResponse,evidence:'agent_reported'};
        if(f.cvssVector!==undefined){const score=cvss31(f.cvssVector);if(score===undefined)warnings.push(`${f.title}: 유효하지 않은 CVSS 벡터를 제외했습니다.`);else item.cvss={vector:f.cvssVector,score};}
        findings.push(item);
    }
    result.checks=CHECKS.map(k=>checks.get(k)||omitted(k));
    result.limitations.push(...report.limitations.filter((s:unknown)=>str(s,2000)));
    if(result.checks.some(c=>c.status!=='tested'))warnings.push('미실행·차단된 검사 범위가 있습니다.');
    if(result.checks.every(c=>c.status==='tested')&&warnings.length===0)result.status='review_required';
    result.warnings=[...new Set(warnings)].slice(0,60);
    return result;
}

export function assessmentMarkdown(a:PentagiAssessment,meta?:{target?:string;createdAt?:string;provider?:string;outcome?:string;phase?:string}):string {
    const clean=(s:string)=>s.replace(/[\r\n]/g,' ').replace(/[<>]/g,'');
    const lines=['# ReCoder 레드팀 진단 보고서','','직접 HTTP 대조 결과와 AI가 제시한 후보를 구분해 표시합니다. 검사한 경로 외의 안전성을 보증하지 않습니다.'];
    if(meta)for(const [label,value] of [['대상',meta.target],['시작',meta.createdAt],['공급자',meta.provider],['실행 결과',meta.outcome],['환경 상태',meta.phase]])if(value)lines.push(`- ${label}: ${clean(value)}`);
    lines.push('','## 검사 범위');
    for(const c of a.checks)lines.push(`- ${c.kind}: ${c.status} — ${clean(c.reason)}`);
    for(const f of a.findings){lines.push('',`## ${clean(f.title)}`,`경로: ${clean(f.route)}`,`근거: ${f.evidence==='http_observed'?'직접 HTTP 응답 대조':'AI 보고 · 독립 검증 전'}`,clean(f.observation),`조치: ${clean(f.remediation)}`);if(f.cvss)lines.push(`CVSS 3.1 (벡터로 계산, 영향은 검토 필요): ${f.cvss.score} · ${f.cvss.vector}`);
        for(const [label,text] of [['요청',f.request],['응답',f.response],['대조 요청',f.controlRequest],['대조 응답',f.controlResponse]]){const fence='`'.repeat(Math.max(3,...(text.match(/`+/g)||[]).map(x=>x.length+1)));lines.push('',label,fence+'text',text,fence);}}
    lines.push('','## 미검증·보고서 주의 사항',...a.warnings.map(x=>'- '+clean(x)),...a.limitations.map(x=>'- '+clean(x)));
    return lines.join('\n');
}
