import React,{useRef} from 'react';
import type {RedTeamHttpEvent} from '../../src/security/pentagiAssessmentTypes';

/**
 * 공격 테스트 무대 — 내 앱을 둘러싼 방패에 실제 요청이 날아와 부딪히는 모습.
 * 점 하나 = 실제로 받은 HTTP 요청 하나. 타이머로 만든 가짜 공격은 그리지 않는다.
 * 연결이 끊겼거나 끝났으면(active=false) 날아오는 점이 없다.
 */
export type Outcome='refused'|'answered'|'error'|'limited';
export function outcomeOf(e:Pick<RedTeamHttpEvent,'status'|'blocked'>):Outcome{
 if(e.blocked)return 'limited';
 if(e.status>=500||e.status===0)return 'error';
 if(e.status>=400)return 'refused';
 return 'answered';
}
const OUT_LABEL:Record<Outcome,string>={refused:'거부',answered:'응답',error:'오류',limited:'범위 제한'};
const W=640,H=250,CX=320,CY=128,RX=176,RY=104;
/** 요청 id 로 정해지는 방향 — 같은 요청은 다시 그려도 같은 자리. */
const angle=(id:number)=>((id*137.508)%360)*Math.PI/180;

export function RedTeamTrafficView({events=[],active,target,isolated,caption,traffic=true}:{events?:RedTeamHttpEvent[];active:boolean;target?:string;isolated?:boolean;caption?:string;traffic?:boolean}) {
 const last=events[events.length-1];
 let name='테스트 앱';try{const u=new URL(target||'');name=u.hostname==='host.docker.internal'?'localhost:'+u.port:u.host;}catch{}
 //: 처음 그릴 때 이미 있던 요청은 날리지 않는다(지난 기록). 이후 새로 들어온 요청만 날아온다.
 const seen=useRef<number|null>(null);
 if(seen.current===null)seen.current=last?.id??0;
 const fresh=active?events.filter(e=>e.id>(seen.current??0)).slice(-8):[];
 //: 가장 최근 요청 하나는 첫 화면에서도 보여 준다 — 지금 실제로 오가는지 알 수 있게.
 const flying=active&&last&&!fresh.some(e=>e.id===last.id)?[...fresh,last]:fresh;
 const count={refused:0,answered:0,error:0,limited:0} as Record<Outcome,number>;events.forEach(e=>count[outcomeOf(e)]++);
 return <div className="rc-rt-traffic" data-active={active} aria-label="실제 HTTP 검사 흐름">
  <style>{CSS}</style>
  <div className="rt-stage">
   <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={last?`${name}에 요청 ${events.length}개 · 마지막 HTTP ${last.status}`:`${name} · 요청 대기`}>
    <defs><linearGradient id="rt-scan" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="var(--rc-teal,#35A9D1)" stopOpacity="0"/><stop offset=".85" stopColor="var(--rc-teal,#35A9D1)" stopOpacity=".18"/><stop offset="1" stopColor="var(--rc-teal,#35A9D1)" stopOpacity=".75"/></linearGradient>
     <clipPath id="rt-site"><rect x={CX-130} y={CY-78} width="260" height="156" rx="10"/></clipPath></defs>
    <rect className="rt-shield" x={CX-RX} y={CY-RY} width={RX*2} height={RY*2} rx="26"/>
    <g className="rt-site">
     <rect x={CX-130} y={CY-78} width="260" height="156" rx="10" className="frame"/>
     <rect x={CX-130} y={CY-78} width="260" height="20" rx="10" className="bar"/><rect x={CX-130} y={CY-66} width="260" height="8" className="bar"/>
     {[0,1,2].map(i=><circle key={i} cx={CX-118+i*9} cy={CY-68} r="2.6" className="dot"/>)}
     <text x={CX-84} y={CY-64.5} className="url">{name}</text>
     {[0,1,2,3].map(i=><g key={i}><rect x={CX-116+i*60} y={CY-44} width="52" height="44" rx="5" className="sk"/><rect x={CX-116+i*60} y={CY+6} width="40" height="5" rx="2" className="sk2"/></g>)}
     {[0,1,2,3].map(i=><rect key={'b'+i} x={CX-116+i*60} y={CY+22} width="52" height="36" rx="5" className="sk"/>)}
     {active&&<g clipPath="url(#rt-site)"><rect className="rt-scanline" x={CX-130} y={CY-78} width="260" height="46" fill="url(#rt-scan)"/></g>}
    </g>
    {flying.map((e,i)=>{const a=angle(e.id),o=outcomeOf(e);const sx=CX+Math.cos(a)*360,sy=CY+Math.sin(a)*230,hx=CX+Math.cos(a)*RX*1.02,hy=CY+Math.sin(a)*RY*1.02;
     return <g key={e.id} className={`rt-probe ${e.id===last?.id?'rt-packet':''}`} data-o={o} style={{['--sx' as string]:`${sx}px`,['--sy' as string]:`${sy}px`,['--hx' as string]:`${hx}px`,['--hy' as string]:`${hy}px`,animationDelay:`${i*.35}s`} as React.CSSProperties}>
      <g className="fly" style={{animationDelay:`${i*.35}s`}}><circle r="4.5"/></g>
      <circle className="hit" cx={hx} cy={hy} r="6" style={{animationDelay:`${i*.35+.7}s`}}/>
      <circle className="mark" cx={hx} cy={hy} r="7" style={{animationDelay:`${i*.35+.7}s`}}/>
     </g>;})}
   </svg>
  </div>
  {caption&&<p className="rt-caption" role="status" aria-live="polite"><i/>{caption}</p>}
  {traffic&&<><div className="rt-count"><span>요청 <b>{events.length}</b></span><span data-o="refused">거부 <b>{count.refused}</b></span><span data-o="answered">응답 <b>{count.answered}</b></span>{count.error>0&&<span data-o="error">오류 <b>{count.error}</b></span>}{count.limited>0&&<span data-o="limited">범위 제한 <b>{count.limited}</b></span>}<span className="iso">{isolated?'대상 전용 네트워크':'격리 상태 확인 전'}</span></div>
  <div className="rt-brief"><span className="rt-path">{last?`마지막 요청 #${last.id} · ${last.method} ${last.path}`:active?'요청을 기다리는 중':'실제 요청이 수신되면 여기에 표시됩니다.'}</span>{last&&<span>{last.status} · {last.durationMs} ms</span>}</div>
  {events.length>1&&<details><summary>최근 요청 {events.length}개</summary><ul>{events.slice(-12).reverse().map(e=><li key={e.id} data-o={outcomeOf(e)}><span className="rt-path">{e.method} {e.path}</span><span>{e.status} · {OUT_LABEL[outcomeOf(e)]}</span></li>)}</ul></details>}</>}
 </div>;
}

const CSS=`
.rc-rt-traffic{margin:14px 0;color:var(--rc-fg,#EEF3F8)}
.rc-rt-traffic .rt-stage{border-radius:14px;border:1px solid var(--rc-line,rgba(160,190,220,.09));background:radial-gradient(circle at 50% 48%,color-mix(in srgb,var(--rc-ok,#3DD68C) 6%,var(--rc-solid,#0D131A)) 0,var(--rc-solid,#0D131A) 70%);overflow:hidden}
.rc-rt-traffic svg{display:block;width:100%;height:auto;max-height:300px}
.rc-rt-traffic .rt-shield{fill:none;stroke:color-mix(in srgb,var(--rc-ok,#3DD68C) 30%,transparent);stroke-width:1.5}
.rc-rt-traffic[data-active=true] .rt-shield{animation:rt-shield 3s ease-in-out infinite}
.rc-rt-traffic .rt-site .frame{fill:#fff}.rc-rt-traffic .rt-site .bar{fill:#1b1f24}.rc-rt-traffic .rt-site .dot{fill:#3a4048}
.rc-rt-traffic .rt-site .url{font:9.5px var(--rc-mono,monospace);fill:#9aa3ad}
.rc-rt-traffic .rt-site .sk{fill:#edf0f3}.rc-rt-traffic .rt-site .sk2{fill:#e3e7eb}
.rc-rt-traffic .rt-scanline{animation:rt-scan 2.6s linear infinite}
.rc-rt-traffic .rt-probe .fly{animation:rt-fly .7s cubic-bezier(.55,0,.9,.6) both}
.rc-rt-traffic .rt-probe .fly circle{fill:var(--rc-bad,#F27272)}
.rc-rt-traffic .rt-probe .hit{fill:none;stroke-width:2;opacity:0;animation:rt-ring .7s ease-out both}
.rc-rt-traffic .rt-probe .mark{opacity:0;transform-box:fill-box;transform-origin:center;animation:rt-mark 1.6s ease-out both}
.rc-rt-traffic .rt-probe[data-o=refused] .hit{stroke:var(--rc-ok,#3DD68C)}
.rc-rt-traffic .rt-probe[data-o=refused] .mark{fill:var(--rc-ok,#3DD68C)}
.rc-rt-traffic .rt-probe[data-o=answered] .hit{stroke:var(--rc-teal,#35A9D1)}.rc-rt-traffic .rt-probe[data-o=answered] .mark{fill:var(--rc-teal,#35A9D1)}
.rc-rt-traffic .rt-probe[data-o=error] .hit{stroke:var(--rc-warn,#F2B544)}.rc-rt-traffic .rt-probe[data-o=error] .mark{fill:var(--rc-warn,#F2B544)}
.rc-rt-traffic .rt-probe[data-o=limited] .hit{stroke:var(--rc-fg3,#67768A)}.rc-rt-traffic .rt-probe[data-o=limited] .mark{fill:var(--rc-fg3,#67768A)}
.rc-rt-traffic .rt-caption{display:flex;align-items:center;justify-content:center;gap:9px;margin:14px 0 0;font-size:15px;font-weight:600;text-align:center;overflow-wrap:anywhere}
.rc-rt-traffic .rt-caption i{width:8px;height:8px;border-radius:50%;background:var(--rc-bad,#F27272);flex:none;animation:rt-blink 1.1s ease-in-out infinite}
.rc-rt-traffic[data-active=false] .rt-caption i{background:var(--rc-fg3,#67768A);animation:none}
.rc-rt-traffic .rt-count{display:flex;flex-wrap:wrap;gap:6px 16px;justify-content:center;margin-top:10px;font-size:12.5px;color:var(--rc-fg2,#9AA8B8)}
.rc-rt-traffic .rt-count b{color:var(--rc-fg,#EEF3F8);font-variant-numeric:tabular-nums}
.rc-rt-traffic .rt-count [data-o=refused] b{color:var(--rc-ok,#3DD68C)}.rc-rt-traffic .rt-count [data-o=error] b{color:var(--rc-warn,#F2B544)}
.rc-rt-traffic .rt-count .iso{color:var(--rc-fg3,#67768A)}
.rc-rt-traffic .rt-brief{display:flex;justify-content:space-between;gap:12px;font:12px var(--rc-mono,monospace);color:var(--rc-fg2,#9AA8B8);padding:10px 2px 0;margin-top:10px;border-top:1px solid var(--rc-line,rgba(160,190,220,.09))}
.rc-rt-traffic .rt-path{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.rc-rt-traffic details{margin:6px 0 0}.rc-rt-traffic summary{cursor:pointer;font-size:12px;color:var(--rc-fg3,#67768A)}
.rc-rt-traffic ul{padding:0;list-style:none;margin:6px 0 0}.rc-rt-traffic li{display:flex;justify-content:space-between;gap:8px;font:12px var(--rc-mono,monospace);color:var(--rc-fg2,#9AA8B8);margin:4px 0}
.rc-rt-traffic li[data-o=refused] span:last-child{color:var(--rc-ok,#3DD68C)}.rc-rt-traffic li[data-o=error] span:last-child{color:var(--rc-warn,#F2B544)}
@keyframes rt-fly{0%{transform:translate(var(--sx),var(--sy));opacity:0}15%{opacity:1}96%{transform:translate(var(--hx),var(--hy));opacity:1}100%{transform:translate(var(--hx),var(--hy));opacity:0}}
@keyframes rt-ring{0%{opacity:1;r:5}100%{opacity:0;r:26}}
@keyframes rt-mark{0%{opacity:1;transform:scale(1.4)}20%{transform:scale(1)}70%{opacity:1}100%{opacity:0}}
@keyframes rt-scan{from{transform:translateY(-46px)}to{transform:translateY(156px)}}
@keyframes rt-shield{0%,100%{stroke-opacity:.6}50%{stroke-opacity:1}}
@keyframes rt-blink{50%{opacity:.3}}
@media(prefers-reduced-motion:reduce){.rc-rt-traffic *{animation:none!important}.rc-rt-traffic .rt-probe .fly{display:none}.rc-rt-traffic .rt-probe .mark{opacity:1}}
`;
