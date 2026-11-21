import React,{useEffect,useRef} from 'react';
import type {PentagiAssessment} from '../../src/security/pentagiAssessmentTypes';
import {RedTeamTrafficView} from './RedTeamTrafficView';
import type {RedTeamHttpEvent} from '../../src/security/pentagiAssessmentTypes';
type Task={id:string;title:string;status:string;result:string};
export interface RedTeamRunViewProps {active:boolean;phase?:string;step?:string;outcome?:string;target?:string;tasks?:Task[];assessment?:PentagiAssessment;cleaned?:boolean;updatedAt?:string;httpEvents?:RedTeamHttpEvent[];isolationVerified?:boolean;controls?:React.ReactNode;}
const names:Record<string,string>={discovery:'경로 탐색',sql_injection:'SQL 입력 검사',authentication:'인증',authorization:'권한',input_handling:'입력 처리'};
const states:Record<string,string>={created:'대기',running:'검사 중',waiting:'추가 입력 대기',finished:'작업 완료',failed:'실패'};
const clean=(t?:string)=>(t||'').replace(/https?:\/\/\S+/g,'').replace(/\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b/g,'').replace(/\s+(at|on|of|for)\s*$/i,'').replace(/\s{2,}/g,' ').trim();
/** 작업 목록 이름: 한국어면 그대로(주소만 뺌), 영어 원문이면 「AI 공격 작업 N」 — 원문은 펼친 기록에 남는다. */
export function taskLabel(title:string,index:number):string{
 const t=clean(title);
 return t&&/[가-힣]/.test(t)?t:`AI 공격 작업 ${index+1}`;
}
/** 화면 자막: 한국어 작업 이름만 그대로 쓰고, 영어 원문·내부 주소(IP/URL)는 보여 주지 않는다. */
export function stageCaption(title:string|undefined,step:string|undefined):string|undefined{
 const t=clean(title);
 if(t&&/[가-힣]/.test(t))return `${t} 진행 중`;
 return clean(step)||(t?'공격 작업 진행 중':undefined);
}
/** Animates only actual active tasks. No invented agent counts, attacks or progress percentages. */
export function RedTeamRunView({active,phase,step,outcome,target,tasks=[],assessment,cleaned,updatedAt,httpEvents,isolationVerified,controls}:RedTeamRunViewProps){
 const element=useRef<HTMLElement>(null);
 useEffect(()=>{element.current?.scrollIntoView?.({block:'start',behavior:'auto'});},[]);
 const finished=tasks.filter(t=>t.status==='finished'&&t.result.trim()).length;
 const visibleTasks=[...tasks.filter(t=>t.status==='running'),...tasks.filter(t=>t.status!=='running').slice(-6)].slice(0,6);
 const hiddenTasks=tasks.filter(t=>!visibleTasks.includes(t));
 const stage=cleaned?4:phase==='cleaning'||phase==='finalizing'?4:phase==='saving'?3:tasks.length?2:phase==='running'?1:0;
 const blocked=!!outcome&&outcome!=='finished'&&outcome!=='completed';
 const current=tasks.find(t=>t.status==='running');
 return <section ref={element} className="rc-rt-run" aria-label="PentAGI 진단 진행" style={{scrollMarginTop:16}}>
  <style>{`.rc-rt-run{margin:18px 0;padding:20px;border:1px solid var(--rc-line2,rgba(160,190,220,.16));border-radius:16px;background:var(--rc-tile,transparent)}.rc-rt-run header{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}.rc-rt-run h4{margin:0 0 4px;font-size:18px;font-weight:700;letter-spacing:-.02em;display:flex;align-items:center;gap:10px}.rc-rt-live{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:500;color:var(--rc-bad,#F27272);padding:2px 9px;border-radius:999px;border:1px solid color-mix(in srgb,var(--rc-bad,#F27272) 35%,transparent);background:color-mix(in srgb,var(--rc-bad,#F27272) 10%,transparent)}.rc-rt-live i{width:6px;height:6px;border-radius:50%;background:currentColor;animation:rc-rt-pulse 1.2s ease-in-out infinite}.rc-rt-run small{color:var(--rc-fg3,#67768A)}.rc-rt-flow{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:4px;padding:0;margin:18px 0 4px;list-style:none}.rc-rt-flow li{padding-top:10px;border-top:3px solid var(--rc-line2,rgba(160,190,220,.16));font-size:12px;color:var(--rc-fg3,#67768A);text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.rc-rt-flow li[data-done=true]{border-top-color:var(--rc-ok,#3DD68C);color:var(--rc-fg2,#9AA8B8)}.rc-rt-flow li[data-current=true]{border-top-color:var(--rc-teal,#35A9D1);color:var(--rc-fg,#EEF3F8)}.rc-rt-dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:6px;background:currentColor;vertical-align:1px}.rc-rt-flow li[data-current=true] .rc-rt-dot,.rc-rt-task[data-active=true] .rc-rt-dot{animation:rc-rt-pulse 1.3s ease-in-out infinite}.rc-rt-status{margin:6px 0 0;font-size:12.5px;color:var(--rc-fg2,#9AA8B8)}.rc-rt-tasks{display:grid;gap:6px;margin-top:8px}.rc-rt-task{display:flex;gap:10px;align-items:baseline;border-top:1px solid var(--rc-line,rgba(160,190,220,.09));padding:8px 2px;min-width:0}.rc-rt-task small{flex:none;width:92px}.rc-rt-task[data-active=true] small{color:var(--rc-teal,#35A9D1)}.rc-rt-task strong{font-weight:500;overflow-wrap:anywhere;flex:1;min-width:0}.rc-rt-task details{margin:0}.rc-rt-run details{margin-top:8px}.rc-rt-run summary{cursor:pointer;overflow-wrap:anywhere;color:var(--rc-fg2,#9AA8B8);font-size:12.5px}.rc-rt-run pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:300px;overflow:auto;padding:10px;border-radius:8px;background:var(--rc-tile2,rgba(148,178,210,.065));font:12px/1.6 var(--rc-mono,monospace)}.rc-rt-coverage{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.rc-rt-coverage span{border:1px solid var(--rc-line2,rgba(160,190,220,.16));border-radius:20px;padding:5px 10px;font-size:12px}.rc-rt-issues{border-top:1px solid var(--rc-line,rgba(160,190,220,.09));margin-top:16px;padding-top:16px}.rc-rt-warning{color:var(--rc-warn,#F2B544)!important}.rc-rt-target{overflow-wrap:anywhere}.rc-rt-evidence{display:grid;grid-template-columns:1fr 1fr;gap:10px}.rc-rt-run h5{margin:14px 0 7px;font-size:13px}@keyframes rc-rt-pulse{50%{opacity:.3;transform:scale(.8)}}@media(prefers-reduced-motion:reduce){.rc-rt-run *{animation:none!important}}@media(max-width:700px){.rc-rt-flow li{font-size:0}.rc-rt-flow li[data-current=true]{font-size:11px}.rc-rt-evidence{grid-template-columns:1fr}.rc-rt-run{padding:14px}.rc-rt-task small{width:auto}}`}</style>
  <header><div><h4>공격 테스트{active&&<span className="rc-rt-live"><i/>진행 중</span>}</h4><small className="rc-rt-target">{target||'승인한 테스트 앱'}{updatedAt?` · 마지막 수신 ${new Date(updatedAt).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})}`:''}</small></div><div style={{display:'flex',alignItems:'center',gap:12,flexWrap:'wrap'}}><small>{tasks.length?`작업 ${finished}/${tasks.length}`:'실행 준비'}</small>{controls}</div></header>
  <ol className="rc-rt-flow" aria-label="실제 실행 단계">{['환경 준비','작업 시작','PentAGI 진단','결과 정리','환경 정리'].map((label,i)=><li key={label} data-current={active&&i===stage} data-done={!blocked&&(i<stage||i===4&&cleaned===true)}><span className="rc-rt-dot"/>{label}</li>)}</ol>
  {(active||httpEvents!==undefined)&&<RedTeamTrafficView events={httpEvents??[]} traffic={httpEvents!==undefined} active={active} target={target} isolated={isolationVerified} caption={active?stageCaption(current?.title,step):undefined}/>}
  <p className="rc-rt-status" role="status" aria-live="polite">{step||'검사 진행 상태를 기다립니다.'}{blocked?' · 완료되지 않은 결과입니다.':''}</p>
  {!!tasks.length&&<div className="rc-rt-tasks">{visibleTasks.map(t=><article key={t.id} className="rc-rt-task" data-active={active&&t.status==='running'}><small><span className="rc-rt-dot"/>{states[t.status]||'상태 확인 필요'}</small><div style={{flex:1,minWidth:0}}><strong title={clean(t.title)}>{taskLabel(t.title,tasks.indexOf(t))}</strong>{(t.result||clean(t.title)!==taskLabel(t.title,tasks.indexOf(t)))&&<details><summary>작업 원문 · AI 기록</summary><pre>{[clean(t.title),t.result].filter(Boolean).join('\n\n')}</pre></details>}</div></article>)}</div>}
  {!!hiddenTasks.length&&<details><summary>이전·추가 작업 {hiddenTasks.length}개</summary>{hiddenTasks.map(t=><details key={t.id}><summary>{taskLabel(t.title,tasks.indexOf(t))} · {states[t.status]||'상태 확인 필요'}</summary><pre>{t.result||'결과 대기 중'}</pre></details>)}</details>}
  {assessment&&<div className="rc-rt-issues"><h4>진단 결과 · 근거 검토</h4><p>근거가 첨부된 후보 {assessment.findings.length}건 · 미검증 범위 {assessment.checks.filter(c=>c.status!=='tested').length}개</p><small>직접 HTTP 대조와 AI 보고를 구분합니다. 검사하지 않은 경로는 결과에 포함되지 않습니다.</small>
   <div className="rc-rt-coverage">{assessment.checks.map(c=><span key={c.kind} title={c.reason}>{names[c.kind]} · {c.status==='tested'?'검사 기록 있음':c.status==='blocked'?'차단됨':'미검증'}</span>)}</div>
   {assessment.findings.map((f,i)=><details key={i}><summary>{f.title}{f.cvss?` · CVSS ${f.cvss.score.toFixed(1)}`:''}</summary><p>{f.route} · {f.evidence==='http_observed'?'HTTP 응답으로 확인':'AI 후보 · 검토 필요'}</p><p>{f.observation}</p>{f.cvss&&<small>벡터로 계산한 점수 · 영향 범위는 검토 필요<br/>{f.cvss.vector}</small>}<div className="rc-rt-evidence"><div><h5>검사 요청·응답</h5><pre>{f.request}{'\n\n'}{f.response}</pre></div><div><h5>대조 요청·응답</h5><pre>{f.controlRequest}{'\n\n'}{f.controlResponse}</pre></div></div><h5>수정 제안</h5><p>{f.remediation}</p></details>)}
   {!!assessment.warnings.length&&<details><summary className="rc-rt-warning">검토·보완 필요 {assessment.warnings.length}개</summary><ul>{assessment.warnings.map((s,i)=><li key={i}>{s}</li>)}</ul></details>}
   <details><summary>검사 범위와 한계</summary>{assessment.checks.map(c=><p key={c.kind}><strong>{names[c.kind]}</strong> — {c.reason}</p>)}{assessment.limitations.map((s,i)=><p key={i}>{s}</p>)}</details>
  </div>}
  {cleaned===true&&<p><small>전용 실행 환경 정리 완료 · 결과는 보관됩니다.</small></p>}
 </section>;
}
