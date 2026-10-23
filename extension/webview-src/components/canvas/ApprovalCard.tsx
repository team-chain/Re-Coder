import React, { useEffect, useRef, useState } from "react";
import { Checkbox } from "../Check";
import { regionMismatchWarning } from "../DeploymentCenter";
import { TrustCheck, TrustReport } from "../TrustCheck";
import { Box, Button, Collapse, Kit, KvRow, Line, Section, SheetHead, regionName } from "../ui/kit";
import { Icon } from "../ui/icons";
import { DEFAULT_PROTECTION, monthlyExtra } from "../EcsProtection";
import { Logo } from "./Scene";
export interface Config { target: "ecs" | "s3" | "github"; image_name: string; tag: string; aws_region: string; ecs_cluster: string; ecs_service: string; task_family: string; container_port: number; cpu: string; memory: string; environment: string; dir: string; env_vars?: Record<string,string>; secret_refs?: Record<string,string>; target_group_arn?: string; cloudfront_domain?: string; assign_public_ip?: boolean; subnet_ids?: string[]; security_group_ids?: string[];
  /** 2.0.25 보호·운영 */
  shield?: boolean; shield_level?: "loose" | "normal" | "strict"; alerts?: boolean; alert_email?: string;
  edge?: "none" | "alb" | "alb_waf"; waf_rate_limit?: number; waf_window?: number; edge_cost_approved?: boolean;
  https?: boolean; custom_domain?: string; database?: "task" | "rds"; db_cost_approved?: boolean }
export interface Issue { code: string; message: string; fix: string; remediation_available: boolean; proposal_id: string | null }
export interface Plan { id: string; config: Config; projectName: string; repository: string; branch: string; commit: string; dirty: boolean; account: string; coreRegion: string; preflight: { blocked?: boolean; summary?: string; reasons?: Issue[]; warnings?: Issue[] }; staticSite?: {files:string[];excluded:string;notes?:string[]}; targetState?: { exists: boolean|null; task_definition:string; images:Array<{image:string;digest:string}>; budget:Array<{name:string;limit:string;spent:string;unit:string;period:string}>|null; warnings:string[] };
  /** 2.0.35 배포 전 신뢰 점검(제로 트러스트) — 코어가 못 하면 없다. */
  trust?: TrustReport | null }
/** 승인 카드에 보이는 자동 수정 — [모두 적용] 이 한 번에 보낸다. */
export function fixableProposals(plan: Plan): string[] {
  return [...new Set([...(plan.preflight.reasons||[]), ...(plan.preflight.warnings||[])]
    .filter(issue=>issue.remediation_available&&issue.proposal_id).map(issue=>issue.proposal_id!))];
}
export function ApprovalCard({ plan, onApprove, onCancel, onFix, onFixAll }: { plan: Plan; onApprove: () => void; onCancel: () => void; onFix: (id:string)=>void; onFixAll?: (ids:string[])=>void }) {
  const ref=useRef<HTMLDivElement>(null), [ack,setAck]=useState(false);
  const fixable=fixableProposals(plan);
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
  const level=({loose:"느슨",normal:"보통",strict:"엄격"} as Record<string,string>)[c.shield_level||"normal"];
  const edgeOn=Boolean(c.edge&&c.edge!=="none");
  //: edge "alb" 는 HTTPS 를 위해 로드밸런서만 켠 것(WAF 없음) — 대규모 방패(alb_waf)와 요금이 다르다.
  const extra=c.target==='ecs' ? monthlyExtra({...DEFAULT_PROTECTION,edge:c.edge==="alb_waf"?"alb_waf":"none",https:Boolean(c.https)||c.edge==="alb",database:c.database==="rds"?"rds":"task"}) : 0;
  const where=c.target==='ecs' ? `${c.ecs_cluster} › ${c.ecs_service}` : c.target==='github' ? plan.repository : 'S3 정적 호스팅';
  const address=c.cloudfront_domain ? `https://${c.cloudfront_domain}` : c.https ? (c.custom_domain ? `https://${c.custom_domain} (발급 전에는 CloudFront 주소)` : "CloudFront 주소 (HTTPS)") : edgeOn ? "로드밸런서 주소 (HTTP)" : "태스크 IP (HTTP)";
  const blockers=plan.preflight.reasons||[], warnings=plan.preflight.warnings||[];
  const approveDisabled=plan.preflight.blocked || Boolean(warning&&!ack);
  return <div className="rc-modal-shade"><Kit className="rc-modal rc-approval" ref={ref} role="dialog" aria-modal="true" aria-labelledby="rc-approval-title">
    <button type="button" className="rc-drawer-x" onClick={onCancel} aria-label="승인 취소"><Icon name="close" size={16} /></button>
    <SheetHead icon={<Logo kind={c.target} />} title={<span id="rc-approval-title">{plan.preflight.blocked ? '배포 전에 해결할 문제가 있습니다' : '이 변경을 진행할까요?'}</span>}
      sub={<><b>{plan.projectName}</b> → <b>{where}</b></>} />
    <Section>
      <Box>
        {c.target==='github' ? <>
          <KvRow label="브랜치" value={plan.branch} />
          <KvRow label="커밋" value={<code>{plan.commit}</code>} />
          <KvRow label="범위" value={<>이미 커밋된 변경만 · 강제 푸시 없음{plan.dirty && ' · 미커밋 변경 제외'}</>} />
        </> : <>
          <KvRow label="계정" value={plan.account || '계정 조회 안 됨'} />
          <KvRow label="지역" value={<>{regionName(c.aws_region) || c.aws_region}{regionName(c.aws_region) && <small>{c.aws_region}</small>}</>} />
        </>}
        {c.target==='ecs' && <>
          <KvRow label="이미지" value={<code>{c.image_name.trim().toLowerCase()}:{c.tag}</code>} />
          <KvRow label="사양" value={`Fargate ${Number(c.cpu)/1024} vCPU · ${Number(c.memory)/1024} GB · 포트 ${c.container_port}`} />
          <KvRow label="주소" value={address} />
        </>}
        {c.target==='s3' && <>
          <KvRow label="폴더" value={c.dir || '워크스페이스 루트'} />
          <KvRow label="파일" value={`${plan.staticSite?.files.length ?? 0}개 · 정적 웹사이트로 공개`} />
        </>}
      </Box>
    </Section>
    {c.target==='ecs' && <Section title="보호 · 유료">
      <Box>
        <div className="rc-ap-list">
          <Line icon={c.shield===false ? "close" : "shield"} tone={c.shield===false ? undefined : "ok"}><span data-testid="approval-shield">{c.shield===false ? "기본 방패 끔" : `기본 방패 켬 — 강도 ${level} (앱이 PORT 를 읽을 때만)`}</span></Line>
          <Line icon={c.alerts===false ? "close" : "bell"} tone={c.alerts===false ? undefined : "ok"}>{c.alerts===false ? "감지 알림 끔" : `감지 알림 켬${c.alert_email ? ` · 메일 ${c.alert_email}` : " · VS Code·디스코드"}`}</Line>
          <Line icon="wall" tone={edgeOn ? "warn" : undefined}><span data-testid="approval-edge">{!edgeOn ? "대규모 방패 끔" : c.edge==="alb_waf" ? `대규모 방패 켬 — 로드밸런서 + WAF(IP당 ${Math.round((c.waf_window||300)/60)}분 ${(c.waf_rate_limit||2000).toLocaleString()}회) · 매달 요금(비용 확인함)` : "로드밸런서 켬 — HTTPS 용 · WAF 없음 · 매달 요금(비용 확인함)"}</span></Line>
          <Line icon="lock" tone={c.https ? "warn" : undefined}><span data-testid="approval-https">{c.https ? `HTTPS 주소 켬 — CloudFront${c.custom_domain ? ` · ${c.custom_domain}(인증서 발급 뒤 연결)` : ""}` : "HTTPS 주소 끔"}</span></Line>
          <Line icon="database" tone={c.database==="rds" ? "warn" : undefined}><span data-testid="approval-db">{c.database==="rds" ? "관리형 DB(RDS PostgreSQL) — 데이터 유지 · 매달 요금(비용 확인함)" : "DB: 태스크 안 임시 DB(staging) 또는 직접 넣은 접속 정보"}</span></Line>
        </div>
      </Box>
    </Section>}
    {c.target!=='github' && <Collapse summary="자세히 · 환경변수 · 롤백 대상 · 예산" testId="approval-more">
      <dl className="rc-ap-dl">
        {c.target==='ecs' && <><dt>배포 환경</dt><dd>{c.environment}</dd><dt>대상 그룹</dt><dd>{c.target_group_arn || "미설정"}</dd>
          <dt>앱 환경변수</dt><dd>{Object.keys(c.env_vars||{}).join(", ") || "없음"}</dd>
          <dt>비밀값 참조</dt><dd>{Object.entries(c.secret_refs||{}).map(([key,arn])=><div key={key}>{key}: <code>{arn}</code></div>)}{!Object.keys(c.secret_refs||{}).length && "없음"}</dd>
          <dt>롤백 대상</dt><dd>{plan.targetState?.task_definition ? <><code>{plan.targetState.task_definition}</code>{plan.targetState.images.map((image,i)=><div key={i}>{image.image}<br/><code>{image.digest || '이미지 digest 미확인'}</code></div>)}</> : plan.targetState?.exists===false ? '새 서비스 · 이전 배포 없음' : '현재 서비스 이미지 ID를 확인하지 못했습니다.'}</dd></>}
        {c.target==='s3' && <><dt>공개 범위</dt><dd>정적 웹사이트로 공개됩니다. 같은 프로젝트의 기존 파일을 갱신합니다.</dd><dt>파일 목록</dt><dd>{plan.staticSite?.files.map(file=><div key={file}>{file}</div>)}{plan.staticSite?.excluded&&<p>{plan.staticSite.excluded}</p>}{plan.staticSite?.notes?.map(note=><p key={note} className="rk-help">{note}</p>)}</dd></>}
        <dt>AWS 예산</dt><dd>{plan.targetState?.budget?.length ? plan.targetState.budget.map((b,i)=><div key={i}>{b.name} · {b.period}: {b.spent ?? '미확인'} / {b.limit ?? '미확인'} {b.unit}</div>) : '이 화면에서 조회되지 않았습니다. AWS 비용·예산을 확인하세요.'}<div>실행 중인 리소스에는 비용이 발생합니다.</div></dd>
      </dl>
    </Collapse>}
    {plan.preflight.summary && <Line icon="check">{plan.preflight.summary}</Line>}
    <TrustCheck report={plan.trust} />
    {plan.targetState?.warnings.map((w,i)=><Line key={i} icon="alert" tone="warn">{w}</Line>)}
    {/* 막는 문제와 참고 경고를 나눠 보여 준다 — 예전엔 한 목록이라 무엇 때문에 승인 버튼이 꺼졌는지 알 수 없었다(실기기 ECS). */}
    {/* 실기기 2.0.12: 자동 수정 4건을 하나씩 누르고 다시 검사해야 했다 — 한 번에 적용하고 다시 판정한다. */}
    {onFixAll && fixable.length > 1 && <div className="rc-ap-fix"><div><b>자동으로 고칠 수 있는 항목 {fixable.length}건</b><p>한 번에 적용하고 다시 검사합니다. 원본은 .recoder/backups 에 남습니다.</p></div><Button small onClick={()=>onFixAll(fixable)}>자동 수정 {fixable.length}건 모두 적용</Button></div>}
    {plan.preflight.blocked && blockers.length > 0 && <section className="rc-ap-block" aria-label="배포를 막는 문제">
      <h4><Icon name="alert" size={14} /> 배포를 막는 문제 {blockers.length}건 — 해결해야 승인할 수 있습니다</h4>
      {blockers.map((issue,i)=><div className="rc-ap-issue" key={`b-${issue.code}-${i}`}><b>{issue.message}</b><p>{issue.fix}</p>{issue.remediation_available&&issue.proposal_id&&<Button small variant="ghost" onClick={()=>onFix(issue.proposal_id!)}>제안된 수정 적용</Button>}</div>)}
      <p className="rk-help">개발 화면의 [배포 준비 점검]에서 자동 수정할 수 있는 항목은 한 번에 고칠 수 있습니다.</p>
    </section>}
    {warnings.length > 0 && <Collapse summary={`참고 ${warnings.length}건 — 배포를 막지 않습니다`} open={!plan.preflight.blocked} testId="approval-warnings">
      {warnings.map((issue,i)=><div className="rc-ap-issue" key={`w-${issue.code}-${i}`}><b>{issue.message}</b><p>{issue.fix}</p>{issue.remediation_available&&issue.proposal_id&&<Button small variant="ghost" onClick={()=>onFix(issue.proposal_id!)}>제안된 수정 적용</Button>}</div>)}
    </Collapse>}
    {!plan.preflight.blocked && blockers.map((issue,i)=><div className="rc-ap-issue" key={`r-${issue.code}-${i}`}><b>{issue.message}</b><p>{issue.fix}</p>{issue.remediation_available&&issue.proposal_id&&<Button small variant="ghost" onClick={()=>onFix(issue.proposal_id!)}>제안된 수정 적용</Button>}</div>)}
    {warning&&<div className="rc-ap-region"><Line icon="globe" tone="warn">현재 연결 리전({plan.coreRegion})과 배포 리전({c.aws_region})이 다릅니다. 선택한 배포 리전을 확인하고 아래 항목을 체크하세요.</Line><Checkbox tone="warn" checked={ack} onChange={setAck} label="이 리전으로 배포하는 것을 확인했습니다."/></div>}
    <div className="rk-foot">
      {c.target!=='github' && <div className="rk-cost"><span>예상 추가 요금</span><b data-testid="approval-extra">{c.target==='s3' ? '거의 없음 · 저장·전송량만큼' : extra ? `월 약 $${extra}+ · Fargate 실행비 별도` : '없음 · Fargate 실행비만'}</b></div>}
      <div className="rc-ap-actions">
        <button type="button" className="rk-btn ghost" onClick={onCancel}>취소</button>
        <button type="button" className="rk-btn" data-testid="approval-approve" disabled={approveDisabled} onClick={onApprove}>{c.target==='github' ? '승인하고 푸시' : '승인하고 배포'}</button>
      </div>
      <p className="rk-hint">승인 후 보안·정책 검사를 거칩니다. 검사 미실행은 통과가 아니며, 서버의 차단 판정을 우회하지 않습니다.</p>
    </div>
  </Kit></div>;
}
