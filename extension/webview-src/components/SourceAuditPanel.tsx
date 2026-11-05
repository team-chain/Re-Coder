import React,{useCallback,useEffect,useRef,useState} from 'react';
import {useVSCodeApi} from '../hooks/useVSCodeApi';
interface Finding {title:string;file:string;line:number;severity:string;evidence:string;impact:string;fix:string;verification:string}
interface AuditState {phase?:string;stage?:string;calls?:number;createdAt?:string;finishedAt?:string;stale?:boolean;error?:string;provider?:string;model?:string;report?:{files:string[];skipped:string[];findings:Finding[];needsValidation:Array<{title:string;reason:string}>}}
export const SourceAuditPanel:React.FC<{onFix?:(finding:Finding)=>void}>=({onFix})=>{
 const {postMessage,useMessage}=useVSCodeApi();const [state,setState]=useState<AuditState>({}),[error,setError]=useState(''),[busy,setBusy]=useState(false),[tick,setTick]=useState(0);const pending=useRef('');const timer=useRef<ReturnType<typeof setTimeout>>();
 const send=useCallback((action:string)=>{
  if(pending.current)return;const requestId=`source-audit-${Date.now()}-${Math.random()}`;pending.current=requestId;setBusy(true);setError('');
  timer.current=setTimeout(()=>{pending.current='';setBusy(false);setError('감사 상태를 확인하지 못했습니다. 상태 확인으로 진행 중인 작업을 확인하세요.');},180000);
  postMessage('security.audit',{action,requestId});
 },[postMessage]);
 useMessage(useCallback(msg=>{
  if(msg.type==='canvas.projectChanged'){clearTimeout(timer.current);pending.current='';setState({});setBusy(false);send('status');return;}
  if(msg.type!=='security.auditResult')return;const p=msg.payload as AuditState&{requestId:string};if(p.requestId!==pending.current)return;
  clearTimeout(timer.current);pending.current='';setBusy(false);setState(p);setTick(n=>n+1);
 },[send]));
 useEffect(()=>{send('status');return()=>clearTimeout(timer.current);},[send]);
 const active=['preparing','running','stopping'].includes(state.phase||'');
 useEffect(()=>{if(busy||!active||error)return;const id=setTimeout(()=>{if(document.visibilityState==='visible')send('status');else setTick(n=>n+1);},1500);return()=>clearTimeout(id);},[busy,active,error,tick,send]);
 return <section className="rc-security-detail" aria-label="AI 코드 감사" aria-busy={busy||active}>
  <p>현재 AI 연결로 코드 구조와 취약점 후보를 분석하고, 발견한 근거를 독립적으로 두 번 검토합니다.</p>
  <div className="rc-security-actions"><button className="rc-btn p" disabled={busy||active} onClick={()=>send('start')}>{state.report?'소스 다시 검토 · 감사':'검사할 소스 검토'}</button>
   {active&&<button className="rc-btn" disabled={busy||state.phase==='stopping'} onClick={()=>send('stop')}>감사 중단</button>}
   <button className="rc-btn" disabled={busy} onClick={()=>send('status')}>상태 확인</button></div>
  <p role="status" aria-live="polite">{busy?'요청 처리 중…':state.stage||'소스를 검토한 후 시작합니다.'}{state.calls?` · AI 호출 ${state.calls}회`:''}</p>
  {(error||state.error)&&<p role="alert" className="rc-security-error">{error||state.error}</p>}
  {state.stale&&<p role="alert">감사 중 소스가 변경됐습니다. 아래는 이전 사본의 결과이므로 수정 후 다시 검사하세요.</p>}
  {state.report&&<>
   <p>{state.report.files.length}개 파일 검사 · 제외 {state.report.skipped.length}개 · {state.finishedAt?new Date(state.finishedAt).toLocaleString():''}</p>
   {!state.report.findings.length&&<p>검사한 범위에서 독립 검토가 일치한 문제를 찾지 못했습니다. 전체 코드의 안전을 보장하지 않습니다.</p>}
   {state.report.findings.map((f,i)=><details key={i}><summary><strong>{f.title}</strong><small>{f.file}:{f.line} · {f.severity}</small></summary><p>{f.impact}</p><pre>{f.evidence}</pre><p>{f.fix}</p><p>{f.verification}</p><div className="rc-security-actions"><button className="rc-btn" onClick={()=>postMessage('map.openFile',{id:f.file,line:f.line})}>코드 위치 열기</button>{onFix&&<button className="rc-btn" onClick={()=>onFix(f)}>Develop에서 수정 검토</button>}</div></details>)}
   {!!state.report.needsValidation.length&&<details><summary>추가 검증 필요 {state.report.needsValidation.length}개</summary>{state.report.needsValidation.map((f,i)=><p key={i}>{f.title} — {f.reason}</p>)}</details>}
   <details><summary>검사 범위 · 사용 모델</summary><p>{state.provider} · {state.model}</p><pre>{state.report.files.join('\n')}</pre><p>제외한 경로</p><pre>{state.report.skipped.join('\n')||'없음'}</pre></details>
  </>}
  <small>읽기 전용 감사입니다. 코드 수정은 기존 Develop의 검토·승인 흐름에서 진행합니다.</small>
 </section>;
};
