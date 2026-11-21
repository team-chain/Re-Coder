import React from 'react';
import type {RedTeamHttpEvent} from '../../src/security/pentagiAssessmentTypes';

/**
 * 공격 테스트 무대 — 왼쪽 AI 공격자가 내 앱의 **실제 경로**에 보낸 요청을 경로별로 쌓아 보여 준다.
 * 줄 하나 = 실제로 받은 요청들이 모인 경로. 가짜 앱 그림이나 타이머로 만든 공격은 그리지 않는다.
 * 연결이 끊겼거나 끝났으면(active=false) 날아가는 점이 없다.
 */
export type Outcome='refused'|'answered'|'error'|'limited';
export function outcomeOf(e:Pick<RedTeamHttpEvent,'status'|'blocked'>):Outcome{
 if(e.blocked)return 'limited';
 if(e.status>=500||e.status===0)return 'error';
 if(e.status>=400)return 'refused';
 return 'answered';
}
//: limited = 중계가 막은 요청(검사 범위 밖 경로·허용 안 된 메서드·너무 빠른 요청). 앱이 막은 것(거부)과 구분한다.
const OUT_LABEL:Record<Outcome,string>={refused:'거부',answered:'응답',error:'오류',limited:'중계 차단'};
/** 같은 화면을 가리키는 요청을 한 줄로 — 쿼리는 빼고 숫자·긴 ID 는 :id 로. */
const ID_START=/^(\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?=[0-9a-f]*\d)[0-9a-f]{8,})(?![0-9a-z_-])/i;
export function routeOf(path:string):string{
 const p=(path||'/').split('?')[0].split('#')[0]||'/';
 //: 숫자·UUID 로 시작하는 칸은 뒤에 붙은 공격 문자열(' OR 1=1 등)까지 :id 하나로 — 실사용에서 UUID 줄이 따로 생겼다.
 const out=p.split('/').map(seg=>{let s=seg;try{s=decodeURIComponent(seg);}catch{}return ID_START.test(s)?':id':seg;}).join('/');
 return out.length>48?out.slice(0,47)+'…':out;
}
interface Route{path:string;methods:string[];n:number;by:Record<Outcome,number>;last:number;lastOutcome:Outcome}
export function routesOf(events:RedTeamHttpEvent[]):Route[]{
 const map=new Map<string,Route>();
 for(const e of events){
  const key=routeOf(e.path);const o=outcomeOf(e);
  const r=map.get(key)??{path:key,methods:[],n:0,by:{refused:0,answered:0,error:0,limited:0},last:0,lastOutcome:o};
  r.n++;r.by[o]++;if(!r.methods.includes(e.method))r.methods.push(e.method);if(e.id>=r.last){r.last=e.id;r.lastOutcome=o;}
  map.set(key,r);
 }
 return [...map.values()].sort((a,b)=>b.n-a.n||a.path.localeCompare(b.path));
}
const ROWS=7;

export function RedTeamTrafficView({events=[],active,target,isolated,caption,traffic=true}:{events?:RedTeamHttpEvent[];active:boolean;target?:string;isolated?:boolean;caption?:string;traffic?:boolean}) {
 const last=events[events.length-1];
 let name='테스트 앱';try{const u=new URL(target||'');name=u.hostname==='host.docker.internal'?'localhost:'+u.port:u.host;}catch{}
 const all=routesOf(events);
 //: 가장 최근 요청이 닿은 경로는 줄 수가 많아도 항상 보이게 둔다.
 const shown=all.slice(0,ROWS);
 const hot=last?all.find(r=>r.last===last.id):undefined;
 if(hot&&!shown.includes(hot))shown[shown.length-1]=hot;
 const hotIndex=hot?shown.indexOf(hot):-1;
 const count={refused:0,answered:0,error:0,limited:0} as Record<Outcome,number>;events.forEach(e=>count[outcomeOf(e)]++);
 return <div className="rc-rt-traffic" data-active={active} aria-label="실제 HTTP 검사 흐름">
  <style>{CSS}</style>
  <div className="rt-stage" role="img" aria-label={last?`${name}의 경로 ${all.length}개에 요청 ${events.length}개 · 마지막 HTTP ${last.status}`:`${name} · 요청 대기`}>
   <div className="rt-atk"><span className="rt-atk-ico" aria-hidden="true">AI</span><b>공격자</b><small>격리된 검사 환경</small></div>
   <div className="rt-lane" aria-hidden="true">
    {active&&last&&hotIndex>=0&&<i key={last.id} className="rt-probe rt-packet" data-o={outcomeOf(last)} style={{['--row' as string]:hotIndex} as React.CSSProperties}/>}
   </div>
   <div className="rt-app">
    <header><span className="rt-dots"><i/><i/><i/></span><b>{name}</b>{all.length>0&&<small>경로 {all.length}개</small>}</header>
    {active&&<div className="rt-scanline" aria-hidden="true"/>}
    {shown.length?<ul className="rt-routes">{shown.map(r=><li key={r.path} data-hot={active&&r===hot} data-o={r.lastOutcome}>
      <span className="m">{r.methods.slice(0,2).join(' ')}{r.methods.length>2?' …':''}</span><code title={r.path}>{r.path}</code>
      <span className="c">{(['refused','answered','error','limited'] as Outcome[]).filter(o=>r.by[o]).map(o=><em key={o} data-o={o}>{OUT_LABEL[o]} {r.by[o]}</em>)}</span>
     </li>)}</ul>
     :<p className="rt-empty">{active?'앱의 경로를 살펴보는 중이에요':'받은 요청이 없어요'}</p>}
    {all.length>shown.length&&<p className="rt-more">그 밖의 경로 {all.length-shown.length}개</p>}
   </div>
  </div>
  {caption&&<p className="rt-caption" role="status" aria-live="polite"><i/>{caption}</p>}
  {traffic&&<><div className="rt-count"><span>요청 <b>{events.length}</b></span><span data-o="refused">거부 <b>{count.refused}</b></span><span data-o="answered">응답 <b>{count.answered}</b></span>{count.error>0&&<span data-o="error">오류 <b>{count.error}</b></span>}{count.limited>0&&<span data-o="limited" title="검사 범위 밖 경로·허용 안 된 메서드·너무 빠른 요청을 중계가 막았어요">중계 차단 <b>{count.limited}</b></span>}<span className="iso">{isolated?'대상 전용 네트워크':'격리 상태 확인 전'}</span></div>
  <div className="rt-brief"><span className="rt-path">{last?`마지막 요청 #${last.id} · ${last.method} ${last.path}`:active?'요청을 기다리는 중':'실제 요청이 수신되면 여기에 표시됩니다.'}</span>{last&&<span>{last.status} · {last.durationMs} ms</span>}</div>
  {events.length>1&&<details><summary>최근 요청 {events.length}개</summary><ul>{events.slice(-12).reverse().map(e=><li key={e.id} data-o={outcomeOf(e)}><span className="rt-path">{e.method} {e.path}</span><span>{e.status} · {OUT_LABEL[outcomeOf(e)]}</span></li>)}</ul></details>}</>}
 </div>;
}

const CSS=`
.rc-rt-traffic{margin:14px 0;color:var(--rc-fg,#EEF3F8)}
.rc-rt-traffic .rt-stage{display:grid;grid-template-columns:132px minmax(40px,1fr) minmax(260px,3fr);align-items:center;gap:0;padding:22px 20px;border-radius:14px;border:1px solid var(--rc-line,rgba(160,190,220,.09));background:var(--rc-solid,#0D131A)}
.rc-rt-traffic .rt-atk{display:flex;flex-direction:column;align-items:center;gap:4px;text-align:center}
.rc-rt-traffic .rt-atk-ico{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;font-weight:600;font-size:14px;color:var(--rc-bad,#F27272);border:1px solid color-mix(in srgb,var(--rc-bad,#F27272) 45%,transparent);background:color-mix(in srgb,var(--rc-bad,#F27272) 10%,transparent)}
.rc-rt-traffic .rt-atk b{font-size:13px}.rc-rt-traffic .rt-atk small{font-size:12px;color:var(--rc-fg3,#67768A)}
.rc-rt-traffic .rt-lane{position:relative;align-self:stretch;margin:0 6px}
.rc-rt-traffic .rt-lane:before{content:"";position:absolute;left:0;right:0;top:50%;border-top:1px dashed var(--rc-line2,rgba(160,190,220,.16))}
.rc-rt-traffic[data-active=true] .rt-lane:before{border-top-color:color-mix(in srgb,var(--rc-bad,#F27272) 35%,transparent)}
.rc-rt-traffic .rt-probe{position:absolute;left:0;top:50%;width:9px;height:9px;margin:-4.5px 0 0 -4.5px;border-radius:50%;background:var(--rc-bad,#F27272);box-shadow:0 0 0 3px color-mix(in srgb,var(--rc-bad,#F27272) 25%,transparent);animation:rt-go .8s cubic-bezier(.5,0,.8,.6) both}
.rc-rt-traffic .rt-app{position:relative;border:1px solid var(--rc-line2,rgba(160,190,220,.16));border-radius:12px;background:var(--rc-tile,rgba(148,178,210,.035));overflow:hidden;min-width:0}
.rc-rt-traffic .rt-app header{display:flex;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid var(--rc-line,rgba(160,190,220,.09));font-size:13px}
.rc-rt-traffic .rt-app header small{margin-left:auto;color:var(--rc-fg3,#67768A);font-size:12px}
.rc-rt-traffic .rt-dots{display:flex;gap:4px}.rc-rt-traffic .rt-dots i{width:7px;height:7px;border-radius:50%;background:var(--rc-line2,rgba(160,190,220,.16))}
.rc-rt-traffic .rt-routes{list-style:none;margin:0;padding:4px 0}
.rc-rt-traffic .rt-routes li{display:grid;grid-template-columns:62px minmax(0,1fr) auto;align-items:center;gap:10px;padding:6px 12px;font-size:12px;border-left:2px solid transparent;transition:background .3s}
.rc-rt-traffic .rt-routes .m{font-weight:600;color:var(--rc-fg2,#9AA8B8);font-variant-numeric:tabular-nums}
.rc-rt-traffic .rt-routes code{font:12px var(--rc-mono,monospace);color:var(--rc-fg,#EEF3F8);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rc-rt-traffic .rt-routes .c{display:flex;gap:8px;white-space:nowrap;color:var(--rc-fg3,#67768A)}
.rc-rt-traffic .rt-routes em{font-style:normal;font-variant-numeric:tabular-nums}
.rc-rt-traffic .rt-routes em[data-o=refused]{color:var(--rc-ok,#3DD68C)}.rc-rt-traffic .rt-routes em[data-o=answered]{color:var(--rc-teal,#35A9D1)}.rc-rt-traffic .rt-routes em[data-o=error]{color:var(--rc-warn,#F2B544)}
.rc-rt-traffic .rt-routes li[data-hot=true]{animation:rt-hit 1.6s ease-out both}
.rc-rt-traffic .rt-routes li[data-hot=true][data-o=refused]{border-left-color:var(--rc-ok,#3DD68C)}.rc-rt-traffic .rt-routes li[data-hot=true][data-o=answered]{border-left-color:var(--rc-teal,#35A9D1)}
.rc-rt-traffic .rt-routes li[data-hot=true][data-o=error]{border-left-color:var(--rc-warn,#F2B544)}.rc-rt-traffic .rt-routes li[data-hot=true][data-o=limited]{border-left-color:var(--rc-fg3,#67768A)}
.rc-rt-traffic .rt-empty,.rc-rt-traffic .rt-more{margin:0;padding:12px;font-size:12px;color:var(--rc-fg3,#67768A)}.rc-rt-traffic .rt-more{padding-top:0}
.rc-rt-traffic .rt-scanline{position:absolute;left:0;right:0;top:0;height:2px;background:linear-gradient(90deg,transparent,var(--rc-bad,#F27272),transparent);opacity:.5;animation:rt-scan 2.4s ease-in-out infinite}
.rc-rt-traffic .rt-caption{display:flex;align-items:center;justify-content:center;gap:9px;margin:14px 0 0;font-size:14px;font-weight:600;text-align:center;overflow-wrap:anywhere}
.rc-rt-traffic .rt-caption i{width:8px;height:8px;border-radius:50%;background:var(--rc-bad,#F27272);flex:none;animation:rt-blink 1.1s ease-in-out infinite}
.rc-rt-traffic[data-active=false] .rt-caption i{background:var(--rc-fg3,#67768A);animation:none}
.rc-rt-traffic .rt-count{display:flex;flex-wrap:wrap;gap:6px 16px;justify-content:center;margin-top:10px;font-size:12px;color:var(--rc-fg2,#9AA8B8)}
.rc-rt-traffic .rt-count b{color:var(--rc-fg,#EEF3F8);font-variant-numeric:tabular-nums}
.rc-rt-traffic .rt-count [data-o=refused] b{color:var(--rc-ok,#3DD68C)}.rc-rt-traffic .rt-count [data-o=error] b{color:var(--rc-warn,#F2B544)}
.rc-rt-traffic .rt-count .iso{color:var(--rc-fg3,#67768A)}
.rc-rt-traffic .rt-brief{display:flex;justify-content:space-between;gap:12px;font:12px var(--rc-mono,monospace);color:var(--rc-fg2,#9AA8B8);padding:10px 2px 0;margin-top:10px;border-top:1px solid var(--rc-line,rgba(160,190,220,.09))}
.rc-rt-traffic .rt-path{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.rc-rt-traffic details{margin:6px 0 0}.rc-rt-traffic summary{cursor:pointer;font-size:12px;color:var(--rc-fg3,#67768A)}
.rc-rt-traffic details ul{padding:0;list-style:none;margin:6px 0 0}.rc-rt-traffic details li{display:flex;justify-content:space-between;gap:8px;font:12px var(--rc-mono,monospace);color:var(--rc-fg2,#9AA8B8);margin:4px 0}
.rc-rt-traffic details li[data-o=refused] span:last-child{color:var(--rc-ok,#3DD68C)}.rc-rt-traffic details li[data-o=error] span:last-child{color:var(--rc-warn,#F2B544)}
@keyframes rt-go{0%{left:0;opacity:0}10%{opacity:1}90%{left:100%;opacity:1}100%{left:100%;opacity:0}}
@keyframes rt-hit{0%{background:color-mix(in srgb,var(--rc-bad,#F27272) 16%,transparent)}100%{background:transparent}}
@keyframes rt-scan{0%{transform:translateX(-60%)}50%{transform:translateX(60%)}100%{transform:translateX(-60%)}}
@keyframes rt-blink{50%{opacity:.3}}
@media(max-width:620px){.rc-rt-traffic .rt-stage{grid-template-columns:1fr;gap:10px}.rc-rt-traffic .rt-atk{flex-direction:row;justify-content:center}.rc-rt-traffic .rt-lane{height:18px}}
@media(prefers-reduced-motion:reduce){.rc-rt-traffic *{animation:none!important}.rc-rt-traffic .rt-probe{left:100%}}
`;
