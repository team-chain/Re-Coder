import React,{useCallback,useEffect,useRef,useState} from 'react';
import {useVSCodeApi} from '../hooks/useVSCodeApi';
import type {ManagedMode,ManagedReport} from '../../src/security/managedReportTypes';
interface Run {id:string;mode?:ManagedMode;createdAt:string;status:string;stage?:string;terminal?:boolean;uncertain?:boolean;stopRequested?:boolean;files:number;bytes:number}
interface State {connected?:boolean;ready?:boolean;account?:string;region?:string;stackStatus?:string;runs?:Run[];activeRun?:Run;report?:ManagedReport;reportError?:string}
const PHASE:Record<string,string>={SUBMITTED:'실행 요청 수락',QUEUED:'실행 대기',PROVISIONING:'작업 환경 준비',DOWNLOAD_SOURCE:'검사 사본 받는 중',INSTALL:'실행 도구 준비',PRE_BUILD:'실행 준비',BUILD:'빌드·격리 확인·레드팀 검사',POST_BUILD:'검사 마무리',UPLOAD_ARTIFACTS:'결과 보관',FINALIZING:'환경 종료',COMPLETED:'작업 종료'};
const STATUS:Record<string,string>={SUBMITTING:'실행 여부 확인 중',IN_PROGRESS:'진행 중',SUCCEEDED:'작업 완료',FAILED:'작업 실패',FAULT:'실행 환경 오류',STOPPED:'중단됨',TIMED_OUT:'시간 제한으로 종료',NOT_STARTED:'실행되지 않음',UPLOAD_FAILED:'업로드 실패'};
const MODE:Record<ManagedMode,string>={audit:'AI 코드 감사',scans:'기본 보안 검사',redteam:'레드팀 검사'};
export const ManagedRedTeamPanel:React.FC<{mode?:ManagedMode;onReport?:(report:ManagedReport|undefined)=>void}>=({mode='redteam',onReport})=>{
 const {postMessage,useMessage}=useVSCodeApi();const [state,setState]=useState<State>({}),[error,setError]=useState(''),[busy,setBusy]=useState(false),[tick,setTick]=useState(0);
 const pending=useRef('');const send=useCallback((action:string)=>{if(pending.current)return;const id=`managed-${Date.now()}-${Math.random()}`;pending.current=id;setBusy(true);setError('');postMessage('security.redteam',{action,mode,requestId:id});},[postMessage,mode]);
 useMessage(useCallback(msg=>{if(msg.type==='aws.configure.result'||msg.type==='aws.clear.result'){pending.current='';setState({});send('managedStatus');return;}
  if(msg.type==='canvas.projectChanged'){pending.current='';setBusy(false);setState({});setError('프로젝트가 변경되었습니다. 상태를 다시 확인하세요.');return;}
  if(msg.type!=='security.redteamResult')return;const p=msg.payload as State&{requestId:string;error?:string;cancelled?:boolean};if(p.requestId!==pending.current)return;pending.current='';setBusy(false);if(p.error)setError(p.error);else if(!p.cancelled)setState(p);setTick(n=>n+1);},[send]));
 useEffect(()=>{send('managedStatus');},[send]);
 const run=state.runs?.[0],report=state.report,active=!!state.activeRun;
 const scans=report?.scans;
 useEffect(()=>{onReport?.(report);},[report,onReport]);
 useEffect(()=>{if(busy||error||(!active&&!state.stackStatus?.endsWith('_IN_PROGRESS')))return;const id=setTimeout(()=>{if(document.visibilityState==='visible')send('managedStatus');else setTick(n=>n+1);},5000);return()=>clearTimeout(id);},[busy,error,active,state.stackStatus,send,tick]);
 return <section className="rc-security-detail" aria-busy={busy}>
  <h3>{mode==='audit'?'AI 코드 감사':mode==='scans'?'관리형 기본 보안 검사':'레드팀 테스트'}</h3><p>{mode==='audit'?'Cloudflare security-audit 기반 · 코드 구조 분석 → 취약점 탐색 → 독립 근거 재검토':'프로젝트 사본을 내 AWS에서 빌드하고 검사합니다. 이 PC에 Docker나 별도 모델 키를 설치할 필요가 없습니다.'}</p>
  <div className="rc-security-actions">
   {!state.connected?<button className="rc-btn p" disabled={busy} onClick={()=>window.dispatchEvent(new CustomEvent('recoder:aws-connection'))}>AWS 연결 확인</button>:!state.ready?<button className="rc-btn p" disabled={busy||state.stackStatus!=='NOT_CREATED'} onClick={()=>send('managedSetup')}>관리형 실행 준비</button>:<button className="rc-btn p" disabled={busy||active} onClick={()=>send(mode==='audit'?'managedAuditStart':mode==='scans'?'managedScansStart':'managedStart')}>검사할 파일 검토 · 시작</button>}
   {active&&<button className="rc-btn" disabled={busy} onClick={()=>send('managedStop')}>진행 중인 검사 중단</button>}
   <button className="rc-btn" disabled={busy} onClick={()=>send('managedStatus')}>상태 확인</button>
  </div>
  {state.connected&&<p>{state.account} · {state.region}{!state.ready?` · 환경 ${state.stackStatus}`:''}</p>}
  {active&&state.activeRun?.mode!==mode&&<p role="status">{MODE[state.activeRun?.mode||'redteam']}가 진행 중입니다. 완료 후 새 검사를 시작할 수 있습니다.</p>}
  {state.stackStatus==='PERMISSION_REQUIRED'&&<p role="alert">기존 AWS 연결은 유지됩니다. 이 기능에 필요한 권한이 부족합니다. <button className="rc-btn" disabled={busy} onClick={()=>send('managedPermissions')}>필요한 권한 보기</button>에서 정책을 확인하고, 권한 추가 후 <button className="rc-btn" disabled={busy} onClick={()=>send('managedStatus')}>다시 확인</button>하세요.</p>}
  <p role="status" aria-live="polite">{busy?'요청 처리 중…':run?`${STATUS[run.status]||run.status} · ${PHASE[run.stage||'']||'상태 확인'}`:state.ready?'검사를 시작할 수 있습니다.':state.connected?'연결된 AWS 계정에 검사 환경을 준비하세요.':'상단의 기존 AWS 연결을 확인하세요.'}</p>
  {state.activeRun?.uncertain&&<p>실행 응답을 확인하지 못했습니다. 상태 확인으로 기존 작업을 복구하며, 확인 전에는 새 검사를 만들지 않습니다.</p>}
  {state.activeRun?.stopRequested&&<p>중단을 요청했습니다. AWS에서 작업이 종료됐는지 확인 중입니다.</p>}
  {error&&<p role="alert" className="rc-security-error">{error}</p>}
  {state.reportError&&<p role="alert" className="rc-security-error">{state.reportError}</p>}
  {report&&<><p>{report.status==='completed'?'검사 완료 · 발견 사항을 검토하세요.':`검사 결과: ${report.status}`}</p>{report.error&&<p className="rc-security-error">{report.error}</p>}
   {active&&<p role="status">{report.stage}</p>}
   {scans&&<>{scans.checks.map(c=><details key={c.tool}><summary>{c.tool} · {c.status==='completed'?`검사 완료 · 발견 ${c.count??c.findings.length}개`:c.status==='not_applicable'?'미실행 · 대상 없음':'검사 실패'}</summary>{c.message&&<p>{c.message}</p>}{c.findings.map((f,i)=><p key={i}>{f.severity} · {f.title}<br/>{f.file}{f.line?`:${f.line}`:''}<br/>{f.fix}</p>)}</details>)}{scans.policy&&<details><summary>OPA 정책 · {scans.policy.decision==='deny'?'차단':'검사 사본 조건 충족'}</summary><p>{scans.policy.scope}</p><p>{scans.policy.reason}</p></details>}</>}
   {report.audit&&<><p>{report.audit.scope} · {report.audit.files.length}개 파일 · 제외 {report.audit.skipped.length}개</p>
    {!report.audit.findings.length&&<p>독립 소스 검토가 일치한 발견 사항이 없습니다. 전체 코드의 안전을 보장하지 않습니다.</p>}
    {report.audit.findings.map((f,i)=><details key={i}><summary>{f.title} · {f.severity}</summary><p>{f.file}:{f.line} · {f.verification}</p><p>{f.impact}</p><p>{f.fix}</p><button className="rc-btn" onClick={()=>postMessage('map.openFile',{id:f.file,line:f.line})}>코드 위치 열기</button></details>)}
    {!!report.audit.needsValidation.length&&<details><summary>추가 검증 필요 {report.audit.needsValidation.length}개</summary>{report.audit.needsValidation.map((f,i)=><p key={i}>{f.title} — {f.reason}</p>)}</details>}
    <details><summary>검사 범위 · 모델 사용량</summary><p>호출 {report.audit.calls}회 · 입력 {report.audit.inputTokens} / 출력 {report.audit.outputTokens} 토큰</p><pre>{report.audit.files.join('\n')}</pre><p>제외한 파일</p><pre>{report.audit.skipped.join('\n')||'없음'}</pre></details>
   </>}
   {report.tasks.map(t=><details key={t.id}><summary>{t.title} · {t.status}</summary><pre>{t.result||'결과 없음'}</pre></details>)}</>}
  {run?.terminal&&!report&&<p>작업은 종료됐지만 결과 보고서가 없습니다. 검사를 통과한 것으로 처리하지 않습니다.</p>}
  {!!state.runs?.length&&<details><summary>실행 이력 {state.runs.length}개</summary><button className="rc-btn" disabled={busy} onClick={()=>send('managedReport')}>저장된 보고서 열기</button>{state.runs.map(r=><p key={r.id}>{new Date(r.createdAt).toLocaleString()} · {STATUS[r.status]||r.status} · {r.files}개 파일</p>)}</details>}
  <details><summary>실행 범위 · 비용 · 보관</summary>{mode==='audit'?<p>읽기 전용 소스 감사입니다. Dockerfile·도구 설치가 필요하지 않습니다. 최대 100개 파일·160KB를 모델에 전달하고, 최대 6개 후보를 각각 두 번 재검토합니다. 나머지는 미검증으로 남기며 실행 증명을 대신하지 않습니다.</p>:mode==='scans'?<p>시크릿·의존성·Dockerfile을 검사합니다. Dockerfile이 있으면 격리된 엔진에서 이미지를 빌드해 검사하고 SBOM을 만듭니다. 앱을 실행하거나 AI 모델을 호출하지 않습니다. Dockerfile이 없거나 검사에 실패한 항목은 통과로 표시하지 않습니다.</p>:<><p>프로젝트의 Dockerfile과 앱 포트를 사용합니다. 운영 DB와 .env는 전송하지 않습니다. 별도 DB·외부 서비스가 필수인 앱은 검사 사본이 시작되지 않을 수 있습니다.</p><p>빌드 중에는 의존성을 다운로드합니다. 앱 실행과 검사 전에는 외부 네트워크·메타데이터·Docker API 차단을 확인하며, 확인하지 못하면 검사를 시작하지 않습니다.</p></>}<p>{mode==='scans'?'CodeBuild와 저장·요청':'CodeBuild와 Bedrock'} 비용은 내 AWS 계정에 청구됩니다. 동시 실행 1개, 작업당 최대 60분이며 달러 기준 자동 상한은 없습니다. 소스는 종료 후 삭제를 시도하고 1일 만료, 결과와 로그는 7일 보관합니다.</p></details>
 </section>;
};
