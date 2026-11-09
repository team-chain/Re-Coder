import React, {useCallback, useEffect, useRef, useState} from 'react';
import {useVSCodeApi} from '../hooks/useVSCodeApi';
import {SecurityScanPanel, PolicyPanel, ScanResultLite} from './Hubs';
import {ScanKind} from './scanQueue';
import {Ring} from './shell/parts';
import {CodeReport, overview, SECURITY_KINDS} from './securityOverview';
import {RedTeamPanel} from './RedTeamPanel';
import {ManagedRedTeamPanel} from './ManagedRedTeamPanel';
import type {ManagedReport} from '../../src/security/managedReportTypes';
import {SourceAuditPanel} from './SourceAuditPanel';
import {setPendingPrompt} from './CodeAgent';

type Kind='code'|'history'|'recheck'|'policy';
type Check={running?:boolean;error?:string;result?:Record<string,unknown>;at?:string};
export const SecurityDashboard: React.FC<{onGo?:(view:string)=>void}> = ({onGo}) => {
 const {postMessage,useMessage}=useVSCodeApi(); const [signal,setSignal]=useState(0); const [scans,setScans]=useState<Partial<Record<ScanKind,ScanResultLite>>>({}); const [scanning,setScanning]=useState(false);
 const [checks,setChecks]=useState<Partial<Record<Kind,Check>>>({}); const pending=useRef(new Map<string,{kind:Kind;timer:ReturnType<typeof setTimeout>}>()); const [redteam,setRedteam]=useState(false);
 const [allFindings,setAllFindings]=useState(false),[audit,setAudit]=useState(false),[managedScans,setManagedScans]=useState(false);
 const [managedReport,setManagedReport]=useState<ManagedReport>();
 const request=useCallback((kind:Kind)=>{
  if([...pending.current.values()].some(v=>v.kind===kind))return;
  const requestId=`security-check-${Date.now()}-${Math.random()}`;
  const timer=setTimeout(()=>{pending.current.delete(requestId);setChecks(c=>({...c,[kind]:{error:'응답을 확인하지 못했습니다. 연결 상태를 확인하고 다시 시도하세요.'}}));},190000);
  pending.current.set(requestId,{kind,timer});setChecks(c=>({...c,[kind]:{running:true}}));postMessage('security.check',{kind,requestId});
 },[postMessage]);
 useMessage(useCallback(msg=>{
  if(msg.type==='canvas.projectChanged'){pending.current.forEach(p=>clearTimeout(p.timer));pending.current.clear();setChecks({});setScans({});setManagedReport(undefined);setScanning(false);setSignal(0);setManagedScans(false);setAudit(false);setRedteam(false);return;}
  if(msg.type!=='security.checkResult')return;const p=msg.payload as {requestId:string;result?:Record<string,unknown>;error?:string};const entry=pending.current.get(p.requestId);if(!entry)return;
  clearTimeout(entry.timer);pending.current.delete(p.requestId);setChecks(c=>({...c,[entry.kind]:{result:p.result,error:p.error,at:new Date().toLocaleTimeString()}}));
  if(entry.kind==='recheck'&&!p.error)request('history');
 },[request]));
 useEffect(()=>()=>{pending.current.forEach(p=>clearTimeout(p.timer));pending.current.clear();},[]);
 const changed=useCallback((results:Partial<Record<ScanKind,ScanResultLite>>,running:boolean)=>{setScans(results);setScanning(running);},[]);
 const code=checks.code?.result as unknown as CodeReport|undefined; const busy=scanning||!!checks.code?.running||!!checks.policy?.running; const summary=overview(scans,code,busy,checks.code?.error);
 const priorities:Record<string,number>={critical:4,high:3,medium:2,low:1};
 const findings=[...(code?.findings??[])].sort((a,b)=>(priorities[b.severity]??0)-(priorities[a.severity]??0));
 const run=()=>{setManagedReport(undefined);setSignal(n=>n+1);request('code');request('policy');};
 return <section className="rc-security-overview" aria-label="보안 현황">
 <style>{`
 .rc-security-overview{max-width:1100px;margin:8px auto 90px;color:var(--rc-fg,var(--vscode-foreground));font-size:14px}
 .rc-security-overview .rc-security-hero,.rc-security-results,.rc-security-extra{border:1px solid var(--rc-line,#28313c);border-radius:20px;background:var(--rc-tile,#10171e);margin-bottom:16px}
 .rc-security-hero{display:flex;align-items:center;gap:28px;padding:24px}.rc-security-hero h1{font-size:24px;margin:0 0 8px;letter-spacing:-.6px}.rc-security-hero p{margin:0;color:var(--rc-muted,#9aa8b8);line-height:1.7}.rc-security-hero>.rc-btn{margin-left:auto;flex-shrink:0}
 .rc-security-results{padding:8px 24px}.rc-security-results details{border-bottom:1px solid var(--rc-line,#28313c)}.rc-security-results details:last-child{border:0}
 .rc-security-overview summary{cursor:pointer;padding:15px 0;line-height:1.7}.rc-security-overview summary strong{font-weight:600}.rc-security-overview summary small{display:block;color:var(--rc-muted,#9aa8b8);margin-left:18px;overflow-wrap:anywhere}
 .rc-security-detail{padding:0 8px 22px;line-height:1.8}.rc-security-detail p,.rc-security-detail small{color:var(--rc-muted,#9aa8b8)}.rc-security-detail pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.7 var(--vscode-editor-font-family,monospace);max-height:420px;overflow:auto}
 .rc-security-extra{padding:0 24px}.rc-security-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.rc-security-error{color:var(--rc-bad,#ff8b8b)!important}.rc-security-note{color:var(--rc-muted,#9aa8b8);font-size:12px;line-height:1.8}
 .rc-security-overview .rc-scan-panel{background:transparent;padding:4px;border:0}.rc-security-overview .rc-scan-grid{grid-template-columns:1fr}.rc-security-overview .rc-scan-row[open]{grid-column:auto}
 .rc-security-redteam{display:flex;align-items:center;justify-content:space-between;padding:20px 24px;border:1px dashed var(--rc-line,#28313c);border-radius:18px;gap:16px}.rc-security-redteam small{display:block;color:var(--rc-muted,#9aa8b8);margin-top:6px}
 @media(max-width:700px){.rc-security-hero{padding:22px;gap:16px;flex-wrap:wrap}.rc-security-hero h1{font-size:20px}.rc-security-hero>.rc-btn{margin-left:0}.rc-security-results{padding:4px 16px}}
 `}</style>
 <div className="rc-security-hero" aria-live="polite"><Ring value={summary.count?1:0} tone={summary.tone} size={90}><div><b style={{fontSize:26}}>{busy?'…':summary.count||'—'}</b><small style={{display:'block'}}>확인할 것</small></div></Ring>
  <div><h1>{summary.title}</h1><p>코드 · 이미지 · Dockerfile · 시크릿{checks.code?.at?` · ${checks.code.at} 코드 검사`:''}</p><p>최종 배포 허용 여부는 배포할 버전과 정책으로 다시 확인합니다.</p></div>
  <div className="rc-security-actions"><button className="rc-btn p" disabled={busy} onClick={run}>{busy?'검사 중…':signal?'다시 검사':'보안 검사 시작'}</button></div></div>
 <div className="rc-security-results">
 {findings.slice(0,allFindings?undefined:3).map((f,i)=><details key={`${f.rule}-${f.file}-${i}`}><summary><strong><span style={{color:'var(--rc-warn,#f0b35b)'}}>● </span>{f.message}</strong><small>{f.file}:{f.line} · {f.severity} · {f.precise?'규칙에 일치':'추가 검토 필요'}</small></summary><div className="rc-security-detail"><p>{f.fix}</p><button className="rc-btn" onClick={()=>postMessage('map.openFile',{id:f.file,line:f.line})}>코드 위치 열기</button></div></details>)}
 {findings.length>3&&<button className="rc-btn" onClick={()=>setAllFindings(!allFindings)}>{allFindings?'접기':`전체 ${findings.length}개 보기`}</button>}
 <details><summary><strong>검사 범위 · 결과 상세</strong><small>코드 · 이미지 · Dockerfile · 시크릿 · 정책 — {busy?'검사 중':summary.partial?'미확인 항목 있음':'결과 확인'}</small></summary>
 <details><summary><strong>코드 보안 규칙 검사 · {checks.code?.running?'검사 중':checks.code?.error?'확인 실패':code?code.score===null?'적용 가능한 규칙 없음':`${code.findings.length}개 확인 필요`:'미실행'}</strong><small>JavaScript · TypeScript 대상 / 심층 AI 감사와 별개</small></summary><div className="rc-security-detail"><p>{checks.code?.error||code?.scope||'프로젝트에서 SQL 구성, 인증, 시크릿 등 지원 규칙을 검사합니다.'}</p>{code&&<p>검사 파일 {code.files_checked??'—'}개</p>}<button className="rc-btn" disabled={!!checks.code?.running} onClick={()=>request('code')}>코드만 다시 검사</button></div></details>
 <details><summary><strong>이미지 · Dockerfile · 시크릿 검사</strong><small>{scanning?'검사 중':summary.gate.label} · 결과와 수정안 보기</small></summary><SecurityScanPanel kinds={SECURITY_KINDS} title="기본 검사" note="" compact runSignal={signal} onChange={changed}/></details>
 <details><summary><strong>배포 정책 · {checks.policy?.result?.opa_available?'OPA 연결됨':checks.policy?'연결 확인 필요':'상태 미확인'}</strong><small>연결 상태와 실제 배포 정책 평가는 별개입니다.</small></summary><div className="rc-security-detail"><p>{checks.policy?.error||`정책 번들: ${String(checks.policy?.result?.bundle_version||'미확인')}`}</p><button className="rc-btn" disabled={!!checks.policy?.running} onClick={()=>request('policy')}>OPA 상태 확인</button><PolicyPanel/></div></details>
 </details>
 </div>
 <details className="rc-security-extra"><summary><strong>배포 후 새 취약점 감시 · 이력 보기</strong><small>이 PC에 기록된 배포 전체 · SBOM ↔ OSV · Core 실행 중 주기적으로 확인</small></summary><div className="rc-security-detail">
 <div className="rc-security-actions"><button className="rc-btn" disabled={!!checks.history?.running} onClick={()=>request('history')}>이력 불러오기</button><button className="rc-btn" disabled={!!checks.recheck?.running} onClick={()=>request('recheck')}>{checks.recheck?.running?'재검사 중…':'지금 재검사'}</button><button className="rc-btn" onClick={()=>onGo?.('hub:monitor')}>업데이트 제안 · 대응 열기</button></div>
 <p role="status">{checks.recheck?.error||checks.recheck?.result?.message as string|| (checks.recheck?.result?.status==='ok'?`${checks.recheck.result.checked}개 배포 재검사 완료`:'')}</p>
 {checks.history?.error&&<p role="alert">{checks.history.error}</p>}
 {checks.history?.result&&Object.keys(checks.history.result).length===0&&<p>기록된 이력이 없습니다. 검사 통과를 의미하지 않습니다.</p>}
 {Object.entries(checks.history?.result??{}).map(([service,value])=>{const entry=value as {checked_at?:string;known?:Record<string,{id:string;name:string;version:string;fixed?:string;fixed_at?:string;fix_applied_at?:string}>};return <details key={service}><summary>{service} · {entry.checked_at||'검사일 미확인'}</summary>{Object.values(entry.known??{}).map((v,i)=><p key={i}>{v.id} · {v.name} {v.version} → {v.fixed||'수정 버전 미확인'} · {v.fixed_at?'재검사에서 사라짐':v.fix_applied_at?'업데이트 적용 · 재배포 필요':'확인 필요'}</p>)}</details>;})}
 </div></details>
 <div className="rc-security-redteam"><div><strong>AI 코드 감사</strong><small>Cloudflare security-audit 기반 · 소스 근거와 수정 방향 확인</small></div><button className="rc-btn" aria-expanded={audit} onClick={()=>{setAudit(!audit);setRedteam(false);setManagedScans(false);}}>{audit?'닫기':'AI 감사'}</button></div>
 {audit&&<SourceAuditPanel onFix={onGo?finding=>{setPendingPrompt(`${finding.file}:${finding.line}의 보안 문제를 검토하고 최소 범위의 수정안을 제안해 주세요.\n문제: ${finding.title}\n영향: ${finding.impact}\n수정 방향: ${finding.fix}\n현재 파일을 다시 확인하고 기존 기능을 유지하세요. 승인 전에는 적용하지 마세요.`);onGo('code');}:undefined}/>}
 <div className="rc-security-redteam"><div><strong>실행 중인 앱 검사</strong><small>PentAGI · 검사 사본 → 실행 검토 → 증거 확인</small></div><button className="rc-btn" aria-expanded={redteam} onClick={()=>{setRedteam(!redteam);setAudit(false);setManagedScans(false);}}>{redteam?'닫기':'레드팀 테스트'}</button></div>
 {redteam&&<RedTeamPanel/>}
 <details className="rc-security-extra"><summary>다른 실행 방식</summary><p>필요할 때만 기존 AWS 연결로 프로젝트 사본을 검사할 수 있습니다. 최초 실행 환경 준비와 사용료가 발생할 수 있습니다.</p><button className="rc-btn" aria-expanded={managedScans} onClick={()=>setManagedScans(!managedScans)}>{managedScans?'닫기':'AWS 사본 검사 열기'}</button>{managedScans&&<ManagedRedTeamPanel mode="scans" onReport={setManagedReport}/>} {managedReport&&<p>사본 검사 결과는 위의 현재 프로젝트 검사와 별도로 표시합니다.</p>}</details>
 <p className="rc-security-note">검사 결과는 실행 당시의 파일과 대상 기준입니다. 파일 수정·새 이미지 배포 후에는 다시 검사하세요. 미실행·오류·해당 없음은 통과로 계산하지 않습니다.</p>
 </section>;
};
