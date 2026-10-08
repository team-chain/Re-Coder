import React, {useCallback, useEffect, useRef, useState} from 'react';
import {useVSCodeApi} from '../hooks/useVSCodeApi';

export type RepairStage = 'build'|'run'|'ecs'|'iam'|'s3';
export interface RepairResult {
  id: string; status: string; approval_required: boolean; workspace: string;
  retrieval_mode: string; llm_calls: number; estimated_cost_usd: number|null; cache_hit: boolean;
  sources: Array<{id:string;title:string;url:string;kind:string;attribution?:string;license_url?:string;source_date?:string;version?:string;text?:string}>;
  diffs: Record<string,string>;
  suggestion?: {explanation:string;guidance:string[]};
  rule_issues?: Array<{code:string;message:string;fix:string}>;
  verification?: {passed:boolean;output?:string};
}
const labels: Record<string,string> = {
  ready_for_approval:'재빌드 통과 · 변경 내용을 확인하세요', applied:'수정 적용 완료 · 다시 배포할 수 있습니다',
  unresolved:'수정안을 검증하지 못했습니다', rule_action_required:'배포 준비 점검의 해결 안내를 확인하세요',
  environment_verification_required:'실제 실행 환경 검증이 필요합니다', manual_action_required:'직접 확인할 항목이 있습니다',
  failure_not_reproduced:'현재 프로젝트에서 빌드 오류가 재현되지 않았습니다', verification_unavailable:'빌드 검증 환경을 사용할 수 없습니다',
  no_document_evidence:'관련 공식 문서를 찾지 못했습니다', insufficient_context:'수정에 필요한 파일 정보가 부족합니다',
  workspace_changed:'프로젝트가 변경됐습니다. 다시 검증하세요', budget_exhausted:'설정한 호출 예산에 도달했습니다',
};
export function canApproveRepair(result:RepairResult):boolean {
  return result.status==='ready_for_approval' && result.approval_required && result.verification?.passed===true;
}
export function documentLink(value:string):string|undefined {
  try { const u=new URL(value);return u.protocol==='https:'&&['docs.docker.com','docs.npmjs.com','nodejs.org','vite.dev','expressjs.com','docs.aws.amazon.com'].includes(u.hostname)?u.href:undefined; }catch{return undefined;}
}
export function RepairDetails({result,onApprove,busy=false}:{result:RepairResult;onApprove:()=>void;busy?:boolean}) {
  return <div aria-label="문서 근거 수정안">
    <p role="status"><strong>{labels[result.status]||result.status}</strong></p>
    {result.suggestion&&<><p>{result.suggestion.explanation}</p><ul>{result.suggestion.guidance.map((s,i)=><li key={i}>{s}</li>)}</ul></>}
    {result.rule_issues?.map(i=><p key={i.code}>{i.message}<br/>{i.fix}</p>)}
    {Object.entries(result.diffs||{}).map(([name,diff])=><details key={name} open><summary>{name}</summary><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',maxHeight:300,overflow:'auto'}}>{diff}</pre></details>)}
    {!!result.sources?.length&&<><b>근거 문서</b><ul>{result.sources.map(s=><li key={s.id}>{documentLink(s.url)?<a href={documentLink(s.url)} target="_blank" rel="noreferrer">{s.title}</a>:s.title}<small> · {s.kind==='authored_summary'?'직접 작성한 요약':'공식 문서 발췌'}{s.source_date?` · 원문 기준 ${s.source_date.slice(0,10)}`:''}{s.attribution?` · ${s.attribution}`:''}</small>{s.text&&<details><summary>근거 단락 보기</summary><blockquote style={{whiteSpace:'pre-wrap'}}>{s.text}</blockquote></details>}</li>)}</ul></>}
    <p>AI 호출 {result.llm_calls}회 · {result.estimated_cost_usd==null?'비용 미확인':`추정 비용 $${result.estimated_cost_usd.toFixed(5)}`}{result.cache_hit?' · 이전 해결 재사용':''} · {result.retrieval_mode==='hybrid'?'키워드 + 의미 검색':'키워드 검색'}</p>
    {canApproveRepair(result)&&<button disabled={busy} onClick={onApprove}>{busy?'적용 중…':'검증된 수정 승인·적용'}</button>}
  </div>;
}

export function GroundedRepairPanel({log,stage='build'}:{log:string;stage?:RepairStage}) {
  const {postMessage,useMessage}=useVSCodeApi();
  const [result,setResult]=useState<RepairResult|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const request=useRef('');
  useEffect(()=>{request.current='';setResult(null);setBusy(false);setError('');},[log,stage]);
  useMessage(useCallback(message=>{
    if(message.type==='canvas.projectChanged') {
      request.current='';setResult(null);setBusy(false);setError('');
      return;
    }
    const p=message.payload as {requestId?:string;result?:RepairResult;message?:string};
    if(!request.current||p?.requestId!==request.current)return;
    if(message.type==='repair.result'&&p.result){setResult(p.result);setBusy(false);request.current='';}
    if(message.type==='repair.error'){setError(p.message||'수정 요청에 실패했습니다');setBusy(false);request.current='';}
  },[]));
  const send=(type:string,payload:Record<string,unknown>)=>{
    if(request.current)return;
    const requestId=`repair-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    request.current=requestId;setBusy(true);setError('');postMessage(type,{...payload,requestId});
  };
  return <section aria-label="문서 근거로 오류 수정" style={{marginTop:12,padding:10,border:'1px solid var(--vscode-panel-border,#555)',borderRadius:5,color:'var(--vscode-foreground,#eee)'}}>
    <b>문서 근거로 오류 수정</b>
    <p>공식 문서를 찾아 수정하고 복사본에서 검증합니다. AI 사용료가 발생할 수 있으며, 파일 적용에는 승인이 필요합니다.</p>
    <button disabled={busy||!log.trim()} onClick={()=>{setResult(null);send('repair.prepare',{log:log.slice(0,1000000),stage});}}>{busy?'수정·검증 중… (빌드에 몇 분 걸릴 수 있습니다)':'수정안 만들고 검증'}</button>
    {error&&<p role="alert">{error}</p>}
    {result&&<RepairDetails result={result} busy={busy} onApprove={()=>send('repair.approve',{runId:result.id,approved:true})}/>}
  </section>;
}
