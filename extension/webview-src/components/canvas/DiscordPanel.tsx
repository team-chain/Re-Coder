import React, { useEffect, useRef, useState } from 'react';
import { useVSCodeApi } from '../../hooks/useVSCodeApi';
import { discordStyles } from './discordStyles';

export interface DiscordState {
  mode?: string; active_channel_id?: string; channel_name?: string; guild_name?: string; guild_id?: string;
  connected_clients?: number; authenticated?: boolean; username?: string; project_id?: string; project_name?: string;
  canvas_project_id?: string; projects?: Array<{id:string;name:string}>; development?: boolean;
  bot_ready?: boolean; oauth_ready?: boolean; plain_chat_ready?: boolean; connection_error?: string;
  /** '내 봇 토큰' 방식: 연결된 봇 이름·ID. */
  bot_user?: string; bot_id?: string;
}
export interface CanvasEvent { id: string; title: string; detail: string; at: string; delivery?: string }
type IconName = 'chat' | 'folder' | 'check' | 'refresh' | 'arrow' | 'alert' | 'bell' | 'code';
function Icon({name}: {name: IconName}) {
  const paths:Record<IconName,React.ReactNode> = {
    chat:<><path d="M20 11.5a7.5 7.5 0 0 1-7.5 7.5H7l-4 3V7.5A4.5 4.5 0 0 1 7.5 3h8A4.5 4.5 0 0 1 20 7.5Z"/><path d="M7 9h9M7 13h6"/></>,
    folder:<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Zm0 1h18"/>,
    check:<path d="m5 12 4 4L19 6"/>, refresh:<><path d="M20 11a8 8 0 1 0-2 7M20 4v7h-7"/></>,
    arrow:<path d="M5 12h14m-5-5 5 5-5 5"/>, alert:<><circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 3v.1"/></>,
    bell:<><path d="M6 8a6 6 0 0 1 12 0v7l2 3H4l2-3Z"/><path d="M9 21h6"/></>,
    code:<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18"/>,
  };
  return <svg className="dc-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
export function DiscordPanel({ state, events, enabled, onEnabled, post, guilds, channels, error }: {
  state: DiscordState | null; events: CanvasEvent[]; enabled:boolean; onEnabled:(value:boolean)=>void;
  post:(type:string,payload?:unknown)=>void; guilds:Array<{id:string;name:string;bot_present?:boolean}>;
  channels:Array<{id:string;name:string}>; error:string;
}) {
  const [guild,setGuild]=useState(state?.guild_id||''), [channel,setChannel]=useState(state?.active_channel_id||'');
  const [pending,setPending]=useState(''), [notice,setNotice]=useState(''), [editing,setEditing]=useState(false);
  const [development,setDevelopment]=useState(Boolean(state?.development));
  const request = useRef('');
  const {useMessage}=useVSCodeApi();
  const oauth=!state?.mode || state.mode==='oauth', bot=state?.mode==='bot', token=state?.mode==='token', connected=Boolean(state?.active_channel_id);
  const tokenReady=token&&Boolean(state?.bot_user);
  const authenticated=Boolean(state?.authenticated), busy=Boolean(pending), step=authenticated?(connected?2:1):0;
  const canNotify=!oauth || state?.project_id===state?.canvas_project_id;
  const selectedGuild=guilds.find(g=>g.id===guild);
  const action=(type:string,payload:Record<string,unknown>={})=>{
    const id=`discord-${Date.now()}-${Math.random()}`;request.current=id;
    setPending(type);setNotice('');post(type,{project_id:state?.project_id,...payload,requestId:id});
  };
  useEffect(()=>{post('canvas.discord.status');},[post]);
  useEffect(()=>{
    setGuild(state?.guild_id||'');setChannel(state?.active_channel_id||'');setDevelopment(Boolean(state?.development));
    setEditing(false);
  },[state?.guild_id,state?.active_channel_id,state?.development,state?.project_id]);
  useEffect(()=>{if(oauth&&authenticated)post('canvas.discord.guilds',{project_id:state?.project_id});},[oauth,authenticated,state?.project_id,post]);
  useEffect(()=>{if(!busy)return;const timer=setTimeout(()=>{request.current='';setPending('');setNotice('응답이 지연되고 있어요. 연결 상태를 다시 확인해 주세요.');},pending.endsWith('.connect')?330000:30000);return()=>clearTimeout(timer);},[busy,pending]);
  useMessage(event=>{
    const p=event.payload as any;
    if(!request.current || p?.requestId!==request.current)return;
    if(event.type.startsWith('canvas.discord.')||(event.type==='canvas.error'&&String(p.context).startsWith('canvas.discord.'))){
      request.current='';setPending('');
      setNotice(p.message||(pending.endsWith('.test')?'테스트 알림을 보냈어요. Discord 채널을 확인해 주세요.':''));
      if(event.type==='canvas.discord.statusResult')setEditing(false);
    }
  });
  const selectGuild=(id:string)=>{setGuild(id);setChannel('');if(id&&guilds.find(g=>g.id===id)?.bot_present!==false)action('canvas.discord.channels',{guildId:id});};
  const changeChannel=()=>{setEditing(true);if(guild)action('canvas.discord.channels',{guildId:guild});};
  const problem=error||state?.connection_error;
  const connectionState=connected?(state?.connected_clients||!oauth?'연결됨':'VS Code 대기'):authenticated?'설정 중':'연결 전';
  const projectPicker=<div className="dc-project"><Icon name="folder"/><div className="dc-project-copy"><span className="dc-eyebrow">연결할 프로젝트</span>
    {(state?.projects?.length||0)>1?<select aria-label="연결할 프로젝트" disabled={busy} value={state?.project_id||''} onChange={e=>{onEnabled(false);action('canvas.discord.status',{project_id:e.target.value});}}>{state?.projects?.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select>:<strong>{state?.project_name||'VS Code에서 프로젝트 폴더를 열어 주세요'}</strong>}
  </div></div>;
  const fields=<div className="dc-fields">
    <label className="dc-field">Discord 서버<select aria-label="Discord 서버" disabled={busy} value={guild} onChange={e=>selectGuild(e.target.value)}><option value="">서버를 선택하세요</option>{guilds.map(g=><option key={g.id} value={g.id}>{g.name}{g.bot_present===false?' · 봇 초대 필요':''}</option>)}</select></label>
    <label className="dc-field">연결할 채널<select aria-label="Discord 채널" value={channel} disabled={!guild||busy||selectedGuild?.bot_present===false} onChange={e=>setChannel(e.target.value)}><option value="">{guild?'채널을 선택하세요':'서버를 먼저 선택하세요'}</option>{channels.map(c=><option key={c.id} value={c.id}># {c.name}</option>)}</select></label>
  </div>;
  const developmentSwitch=(persist:boolean)=><label className="dc-option"><span className="dc-option-copy"><strong>Discord에서 개발 요청</strong><small>{state?.plain_chat_ready?'이 채널에 내가 보내는 채팅을 개발 요청으로 처리합니다.':'내 계정의 요청으로 이 프로젝트에 코드를 생성·수정합니다.'}</small></span><input className="dc-switch" type="checkbox" role="switch" aria-label="Discord 개발 요청 허용" disabled={busy} checked={persist?Boolean(state?.development):development} onChange={e=>persist?action('canvas.discord.setChannel',{guildId:state?.guild_id,channelId:state?.active_channel_id,development:e.target.checked}):setDevelopment(e.target.checked)}/></label>;
  return <section className="rc-panel rc-discord" aria-label="Discord 연결과 이벤트">
    <style>{discordStyles}</style>
    <header className="dc-header"><div className="dc-heading"><span className="dc-logo"><Icon name="chat"/></span><div><h3>Discord</h3><p>개발 요청부터 배포 알림까지</p></div></div><span className="dc-badge" data-tone={connected?(state?.connected_clients||!oauth?'success':'pending'):authenticated?'pending':''}><i className="dc-dot"/>{connectionState}</span><button className="dc-icon-button" disabled={busy} aria-label="연결 상태 새로고침" title="연결 상태 새로고침" onClick={()=>action('canvas.discord.status')}><Icon name="refresh"/></button></header>
    {oauth&&<ol className="dc-steps" aria-label="Discord 연결 단계">{['계정 연결','채널 선택','연결 완료'].map((label,i)=><li key={label} aria-current={step===i?'step':undefined} className={step>i?'dc-step-done':''}><span className="dc-step-number">{step>i?<Icon name="check"/>:i+1}</span>{label}</li>)}</ol>}
    {problem&&<div className="dc-alert dc-alert-error" role="alert"><Icon name="alert"/><div><strong>연결 상태를 확인해 주세요</strong><p>{problem}</p><button className="dc-text-button" onClick={()=>post('canvas.discord.settings')}>연결 서버 설정 열기 →</button>{oauth&&<p className="dc-helper">로그인 서버 없이도 알림을 받을 수 있어요: <button className="dc-text-button" disabled={busy} onClick={()=>{onEnabled(false);action('canvas.discord.status',{mode:'token'});}}>내 봇 토큰으로 연결</button> · <button className="dc-text-button" disabled={busy} onClick={()=>{onEnabled(false);action('canvas.discord.status',{mode:'webhook'});}}>웹후크로 연결</button></p>}</div></div>}
    {!problem&&oauth&&!authenticated&&(state?.bot_ready===false||state?.oauth_ready===false)&&<div className="dc-alert" role="status"><Icon name="alert"/><div><strong>봇이 아직 준비되지 않았어요</strong><p>{state.bot_ready===false?'봇이 Discord에 로그인할 수 있도록 실행 설정을 확인해 주세요.':'운영자가 Discord 로그인 설정을 완료하면 연결할 수 있어요.'}</p></div></div>}
    {notice&&!problem&&<p className="dc-notice" role="status">{notice}</p>}
    {oauth&&<>
      {projectPicker}
      {!authenticated?<>
        <div className="dc-intro"><h4>프로젝트를 Discord와 연결하세요</h4><p>로그인한 뒤 알림을 받을 채널을 고르면 됩니다.<br/>이 프로젝트에 대한 요청과 결과를 한곳에서 확인하세요.</p><button className="dc-primary dc-wide" disabled={busy||!state?.project_id} onClick={()=>action('canvas.discord.connect')}><Icon name="chat"/>{pending.endsWith('.connect')?'브라우저 승인 기다리는 중…':'Discord 연결'}<Icon name="arrow"/></button><div className="dc-footnote dc-center">브라우저에서 Discord 로그인을 진행합니다.</div></div>
        <div className="dc-preview"><Icon name="code"/><span>Discord에서 개발 요청</span><small>선택하여 사용</small></div><div className="dc-preview"><Icon name="bell"/><span>배포·검사·롤백 결과 알림</span><small>채널로 전달</small></div>
      </>:<>
        <div className="dc-account"><span className="dc-avatar">{(state?.username||'D').slice(0,1).toUpperCase()}</span><div className="dc-account-copy"><strong>{state?.username}</strong><small>Discord 계정 연결됨</small></div><button className="dc-text-button" disabled={busy} onClick={()=>action('canvas.discord.connect')}>계정 변경</button></div>
        {connected&&!editing?<>
          <div className="dc-destination"><div className="dc-destination-top"><span className="dc-channel-icon">#</span><div className="dc-destination-copy"><strong>{state?.channel_name}</strong><small>{state?.guild_name} · 이 프로젝트의 연결 채널</small></div><Icon name="check"/></div><div className="dc-destination-actions"><button disabled={busy} onClick={()=>action('canvas.discord.test')}>{pending.endsWith('.test')?'전송 중…':'테스트 알림 보내기'}</button><button disabled={busy} onClick={changeChannel}>채널 변경</button>{state?.guild_id&&<a className="dc-link" href={`https://discord.com/channels/${state.guild_id}/${state.active_channel_id}`}>Discord에서 열기 ↗</a>}</div></div>
          <div className="dc-options">{developmentSwitch(true)}{canNotify&&<label className="dc-option"><span className="dc-option-copy"><strong>배포 결과 알림</strong><small>새 배포·검사·롤백 결과를 이 채널로 받습니다.</small></span><input className="dc-switch" role="switch" type="checkbox" aria-label="배포 결과 알림" checked={enabled} onChange={e=>onEnabled(e.target.checked)}/></label>}</div>
          {!canNotify&&<p className="dc-footnote">이 캔버스의 배포 알림은 캔버스 프로젝트를 선택한 뒤 설정할 수 있어요.</p>}
          {state?.development&&<div className="dc-example"><small>{state.plain_chat_ready?'연결한 채널에 그냥 채팅으로 요청하세요':'연결한 채널에서 이렇게 시작하세요'}</small><code>{state.plain_chat_ready?'인사 페이지 만들어줘':'/recoder develop prompt:인사 페이지 만들어줘'}</code>{state.plain_chat_ready?<p>슬래시 명령 <code>/recoder develop</code>도 사용할 수 있어요. 일반 대화는 다른 채널에서 해 주세요.</p>:<p>일반 채팅 요청은 운영자가 Message Content Intent를 켜고 봇을 재시작하면 사용할 수 있어요.</p>}<p>이 프로젝트의 VS Code를 켜 두세요. 터미널 실행은 VS Code에서 확인합니다.</p></div>}
          <p className="dc-footnote">알림 설정은 프로젝트별로 저장됩니다. 배포 캔버스를 열어 둔 동안 Docker·ECS·S3의 새 배포 결과를 전달합니다.</p>
        </>:<div className="dc-form"><div className="dc-form-heading"><h4>{editing?'연결할 채널 변경':'어디에서 함께 작업할까요?'}</h4><button className="dc-text-button" disabled={busy} onClick={()=>{action('canvas.discord.guilds');if(guild)post('canvas.discord.channels',{guildId:guild,project_id:state?.project_id});}}>목록 새로고침</button></div>{fields}
          {(selectedGuild?.bot_present===false||!guilds.length)&&<div className="dc-invite"><p>서버 관리자가 ReCoder를 한 번 초대하면 이 서버의 채널을 선택할 수 있어요.</p><button disabled={busy} onClick={()=>action('canvas.discord.invite',{guildId:guild})}>서버에 봇 초대</button></div>}
          <div className="dc-options">{developmentSwitch(false)}</div><button className="dc-primary dc-wide" disabled={!guild||!channel||busy||selectedGuild?.bot_present===false} onClick={()=>action('canvas.discord.setChannel',{guildId:guild,channelId:channel,development})}>{pending.endsWith('.setChannel')?'연결하는 중…':editing?'변경 사항 저장':'이 채널에 연결'}<Icon name="arrow"/></button><p className="dc-footnote dc-center">연결 후 테스트 알림으로 수신을 확인할 수 있어요.</p>{editing&&<button className="dc-text-button" onClick={()=>{setEditing(false);setGuild(state?.guild_id||'');setChannel(state?.active_channel_id||'');}}>변경 취소</button>}
        </div>}
      </>}
    </>}
    {token&&<div className="dc-form" aria-label="내 봇 연결"><h4>내 Discord 봇으로 알림 받기</h4>
      {!tokenReady?<>
        <ol className="dc-helper" style={{paddingLeft:20,lineHeight:1.8}}><li><a href="https://discord.com/developers/applications">Discord Developer Portal</a>에서 내 앱 → <b>Bot</b> → <b>Reset Token</b>으로 토큰을 복사하세요.</li><li>아래 버튼을 누르고 토큰을 붙여넣으세요. 별도 봇 프로그램을 실행할 필요가 없습니다.</li><li>봇을 서버에 초대하고 알림 채널을 고르세요.</li></ol>
        <button className="dc-primary dc-wide" disabled={busy} onClick={()=>{onEnabled(false);action('canvas.discord.connectToken',{mode:'token'});}}>내 봇 토큰으로 연결<Icon name="arrow"/></button>
        <p className="dc-footnote">토큰은 VS Code 보안 저장소에만 저장합니다. 알림 전송에는 Privileged Intent가 필요 없습니다.</p>
      </>:<>
        <div className="dc-account"><span className="dc-avatar">{(state?.bot_user||'B').slice(0,1).toUpperCase()}</span><div className="dc-account-copy"><strong>{state?.bot_user}</strong><small>내 봇 연결됨{connected?` · #${state?.channel_name}${state?.guild_name?` (${state.guild_name})`:''}`:' · 알림 받을 채널을 고르세요'}</small></div></div>
        {guilds.length===0&&<p className="dc-helper">봇이 아직 어느 서버에도 없습니다. <b>봇 초대</b>로 서버에 추가한 뒤 새로고침을 누르세요.</p>}
        {fields}
        <div className="dc-management"><button className="dc-primary" disabled={!channel||busy} onClick={()=>action('canvas.discord.setChannel',{channelId:channel})}>이 채널로 알림 받기</button><button disabled={busy} onClick={()=>action('canvas.discord.invite')}>봇 초대</button>{connected&&<button disabled={busy} onClick={()=>action('canvas.discord.testWebhook')}>테스트 알림 보내기</button>}<button className="dc-disconnect" disabled={busy} onClick={()=>{onEnabled(false);action('canvas.discord.disconnectToken');}}>연결 해제</button></div>
        {connected&&state?.guild_id&&<a className="dc-link" href={`https://discord.com/channels/${state.guild_id}/${state.active_channel_id}`}>Discord에서 열기 ↗</a>}
      </>}
    </div>}
    {!oauth&&!bot&&!token&&<div className="dc-form"><h4>웹후크로 알림 받기</h4><p className="dc-helper">Discord 채널 설정 → 연동 → 웹후크에서 복사한 URL을 연결하세요.</p>{connected&&<p className="dc-notice"># {state?.channel_name} 연결됨</p>}<div className="dc-management"><button disabled={busy} onClick={()=>{onEnabled(false);action('canvas.discord.connectWebhook');}}>웹후크로 연결</button>{connected&&<><button disabled={busy} onClick={()=>action('canvas.discord.testWebhook')}>테스트 알림 보내기</button><button onClick={()=>{onEnabled(false);action('canvas.discord.disconnectWebhook');}}>연결 해제</button></>}</div></div>}
    {bot&&<div className="dc-form"><h4>기존 봇 서버 연결</h4>{fields}<div className="dc-management"><button onClick={()=>post('canvas.discord.settings')}>서버 설정</button><button onClick={()=>post('canvas.discord.invite')}>봇 초대</button><button disabled={!channel||busy} onClick={()=>action('canvas.discord.setChannel',{channelId:channel})}>선택한 채널 연결</button></div></div>}
    {!oauth&&connected&&<div className="dc-options"><label className="dc-option"><span className="dc-option-copy"><strong>배포 결과 알림</strong><small>캔버스를 연 동안 새 이벤트를 전달합니다.</small></span><input className="dc-switch" role="switch" type="checkbox" aria-label="배포 결과 알림" checked={enabled} onChange={e=>onEnabled(e.target.checked)}/></label></div>}
    <details className="dc-details"><summary>연결 관리</summary><div className="dc-management"><button onClick={()=>post('canvas.discord.settings')}>연결 서버 설정</button>{oauth&&authenticated&&<><button disabled={busy} onClick={()=>action('canvas.discord.invite',{guildId:guild})}>서버에 봇 초대</button><button className="dc-disconnect" disabled={busy} onClick={()=>{onEnabled(false);action('canvas.discord.disconnect');}}>연결 해제</button></>}</div><p>기존 연결이 필요한 경우 아래에서 방식을 선택할 수 있어요.</p><div className="dc-management"><button onClick={()=>{onEnabled(false);action('canvas.discord.status',{mode:'oauth'});}}>Discord 로그인</button><button onClick={()=>{onEnabled(false);action('canvas.discord.status',{mode:'token'});}}>내 봇 토큰</button><button onClick={()=>{onEnabled(false);action('canvas.discord.status',{mode:'webhook'});}}>웹후크 알림</button><button onClick={()=>{onEnabled(false);action('canvas.discord.status',{mode:'bot'});post('canvas.discord.guilds',{mode:'bot'});}}>기존 봇 서버</button></div></details>
    <details className="dc-details"><summary>최근 알림 {events.length>0&&`· ${events.length}`}</summary>{events.length?<ul className="dc-events">{events.map(event=><li key={event.id}><div className="dc-event-heading"><b>{event.title}</b><small>{new Date(event.at).toLocaleTimeString()}</small></div><p>{event.detail}</p><small>{event.delivery||'프로젝트에 기록됨'}</small></li>)}</ul>:<div className="dc-empty"><Icon name="bell"/>새로운 작업 결과가 여기에 표시됩니다.</div>}</details>
  </section>;
}
