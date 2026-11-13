import React from 'react';
import type {RedTeamHttpEvent} from '../../src/security/pentagiAssessmentTypes';

/** Each pulse corresponds to a received HTTP event, never a timer-generated attack. */
export function RedTeamTrafficView({events=[],active,target,isolated}:{events?:RedTeamHttpEvent[];active:boolean;target?:string;isolated?:boolean}) {
 const last=events[events.length-1];
 let name='테스트 앱';try{const u=new URL(target||'');name=u.hostname==='host.docker.internal'?'내 앱 :'+u.port:u.host;}catch{}
 return <div className="rc-rt-traffic" aria-label="실제 HTTP 검사 흐름">
  <style>{`.rc-rt-traffic{background:#0c1118;color:#dbe8f3;border:1px solid #263544;border-radius:12px;padding:12px;margin:12px 0}.rc-rt-traffic-top{display:flex;justify-content:space-between;gap:8px;font-size:12px;color:#94aabd}.rc-rt-traffic svg{width:100%;height:auto;max-height:220px}.rc-rt-traffic text{font-family:inherit;fill:#dbe8f3}.rc-rt-traffic .rt-wire{fill:none;stroke:#284858;stroke-width:2}.rc-rt-traffic .rt-packet{fill:#57c9df;offset-path:path('M 120 91 L 292 91');animation:rt-request 1.2s ease-out 1 forwards}.rc-rt-traffic .rt-return{fill:#8cb6cf;offset-path:path('M 310 91 L 494 91');animation:rt-request 1.2s .8s ease-out 1 forwards;opacity:0}.rc-rt-traffic .rt-brief{display:flex;justify-content:space-between;gap:12px;font:12px var(--vscode-editor-font-family,monospace);padding:8px 2px;border-top:1px solid #22323e}.rc-rt-traffic .rt-path{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.rc-rt-traffic details{margin:6px 0 0}.rc-rt-traffic li{display:flex;justify-content:space-between;gap:8px;font-size:12px;color:#8fa5b7;margin:4px 0}.rc-rt-traffic ul{padding:0;list-style:none}@keyframes rt-request{0%{opacity:0;offset-distance:0%}15%,80%{opacity:1}100%{opacity:0;offset-distance:100%}}@media(prefers-reduced-motion:reduce){.rc-rt-traffic .rt-packet,.rc-rt-traffic .rt-return{animation:none;display:none}}`}</style>
  <div className="rc-rt-traffic-top"><span>{active?'실시간 진단':'요청 기록'} · {last?'마지막 요청 #'+last.id:'응답 대기'}</span><span>{isolated?'대상 전용 네트워크':'격리 상태 확인 전'}</span></div>
  <svg viewBox="0 0 620 170" role="img" aria-label={last?`진단 도구에서 ${name}에 요청, HTTP ${last.status}`:'진단 도구, 테스트 앱, 응답을 잇는 검사 경로'}>
   <path className="rt-wire" d="M 120 91 H 292 M 310 91 H 494"/>
   <g transform="translate(42 43)"><path d="M15 16Q48 -6 81 16V56Q77 84 48 96Q18 82 15 56Z" fill="#112a35" stroke="#55c5dc" strokeWidth="2"/><rect x="26" y="29" width="44" height="30" rx="13" fill="#0a1720" stroke="#55c5dc"/><circle cx="39" cy="42" r="4" fill="#9ceded"/><circle cx="57" cy="42" r="4" fill="#9ceded"/><path d="M40 66Q48 73 56 66" stroke="#55c5dc" fill="none"/><path d="M32 15L25 5 M64 15L71 5" stroke="#55c5dc" strokeWidth="3"/></g>
   <g transform="translate(260 48)"><rect width="80" height="80" rx="18" fill="#12232f" stroke="#4f94bc" strokeWidth="2"/><rect x="20" y="19" width="40" height="13" rx="4" fill="#284d65"/><rect x="20" y="39" width="40" height="13" rx="4" fill="#284d65"/><circle cx="51" cy="25" r="2" fill="#71e5b1"/><circle cx="51" cy="45" r="2" fill="#71e5b1"/><path d="M27 66H53" stroke="#4f94bc" strokeWidth="3"/></g>
   <g transform="translate(482 57)"><circle cx="35" cy="35" r="34" fill="#13222c" stroke={last?.blocked?'#e5a955':'#456a82'} strokeWidth="2"/><text x="35" y="41" textAnchor="middle" fontSize="20" fontWeight="700">{last?.status||'…'}</text></g>
   {active&&last&&<g key={last.id}><circle className="rt-packet" r="4"/><circle className="rt-return" r="4"/></g>}
   <text x="90" y="161" textAnchor="middle" fontSize="12">ReCoder 진단</text><text x="300" y="151" textAnchor="middle" fontSize="12">{name}</text><text x="517" y="151" textAnchor="middle" fontSize="12">{last?.blocked?'범위·요청 제한':'앱 응답'}</text>
  </svg>
  <div className="rt-brief"><span className="rt-path">{last?`${last.method} ${last.path}`:'실제 요청이 수신되면 여기에 표시됩니다.'}</span>{last&&<span>{last.durationMs} ms</span>}</div>
  {events.length>1&&<details><summary>최근 요청 {events.length}개</summary><ul>{events.slice(-8).reverse().map(e=><li key={e.id}><span className="rt-path">{e.method} {e.path}</span><span>{e.status}{e.blocked?' · 제한':''}</span></li>)}</ul></details>}
 </div>;
}
