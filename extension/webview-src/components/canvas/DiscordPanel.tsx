import React, { useEffect, useState } from "react";
export interface DiscordState { active_channel_id?: string; channel_name?: string; guild_name?: string; connected_clients?: number }
export interface CanvasEvent { id: string; title: string; detail: string; at: string; delivery?: string }
export function DiscordPanel({ state, events, enabled, onEnabled, post, guilds, channels, error }: { state: DiscordState | null; events: CanvasEvent[]; enabled:boolean; onEnabled:(value:boolean)=>void; post:(type:string,payload?:unknown)=>void; guilds:Array<{id:string;name:string}>; channels:Array<{id:string;name:string}>; error:string }) {
  const [guild,setGuild]=useState(''),[channel,setChannel]=useState('');
  const refresh=()=>{post('canvas.discord.status');post('canvas.discord.guilds');if(guild)post('canvas.discord.channels',{guildId:guild});};
  useEffect(()=>{post('canvas.discord.status');post('canvas.discord.guilds');},[post]);
  return <section className="rc-panel" aria-label="Discord 연결과 이벤트">
    <header><h3>Discord · {state?.channel_name ? `#${state.channel_name}` : '알림 연결'}</h3><button onClick={refresh}>다시 연결</button></header>
    <p className="rc-muted">배포·보안 검사·롤백 결과를 선택한 채널로 받습니다.</p>
    {error&&<div className="rc-note rc-error" role="alert">{error}</div>}
    {(!state||error)&&<div className="rc-note"><p>ReCoder Discord 봇 서버가 필요합니다. 확장 설치만으로 봇이 실행되지는 않습니다. 봇을 실행한 뒤 서버 주소와 인증 키를 설정하세요.</p><button onClick={()=>post('canvas.discord.settings')}>봇 서버 연결 설정</button></div>}
    <div className="rc-fields"><label>서버<select value={guild} disabled={!state||Boolean(error)} onChange={e=>{setGuild(e.target.value);setChannel('');if(e.target.value)post('canvas.discord.channels',{guildId:e.target.value});}}><option value="">서버 선택</option>{guilds.map(g=><option key={g.id} value={g.id}>{g.name}</option>)}</select></label><label>알림 채널<select value={channel} disabled={!guild||Boolean(error)} onChange={e=>setChannel(e.target.value)}><option value="">채널 선택</option>{channels.map(c=><option key={c.id} value={c.id}>#{c.name}</option>)}</select></label></div>
    {state&&!error&&!guilds.length&&<p className="rc-muted">접근 가능한 서버가 없습니다. 봇을 초대한 뒤 다시 연결을 누르세요.</p>}
    <div className="rc-actions" style={{marginTop:12}}><button onClick={()=>post('canvas.discord.invite')}>봇 초대</button><button disabled={!channel} onClick={()=>post('canvas.discord.setChannel',{channelId:channel})}>선택한 채널 연결</button><button disabled={!state?.active_channel_id} onClick={()=>{onEnabled(false);post('canvas.discord.setChannel',{channelId:''});}}>연결 해제</button></div>
    <p className="rc-note">{state?.active_channel_id ? `${state.guild_name || '서버'} · #${state.channel_name || state.active_channel_id}` : '알림 채널 미연결'} · Bridge 클라이언트 {state?.connected_clients ?? '미확인'}</p>
    <label><input type="checkbox" checked={enabled} disabled={!state?.active_channel_id} onChange={e=>onEnabled(e.target.checked)}/> 이 캔버스에서 관측한 새 배포·게이트·롤백 이벤트를 채널로 보내기</label>
    <p className="rc-muted" style={{marginTop:8}}>화면을 연 동안 관측한 이벤트입니다. 알림을 켜기 전의 이력을 소급 전송하지 않습니다.</p>
    <ul className="rc-events">{events.map(event=><li key={event.id}><div className="rc-event-title"><b>{event.title}</b><small>{new Date(event.at).toLocaleTimeString()}</small></div><div>{event.detail}</div><small>{event.delivery || '이 화면에 기록됨'}</small></li>)}</ul>
    {!events.length&&<p className="rc-muted">아직 관측한 이벤트가 없습니다.</p>}
  </section>;
}
