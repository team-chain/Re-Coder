import React, {useCallback, useEffect, useRef, useState} from 'react';
import {useVSCodeApi} from '../hooks/useVSCodeApi';
import {SecurityScanPanel, PolicyPanel, ScanResultLite} from './Hubs';
import {ScanKind} from './scanQueue';
import {CodeReport, SECURITY_KINDS} from './securityOverview';
import {securityModel, fixFor, type Risk, type Step, type AttackRow, type FixLite, type FixMatch} from './securityRisks';
import {RedTeamPanel} from './RedTeamPanel';
import {ManagedRedTeamPanel} from './ManagedRedTeamPanel';
import type {ManagedReport} from '../../src/security/managedReportTypes';
import type {PentagiAssessment} from '../../src/security/pentagiAssessmentTypes';
import {SourceAuditPanel} from './SourceAuditPanel';
import {setPendingPrompt} from './CodeAgent';
import {takeFocus} from './focusIntent';

type Kind='code'|'history'|'recheck'|'policy';
type Check={running?:boolean;error?:string;result?:Record<string,unknown>;at?:string};
type Section='detail'|'watch'|'audit'|'attack'|'more';
const later=(fn:()=>void)=>{if(typeof requestAnimationFrame==='function')requestAnimationFrame(()=>requestAnimationFrame(fn));else setTimeout(fn,0);};

/** 숫자가 0 에서 목표까지 올라간다. 움직임 줄이기 설정이면 바로 목표. */
function useCount(target:number){
 const [n,setN]=useState(target);const from=useRef(target);
 useEffect(()=>{
  const reduce=typeof window!=='undefined'&&window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if(reduce||typeof requestAnimationFrame==='undefined'){setN(target);from.current=target;return;}
  const start=from.current,t0=performance.now();let id=0;
  const tick=(t:number)=>{const p=Math.min(1,(t-t0)/650);const v=Math.round(start+(target-start)*(1-Math.pow(1-p,3)));setN(v);if(p<1)id=requestAnimationFrame(tick);else from.current=target;};
  id=requestAnimationFrame(tick);return()=>cancelAnimationFrame(id);
 },[target]);
 return n;
}

const Icon={
 warn:<svg viewBox="0 0 24 24" aria-hidden><path d="M12 2.8 22 20.2H2z" fill="currentColor"/><path d="M12 9.2v5" stroke="var(--rc-sec-ink)" strokeWidth="2.2" strokeLinecap="round"/><circle cx="12" cy="17.2" r="1.25" fill="var(--rc-sec-ink)"/></svg>,
 bad:<svg viewBox="0 0 24 24" aria-hidden><circle cx="12" cy="12" r="10" fill="currentColor"/><path d="M12 6.8v6.4" stroke="var(--rc-sec-ink)" strokeWidth="2.4" strokeLinecap="round"/><circle cx="12" cy="16.9" r="1.35" fill="var(--rc-sec-ink)"/></svg>,
 ok:<svg viewBox="0 0 24 24" aria-hidden><path d="M12 2 4 5v6c0 5 3.4 9 8 11 4.6-2 8-6 8-11V5z" fill="currentColor"/><path d="m8.4 12.1 2.5 2.5 4.8-5.2" fill="none" stroke="var(--rc-sec-ink)" strokeWidth="2.3" strokeLinecap="round" strokeLinejoin="round"/></svg>,
 idle:<svg viewBox="0 0 24 24" aria-hidden><path d="M12 2 4 5v6c0 5 3.4 9 8 11 4.6-2 8-6 8-11V5z" fill="none" stroke="currentColor" strokeWidth="1.8"/></svg>,
 clock:<svg viewBox="0 0 16 16" aria-hidden><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.5"/><path d="M8 4.6V8l2.4 1.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg>,
 check:<svg viewBox="0 0 16 16" aria-hidden><circle cx="8" cy="8" r="7.5" fill="currentColor" opacity=".18"/><path d="m4.8 8.2 2.2 2.2 4.2-4.6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/></svg>,
 x:<svg viewBox="0 0 16 16" aria-hidden><circle cx="8" cy="8" r="7.5" fill="currentColor" opacity=".18"/><path d="m5.5 5.5 5 5m0-5-5 5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/></svg>,
 q:<svg viewBox="0 0 16 16" aria-hidden><circle cx="8" cy="8" r="7.5" fill="currentColor" opacity=".18"/><path d="M6.3 6.2a1.8 1.8 0 1 1 2.5 1.7c-.5.2-.8.6-.8 1.1v.4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/><circle cx="8" cy="11.6" r=".9" fill="currentColor"/></svg>,
 dash:<svg viewBox="0 0 16 16" aria-hidden><circle cx="8" cy="8" r="7.5" fill="currentColor" opacity=".14"/><path d="M5 8h6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/></svg>,
 dot:<svg viewBox="0 0 16 16" aria-hidden><circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" strokeWidth="1.5"/></svg>,
 chev:<svg viewBox="0 0 16 16" aria-hidden><path d="m6 4 4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/></svg>,
};

const CSS=`
.rc-sec{--rc-sec-ink:var(--rc-bg,#070A0E);max-width:1080px;margin:8px auto 96px;color:var(--rc-fg,var(--vscode-foreground));font-size:14px}
.rc-sec button{font:inherit}
.rc-sec .rc-sec-banner{display:flex;align-items:center;gap:20px;padding:22px 26px;border-radius:16px;border:1px solid var(--rc-line2,rgba(160,190,220,.16));background:var(--rc-tile,transparent);animation:rc-sec-up .45s cubic-bezier(.2,.8,.2,1) both}
.rc-sec-banner[data-tone=danger]{color:var(--rc-bad,#F27272);border-color:color-mix(in srgb,var(--rc-bad,#F27272) 35%,transparent);background:linear-gradient(90deg,color-mix(in srgb,var(--rc-bad,#F27272) 13%,transparent),transparent 72%)}
.rc-sec-banner[data-tone=caution]{color:var(--rc-warn,#F2B544);border-color:color-mix(in srgb,var(--rc-warn,#F2B544) 32%,transparent);background:linear-gradient(90deg,color-mix(in srgb,var(--rc-warn,#F2B544) 13%,transparent),transparent 72%)}
.rc-sec-banner[data-tone=safe]{color:var(--rc-ok,#3DD68C);border-color:color-mix(in srgb,var(--rc-ok,#3DD68C) 30%,transparent);background:linear-gradient(90deg,color-mix(in srgb,var(--rc-ok,#3DD68C) 11%,transparent),transparent 72%)}
.rc-sec-banner[data-tone=idle],.rc-sec-banner[data-tone=running]{color:var(--rc-fg3,#67768A)}
.rc-sec-banner .ic{width:46px;height:46px;flex:none}.rc-sec-banner .ic svg{width:100%;height:100%}
.rc-sec-banner[data-tone=danger] .ic,.rc-sec-banner[data-tone=caution] .ic{animation:rc-sec-breathe 2.4s ease-in-out infinite}
.rc-sec-banner[data-tone=running] .ic{animation:rc-sec-spin 1.6s linear infinite}
.rc-sec-banner .t{flex:1;min-width:0}.rc-sec-banner h1{margin:0;font-size:28px;font-weight:750;letter-spacing:-.03em;line-height:1.15}
.rc-sec-banner p{margin:4px 0 0;font-size:13.5px;color:var(--rc-fg2,#9AA8B8)}.rc-sec-banner p.skip{font-size:12.5px;color:var(--rc-fg3,#67768A);margin-top:2px}
.rc-sec-tally{display:flex;gap:22px;margin-right:6px}.rc-sec-tally div{text-align:center;min-width:34px}
.rc-sec-tally b{display:block;font-size:28px;font-weight:750;line-height:1;font-variant-numeric:tabular-nums}.rc-sec-tally span{display:block;font-size:12px;color:var(--rc-fg2,#9AA8B8);margin-top:6px}
.rc-sec-tally .d b{color:var(--rc-bad,#F27272)}.rc-sec-tally .c b{color:var(--rc-warn,#F2B544)}.rc-sec-tally .l b{color:var(--rc-fg3,#67768A)}
.rc-sec-grid{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:20px;margin-top:20px;align-items:start}
.rc-sec-risks{display:grid;gap:8px}
.rc-sec-risk{border-radius:12px;border:1px solid var(--rc-line,rgba(160,190,220,.09));background:var(--rc-tile,transparent);animation:rc-sec-up .45s cubic-bezier(.2,.8,.2,1) both}
.rc-sec-risk[data-level=danger]{border-color:color-mix(in srgb,var(--rc-bad,#F27272) 32%,transparent);animation:rc-sec-up .45s cubic-bezier(.2,.8,.2,1) both,rc-sec-glow 2.6s ease-in-out 1.2s infinite}
.rc-sec-risk>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:16px;padding:15px 18px;border-radius:12px}
.rc-sec-risk>summary::-webkit-details-marker{display:none}
.rc-sec-risk>summary:hover{background:var(--rc-tile2,rgba(148,178,210,.065))}
.rc-sec-risk>summary:focus-visible{outline:2px solid var(--rc-teal,#35A9D1);outline-offset:2px}
.rc-sec-bars{display:flex;gap:3px;align-items:flex-end;height:20px;flex:none}
.rc-sec-bars i{width:7px;border-radius:2px;background:var(--rc-line2,rgba(160,190,220,.16));transform-origin:bottom;animation:rc-sec-grow .5s cubic-bezier(.2,.8,.2,1) both}
.rc-sec-bars i:nth-child(1){height:9px}.rc-sec-bars i:nth-child(2){height:14px;animation-delay:.06s}.rc-sec-bars i:nth-child(3){height:20px;animation-delay:.12s}
.rc-sec-risk[data-level=danger] .rc-sec-bars i{background:var(--rc-bad,#F27272)}
.rc-sec-risk[data-level=caution] .rc-sec-bars i:nth-child(-n+2){background:var(--rc-warn,#F2B544)}
.rc-sec-risk[data-level=low] .rc-sec-bars i:nth-child(1){background:var(--rc-fg3,#67768A)}
.rc-sec-risk .name{font-size:16px;font-weight:650;letter-spacing:-.01em;width:132px;flex:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-sec-risk .where{flex:1;min-width:0;font-size:13px;color:var(--rc-fg3,#67768A);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-sec-risk .n{font-size:12px;color:var(--rc-fg3,#67768A);flex:none}
.rc-sec-risk .wait{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--rc-fg3,#67768A);flex:none}.rc-sec-risk .wait svg{width:14px;height:14px}
.rc-sec-risk .body{padding:2px 18px 16px 62px;font-size:12.5px;line-height:1.7;color:var(--rc-fg2,#9AA8B8)}
.rc-sec-risk .body ul{margin:0 0 10px;padding:0;list-style:none}.rc-sec-risk .body li{padding:5px 0;border-top:1px solid var(--rc-line,rgba(160,190,220,.09));overflow-wrap:anywhere}.rc-sec-risk .body li:first-child{border-top:0}
.rc-sec-risk .body code{font-family:var(--rc-mono,monospace);font-size:11.5px;color:var(--rc-fg,#EEF3F8)}
.rc-sec-act{display:flex;gap:8px;flex-wrap:wrap}
.rc-sec-more{all:unset;cursor:pointer;font-size:13px;color:var(--rc-fg3,#67768A);padding:6px 4px}.rc-sec-more:hover{color:var(--rc-fg,#EEF3F8)}
.rc-sec-empty{display:flex;align-items:center;gap:14px;padding:22px;border-radius:12px;border:1px dashed var(--rc-line2,rgba(160,190,220,.16));color:var(--rc-fg2,#9AA8B8);font-size:13.5px}
.rc-sec-empty svg{width:28px;height:28px;flex:none}
.rc-sec-skel{height:52px;border-radius:12px;background:linear-gradient(90deg,var(--rc-tile,rgba(148,178,210,.035)) 0,var(--rc-tile2,rgba(148,178,210,.065)) 40%,var(--rc-tile,rgba(148,178,210,.035)) 80%);background-size:200% 100%;animation:rc-sec-shimmer 1.4s linear infinite}
.rc-sec-attack{border-radius:14px;border:1px solid var(--rc-line,rgba(160,190,220,.09));background:var(--rc-tile,transparent);padding:18px 18px 16px;animation:rc-sec-up .45s .1s cubic-bezier(.2,.8,.2,1) both}
.rc-sec-attack[data-tone=ok]{border-color:color-mix(in srgb,var(--rc-ok,#3DD68C) 28%,transparent);background:color-mix(in srgb,var(--rc-ok,#3DD68C) 5%,var(--rc-tile,transparent))}
.rc-sec-attack[data-tone=bad]{border-color:color-mix(in srgb,var(--rc-bad,#F27272) 35%,transparent);background:color-mix(in srgb,var(--rc-bad,#F27272) 6%,var(--rc-tile,transparent))}
.rc-sec-attack header{display:flex;align-items:center;gap:12px}.rc-sec-attack header svg{width:32px;height:32px;flex:none}
.rc-sec-attack[data-tone=ok] header{color:var(--rc-ok,#3DD68C)}.rc-sec-attack[data-tone=bad] header{color:var(--rc-bad,#F27272)}.rc-sec-attack[data-tone=idle] header,.rc-sec-attack[data-tone=warn] header{color:var(--rc-fg3,#67768A)}
.rc-sec-attack header b{display:block;font-size:20px;font-weight:750;letter-spacing:-.02em;line-height:1.15}.rc-sec-attack header small{display:block;font-size:12px;color:var(--rc-fg3,#67768A)}
.rc-sec-attack ul{list-style:none;margin:14px 0 14px;padding:0}
.rc-sec-attack li{display:flex;align-items:center;gap:10px;padding:7px 2px;border-top:1px solid var(--rc-line,rgba(160,190,220,.09));font-size:13.5px;animation:rc-sec-up .4s cubic-bezier(.2,.8,.2,1) both}
.rc-sec-attack li svg{width:16px;height:16px;flex:none}.rc-sec-attack li span{flex:1}.rc-sec-attack li small{font-size:11.5px;color:var(--rc-fg3,#67768A)}
.rc-sec-attack li[data-s=blocked] svg{color:var(--rc-ok,#3DD68C)}.rc-sec-attack li[data-s=found] svg{color:var(--rc-bad,#F27272)}.rc-sec-attack li[data-s=review] svg{color:var(--rc-warn,#F2B544)}
.rc-sec-attack li[data-s=untested],.rc-sec-attack li[data-s=unverified]{color:var(--rc-fg3,#67768A)}.rc-sec-attack li[data-s=untested] svg,.rc-sec-attack li[data-s=unverified] svg{color:var(--rc-fg3,#67768A)}
.rc-sec-attack li[data-s=found]{color:var(--rc-bad,#F27272)}
.rc-sec-attack .rc-btn{width:100%;justify-content:center}
.rc-sec-steps{display:flex;margin:30px 0 4px;position:relative;padding-top:2px}
.rc-sec-steps:before{content:"";position:absolute;left:24px;right:24px;top:9px;height:2px;background:var(--rc-line2,rgba(160,190,220,.16))}
.rc-sec-steps .fill{position:absolute;left:24px;top:9px;height:2px;background:var(--rc-fg3,#67768A);transition:width .9s cubic-bezier(.2,.8,.2,1)}
.rc-sec-step{all:unset;cursor:pointer;flex:1;display:flex;flex-direction:column;align-items:center;gap:8px;position:relative;font-size:12px;color:var(--rc-fg3,#67768A);border-radius:8px;padding:0 2px 6px}
.rc-sec-step:hover{color:var(--rc-fg,#EEF3F8)}.rc-sec-step:focus-visible{outline:2px solid var(--rc-teal,#35A9D1);outline-offset:2px}
.rc-sec-step i{width:12px;height:12px;border-radius:50%;background:var(--rc-solid,#0D131A);border:2px solid var(--rc-fg3,#67768A);box-sizing:border-box;position:relative}
.rc-sec-step[data-s=ok] i{background:var(--rc-ok,#3DD68C);border-color:var(--rc-ok,#3DD68C)}
.rc-sec-step[data-s=warn] i{background:var(--rc-warn,#F2B544);border-color:var(--rc-warn,#F2B544)}
.rc-sec-step[data-s=bad] i{background:var(--rc-bad,#F27272);border-color:var(--rc-bad,#F27272)}
.rc-sec-step[data-s=na] i{border-style:dashed}
.rc-sec-step[data-s=running] i{border-color:var(--rc-teal,#35A9D1);animation:rc-sec-pulse 1.1s ease-in-out infinite}
.rc-sec-step[data-s=ok][data-k=watch] i:after{content:"";position:absolute;inset:-2px;border-radius:50%;border:2px solid var(--rc-ok,#3DD68C);animation:rc-sec-ping 1.8s ease-out infinite}
.rc-sec-detail{margin-top:26px;border-top:1px solid var(--rc-line,rgba(160,190,220,.09))}
.rc-sec-detail>details{border-bottom:1px solid var(--rc-line,rgba(160,190,220,.09))}
.rc-sec-detail>details>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:10px;padding:15px 4px;font-size:14px}
.rc-sec-detail>details>summary::-webkit-details-marker{display:none}
.rc-sec-detail>details>summary svg{width:14px;height:14px;color:var(--rc-fg3,#67768A);transition:transform .2s}.rc-sec-detail>details[open]>summary svg{transform:rotate(90deg)}
.rc-sec-detail>details>summary small{margin-left:auto;font-size:12px;color:var(--rc-fg3,#67768A);text-align:right}
.rc-sec-detail>details>summary:focus-visible{outline:2px solid var(--rc-teal,#35A9D1);outline-offset:2px}
.rc-sec-detail .rc-security-detail{padding:0 4px 20px;line-height:1.75}
.rc-sec .rc-security-detail p,.rc-sec .rc-security-detail small{color:var(--rc-fg2,#9AA8B8)}
.rc-sec .rc-security-detail pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.7 var(--rc-mono,monospace);max-height:420px;overflow:auto}
.rc-sec .rc-security-detail details>summary{cursor:pointer;padding:10px 0;line-height:1.6}
.rc-sec .rc-security-detail details>summary small{display:block;color:var(--rc-fg3,#67768A)}
.rc-sec .rc-security-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.rc-sec .rc-security-error{color:var(--rc-bad,#F27272)!important}
.rc-sec .rc-scan-panel{background:transparent;padding:4px 0;border:0}.rc-sec .rc-scan-grid{grid-template-columns:1fr}.rc-sec .rc-scan-row[open]{grid-column:auto}
.rc-sec-note{color:var(--rc-fg3,#67768A);font-size:12px;line-height:1.8;margin-top:18px}
@keyframes rc-sec-up{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes rc-sec-grow{from{transform:scaleY(0)}to{transform:scaleY(1)}}
@keyframes rc-sec-glow{0%,100%{box-shadow:0 0 0 0 transparent}50%{box-shadow:0 0 0 4px color-mix(in srgb,var(--rc-bad,#F27272) 12%,transparent)}}
@keyframes rc-sec-breathe{0%,100%{transform:scale(1)}50%{transform:scale(1.07)}}
@keyframes rc-sec-spin{to{transform:rotate(360deg)}}
@keyframes rc-sec-pulse{50%{transform:scale(.75);opacity:.5}}
@keyframes rc-sec-ping{from{transform:scale(1);opacity:.8}to{transform:scale(2.6);opacity:0}}
@keyframes rc-sec-shimmer{to{background-position:-200% 0}}
@media(prefers-reduced-motion:reduce){.rc-sec *,.rc-sec *:before,.rc-sec *:after{animation:none!important;transition:none!important}}
@media(max-width:820px){.rc-sec-grid{grid-template-columns:1fr}}
@media(max-width:620px){.rc-sec .rc-sec-banner{flex-wrap:wrap;padding:18px;gap:14px}.rc-sec-banner .ic{width:36px;height:36px}.rc-sec-banner h1{font-size:22px}.rc-sec-tally{order:3;width:100%;justify-content:flex-start}.rc-sec-banner>.rc-btn{order:4}
 .rc-sec-risk>summary{gap:12px;padding:13px 14px;flex-wrap:wrap}.rc-sec-risk .name{width:auto;flex:1}.rc-sec-risk .where{order:5;flex-basis:100%;padding-left:33px}.rc-sec-risk .body{padding-left:14px}
 .rc-sec-steps{flex-wrap:wrap;row-gap:12px}.rc-sec-steps:before,.rc-sec-steps .fill{display:none}.rc-sec-step{flex:0 0 25%}}
`;

const BANNER_ICON={danger:Icon.bad,caution:Icon.warn,safe:Icon.ok,idle:Icon.idle,running:Icon.idle} as const;
const ATTACK_ICON={blocked:Icon.check,found:Icon.x,review:Icon.q,unverified:Icon.dash,untested:Icon.dot} as const;
const ATTACK_NOTE={blocked:'막음',found:'뚫림',review:'확인 필요',unverified:'확인 못 함',untested:'안 함'} as const;

export const SecurityDashboard: React.FC<{onGo?:(view:string)=>void}> = ({onGo}) => {
 const {postMessage,useMessage}=useVSCodeApi(); const [signal,setSignal]=useState(0); const [scans,setScans]=useState<Partial<Record<ScanKind,ScanResultLite>>>({}); const [scanning,setScanning]=useState(false);
 const [checks,setChecks]=useState<Partial<Record<Kind,Check>>>({}); const pending=useRef(new Map<string,{kind:Kind;timer:ReturnType<typeof setTimeout>}>());
 const [focused]=useState(()=>takeFocus('redteam'));const [redteam,setRedteam]=useState(focused);const [redteamLocal,setRedteamLocal]=useState(focused);
 const [audit,setAudit]=useState(false),[managedScans,setManagedScans]=useState(false),[allRisks,setAllRisks]=useState(false);
 const [managedReport,setManagedReport]=useState<ManagedReport>();
 const [attack,setAttack]=useState<{assessment?:PentagiAssessment;running?:boolean;at?:string;live?:boolean}>({});
 const [plan,setPlan]=useState<{loading:boolean;proposals:FixLite[]}>({loading:false,proposals:[]});const [focusFix,setFocusFix]=useState<string>();
 const onPlan=useCallback((p:{loading:boolean;proposals:FixLite[]})=>setPlan(p),[]);
 const summaryId=useRef('');
 //: 창을 다시 열면 결과 목록은 비어 있다 — 진행 흐름에 남은 지난 검사 기록을 한 줄로 보여 준다(실사용 2026-11-21: 진행 흐름은 「완료」, 보안 화면은 「검사 전」).
 const [last,setLast]=useState<{summary:string;at?:string}|null>(null);
 useEffect(()=>{postMessage('journey.get');},[postMessage]);
 //: 화면을 열 때 지난 공격 테스트 결과를 저장 파일에서만 읽어 온다(Docker·클라우드 호출 없음).
 const askSummary=useCallback(()=>{summaryId.current=`rt-summary-${Date.now()}-${Math.random()}`;postMessage('security.redteamSummary',{requestId:summaryId.current});},[postMessage]);
 useEffect(()=>{askSummary();},[askSummary]);
 const [open,setOpen]=useState<Partial<Record<Section,boolean>>>({});
 const refs={detail:useRef<HTMLDetailsElement>(null),watch:useRef<HTMLDetailsElement>(null),audit:useRef<HTMLDetailsElement>(null),attack:useRef<HTMLDetailsElement>(null),more:useRef<HTMLDetailsElement>(null)};
 const reveal=useCallback((s:Section)=>{setOpen(o=>({...o,[s]:true}));later(()=>refs[s].current?.scrollIntoView?.({block:'start',behavior:'smooth'}));
 // eslint-disable-next-line react-hooks/exhaustive-deps
 },[]);
 const openRedteam=useCallback((local:boolean)=>{setRedteam(true);setRedteamLocal(local);setAudit(false);setManagedScans(false);reveal('attack');},[reveal]);
 useEffect(()=>{const on=(e:Event)=>{if((e as CustomEvent).detail==='redteam'&&takeFocus('redteam'))openRedteam(true);};window.addEventListener('recoder:focus',on);return()=>window.removeEventListener('recoder:focus',on);},[openRedteam]);
 useEffect(()=>{if(focused)reveal('attack');},[focused,reveal]);
 const request=useCallback((kind:Kind)=>{
  if([...pending.current.values()].some(v=>v.kind===kind))return;
  const requestId=`security-check-${Date.now()}-${Math.random()}`;
  const timer=setTimeout(()=>{pending.current.delete(requestId);setChecks(c=>({...c,[kind]:{error:'응답을 확인하지 못했습니다. 연결 상태를 확인하고 다시 시도하세요.'}}));},190000);
  pending.current.set(requestId,{kind,timer});setChecks(c=>({...c,[kind]:{running:true}}));postMessage('security.check',{kind,requestId});
 },[postMessage]);
 useMessage(useCallback(msg=>{
  if(msg.type==='canvas.projectChanged'){pending.current.forEach(p=>clearTimeout(p.timer));pending.current.clear();setChecks({});setScans({});setManagedReport(undefined);setScanning(false);setSignal(0);setManagedScans(false);setAudit(false);setRedteam(false);setAttack({});setPlan({loading:false,proposals:[]});setFocusFix(undefined);askSummary();return;}
  if(msg.type==='security.redteamSummaryResult'){const p=msg.payload as {requestId?:string;local?:{phase?:string;assessment?:PentagiAssessment;updatedAt?:string}|null};
   if(p.requestId!==summaryId.current)return;
   //: 패널에서 이미 실시간 결과를 받았으면 저장본으로 덮지 않는다.
   setAttack(cur=>cur.live?cur:p.local?{assessment:p.local.assessment,running:!['removed','cleanup_failed'].includes(String(p.local.phase)),at:p.local.updatedAt}:{});return;}
  //: 공격 테스트 카드는 패널이 받은 결과를 함께 읽기만 한다 — 따로 묻지 않는다(로컬 엔진 잠금과 겹치지 않게).
  if(msg.type==='security.redteamResult'){const p=msg.payload as {local?:{phase?:string;assessment?:PentagiAssessment;updatedAt?:string}|null;report?:ManagedReport;activeRun?:unknown};
   if(p.local!==undefined){const l=p.local;setAttack(l?{assessment:l.assessment,running:!['removed','cleanup_failed'].includes(String(l.phase)),at:l.updatedAt,live:true}:{live:true});}
   else if(p.report&&(p.report.mode??'redteam')==='redteam')setAttack({assessment:p.report.assessment,running:!!p.activeRun&&!p.report.cleaned,live:true});
   return;}
  if(msg.type==='journey.state'){const st=((msg.payload as {steps?:Array<{key:string;status:string;summary:string;at?:string}>})?.steps||[]).find(x=>x.key==='security');setLast(st&&st.status!=='todo'&&st.status!=='locked'?{summary:st.summary,at:st.at}:null);return;}
  if(msg.type!=='security.checkResult')return;const p=msg.payload as {requestId:string;result?:Record<string,unknown>;error?:string};const entry=pending.current.get(p.requestId);if(!entry)return;
  clearTimeout(entry.timer);pending.current.delete(p.requestId);setChecks(c=>({...c,[entry.kind]:{result:p.result,error:p.error,at:new Date().toLocaleTimeString()}}));
  if(entry.kind==='recheck'&&!p.error)request('history');
 },[request,askSummary]));
 useEffect(()=>()=>{pending.current.forEach(p=>clearTimeout(p.timer));pending.current.clear();},[]);
 const changed=useCallback((results:Partial<Record<ScanKind,ScanResultLite>>,running:boolean)=>{setScans(results);setScanning(running);},[]);
 const code=checks.code?.result as unknown as CodeReport|undefined;
 const model=securityModel({scans,scanning,code,codeRunning:!!checks.code?.running,codeError:checks.code?.error,policy:checks.policy,watch:checks.recheck?.result||checks.recheck?.running||checks.recheck?.error?checks.recheck:checks.history,attack:attack.assessment,attackRunning:attack.running});
 const busy=model.verdict==='running';
 const run=()=>{setManagedReport(undefined);setSignal(n=>n+1);request('code');request('policy');};
 const danger=useCount(model.tally.danger),caution=useCount(model.tally.caution),low=useCount(model.tally.low);
 const serious=model.risks.filter(r=>r.level!=='low');
 //: 위험·주의가 없으면 낮음이라도 보여 준다(빈 목록 + "더 보기" 만 남지 않게).
 const shown=allRisks?model.risks:serious.length?serious.slice(0,5):model.risks.slice(0,3);
 const hidden=model.risks.length-shown.length;

 const toFix=(m:FixMatch)=>{if(m)setFocusFix(m.proposal.id);reveal('detail');later(()=>{const el=(m&&document.querySelector(`.rc-sec [data-fix-id="${m.proposal.id}"]`))||document.querySelector('.rc-sec .rc-fixes');el?.scrollIntoView?.({block:'center',behavior:'smooth'});});};
 const fixCode=(r:Risk)=>{const d=r.details[0];if(!onGo)return;setPendingPrompt(`${r.where}의 보안 문제를 검토하고 최소 범위의 수정안을 제안해 주세요.\n문제: ${d?.id??''} ${d?.title??r.title}\n현재 파일을 다시 확인하고 기존 기능을 유지하세요. 승인 전에는 적용하지 마세요.`);onGo('code');};
 const stepTo=(s:Step)=>{if(s.key==='attack'){openRedteam(false);return;}if(s.key==='watch'){reveal('watch');return;}reveal('detail');};

 const attackTone=attack.running?'warn':!attack.assessment?'idle':model.attack.some(a=>a.state==='found')?'bad':model.attack.every(a=>a.state==='blocked')?'ok':'warn';
 const tested=model.attack.filter(a=>a.state!=='untested'&&a.state!=='unverified').length,blocked=model.attack.filter(a=>a.state==='blocked').length;
 const lastDone=Math.max(-1,...model.steps.map((s,i)=>s.state!=='idle'&&s.state!=='running'?i:-1));

 return <section className="rc-sec rc-security-overview" aria-label="보안 현황">
 <style>{CSS}</style>
 <div className="rc-sec-banner" data-tone={model.verdict} aria-live="polite">
  <span className="ic">{BANNER_ICON[model.verdict]}</span>
  <div className="t"><h1>{model.title}</h1>{model.reason&&<p>{model.reason}</p>}{!busy&&model.skipped.map(t=><p key={t} className="skip">{t}</p>)}{model.verdict==='idle'&&<p>시크릿 · Dockerfile · 이미지 · 코드를 한 번에 검사해요</p>}{model.verdict==='idle'&&last&&<p className="skip" data-testid="sec-last">지난 검사{last.at?` (${new Date(last.at).toLocaleString('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false})})`:''}: {last.summary||'기록 있음'} · 다시 검사하면 항목별로 보여요</p>}{busy&&<p>{model.pending.length?`${model.pending.join(' · ')} 검사 중 · 끝난 검사 ${model.steps.slice(0,4).length-model.pending.length}/${model.steps.slice(0,4).length}`:'결과를 정리하고 있어요'}</p>}</div>
  {(model.verdict!=='idle')&&<div className="rc-sec-tally" aria-label={`위험 ${model.tally.danger}, 주의 ${model.tally.caution}, 낮음 ${model.tally.low}`}>
   <div className="d"><b>{danger}</b><span>위험</span></div><div className="c"><b>{caution}</b><span>주의</span></div><div className="l"><b>{low}</b><span>낮음</span></div></div>}
  <button className={`rc-btn${signal?'':' p'}`} disabled={busy} onClick={run}>{busy?'검사 중…':signal?'다시 검사':'보안 검사 시작'}</button>
 </div>

 <div className="rc-sec-grid">
  <div className="rc-sec-risks" aria-label="위험 목록">
   {busy&&!model.risks.length&&[0,1,2].map(i=><div key={i} className="rc-sec-skel" style={{animationDelay:`${i*.15}s`}}/>)}
   {!busy&&!model.risks.length&&<div className="rc-sec-empty">{model.verdict==='safe'?<span style={{color:'var(--rc-ok,#3DD68C)'}}>{Icon.ok}</span>:<span style={{color:'var(--rc-fg3,#67768A)'}}>{Icon.idle}</span>}
    <span>{model.verdict==='safe'?'찾은 문제가 없어요':model.verdict==='idle'?'검사하면 위험한 것부터 여기에 보여요':'찾은 문제는 없지만 끝나지 않은 검사가 있어요 — 아래 단계를 눌러 확인하세요'}</span></div>}
   {shown.map((r,i)=><RiskRow key={r.key} risk={r} delay={i*.07} match={fixFor(r,plan.proposals)} planning={plan.loading} onFix={toFix} onFixCode={onGo?()=>fixCode(r):undefined} onOpen={(file,line)=>postMessage('map.openFile',{id:file,line})}/>)}
   {hidden>0&&<button className="rc-sec-more" onClick={()=>setAllRisks(true)}>+ {hidden}개 더 보기</button>}
   {allRisks&&model.risks.length>5&&<button className="rc-sec-more" onClick={()=>setAllRisks(false)}>접기</button>}
  </div>
  <AttackCard tone={attackTone} rows={model.attack} running={!!attack.running} has={!!attack.assessment} blocked={blocked} tested={tested} onOpen={()=>{if(redteam){setRedteam(false);}else openRedteam(false);}} openNow={redteam}/>
 </div>

 <nav className="rc-sec-steps" aria-label="검사 단계">
  <span className="fill" style={{width:lastDone<0?0:`calc((100% - 48px) * ${lastDone/(model.steps.length-1)})`}}/>
  {model.steps.map(s=><button key={s.key} className="rc-sec-step" data-s={s.state} data-k={s.key} onClick={()=>stepTo(s)} aria-label={`${s.label} · ${STEP_TEXT[s.state]}`}><i/>{s.label}</button>)}
 </nav>

 <div className="rc-sec-detail">
  <details ref={refs.detail} open={!!open.detail} onToggle={e=>{const o=(e.currentTarget as HTMLDetailsElement).open;setOpen(x=>x.detail===o?x:{...x,detail:o});}}>
   <summary>{Icon.chev}검사 결과 상세<small>시크릿 · Dockerfile · 이미지 · 코드 · OPA 정책{busy?' · 검사 중':''}</small></summary>
   <div className="rc-security-detail">
    <details><summary><strong>코드 보안 규칙 · {checks.code?.running?'검사 중':checks.code?.error?'확인 실패':code?code.score===null?'적용 가능한 규칙 없음':`${code.findings.length}개 확인 필요`:'미실행'}</strong><small>JavaScript · TypeScript 대상 / AI 감사와 별개</small></summary><p>{checks.code?.error||code?.scope||'프로젝트에서 SQL 구성, 인증, 시크릿 등 지원 규칙을 검사합니다.'}</p>{code&&<p>검사 파일 {code.files_checked??'—'}개</p>}<button className="rc-btn" disabled={!!checks.code?.running} onClick={()=>request('code')}>코드만 다시 검사</button></details>
    <details open><summary><strong>이미지 · Dockerfile · 시크릿</strong><small>{scanning?'검사 중':model.gate.label} · 결과와 수정안</small></summary><SecurityScanPanel kinds={SECURITY_KINDS} title="기본 검사" note="" compact runSignal={signal} onChange={changed} onPlan={onPlan} focusFix={focusFix}/></details>
    <details><summary><strong>배포 정책 · {checks.policy?.result?.opa_available?'OPA 연결됨':checks.policy?'연결 확인 필요':'상태 미확인'}</strong><small>연결 상태와 실제 배포 정책 평가는 별개입니다.</small></summary><p>{checks.policy?.error||`정책 번들: ${String(checks.policy?.result?.bundle_version||'미확인')}`}</p><button className="rc-btn" disabled={!!checks.policy?.running} onClick={()=>request('policy')}>OPA 상태 확인</button><PolicyPanel/></details>
   </div>
  </details>
  <details ref={refs.watch} open={!!open.watch} onToggle={e=>{const o=(e.currentTarget as HTMLDetailsElement).open;setOpen(x=>x.watch===o?x:{...x,watch:o});}}>
   <summary>{Icon.chev}배포 후 새 취약점 감시<small>SBOM ↔ OSV · Core 실행 중 주기적으로 확인</small></summary>
   <div className="rc-security-detail">
    <div className="rc-security-actions"><button className="rc-btn" disabled={!!checks.history?.running} onClick={()=>request('history')}>이력 불러오기</button><button className="rc-btn" disabled={!!checks.recheck?.running} onClick={()=>request('recheck')}>{checks.recheck?.running?'재검사 중…':'지금 재검사'}</button><button className="rc-btn" onClick={()=>onGo?.('hub:monitor')}>업데이트 제안 · 대응 열기</button></div>
    <p role="status">{checks.recheck?.error||checks.recheck?.result?.message as string|| (checks.recheck?.result?.status==='ok'?`${checks.recheck.result.checked}개 배포 재검사 완료`:'')}</p>
    {checks.history?.error&&<p role="alert">{checks.history.error}</p>}
    {checks.history?.result&&Object.keys(checks.history.result).length===0&&<p>기록된 이력이 없습니다. 검사 통과를 의미하지 않습니다.</p>}
    {Object.entries(checks.history?.result??{}).map(([service,value])=>{const entry=value as {checked_at?:string;known?:Record<string,{id:string;name:string;version:string;fixed?:string;fixed_at?:string;fix_applied_at?:string}>};return <details key={service}><summary>{service} · {entry.checked_at||'검사일 미확인'}</summary>{Object.values(entry.known??{}).map((v,i)=><p key={i}>{v.id} · {v.name} {v.version} → {v.fixed||'수정 버전 미확인'} · {v.fixed_at?'재검사에서 사라짐':v.fix_applied_at?'업데이트 적용 · 재배포 필요':'확인 필요'}</p>)}</details>;})}
   </div>
  </details>
  <details ref={refs.audit} open={audit} onToggle={e=>{const o=(e.currentTarget as HTMLDetailsElement).open;if(o!==audit){setAudit(o);if(o){setRedteam(false);setManagedScans(false);}}}}>
   <summary>{Icon.chev}AI 코드 감사<small>Cloudflare security-audit 기반 · 소스 근거와 수정 방향</small><button className="rc-btn sm" aria-expanded={audit} onClick={e=>{e.preventDefault();e.stopPropagation();setAudit(!audit);setRedteam(false);setManagedScans(false);}}>{audit?'닫기':'AI 감사'}</button></summary>
   <div className="rc-security-detail">
    {audit&&<SourceAuditPanel onFix={onGo?finding=>{setPendingPrompt(`${finding.file}:${finding.line}의 보안 문제를 검토하고 최소 범위의 수정안을 제안해 주세요.\n문제: ${finding.title}\n영향: ${finding.impact}\n수정 방향: ${finding.fix}\n현재 파일을 다시 확인하고 기존 기능을 유지하세요. 승인 전에는 적용하지 마세요.`);onGo('code');}:undefined}/>}</div>
  </details>
  <details ref={refs.attack} open={redteam} onToggle={e=>{const o=(e.currentTarget as HTMLDetailsElement).open;if(o!==redteam){if(o)openRedteam(false);else setRedteam(false);}}}>
   <summary>{Icon.chev}공격 테스트 (레드팀)<small>PentAGI · 실행 중인 내 앱을 격리된 환경에서 공격</small></summary>
   {redteam&&<div><RedTeamPanel key={redteamLocal?'local':'choose'} initialMode={redteamLocal?'local':undefined}/></div>}
  </details>
  <details ref={refs.more} open={!!open.more||managedScans} onToggle={e=>{const o=(e.currentTarget as HTMLDetailsElement).open;setOpen(x=>x.more===o?x:{...x,more:o});}}>
   <summary>{Icon.chev}다른 실행 방식<small>AWS 연결로 프로젝트 사본 검사</small></summary>
   <div className="rc-security-detail"><p>필요할 때만 기존 AWS 연결로 프로젝트 사본을 검사할 수 있습니다. 최초 실행 환경 준비와 사용료가 발생할 수 있습니다.</p><button className="rc-btn" aria-expanded={managedScans} onClick={()=>setManagedScans(!managedScans)}>{managedScans?'사본 검사 닫기':'AWS 사본 검사 열기'}</button>{managedScans&&<ManagedRedTeamPanel mode="scans" onReport={setManagedReport}/>} {managedReport&&<p>사본 검사 결과는 위의 현재 프로젝트 검사와 별도로 표시합니다.</p>}</div>
  </details>
 </div>
 <p className="rc-sec-note">검사 결과는 실행 당시의 파일과 대상 기준입니다. 파일을 고치거나 새 이미지를 배포한 뒤에는 다시 검사하세요. 실행하지 못한 검사는 통과로 세지 않습니다.</p>
 </section>;
};

const STEP_TEXT:Record<Step['state'],string>={idle:'아직',running:'검사 중',ok:'통과',warn:'확인 필요',bad:'위험',na:'해당 없음'};

const RiskRow:React.FC<{risk:Risk;delay:number;match:FixMatch;planning:boolean;onFix:(m:FixMatch)=>void;onFixCode?:()=>void;onOpen:(file:string,line?:number)=>void}>=({risk:r,delay,match,planning,onFix,onFixCode,onOpen})=>{
 const stop=(e:React.MouseEvent)=>{e.preventDefault();e.stopPropagation();};
 //: "고치기" 는 실제로 적용할 수 있는 수정안이 있을 때만. 코드 문제는 AI 수정 요청으로 넘긴다.
 const fixBtn=r.source==='code'||r.source==='attack'?(onFixCode?<button className="rc-btn sm p" onClick={e=>{stop(e);onFixCode();}}>고치기</button>:<span className="wait">직접 수정</span>)
  :match?.kind==='auto'?<button className="rc-btn sm p" onClick={e=>{stop(e);onFix(match);}}>고치기</button>
  :planning&&r.fix!=='wait'?<span className="wait" aria-busy="true">수정안 준비 중</span>
  :r.fix==='wait'?<span className="wait" title="아직 고친 버전이 나오지 않았어요">{Icon.clock}대기</span>
  :<span className="wait">직접 수정</span>;
 const file=r.details.find(d=>d.file);
 return <details className="rc-sec-risk" data-level={r.level} style={{animationDelay:`${delay}s`}}>
  <summary aria-label={`${r.level==='danger'?'위험':r.level==='caution'?'주의':'낮음'} · ${r.title} · ${r.where}`}>
   <span className="rc-sec-bars" aria-hidden><i/><i/><i/></span>
   <span className="name">{r.title}</span><span className="where">{r.where}</span>
   {r.count>1&&<span className="n">{r.count}건</span>}
   {fixBtn}
  </summary>
  <div className="body">
   <ul>{r.details.slice(0,8).map((d,i)=><li key={i}>{d.id&&<code>{d.id}</code>} {d.title}{(d.installed||d.fixed)&&<> · <code>{d.installed??'?'}</code> → <code>{d.fixed||'고친 버전 없음'}</code></>}{d.where&&<> · {d.where}</>}{d.file&&<> · <code>{d.file}{d.line?`:${d.line}`:''}</code></>}</li>)}
    {r.details.length>8&&<li>외 {r.details.length-8}건</li>}</ul>
   {match&&<p><b>{match.kind==='auto'?'자동 수정':'직접 수정'}</b> · {match.proposal.title} — {match.proposal.detail}</p>}
   {!match&&r.fix==='auto'&&r.source==='image'&&!planning&&<p>자동 수정안이 없어요. 위 버전으로 직접 올린 뒤 다시 검사하세요.</p>}
   <div className="rc-sec-act">{file?.file&&<button className="rc-btn sm" onClick={()=>onOpen(file.file!,file.line)}>코드 위치 열기</button>}
    {match&&<button className="rc-btn sm" onClick={()=>onFix(match)}>수정안 보기</button>}
    {r.fix==='wait'&&<span>고친 버전이 나오면 배포 후 감시가 알려줘요. 배포를 막지는 않아요.</span>}
    {r.source==='secret'&&<span>노출된 키는 새로 발급하고, 코드 대신 환경변수로 옮기세요.</span>}</div>
  </div>
 </details>;
};

const AttackCard:React.FC<{tone:string;rows:AttackRow[];running:boolean;has:boolean;blocked:number;tested:number;onOpen:()=>void;openNow:boolean}>=({tone,rows,running,has,blocked,tested,onOpen,openNow})=>{
 const found=rows.filter(r=>r.state==='found').length;
 return <aside className="rc-sec-attack" data-tone={tone} aria-label="공격 테스트">
  <header>{tone==='bad'?Icon.bad:tone==='ok'?Icon.ok:Icon.idle}<div>
   <b>{running?'진행 중':!has?'공격 테스트':found?`${found}곳 뚫림`:!tested&&!rows.some(r=>r.state==='review')?'확인 못 함':`${blocked}/${rows.length} 막음`}</b>
   <small>{running?'해커처럼 내 앱을 공격하는 중':!has?'해커처럼 내 앱을 공격해 봐요':!tested&&!rows.some(r=>r.state==='review')?'지난 검사에서 끝까지 확인한 공격이 없어요 · 다시 해 보세요':`시도한 공격 ${tested}종 기준 · 안전 보증 아님`}</small></div></header>
  <ul>{rows.map((r,i)=><li key={r.kind} data-s={r.state} style={{animationDelay:`${.15+i*.06}s`}}>{ATTACK_ICON[r.state]}<span>{r.label}</span><small>{has?`${ATTACK_NOTE[r.state]}${r.requests?` · ${r.requests}`:''}`:''}</small></li>)}</ul>
  <button className={`rc-btn${has||running?'':' p'}`} aria-expanded={openNow} onClick={onOpen}>{openNow?'공격 테스트 닫기':'레드팀 테스트'}</button>
 </aside>;
};
