import React, {useCallback, useEffect, useRef, useState} from 'react';
import {useVSCodeApi} from '../hooks/useVSCodeApi';
import {ManagedRedTeamPanel} from './ManagedRedTeamPanel';
interface LocalReport {session:string;target:string;createdAt:string;phase:string;step:string;provider?:string;outcome?:string;error?:string;flow?:{status:string};tasks?:Array<{id:string;title:string;status:string;result:string}>;evidence?:Array<{id:string;type:string;message:string;result:string}>}
interface State {localMode?:boolean;local?:LocalReport;localHistory?:LocalReport[];configured?: boolean; server?: string; runs?: Array<{id?: string; target: string; createdAt: string; uncertain?: boolean; stopped?: boolean; terminal?: boolean}>; flow?: {id: string; title: string; status: string}; tasks?: Array<{id: string; title: string; status: string; result: string}>}
const STATUS: Record<string,string> = {created:'실행 준비',running:'검사 중',waiting:'입력 대기 또는 중단',finished:'실행 완료 · 결과 검토 필요',failed:'실행 실패'};
const OUTCOME:Record<string,string>={finished:'검사 완료',failed:'검사 실패',cancelled:'검사 중단',timed_out:'30분 제한으로 중단',interrupted:'작업 종료 확인 필요',needs_input:'추가 입력 필요 · 전체 검사 미완료'};
export const RedTeamPanel: React.FC = () => {
 const [mode,setMode]=useState<'local'|'aws'|null>(null);
 return <section className="rc-security-detail"><p>검사할 앱에 맞는 실행 방식을 선택하세요.</p><div className="rc-security-actions"><button className="rc-btn" aria-pressed={mode==='local'} onClick={()=>setMode('local')}>내 PC의 테스트 앱</button><button className="rc-btn" aria-pressed={mode==='aws'} onClick={()=>setMode('aws')}>현재 AWS 연결로 앱 사본 검사</button></div>{mode==='local'?<LegacyRedTeamPanel/>:mode==='aws'?<ManagedRedTeamPanel/>:<p>로컬은 Docker 환경을 사용하고, AWS 방식은 검사 전용 환경과 비용을 검토한 뒤 실행합니다.</p>}</section>;
};
const LegacyRedTeamPanel: React.FC = () => {
 const {postMessage,useMessage}=useVSCodeApi(); const [state,setState]=useState<State>({}); const [busy,setBusy]=useState(false); const [error,setError]=useState('');
 const pending=useRef(''); const [serial,setSerial]=useState(0);const [localMode,setLocalMode]=useState(true);
 const send=useCallback((action:string)=>{if(pending.current)return; const id=`redteam-${Date.now()}-${Math.random()}`;pending.current=id;setBusy(true);setError('');postMessage('security.redteam',{action,requestId:id});},[postMessage]);
 useMessage(useCallback(msg=>{if(msg.type==='canvas.projectChanged'){pending.current='';setState({});setBusy(false);send(localMode?'localStatus':'status');return;}if(msg.type!=='security.redteamResult')return;const p=msg.payload as State & {requestId:string;error?:string;cancelled?:boolean};if(p.requestId!==pending.current)return;pending.current='';setBusy(false);if(p.error){setError(p.error);}else if(!p.cancelled){setState(p);}setSerial(n=>n+1);},[send,localMode]));
 useEffect(()=>{send(localMode?'localStatus':'status');},[send,localMode]);
 const run=state.runs?.[0];
 const local=state.local; const localActive=!!local&&!['removed','cleanup_failed'].includes(local.phase);
 useEffect(()=>{if(busy || error || (localMode?!localActive:!run?.id||run.terminal))return;const timer=setTimeout(()=>{if(document.visibilityState==='visible')send(localMode?'localStatus':'status');else setSerial(n=>n+1);},3000);return()=>clearTimeout(timer);},[busy,run?.id,run?.terminal,error,serial,send,localMode,localActive]);
 if(localMode)return <div className="rc-security-detail" aria-busy={busy}>
  <h3>내 앱 레드팀 테스트</h3>
  <p>로컬 앱을 선택하면 검사 전용 PentAGI Docker 환경을 자동으로 준비합니다. 네트워크 범위는 별도 격리가 필요합니다.</p>
  <div className="rc-security-actions">
   <button className="rc-btn p" disabled={busy||!!local&&local.phase!=='removed'} onClick={()=>send('localStart')}>{local?'다시 테스트':'레드팀 테스트 시작'}</button>
   {localActive&&<button className="rc-btn" disabled={busy} onClick={()=>send('localStop')}>중단 · 환경 삭제</button>}
   {local?.phase==='cleanup_failed'&&<button className="rc-btn" disabled={busy} onClick={()=>send('localCleanup')}>환경 정리 재시도</button>}
   {local&&<button className="rc-btn" disabled={busy} onClick={()=>send('localOpenReport')}>저장된 보고서</button>}
   <button className="rc-btn" disabled={busy} onClick={()=>send('localStatus')}>상태 확인</button>
  </div>
  <p role="status" aria-live="polite">{busy?'요청 처리 중…':local?`${local.outcome?OUTCOME[local.outcome]+' · ':''}${local.step}`:'대상 선택 → 환경 준비 → 검사 → 결과 저장 · 환경 삭제'}</p>
  {(error||local?.error)&&<p role="alert" className="rc-security-error">{error||local?.error}</p>}
  {local&&<p>{local.target} · {new Date(local.createdAt).toLocaleString()}</p>}
  {local?.phase==='removed'&&<p>검사 컨테이너·DB·볼륨·네트워크를 삭제했습니다. 아래 결과와 재사용할 이미지 캐시는 남아 있습니다.</p>}
  {local?.flow?.status==='waiting'&&localActive&&<p>검사 도구가 입력을 기다리고 있습니다. 필요한 경우 중단한 뒤 대상을 다시 확인하세요.</p>}
  {local?.tasks?.map(t=><details key={t.id}><summary>{t.title} · {STATUS[t.status]||t.status}</summary><pre>{t.result||'아직 결과가 없습니다.'}</pre></details>)}
  {local?.evidence?.map(m=><details key={m.id}><summary>{m.type==='ask'?'추가 확인 요청':'상세 보고서'}</summary><pre>{m.message}{m.result?'\n'+m.result:''}</pre></details>)}
  {!!state.localHistory?.length&&<details><summary>이전 검사 {state.localHistory.length}개</summary>{state.localHistory.map(r=><details key={r.session}><summary>{new Date(r.createdAt).toLocaleString()} · {OUTCOME[r.outcome||'']||r.step}</summary><p>{r.target} · {r.step}</p>{r.tasks?.map(t=><pre key={t.id}>{t.title}{'\n'}{t.result}</pre>)}</details>)}</details>}
  <details><summary>실행 조건 · 연결 설정</summary><p>Docker Desktop Linux 엔진과 OpenAI 또는 Anthropic 키를 사용합니다. 환경 준비는 최대 60분, 검사는 최대 30분입니다. 모델 사용료가 발생할 수 있습니다. 결과는 검토할 근거이며 안전 보증이 아닙니다.</p><p>대상 입력은 로컬 주소만 허용하지만 PentAGI 에이전트의 외부 네트워크 접근을 방화벽으로 차단하지 않습니다. 운영·공유망이나 민감 정보가 있는 환경에서 실행하지 마세요.</p><p>VS Code를 닫아도 검사와 정리가 이어집니다. PC나 Docker가 종료되면 다시 켠 후 환경 정리를 재시도할 수 있습니다.</p><div className="rc-security-actions"><button className="rc-btn" disabled={busy} onClick={()=>send('localKey')}>모델 키 변경</button><button className="rc-btn" disabled={busy||localActive} onClick={()=>{setState({});setLocalMode(false);}}>별도 PentAGI 서버 연결</button></div></details>
 </div>;
 return <div className="rc-security-detail" aria-busy={busy}>
  <h3>내 테스트 앱 레드팀 검사</h3><p>PentAGI가 실행 중인 앱을 점검합니다. 결과는 검토할 근거이며 배포 승인이나 안전 보증이 아닙니다.</p>
  <button className="rc-btn" disabled={busy} onClick={()=>{setState({});setLocalMode(true);}}>로컬 Docker 자동 실행으로 돌아가기</button>
  <div className="rc-security-actions"><button className="rc-btn" disabled={busy} onClick={()=>send('configure')}>{state.configured?'연결 변경':'PentAGI 연결'}</button>
   <button className="rc-btn p" disabled={busy||!state.configured||!!run&&!run.terminal} onClick={()=>send('start')}>대상 선택 · 실행 검토</button>
   <button className="rc-btn" disabled={busy} onClick={()=>send('status')}>상태 확인</button>
   {run?.id&&!run.terminal&&<button className="rc-btn" disabled={busy} onClick={()=>send('stop')}>검사 중단</button>}
   {(run?.uncertain||error)&&<button className="rc-btn" disabled={busy} onClick={()=>send('recover')}>실행 ID로 복구</button>}
  </div>
  <p role="status">{busy?'요청 처리 중…':state.configured?`연결됨 · ${state.server}`:'별도 PentAGI 서버와 모델 공급자 설정이 필요합니다. 기존 검사는 연결 없이 사용할 수 있습니다.'}</p>
  {error&&<p role="alert" className="rc-security-error">{error}</p>}
  {run&&<p>{run.target} · {new Date(run.createdAt).toLocaleString()}<br/>{run.uncertain?'실행 여부 미확인 — 새로 실행하지 말고 서버에서 실행 ID를 복구하세요.':STATUS[state.flow?.status||'']||'상태 확인 필요'}{run.stopped?' · 중단 요청 수락됨':''}</p>}
  {state.tasks?.map(t=><details key={t.id}><summary>{t.title} · {STATUS[t.status]||t.status}</summary><pre>{t.result||'아직 결과가 없습니다.'}</pre></details>)}
  {(state.runs?.length??0)>1&&<details><summary>이전 실행 {state.runs!.length-1}개</summary>{state.runs!.slice(1).map((r,i)=><p key={i}>{r.createdAt} · {r.target} · Flow {r.id||'미확인'}</p>)}</details>}
  <small>요청은 PentAGI 서버에서 실행됩니다. VS Code를 닫아도 계속되며, 대상 네트워크 격리와 모델 비용 한도는 서버에서 설정해야 합니다.</small>
 </div>;
};
