import React, { useEffect, useRef, useState } from "react";
import { regionMismatchWarning } from "../DeploymentCenter";
export interface Config { target: "ecs" | "s3" | "github"; image_name: string; tag: string; aws_region: string; ecs_cluster: string; ecs_service: string; task_family: string; container_port: number; cpu: string; memory: string; environment: string; dir: string }
export interface Issue { code: string; message: string; fix: string; remediation_available: boolean; proposal_id: string | null }
export interface Plan { id: string; config: Config; projectName: string; repository: string; branch: string; commit: string; dirty: boolean; account: string; coreRegion: string; preflight: { blocked?: boolean; summary?: string; reasons?: Issue[]; warnings?: Issue[] }; staticSite?: {files:string[];excluded:string}; targetState?: { exists: boolean|null; task_definition:string; images:Array<{image:string;digest:string}>; budget:Array<{name:string;limit:string;spent:string;unit:string;period:string}>|null; warnings:string[] } }
export function ApprovalCard({ plan, onApprove, onCancel, onFix }: { plan: Plan; onApprove: () => void; onCancel: () => void; onFix: (id:string)=>void }) {
  const ref=useRef<HTMLDivElement>(null), [ack,setAck]=useState(false);
  const c=plan.config, warning=c.target!=='github' ? regionMismatchWarning(plan.coreRegion,c.aws_region) : null;
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLButtonElement>('button')?.focus();
    const key=(e:KeyboardEvent)=>{
      if(e.key==='Escape') { e.preventDefault(); onCancel(); }
      if(e.key!=='Tab') return;
      const items=Array.from(ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input,a[href]') || []);
      if(!items.length) return;
      if(e.shiftKey&&document.activeElement===items[0]) { e.preventDefault(); items[items.length-1].focus(); }
      else if(!e.shiftKey&&document.activeElement===items[items.length-1]) { e.preventDefault(); items[0].focus(); }
    };
    document.addEventListener('keydown',key); return()=>{document.removeEventListener('keydown',key);previous?.focus();};
  },[onCancel]);
  return <div className="rc-modal-shade"><div className="rc-panel rc-modal" ref={ref} role="dialog" aria-modal="true" aria-labelledby="rc-approval-title">
    <header><h3 id="rc-approval-title">{plan.preflight.blocked ? '배포 전에 해결할 문제가 있습니다' : '이 변경을 진행할까요?'}</h3><button onClick={onCancel} aria-label="승인 취소">닫기</button></header>
    <p><b>{plan.projectName}</b> → <b>{c.target==='ecs' ? `${c.ecs_cluster} › ${c.ecs_service}` : c.target==='github' ? plan.repository : 'S3 정적 호스팅'}</b></p>
    <dl>
      {c.target==='github' ? <><dt>브랜치</dt><dd>{plan.branch}</dd><dt>커밋</dt><dd><code>{plan.commit}</code></dd><dt>범위</dt><dd>이미 커밋된 변경만 푸시 · 강제 푸시 없음{plan.dirty && ' · 미커밋 변경은 포함되지 않음'}</dd></> : <><dt>계정 · 리전</dt><dd>{plan.account || '계정 조회 안 됨'} · {c.aws_region}</dd></>}
      {c.target==='ecs' && <><dt>이미지</dt><dd><code>{c.image_name}:{c.tag}</code></dd><dt>사양</dt><dd>Fargate {Number(c.cpu)/1024} vCPU · {Number(c.memory)/1024} GB · 포트 {c.container_port}</dd><dt>배포 환경</dt><dd>{c.environment}</dd><dt>롤백 대상</dt><dd>{plan.targetState?.task_definition ? <><code>{plan.targetState.task_definition}</code>{plan.targetState.images.map((image,i)=><div key={i}>{image.image}<br/><code>{image.digest || '이미지 digest 미확인'}</code></div>)}</> : plan.targetState?.exists===false ? '새 서비스 · 이전 배포 없음' : '현재 서비스 이미지 ID를 확인하지 못했습니다.'}</dd></>}
      {c.target==='s3' && <><dt>올릴 폴더</dt><dd>{c.dir || '워크스페이스 루트'}</dd><dt>공개 범위</dt><dd>정적 웹사이트로 공개됩니다. 같은 프로젝트의 기존 파일을 갱신합니다.</dd><dt>업로드 파일</dt><dd>{plan.staticSite?.files.length ?? 0}개<details><summary>파일 목록</summary>{plan.staticSite?.files.map(file=><div key={file}>{file}</div>)}</details>{plan.staticSite?.excluded&&<p>{plan.staticSite.excluded}</p>}</dd></>}
      {c.target!=='github' && <><dt>AWS 예산</dt><dd>{plan.targetState?.budget?.length ? plan.targetState.budget.map((b,i)=><div key={i}>{b.name} · {b.period}: {b.spent ?? '미확인'} / {b.limit ?? '미확인'} {b.unit}</div>) : '이 화면에서 조회되지 않았습니다. AWS 비용·예산을 확인하세요.'}<div>실행 중인 리소스에는 비용이 발생합니다.</div></dd></>}
    </dl>
    {plan.preflight.summary && <p className="rc-muted">{plan.preflight.summary}</p>}
    {plan.targetState?.warnings.map((warning,i)=><p className="rc-note" key={i}>{warning}</p>)}
    {[...(plan.preflight.reasons||[]),...(plan.preflight.warnings||[])].map((issue,i)=><div className="rc-finding" key={`${issue.code}-${i}`}><b>{issue.message}</b><p>{issue.fix}</p>{issue.remediation_available&&issue.proposal_id&&<button onClick={()=>onFix(issue.proposal_id!)}>제안된 수정 적용</button>}</div>)}
    {warning&&<div className="rc-note"><p>현재 연결 리전({plan.coreRegion})과 배포 리전({c.aws_region})이 다릅니다. 선택한 배포 리전을 확인하고 아래 항목을 체크하세요.</p><label><input type="checkbox" checked={ack} onChange={e=>setAck(e.target.checked)}/> 이 리전으로 배포하는 것을 확인했습니다.</label></div>}
    <p className="rc-muted">승인 후 기존 보안·정책 검사를 거칩니다. 검사 미실행과 통과는 구분되며 서버의 차단 판정을 우회하지 않습니다.</p>
    <div className="rc-actions"><button className="rc-primary" disabled={plan.preflight.blocked || Boolean(warning&&!ack)} onClick={onApprove}>{c.target==='github' ? '승인하고 푸시' : '승인하고 배포'}</button><button onClick={onCancel}>취소</button></div>
  </div></div>;
}
