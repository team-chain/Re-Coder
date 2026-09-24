import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVSCodeApi } from "../../hooks/useVSCodeApi";
import DeploymentCenter from "../DeploymentCenter";
import { AwsConnection } from "../AwsConnection";
import { ShipMode } from "../ShipMode";
import { Replay } from "../Replay";
import { SecurityScanPanel, PolicyPanel } from "../Hubs";
import { EcsExecutionRole } from "../EcsExecutionRole";
import { EcsDeploymentProgress, EcsProgressStatus, initialEcsProgress, serviceLink } from "../EcsDeploymentProgress";
import { Scene } from "./Scene";
import { AnalysisGraph, SceneNode, Snapshot, Target, analysisScene, available, deploymentScene } from "./model";
import { CanvasDrawer } from "./CanvasDrawer";
import { ApprovalCard, Config, Issue, Plan } from "./ApprovalCard";
import { CanvasEvent, DiscordPanel, DiscordState } from "./DiscordPanel";
import { GitHubPanel } from "./GitHubPanel";
import { canvasStyles } from "./styles";
import { mergeDeployment, SnapshotRequests } from "./state";

type Pane = "canvas" | "details" | "history" | "aws" | "docker" | "security" | "discord" | "status" | "analysis";
const defaultConfig: Config = { target:"ecs",image_name:"recoder-app",tag:"v1",aws_region:"",ecs_cluster:"recoder-cluster",ecs_service:"recoder-app",task_family:"recoder-task",container_port:8000,cpu:"256",memory:"512",environment:"staging",dir:"" };
const toolLabels: Record<string,string> = {pending:'대기',running:'검사 중',passed:'통과',findings:'발견 항목 있음',unverified:'미확인',not_run:'미실행',accepted:'요청 승인'};

export default function DeploymentCanvas({ onOpenDocker, onOpenOperate, navigation, isAiReady=false, isDockerReady=false, isOpsReady=false }: { onOpenDocker:()=>void; onOpenOperate?:()=>void; navigation?:React.ReactNode; isAiReady?:boolean; isDockerReady?:boolean; isOpsReady?:boolean }) {
  const {postMessage,useMessage}=useVSCodeApi();
  const [snapshot,setSnapshot]=useState<Snapshot|null>(null), [loading,setLoading]=useState(true);
  const [pane,setPane]=useState<Pane>('canvas'), [visited,setVisited]=useState<Set<Pane>>(new Set(['canvas']));
  const [selection,setSelection]=useState<Target|null>(null),[config,setConfig]=useState<Config>(defaultConfig),[plan,setPlan]=useState<Plan|null>(null);
  const [busy,setBusy]=useState(false),[message,setMessage]=useState(''),[error,setError]=useState('');
  const [blocked,setBlocked]=useState<Issue[]>([]),[security,setSecurity]=useState(false),[force2D,setForce2D]=useState(false);
  const [expanded,setExpanded]=useState(false),[showNodeDetails,setShowNodeDetails]=useState(false);
  const [graph,setGraph]=useState<AnalysisGraph|null>(null),[level,setLevel]=useState<1|2|3>(1),[file,setFile]=useState(''),[folder,setFolder]=useState<string|null>(null),[search,setSearch]=useState(''),[graphLoading,setGraphLoading]=useState(false);
  const [live,setLive]=useState<EcsProgressStatus|null>(null);
  const [s3Result,setS3Result]=useState<{url:string;bucket:string;region:string;uploaded:string[];index_copied_from?:string;excluded_note?:string}|null>(null);
  const [discord,setDiscord]=useState<DiscordState|null>(null),[discordError,setDiscordError]=useState(''),[notify,setNotify]=useState(false);
  const [guilds,setGuilds]=useState<Array<{id:string;name:string}>>([]),[channels,setChannels]=useState<Array<{id:string;name:string}>>([]),[events,setEvents]=useState<CanvasEvent[]>([]);
  const snapshotRef=useRef<Snapshot|null>(null), busyRef=useRef(false),touched=useRef(false),dirTouched=useRef(false),notifyRef=useRef(false);
  const snapshotRequests=useRef(new SnapshotRequests()),graphRequest=useRef(''),actionRequest=useRef(''),expectedId=useRef(''),ids=useRef(0),observed=useRef(new Set<string>());
  const liveRef=useRef<EcsProgressStatus|null>(null);
  const graphTimer=useRef<ReturnType<typeof setTimeout>>(),graphFile=useRef('');
  const [graphError,setGraphError]=useState(false);
  const stopGraph=()=>{clearTimeout(graphTimer.current);graphRequest.current='';setGraphLoading(false);};
  const lastEventStatus=useRef(''),lastEventDeployment=useRef('');
  const requestId=()=>`canvas-${Date.now()}-${++ids.current}`;
  const refresh=useCallback(()=> { const id=`snapshot-${Date.now()}-${++ids.current}`; if(!snapshotRequests.current.begin(id))return; setLoading(true);postMessage('canvas.snapshot',{requestId:id}); },[postMessage]);
  const openPane=(next:Pane)=>{setSelection(null);setPane(next);setVisited(v=>new Set([...v,next]));};
  const addEvent=useCallback((id:string,title:string,detail:string)=>{
    if(observed.current.has(id)) return;
    observed.current.add(id);
    const enabled=notifyRef.current;
    setEvents(items=>[{id,title,detail,at:new Date().toISOString(),delivery:enabled?'전송 중…':'이 화면에 기록됨'},...items].slice(0,60));
    if(enabled) postMessage('canvas.discord.event',{requestId:`event:${id}`,enabled:true,eventId:id,title,detail});
  },[postMessage]);
  const finish=()=>{busyRef.current=false;setBusy(false);};

  useMessage(useCallback(event=>{
    const p=event.payload as any; // Message contracts are narrowed per discriminant below.
    if (['aws.configure.result','aws.clear.result','aws.role.result'].includes(event.type) && p?.ok) refresh();
    if(event.type==='canvas.snapshotResult'&&snapshotRequests.current.finish(p.requestId)) {
      const next=p.snapshot as Snapshot;
      if(snapshotRef.current && snapshotRef.current.workspace!==next.workspace) {liveRef.current=null;expectedId.current='';touched.current=false;}
      snapshotRef.current=next;setSnapshot(next);setLoading(false);
      if(!touched.current) setConfig(c=>({...c,container_port:next.container_port||defaultConfig.container_port,aws_region:next.aws.region||c.aws_region,ecs_cluster:next.resource?.cluster||c.ecs_cluster,ecs_service:next.resource?.service||c.ecs_service}));
      if(!expectedId.current || expectedId.current===next.deployment.deployment_id) {
        if(liveRef.current?.running && !next.deployment.running && ['done','failed'].includes(next.deployment.stage)) setMessage('');
        liveRef.current=mergeDeployment(liveRef.current,next.deployment);setLive(liveRef.current);
        expectedId.current=liveRef.current.running?liveRef.current.deployment_id||'':'';
      }
      const signature=`${next.deployment.deployment_id}:${next.deployment.stage}:${Boolean(next.scan?.blocked)}:${next.scan?.tools.map(t=>t.state).join(',')||''}`;
      if(lastEventStatus.current && lastEventStatus.current!==signature && next.deployment.deployment_id) {
        addEvent(signature, next.scan?.blocked?'보안 게이트 차단':next.deployment.stage==='done'?'배포 완료':next.deployment.stage==='failed'?'배포 실패':'배포 상태 변경', `${next.resource?.cluster||''} / ${next.resource?.service||''} · ${next.deployment.stage_text||next.deployment.stage}`);
        if(next.scan&&!next.scan.blocked&&next.scan.tools.every(t=>['passed','accepted'].includes(t.state))) addEvent(`${next.deployment.deployment_id}:gate-passed`,'보안 게이트 통과','소스·이미지·정책 검사 결과를 확인했습니다.');
      }
      lastEventStatus.current=signature;
      return;
    }
    if(event.type==='canvas.graphResult'&&p.requestId===graphRequest.current) {stopGraph();setGraph(p.graph);setLevel(p.file?3:2);setFile(p.file);return;}
    if(event.type==='canvas.plan'&&p.requestId===actionRequest.current) {setSelection(null);setPlan(p);setConfig(p.config);setMessage('');finish();return;}
    if(event.type==='canvas.blocked'&&p.requestId===actionRequest.current) {setBlocked(p.preflight?.reasons||[]);setError('보안 게이트가 배포를 차단했습니다.');addEvent(`gate-${p.requestId}`,'보안 게이트 차단','사전 검사에서 배포를 차단했습니다.');finish();return;}
    if(event.type==='canvas.error') {
      if(String(p.context).startsWith('canvas.discord')) {setDiscordError(p.message);if(String(p.requestId).startsWith('event:')) setEvents(items=>items.map(e=>`event:${e.id}`===p.requestId?{...e,delivery:'전송 실패'}:e));return;}
      if(snapshotRequests.current.finish(p.requestId)) {setLoading(false);setError(p.message);return;}
      if(p.requestId===graphRequest.current) {stopGraph();setGraphError(true);setError(p.message);return;}
      if(p.requestId===actionRequest.current) {finish();setError(p.message);setMessage('');} return;
    }
    if(event.type==='canvas.executing'&&p.requestId===actionRequest.current) setMessage(p.message);
    if(event.type==='canvas.completed'&&p.requestId===actionRequest.current) {finish();setMessage(p.message);addEvent(p.requestId,'GitHub 푸시 완료','승인한 브랜치의 커밋을 푸시했습니다.');refresh();}
    if(event.type==='workspace.deploy.result') {
      expectedId.current=p.deployment_id||'';lastEventDeployment.current=p.deployment_id||'';
      liveRef.current={running:true,stage:'pending',deployment_id:p.deployment_id};setLive(liveRef.current);finish();setMessage(p.message||'배포 요청이 접수되었습니다.');setSelection(null);
      addEvent(`${p.deployment_id}:started`,'배포 시작','서버 보안·정책 검사와 배포 진행 상태를 확인합니다.');refresh();
    }
    if(event.type==='workspace.deploy.ecs.statusResult'&&expectedId.current&&p.deployment_id===expectedId.current) {
      liveRef.current=mergeDeployment(liveRef.current,p);setLive(liveRef.current);
      if(!liveRef.current.running) { expectedId.current=''; setMessage(''); refresh(); }
    }
    if(event.type==='workspace.deploy.ecs.error' || event.type==='workspace.deploy.ecs.policyDenied') {finish();setError(p.message||'정책 게이트가 배포를 차단했습니다.');setMessage('');if(p.fix)setBlocked([{code:p.code,message:p.message,fix:p.fix,remediation_available:false,proposal_id:null}]);addEvent(`gate-${actionRequest.current}`,'배포 요청 차단','보안·정책 검사 결과를 확인하세요.');}
    if(event.type==='workspace.deploy.ecs.statusError'&&p.deploymentId===expectedId.current) setError(p.message||'배포 상태 조회 실패');
    if(event.type==='workspace.deploy.s3.dirs'&&!dirTouched.current) setConfig(c=>({...c,dir:p.suggested||''}));
    if(event.type==='workspace.deploy.s3.progress') setMessage(p.message||`S3 · ${p.step}${p.total ? ` ${p.done_count||0}/${p.total}`:''}`);
    if(event.type==='workspace.deploy.s3.result') {finish();if(p.ok) {setS3Result(p.result);setMessage(p.result?.message||'S3 배포 완료');addEvent(`s3-${Date.now()}`,'S3 배포 완료',p.result?.bucket||'');refresh();}else setError(p.message||'S3 배포 실패');}
    if(event.type==='workspace.deploy.remediationResult') {finish();setMessage(p.message||'수정을 적용했습니다. 다시 승인 내용을 확인하세요.');setBlocked([]);refresh();}
    if(event.type==='workspace.deploy.remediationError') {finish();setError(p.message);}
    if(event.type==='workspace.deploy.ecs.rollbackResult'||event.type==='replay.rollbackResult'||event.type==='deploy.rollbackResult') {addEvent(`rollback-${p.deployment_id||p.deploymentId||p.requestId||Date.now()}`,'롤백 결과',p.message||p.status||'결과 확인');refresh();}
    if(event.type==='canvas.discord.statusResult') {setDiscord(p);setDiscordError('');if(!p.active_channel_id){notifyRef.current=false;setNotify(false);}}
    if(event.type==='canvas.discord.guildsResult') setGuilds(p.guilds||[]);
    if(event.type==='canvas.discord.channelsResult') setChannels(p.channels||[]);
    if(event.type==='canvas.discord.eventResult') {setEvents(items=>items.map(e=>e.id===p.event_id?{...e,delivery:'채널 전송 완료'}:e));setDiscordError('');}
  },[addEvent,postMessage,refresh]));

  useEffect(()=>{refresh();postMessage('canvas.discord.status');const timer=window.setInterval(refresh,15000);return()=>window.clearInterval(timer);},[refresh,postMessage]);
  useEffect(()=>{if(!live?.running||!live.deployment_id)return;const id=live.deployment_id;const timer=window.setInterval(()=>postMessage('workspace.deploy.ecs.status',{deploymentId:id}),4000);return()=>window.clearInterval(timer);},[live?.running,live?.deployment_id,postMessage]);
  useEffect(()=>()=>{clearTimeout(graphTimer.current);postMessage('canvas.graph.cancel');},[postMessage]);
  const loadGraph=(id='')=>{
    clearTimeout(graphTimer.current);
    const request=requestId();graphRequest.current=request;graphFile.current=id;setFolder(null);setSearch('');setGraphLoading(true);setGraphError(false);setError('');
    graphTimer.current=setTimeout(()=>{if(graphRequest.current!==request)return;stopGraph();postMessage('canvas.graph.cancel');setGraphError(true);setError('소스 분석 응답이 없습니다. VS Code 실행 창을 다시 시작한 뒤 프로젝트를 다시 눌러 주세요.');},35000);
    postMessage('canvas.graph',{requestId:request,file:id});
  };
  const choose=(target:Target)=>{
    if(busyRef.current||live?.running)return;
    setError('');setMessage('');setBlocked([]);
    if(!snapshot?.workspace){setError('먼저 프로젝트 폴더를 열고 조회를 완료하세요.');return;}
    if(!available(target,snapshot)){openPane('aws');return;}
    if(target==='docker'){openPane('docker');return;}
    setPane('canvas');setSelection(target);setConfig(c=>({...c,target}));
    if(target==='s3')postMessage('workspace.deploy.s3.dirs');
  };
  const activate=(node:SceneNode)=>{
    if(level!==1&&graph){if(node.kind==='folder'){setFolder(node.id);return;}if(level===2){loadGraph(node.id);return;}setMessage(`${node.name} · ${node.badge}`);return;}
    if(node.id==='project'){setFolder(null);setSearch('');loadGraph();return;}
    if(node.id==='gate'){openPane('security');return;}
    if(node.id==='discord'){openPane('discord');return;}
    if(['cluster','service','task','public'].includes(node.kind)&&!node.target){openPane('status');return;}
    if(node.target)choose(node.target);
  };
  const cancel=useCallback(()=>{setPlan(p=>{if(p)postMessage('canvas.cancel',{planId:p.id});return null;});},[postMessage]);
  const prepare=(chosen=config)=>{if(busyRef.current||live?.running)return;busyRef.current=true;setBusy(true);setError('');setMessage('배포 대상과 사전 검사 결과를 확인 중…');const id=requestId();actionRequest.current=id;postMessage('canvas.prepare',{requestId:id,config:chosen,autoDir:chosen.target==='s3'&&!dirTouched.current});};
  const drop=(target:Target)=>{choose(target);};
  const approve=()=>{if(!plan||busyRef.current)return;busyRef.current=true;setBusy(true);setMessage('승인한 요청의 보안·권한 검사를 시작합니다…');setError('');const id=requestId();actionRequest.current=id;postMessage('canvas.execute',{requestId:id,planId:plan.id,approved:true});setPlan(null);};
  const fix=(id:string)=>{cancel();busyRef.current=true;setBusy(true);postMessage('workspace.deploy.remediation.apply',{proposalId:id});};
  const scan=live?.deployment_id&&live.deployment_id!==snapshot?.deployment.deployment_id?null:snapshot?.scan;
  const scene=useMemo(()=>{
    if(level!==1&&graph)return analysisScene(graph,folder,search);
    const result=deploymentScene(snapshot?{...snapshot,deployment:live||snapshot.deployment,scan:scan||null}:null,discord?.channel_name?`#${discord.channel_name}`:'',security,showNodeDetails);
    if(blocked.length){const gate=result.nodes.find(n=>n.id==='gate');if(gate){gate.color='#ff6a73';gate.badge='요청 차단';}}
    return result;
  },[snapshot,live,scan,blocked,discord?.channel_name,security,showNodeDetails,level,graph,folder,search]);
  const link=serviceLink(live?.service_url), running=busy||Boolean(live?.running);
  const completed = !running && (Boolean(s3Result) || live?.stage === 'done');
  const completedLink = completed ? serviceLink(s3Result?.url) || link : null;
  const rollbackPending=(live as EcsProgressStatus & {rollback_proposal?:{status?:string}} | null)?.rollback_proposal;
  const update=(key:keyof Config,value:string|number)=>{touched.current=true;if(key==='dir')dirTouched.current=true;setConfig(c=>({...c,[key]:value}));};
  const panes:Array<[Pane,string]>=[['details','상세 배포'],['history','이력·롤백'],['aws','AWS 연결'],['docker','Docker'],['security','보안'],['discord','Discord'],['status','배포 상태']];
  const drawerOpen=pane!=='canvas'||Boolean(selection);
  const targetTitle=selection==='ecs'?'ECS로 배포':selection==='s3'?'S3로 배포':'GitHub로 푸시';
  const closeDrawer=()=>{setSelection(null);setPane('canvas');};
  const resetGraph=()=>{stopGraph();postMessage('canvas.graph.cancel');setLevel(1);setFolder(null);setSearch('');};
  const configField=(key:keyof Config,label:string)=><label key={key}>{label}<input value={config[key]} disabled={running} onChange={e=>update(key,key==='container_port'?Number(e.target.value):e.target.value)}/></label>;
  const notice=<>{error&&<div className="rc-note rc-error" role="alert">{error}</div>}{message&&<div className="rc-note" role="status">{message}</div>}{blocked.map((issue,i)=><div className="rc-finding" key={i}><b>{issue.message}</b><p>{issue.fix}</p>{issue.remediation_available&&issue.proposal_id&&<button disabled={busy} onClick={()=>fix(issue.proposal_id!)}>제안된 수정 적용</button>}</div>)}</>;
  return <section className={`rc-canvas rc-canvas-focused ${completed?'rc-canvas-completed':''}`} aria-label="배포 캔버스" style={expanded?{position:'fixed',inset:0,zIndex:40,padding:20,overflow:'auto',background:'var(--vscode-editor-background,#141920)'}:undefined}>
    <style>{canvasStyles}</style>
    <div className="rc-canvas-bar">
      <nav className="rc-mode-nav" aria-label="작업 전환">{navigation||<span>Deploy</span>}</nav>
      <div className="rc-canvas-context"><h1>{snapshot?.projectName || '배포 캔버스'}</h1><span>{snapshot?.git.branch || (snapshot?.workspace ? '프로젝트' : '프로젝트를 열어주세요')}</span></div>
      <button className="rc-history-trigger" onClick={()=>openPane('history')}>배포 이력</button>
      <details className="rc-tool-menu rc-more-menu"><summary aria-label="캔버스 메뉴">•••</summary><div className="rc-menu-content">
        <nav aria-label="배포 도구">{panes.map(([id,title])=><button key={id} onClick={e=>{openPane(id);e.currentTarget.closest('details')?.removeAttribute('open');}}>{title}</button>)}{onOpenOperate&&<button onClick={onOpenOperate} disabled={!isOpsReady} title={isOpsReady?undefined:'AI · AWS 연결 필요'}>운영 대응</button>}</nav>
        <div className="rc-menu-options"><label><input type="checkbox" checked={security} onChange={e=>setSecurity(e.target.checked)}/> 보안 레이어</label><label><input type="checkbox" checked={showNodeDetails} onChange={e=>setShowNodeDetails(e.target.checked)}/> 상세 구조 표시</label><label><input type="checkbox" checked={force2D} onChange={e=>setForce2D(e.target.checked)}/> 2D 보기</label><button onClick={()=>setExpanded(v=>!v)}>{expanded?'원래 크기':'캔버스 넓게'}</button><button onClick={refresh} disabled={loading}>{loading?'조회 중…':'새로고침'}</button></div>
      </div></details>
    </div>
    {!drawerOpen&&notice}
    {rollbackPending&&['pending','failed'].includes(rollbackPending.status||'')&&<div className="rc-inline-alert" role="alert"><span>롤백 승인이 필요합니다</span><button onClick={()=>openPane('details')}>확인</button></div>}
    {scan?.blocked&&<div className="rc-inline-alert" role="alert"><span>보안 게이트 차단</span><button onClick={()=>openPane('security')}>검사 결과와 조치 보기</button></div>}
    {level>1&&<div className="rc-breadcrumb"><button onClick={resetGraph}>← 배포</button><button onClick={()=>loadGraph()}>{snapshot?.projectName||'프로젝트'}</button>{folder!==null&&<button onClick={()=>setFolder(null)}>{folder} /</button>}{level===3&&<span>› {file}</span>}<input className="rc-filter" aria-label="파일·함수 검색" placeholder="이름 검색" value={search} onChange={e=>setSearch(e.target.value)}/><button onClick={()=>openPane('analysis')}>분석 상세</button></div>}
    {graphLoading&&<p className="rc-analysis-pending" role="status">소스 분석 중… <button onClick={()=>{stopGraph();postMessage('canvas.graph.cancel');}}>취소</button></p>}
    {graphError&&<button onClick={()=>loadGraph(graphFile.current)}>소스 분석 다시 시도</button>}
    <div className="rc-stage">
      <Scene nodes={scene.nodes} edges={scene.edges} busy={running||!snapshot?.workspace} onActivate={activate} onDrop={drop} force2D={force2D} showDetails={showNodeDetails} dragPrimary={level===1} idleHint={!snapshot?'프로젝트를 불러오는 중…':!snapshot.workspace?'VS Code에서 프로젝트 폴더를 열어주세요':running?(live?.stage_text||'배포 준비 중…'):undefined}/>
      {security&&<button className="rc-security-link" onClick={()=>openPane('security')}>보안 경로 상세</button>}
      {level===1&&!completed&&(live?.deployment_id||running||Boolean(snapshot?.warnings.length))&&<button className="rc-activity" onClick={()=>openPane('status')}>{running?(live?.stage_text||'배포 준비 중…'):live?.stage==='failed'?'배포 실패 · 결과 확인':'배포 상태 확인'} →</button>}
    </div>
    {level===1&&completed&&<div className="rc-result-bar" role="status"><span><strong>{s3Result?'S3 배포 완료':'배포 완료'}</strong><small>{s3Result?.bucket || snapshot?.resource?.service}</small></span><div className="rc-actions">{completedLink&&<a className="rc-service-link" href={completedLink} target="_blank" rel="noreferrer">서비스 열기 ↗</a>}<button onClick={()=>openPane('status')}>결과 상세</button></div></div>}
    <CanvasDrawer open={drawerOpen&&!plan} title={selection?targetTitle:pane==='analysis'?'소스 분석':panes.find(([id])=>id===pane)?.[1]||'배포'} onClose={closeDrawer} busy={busy}>
      {drawerOpen&&notice}
      {selection&&<div className="rc-target-setup">
        <p className="rc-target-summary">{snapshot?.projectName||'프로젝트'} → {selection==='ecs'?config.ecs_cluster:selection==='s3'?'S3':snapshot?.git.repository||'GitHub'}</p>
        {selection==='ecs'&&<>
          <div className="rc-fields">{configField('ecs_service','서비스')}{configField('tag','이미지 태그')}{configField('aws_region','AWS 리전')}</div>
          <details className="rc-details rc-advanced"><summary>고급 설정</summary><div className="rc-fields">{([['image_name','이미지 이름'],['ecs_cluster','Cluster'],['task_family','Task family'],['container_port','컨테이너 포트'],['cpu','CPU 단위'],['memory','메모리 MB']] as Array<[keyof Config,string]>).map(([key,label])=>configField(key,label))}<label>배포 환경<select disabled={running} value={config.environment} onChange={e=>update('environment',e.target.value)}><option value="staging">staging</option><option value="production">production</option></select></label></div><EcsExecutionRole region={config.aws_region} disabled={!snapshot?.aws.ready||running}/></details>
        </>}
        {selection==='s3'&&<div className="rc-fields"><label>빌드 산출물 폴더<input value={config.dir} disabled={running} onChange={e=>update('dir',e.target.value)} placeholder="자동 감지"/></label>{configField('aws_region','AWS 리전')}</div>}
        {selection==='github'&&snapshot&&<GitHubPanel workspace={snapshot.workspace} git={snapshot.git} onChanged={git=>{setSnapshot(s=>s?{...s,git}:s);refresh();}} onReview={()=>prepare()} onWorkflow={()=>openPane('details')} disabled={running}/>}
        {selection!=='github'&&<><button className="rc-primary rc-review-button" onClick={()=>prepare()} disabled={running||!snapshot?.workspace}>{busy?'확인 중…':'배포 내용 확인'}</button><p className="rc-muted rc-approval-hint">다음 단계에서 내용을 검토하고 승인합니다.</p></>}
      </div>}
      {visited.has('details')&&<div hidden={pane!=='details'}><DeploymentCenter onOpenDocker={onOpenDocker}/></div>}
      {visited.has('history')&&<div hidden={pane!=='history'}><Replay/></div>}
      {visited.has('aws')&&<div hidden={pane!=='aws'}><AwsConnection ecsPolicyContext={{cluster:config.ecs_cluster,service:config.ecs_service,ecrRepo:config.ecs_service}}/></div>}
      {visited.has('docker')&&<div hidden={pane!=='docker'}><ShipMode isAiReady={isAiReady} isDockerReady={isDockerReady}/></div>}
      {visited.has('security')&&<div hidden={pane!=='security'}>{security&&<p className="rc-muted">빨간 점선은 소스의 시크릿이 저장소로 유출될 수 있는 경로입니다. 커밋·이미지 포함 여부는 별도 확인이 필요합니다. 검사 미실행은 안전 판정이 아닙니다.</p>}<SecurityScanPanel kinds={['trivy','hadolint','gitleaks']} title="보안 검사" note="수정한 소스를 다시 검사하세요."/><PolicyPanel/>{scan?.findings.map((f,i)=><div key={i} className="rc-finding"><b>{f.tool} · {f.title}</b><p>{f.fix}</p><small>{f.location}</small></div>)}{Boolean(scan?.findings.length)&&<button disabled={!isAiReady} onClick={()=>postMessage('canvas.fix',{findings:scan?.findings})}>AI 수정안 만들기</button>}</div>}
      {visited.has('discord')&&<div hidden={pane!=='discord'}><DiscordPanel state={discord} events={events} enabled={notify} onEnabled={value=>{notifyRef.current=value;setNotify(value);}} post={postMessage} guilds={guilds} channels={channels} error={discordError}/></div>}
      {pane==='analysis'&&graph&&<div><header><h3>{level===2?'파일 · import':'함수 · 호출'} {graph.nodes.length}개</h3><button onClick={()=>loadGraph(level===3?file:'')}>다시 분석</button></header>{graph.nodes.length===0&&<p>분석 가능한 항목이 없습니다.</p>}{level===3&&<button onClick={()=>postMessage('map.openFile',{id:file})}>파일을 에디터에서 열기</button>}{graph.findings.map((f,i)=><div className="rc-finding" key={i}><b>{f.title}</b><p>{f.detail}</p><small>{f.fix}</small></div>)}</div>}
      {pane==='status'&&<div>
        <h3>보안 검사</h3><div className="rc-gates" aria-label="보안 게이트 검사 단계">{(scan?.tools||['hadolint','gitleaks','trivy','OPA'].map(tool=>({tool,state:'pending',detail:''}))).map(t=><span key={t.tool} data-state={t.state} title={t.detail}>{t.tool} · {toolLabels[t.state]||t.state}</span>)}</div>
        {!scan&&<p className="rc-muted">배포를 승인하면 검사를 진행합니다.</p>}{scan&&!live?.running&&<p className="rc-muted">마지막 배포의 검사 기록입니다. 현재 소스는 다음 배포에서 다시 검사합니다.</p>}
        {snapshot?.warnings.map((warning,i)=><div key={i} className="rc-note">{warning}</div>)}
        {(live?.deployment_id||running)&&<EcsDeploymentProgress state={{...initialEcsProgress,status:live,submitting:busy}}/>}
        {snapshot?.topology&&<section className="rc-panel" aria-label="ECS 실시간 계층"><h3>{snapshot.topology.cluster} › {snapshot.topology.service}</h3><p>실행 {snapshot.topology.running??'미확인'} / 희망 {snapshot.topology.desired??'미확인'} · {snapshot.topology.region}</p><small>{new Date(snapshot.topology.observed_at).toLocaleTimeString()} 조회</small><div className="rc-tree">{snapshot.topology.tasks.map(task=><div key={task.id}><b>{task.id}</b><small>{task.launch_type} · {task.status} · Health {task.health}</small>{task.images.map((image,i)=><small key={i}>{image.image}<br/>{image.digest||'이미지 digest 미확인'}</small>)}</div>)}</div>{!snapshot.topology.tasks.length&&<p>조회된 실행 태스크가 없습니다.</p>}{snapshot.topology.truncated&&<p>첫 100개 태스크를 표시합니다.</p>}</section>}
        {s3Result&&<section className="rc-panel" aria-label="S3 배포 결과"><h3>S3 배포 완료</h3>{serviceLink(s3Result.url)&&<a href={serviceLink(s3Result.url)!} target="_blank" rel="noreferrer">정적 사이트 열기 ↗</a>}<p>{s3Result.bucket} · {s3Result.region} · 파일 {s3Result.uploaded.length}개</p>{s3Result.index_copied_from&&<p>{s3Result.index_copied_from}를 index.html로 함께 올렸습니다.</p>}{s3Result.excluded_note&&<div className="rc-note">{s3Result.excluded_note}</div>}</section>}
      </div>}
    </CanvasDrawer>
    {plan&&<ApprovalCard plan={plan} onApprove={approve} onCancel={cancel} onFix={fix}/>}
  </section>;
}
