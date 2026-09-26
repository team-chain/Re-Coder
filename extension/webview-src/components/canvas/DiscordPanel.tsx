import React, { useEffect, useState } from 'react';
import { useVSCodeApi } from '../../hooks/useVSCodeApi';
export interface DiscordState { mode?: string; active_channel_id?: string; channel_name?: string; guild_name?: string; guild_id?: string; connected_clients?: number }
export interface CanvasEvent { id: string; title: string; detail: string; at: string; delivery?: string }
export function DiscordPanel({ state, events, enabled, onEnabled, post, guilds, channels, error }: { state: DiscordState | null; events: CanvasEvent[]; enabled:boolean; onEnabled:(value:boolean)=>void; post:(type:string,payload?:unknown)=>void; guilds:Array<{id:string;name:string}>; channels:Array<{id:string;name:string}>; error:string }) {
  const [guild,setGuild]=useState(''),[channel,setChannel]=useState(''),[pending,setPending]=useState(false),[notice,setNotice]=useState('');
  const {useMessage}=useVSCodeApi();
  const action=(type:string,payload?:unknown)=>{setPending(true);setNotice('');post(type,payload);};
  useEffect(()=>{post('canvas.discord.status');},[post]);
  useEffect(()=>{if(!pending)return;const timer=setTimeout(()=>{setPending(false);setNotice('응답이 없습니다. VS Code 입력창이나 연결 상태를 확인하세요.');},60000);return()=>clearTimeout(timer);},[pending]);
  useMessage(event=>{if(event.type.startsWith('canvas.discord.')||(event.type==='canvas.error'&&String((event.payload as any)?.context).startsWith('canvas.discord.'))){setPending(false);setNotice((event.payload as any)?.message||'');}});
  const bot=state?.mode==='bot',connected=Boolean(state?.active_channel_id);
  return <section className="rc-panel" aria-label="Discord 연결과 이벤트">
    <header><h3>Discord · 배포 알림</h3><button disabled={pending} onClick={()=>action('canvas.discord.status')}>상태 확인</button></header>
    <p className="rc-muted">배포·보안 검사·롤백 결과를 원하는 채널로 받습니다.</p>
    {error&&<p className="rc-note rc-error" role="alert">{error}</p>}
    {notice&&!error&&<p role="status" className="rc-note">{notice}</p>}
    {!bot&&<>
      {connected?<p className="rc-note"><b>{state?.channel_name}</b> · 연결됨</p>:<ol style={{paddingLeft:20,lineHeight:1.8}}><li>Discord에서 알림 받을 텍스트 채널의 설정을 여세요.</li><li><b>연동 → 웹후크 → 새 웹후크 → 웹후크 URL 복사</b>를 선택하세요.</li><li>아래 버튼을 누르고 URL을 붙여넣으세요.</li></ol>}
      <div className="rc-actions"><button disabled={pending} className="rc-primary" onClick={()=>{onEnabled(false);action('canvas.discord.connectWebhook');}}>{connected?'알림 채널 변경':'웹후크로 연결'}</button>{connected&&<><button disabled={pending} onClick={()=>action('canvas.discord.testWebhook')}>테스트 알림 보내기</button><button disabled={pending} onClick={()=>{onEnabled(false);action('canvas.discord.disconnectWebhook');}}>연결 해제</button></>}</div>
      {!connected&&<p className="rc-muted">웹후크 관리 권한이 필요합니다. URL은 VS Code의 보안 저장소에 저장합니다.</p>}
      {connected&&state?.guild_id&&<a href={`https://discord.com/channels/${state.guild_id}/${state.active_channel_id}`}>연결한 채널 열기</a>}
    </>}
    <details className="rc-details" style={{marginTop:16}} open={bot||undefined}><summary>기존 봇 서버 연결</summary>
      <p className="rc-muted">이미 ReCoder 봇 서버를 운영하는 경우에 사용합니다.</p>
      <div className="rc-actions"><button onClick={()=>{action('canvas.discord.status',{mode:'bot'});post('canvas.discord.guilds');}}>봇 서버 불러오기</button><button onClick={()=>post('canvas.discord.settings')}>서버 설정</button><button onClick={()=>post('canvas.discord.invite')}>봇 초대</button></div>
      {bot&&<><div className="rc-fields"><label>서버<select value={guild} onChange={e=>{setGuild(e.target.value);setChannel('');if(e.target.value)post('canvas.discord.channels',{guildId:e.target.value});}}><option value="">서버 선택</option>{guilds.map(g=><option key={g.id} value={g.id}>{g.name}</option>)}</select></label><label>알림 채널<select value={channel} disabled={!guild} onChange={e=>setChannel(e.target.value)}><option value="">채널 선택</option>{channels.map(c=><option key={c.id} value={c.id}>#{c.name}</option>)}</select></label></div><div className="rc-actions"><button disabled={!channel||pending} onClick={()=>action('canvas.discord.setChannel',{channelId:channel})}>선택한 채널 연결</button><button onClick={()=>{onEnabled(false);action('canvas.discord.setChannel',{channelId:''});}}>봇 채널 연결 해제</button><button onClick={()=>{onEnabled(false);action('canvas.discord.status',{mode:'webhook'});}}>웹후크 방식 사용</button></div></>}
    </details>
    {connected&&<label style={{display:'block',marginTop:16}}><input type="checkbox" checked={enabled} onChange={e=>onEnabled(e.target.checked)}/> 새 배포·검사·롤백 이벤트를 이 채널로 보내기</label>}
    <p className="rc-muted" style={{marginTop:8}}>캔버스를 연 동안의 새 이벤트를 보냅니다. 이전 이력은 전송하지 않습니다.</p>
    <ul className="rc-events">{events.map(event=><li key={event.id}><div className="rc-event-title"><b>{event.title}</b><small>{new Date(event.at).toLocaleTimeString()}</small></div><div>{event.detail}</div><small>{event.delivery || '이 화면에 기록됨'}</small></li>)}</ul>
  </section>;
}
