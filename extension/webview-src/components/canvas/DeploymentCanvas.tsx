import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVSCodeApi } from "../../hooks/useVSCodeApi";
import DeploymentCenter from "../DeploymentCenter";
import { AwsConnection } from "../AwsConnection";
import { ShipMode } from "../ShipMode";
import { DeploymentActivity, DeploymentActivityEvent } from '../DeploymentActivity';
import { Replay } from "../Replay";
import { SecurityScanPanel, PolicyPanel, ScanResultLite } from "../Hubs";
import { GateVerdict, gateColors, gateVerdict } from "../securityGate";
import { EcsRuntimeEnvironment, parseRuntimeEnvironment } from "../EcsRuntimeEnvironment";
import { DEFAULT_PROTECTION, protectionRequest, type Protection } from "../EcsProtection";
import { EcsExecutionRole } from "../EcsExecutionRole";
import { EcsDeploymentProgress, EcsProgressStatus, initialEcsProgress, serviceLink } from "../EcsDeploymentProgress";
import { Logo } from "./Logo";
import { FlatBoard } from "./FlatBoard";
import { Icon } from "../ui/icons";
import { SceneNode, Snapshot, Target, available, deploymentScene } from "./model";
import { CanvasDrawer } from "./CanvasDrawer";
import { ApprovalCard, Config, Issue, Plan } from "./ApprovalCard";
import { CanvasEvent, DiscordPanel, DiscordState } from "./DiscordPanel";
import { GitHubPanel } from "./GitHubPanel";
import { EcsSheet, S3Sheet, nextTag } from "./TargetSheets";
import { Kit } from "../ui/kit";
import { canvasStyles } from "./styles";
import { mergeDeployment, SnapshotRequests } from "./state";
import { DockerNotifications } from './dockerNotifications';
import {GroundedRepairPanel} from '../GroundedRepairPanel';
import {ProjectSummary} from './ProjectSummary';
import { DiscordOpsRequest, opsFor, opsOutcome } from './discordOps';

type Pane = "canvas" | "details" | "history" | "aws" | "docker" | "security" | "discord" | "status" | "project";
type OpenablePane = "docker" | "project" | "history";
//: 다른 화면(로컬 Docker 바로가기 · 홈)이 캔버스의 패널을 열 때. 캔버스가 아직 없으면 처음 그릴 때 연다.
let pendingPane: OpenablePane | null = null;
export function requestCanvasPane(pane: OpenablePane): void {
  pendingPane = pane;
  if (typeof window.dispatchEvent === "function") window.dispatchEvent(new CustomEvent("recoder:canvas.pane", { detail: pane }));
}
/** 2.0.47 패널 머리 — 설정 시트와 같은 아이콘 · 제목 · 한 줄 설명. Discord 는 자기 머리를 그린다. */
const PANE_HEAD: Partial<Record<Pane, { icon: React.ReactNode; title: string; sub: string }>> = {
  details: { icon: <Icon name="settings" size={18} />, title: "상세 배포", sub: "파이프라인 · 워크플로 · 고급 설정" },
  history: { icon: <Icon name="history" size={18} />, title: "배포 이력", sub: "이 컴퓨터에 저장된 기록 · 이전 버전으로 되돌리기" },
  aws: { icon: <Logo kind="ecs" />, title: "AWS 연결", sub: "배포 전용 권한으로 내 AWS 계정에 연결" },
  docker: { icon: <Logo kind="docker" />, title: "Docker", sub: "로컬에서 빌드 · 실행 · 롤백" },
  security: { icon: <Icon name="shield" size={18} />, title: "보안 검사", sub: "시크릿 · 이미지 · Dockerfile · 배포 정책" },
  status: { icon: <Icon name="pulse" size={18} />, title: "배포 상태", sub: "검사 단계 · 진행 · 실행 중인 태스크" },
};
const goView = (view: string) => typeof window.dispatchEvent === "function" && window.dispatchEvent(new CustomEvent("recoder:go", { detail: view }));
const defaultConfig: Config = { target:"ecs",image_name:"recoder-app",tag:"v1",aws_region:"",ecs_cluster:"recoder-cluster",ecs_service:"recoder-app",task_family:"recoder-task",container_port:8000,cpu:"256",memory:"512",environment:"staging",dir:"" };
const toolLabels: Record<string,string> = {pending:'대기',running:'검사 중',passed:'통과',findings:'발견 항목 있음',unverified:'미확인',not_run:'미실행',accepted:'요청 승인'};

export default function DeploymentCanvas({ onOpenOperate, navigation, isAiReady=false, isDockerReady=false, isOpsReady=false }: { onOpenOperate?:()=>void; navigation?:React.ReactNode; isAiReady?:boolean; isDockerReady?:boolean; isOpsReady?:boolean }) {
  const {postMessage,useMessage}=useVSCodeApi();
  const [snapshot,setSnapshot]=useState<Snapshot|null>(null), [loading,setLoading]=useState(true);
  const [pane,setPane]=useState<Pane>('canvas'), [visited,setVisited]=useState<Set<Pane>>(new Set(['canvas']));
  const [selection,setSelection]=useState<Target|null>(null),[config,setConfig]=useState<Config>(defaultConfig),[plan,setPlan]=useState<Plan|null>(null);
  const [busy,setBusy]=useState(false),[message,setMessage]=useState(''),[error,setError]=useState('');
  const [runtimeDraft,setRuntimeDraft]=useState({environment:'',secrets:''});
  //: 2.0.25 보호·운영(기본 방패·감지 알림) — 승인 카드에 그대로 보인다
  const [protection,setProtection]=useState<Protection>(DEFAULT_PROTECTION);
  const [blocked,setBlocked]=useState<Issue[]>([]);
  const [live,setLive]=useState<EcsProgressStatus|null>(null);
  const [manualGate,setManualGate]=useState<GateVerdict|null>(null);
  const [s3Progress,setS3Progress]=useState<DeploymentActivityEvent|null>(null);
  const actionTarget=useRef<Target|null>(null);
  const [s3Result,setS3Result]=useState<{url:string;bucket:string;region:string;uploaded:string[];index_copied_from?:string;excluded_note?:string}|null>(null);
  const [discord,setDiscord]=useState<DiscordState|null>(null),[discordError,setDiscordError]=useState(''),[notify,setNotify]=useState(false);
  const [guilds,setGuilds]=useState<Array<{id:string;name:string}>>([]),[channels,setChannels]=useState<Array<{id:string;name:string}>>([]),[events,setEvents]=useState<CanvasEvent[]>([]);
  const snapshotRef=useRef<Snapshot|null>(null), busyRef=useRef(false),touched=useRef(false),dirTouched=useRef(false),notifyRef=useRef(false);
  const snapshotRequests=useRef(new SnapshotRequests()),actionRequest=useRef(''),expectedId=useRef(''),ids=useRef(0),observed=useRef(new Set<string>());
  const liveRef=useRef<EcsProgressStatus|null>(null);
  const dockerNotifications=useRef(new DockerNotifications());
  const discordRef=useRef<DiscordState|null>(null), notificationPreference=useRef(false), notificationRequest=useRef('');
  const lastEventStatus=useRef(''),lastEventDeployment=useRef('');
  //: Discord 에서 온 배포·롤백 요청(확장이 들고 있다). 이 화면에서 승인·결과가 나오면 그 채널에 답한다.
  const [ops,setOps]=useState<DiscordOpsRequest[]>([]);
  const opsRef=useRef<DiscordOpsRequest[]>([]),s3UrlRef=useRef('');
  const requestId=()=>`canvas-${Date.now()}-${++ids.current}`;
  const refresh=useCallback((force=false)=> { const id=`snapshot-${Date.now()}-${++ids.current}`; if(!snapshotRequests.current.begin(id,force))return; setLoading(true);postMessage('canvas.snapshot',{requestId:id}); },[postMessage]);
  const openPane=(next:Pane)=>{setSelection(null);setPane(next);setVisited(v=>new Set([...v,next]));};
  const openPaneRef=useRef(openPane);openPaneRef.current=openPane;
  useEffect(()=>{
    const on=(e:Event)=>{const p=(e as CustomEvent<OpenablePane>).detail;pendingPane=null;openPaneRef.current(p);};
    if(pendingPane){const p=pendingPane;pendingPane=null;openPaneRef.current(p);}
    if(typeof window.addEventListener!=='function')return;
    window.addEventListener('recoder:canvas.pane',on);
    return()=>window.removeEventListener('recoder:canvas.pane',on);
  },[]);
  const applyNotificationPreference=()=>{
    const state=discordRef.current;
    const enabled=notificationPreference.current && Boolean(state?.active_channel_id) && (state?.mode!=='oauth' || state.project_id===state.canvas_project_id);
    notifyRef.current=enabled;setNotify(enabled);
  };
  const notificationSettings=(enabled?:boolean)=>{
    const workspace=snapshotRef.current?.workspace;
    if(!workspace)return;
    const id=requestId();notificationRequest.current=id;
    if(enabled!==undefined){notificationPreference.current=enabled;applyNotificationPreference();}
    postMessage('canvas.discord.notifications',{requestId:id,workspace,...(enabled===undefined?{}:{enabled})});
  };
  const addEvent=useCallback((id:string,title:string,detail:string)=>{
    if(observed.current.has(id)) return;
    observed.current.add(id);
    const enabled=notifyRef.current;
    setEvents(items=>[{id,title,detail,at:new Date().toISOString(),delivery:enabled?'전송 중…':'이 화면에 기록됨'},...items].slice(0,60));
    if(enabled) postMessage('canvas.discord.event',{requestId:`event:${id}`,enabled:true,eventId:id,title,detail});
    const op=opsFor(opsRef.current,snapshotRef.current?.workspace);
    const outcome=op&&opsOutcome(op,title,detail);
    if(op&&outcome) postMessage('canvas.discord.opsReply',{requestId:`ops-${id}`,opsId:op.requestId,...outcome,
      url:outcome.state==='done'?(title==='S3 배포 완료'?s3UrlRef.current:liveRef.current?.service_url||''):''});
  },[postMessage]);
  const replyOps=useCallback((op:DiscordOpsRequest,state:'accepted'|'failed'|'declined',title:string,detail:string)=>{
    postMessage('canvas.discord.opsReply',{requestId:`ops-${state}-${Date.now()}`,opsId:op.requestId,ok:state==='accepted',state,title,detail});
  },[postMessage]);
  const finish=()=>{busyRef.current=false;setBusy(false);};
  const gateRunning=useRef(false);
  const onScanResults=useCallback((results:Partial<Record<'trivy'|'hadolint'|'gitleaks',ScanResultLite>>,running:boolean)=>{
    const verdict=gateVerdict(['trivy','hadolint','gitleaks'],results,running);
    setManualGate(verdict.state==='idle'?null:verdict);
    if(gateRunning.current&&!running&&(verdict.state==='clean'||verdict.state==='issues')) addEvent(`manual-gate-${Date.now()}`,verdict.state==='clean'?'보안 검사 통과':'보안 검사 이상 발견',verdict.state==='clean'?'이미지·Dockerfile·시크릿 검사에서 이상이 없습니다.':`${verdict.label} · 보안 화면에서 확인하세요.`);
    gateRunning.current=running;
  },[addEvent]);

  useMessage(useCallback(event=>{
    const p=event.payload as any; // Message contracts are narrowed per discriminant below.
    const dockerEvent=dockerNotifications.current.consume(event.type,p);
    if(dockerEvent)addEvent(dockerEvent.id,dockerEvent.title,dockerEvent.detail);
    if (['aws.configure.result','aws.clear.result','aws.role.result'].includes(event.type) && p?.ok) refresh();
    if(event.type==='canvas.snapshotResult'&&snapshotRequests.current.finish(p.requestId)) {
      const next=p.snapshot as Snapshot;
      const workspaceChanged=snapshotRef.current?.workspace!==next.workspace;
      if(snapshotRef.current && workspaceChanged) {liveRef.current=null;setLive(null);expectedId.current='';touched.current=false;dirTouched.current=false;notificationPreference.current=false;notifyRef.current=false;setNotify(false);discordRef.current=null;setDiscord(null);dockerNotifications.current=new DockerNotifications();observed.current.clear();setEvents([]);setS3Progress(null);setS3Result(null);
        setRuntimeDraft({environment:'',secrets:''});setPlan(null);setSelection(null);setBlocked([]);setError('');setMessage('');setManualGate(null);lastEventStatus.current='';postMessage('canvas.discord.status');}
      snapshotRef.current=next;setSnapshot(next);setLoading(false);
      if(workspaceChanged)notificationSettings();
      if(!touched.current) setConfig(c=>({...c,container_port:next.container_port||defaultConfig.container_port,aws_region:next.aws.region||c.aws_region,ecs_cluster:next.resource?.cluster||c.ecs_cluster,ecs_service:next.resource?.service||c.ecs_service,tag:nextTag(next.resource?.image)||c.tag}));
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
    if(event.type==='canvas.projectChanged') {refresh(true);return;}
    if(event.type==='canvas.plan'&&p.requestId===actionRequest.current) {setSelection(null);setPlan(p);setConfig(p.config);setMessage('');finish();return;}
    if(event.type==='canvas.blocked'&&p.requestId===actionRequest.current) {if(actionTarget.current==='s3')setS3Progress({step:'error',message:'사전 검사에서 배포를 차단했습니다'});setBlocked(p.preflight?.reasons||[]);setError('보안 게이트가 배포를 차단했습니다.');addEvent(`gate-${p.requestId}`,'보안 게이트 차단','사전 검사에서 배포를 차단했습니다.');finish();return;}
    if(event.type==='canvas.error') {
      if(String(p.context).startsWith('canvas.discord')) {setDiscordError(p.message);if(p.requestId===notificationRequest.current){notificationPreference.current=false;applyNotificationPreference();}if(String(p.requestId).startsWith('event:')) setEvents(items=>items.map(e=>`event:${e.id}`===p.requestId?{...e,delivery:'전송 실패'}:e));return;}
      if(snapshotRequests.current.finish(p.requestId)) {setLoading(false);setError(p.message);return;}
      if(p.requestId===actionRequest.current) {if(actionTarget.current==='s3')setS3Progress({step:'error',message:p.message});finish();setError(p.message);setMessage('');
        const op=opsFor(opsRef.current,snapshotRef.current?.workspace);if(op?.accepted&&op.action==='deploy')replyOps(op,'failed','배포 실패',p.message||'');} return;
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
    if(event.type==='workspace.deploy.s3.progress') {setS3Progress(p);setMessage('');}
    if(event.type==='workspace.deploy.s3.result') {finish();setS3Progress(p.ok?{step:'done',result:{status:'success'}}:{step:'error',message:p.message||'S3 배포 실패'});if(p.ok) {s3UrlRef.current=p.result?.url||'';setS3Result(p.result);setMessage('');addEvent(`s3-${Date.now()}`,'S3 배포 완료',p.result?.bucket||'');refresh();}else {setError(p.message||'S3 배포 실패');const op=opsFor(opsRef.current,snapshotRef.current?.workspace);if(op?.accepted&&op.action==='deploy')replyOps(op,'failed','S3 배포 실패',p.message||'');}}
    if(event.type==='workspace.deploy.remediationResult') {finish();setMessage(p.message||'수정을 적용했습니다. 다시 승인 내용을 확인하세요.');setBlocked([]);refresh();const again=reopenAfterFix.current;reopenAfterFix.current=null;if(again)setTimeout(()=>prepareRef.current(again),0);}
    if(event.type==='workspace.deploy.remediationError') reopenAfterFix.current=null;
    if(event.type==='workspace.deploy.remediationError') {finish();setError(p.message);}
    if(event.type==='canvas.ecs.stopResult') {if(!p.cancelled){setMessage(p.message||'서비스를 중지했습니다.');addEvent(`ecs-stop-${Date.now()}`,'ECS 서비스 중지',p.message||'태스크 수를 0으로 줄였습니다.');refresh();}return;}
    //: 로컬 Docker 결과 알림은 DockerNotifications(develop)가 계획별로 한 번만 만든다 — 여기서 또 만들면 중복 전송된다.
    if(event.type==='workspace.deploy.ecs.rollbackResult'||event.type==='replay.rollbackResult'||event.type==='deploy.rollbackResult') {addEvent(`rollback-${p.deployment_id||p.deploymentId||p.requestId||Date.now()}`,'롤백 결과',p.message||p.status||'결과 확인');refresh();}
    if(event.type==='canvas.discord.statusResult') {discordRef.current=p;setDiscord(p);setDiscordError('');applyNotificationPreference();}
    if(event.type==='canvas.discord.notificationsResult'&&p.requestId===notificationRequest.current&&p.workspace===snapshotRef.current?.workspace) {notificationPreference.current=p.enabled===true;applyNotificationPreference();setDiscordError('');}
    if(event.type==='canvas.discord.ops'&&Array.isArray(p.requests)) {opsRef.current=p.requests;setOps(p.requests);}
    if(event.type==='canvas.discord.guildsResult') setGuilds(p.guilds||[]);
    if(event.type==='canvas.discord.channelsResult') setChannels(p.channels||[]);
    if(event.type==='canvas.discord.eventResult') {setEvents(items=>items.map(e=>e.id===p.event_id?{...e,delivery:'채널 전송 완료'}:e));setDiscordError('');}
  },[addEvent,postMessage,refresh]));

  useEffect(()=>{refresh();postMessage('canvas.discord.status');postMessage('canvas.discord.opsPending',{requestId:requestId()});const timer=window.setInterval(refresh,15000);return()=>window.clearInterval(timer);},[refresh,postMessage]);
  useEffect(()=>{if(!live?.running||!live.deployment_id)return;const id=live.deployment_id;const timer=window.setInterval(()=>postMessage('workspace.deploy.ecs.status',{deploymentId:id}),4000);return()=>window.clearInterval(timer);},[live?.running,live?.deployment_id,postMessage]);
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
    if(node.id==='project'){openPane('project');return;}
    if(node.id==='gate'){openPane('security');return;}
    if(node.id==='discord'){openPane('discord');return;}
    if(['cluster','service','task','public'].includes(node.kind)&&!node.target){openPane('status');return;}
    if(node.target)choose(node.target);
  };
  const cancel=useCallback(()=>{setPlan(p=>{if(p)postMessage('canvas.cancel',{planId:p.id});return null;});},[postMessage]);
  const prepareRef=useRef<(chosen:Config)=>void>(()=>{});
  const prepare=(chosen=config)=>{if(busyRef.current||live?.running)return;try { if(chosen.target==='ecs')chosen={...chosen,...parseRuntimeEnvironment(runtimeDraft),...protectionRequest(protection)}; } catch(err) {setError(String(err));return;}busyRef.current=true;setBusy(true);setError('');setMessage('배포 대상과 사전 검사 결과를 확인 중…');const id=requestId();actionRequest.current=id;postMessage('canvas.prepare',{requestId:id,config:chosen,autoDir:chosen.target==='s3'&&!dirTouched.current});};
  prepareRef.current=prepare;
  const drop=(target:Target)=>{choose(target);};
  const approve=()=>{if(!plan||busyRef.current)return;
    {const op=opsFor(opsRef.current,snapshotRef.current?.workspace);const where=plan.config.target==='s3'?'S3 정적 사이트':plan.config.target==='ecs'?'ECS':'GitHub';
     if(op&&op.action==='deploy'&&!op.accepted)replyOps(op,'accepted','VS Code에서 배포를 승인했어요',`${where}로 배포를 시작합니다. 끝나면 결과를 알려 드릴게요.`);}actionTarget.current=plan.config.target;busyRef.current=true;setBusy(true);if(plan.config.target==='s3'){setS3Result(null);setS3Progress({step:'plan',message:'승인한 파일을 확인합니다'});}setMessage('승인한 요청의 보안·권한 검사를 시작합니다…');setError('');const id=requestId();actionRequest.current=id;postMessage('canvas.execute',{requestId:id,planId:plan.id,approved:true});setPlan(null);};
  const fix=(id:string)=>{cancel();busyRef.current=true;setBusy(true);postMessage('workspace.deploy.remediation.apply',{proposalId:id});};
  //: [모두 적용] 뒤에는 같은 설정으로 승인 카드를 다시 연다 — 고친 결과를 다시 검사해 바로 승인할 수 있게.
  const reopenAfterFix=useRef<Config|null>(null);
  const fixAll=(ids:string[])=>{reopenAfterFix.current=plan?.config??null;cancel();busyRef.current=true;setBusy(true);setMessage(`자동 수정 ${ids.length}건을 적용하는 중…`);postMessage('workspace.deploy.remediation.applyAll',{proposalIds:ids});};
  const scan=live?.deployment_id&&live.deployment_id!==snapshot?.deployment.deployment_id?null:snapshot?.scan;
  const deployment=useMemo(()=>{
    const result=deploymentScene(snapshot?{...snapshot,deployment:live||snapshot.deployment,scan:scan||null}:null,discord?.channel_name?`#${discord.channel_name}`:'',false,false);
    const gate=result.nodes.find(n=>n.id==='gate');
    //: 보안 패널에서 직접 돌린 검사가 가장 최근 결과다. 배포 진행·요청 차단이 더 우선한다.
    if(gate&&manualGate&&manualGate.state!=='idle'&&!live?.running){gate.color=gateColors[manualGate.state];gate.badge=manualGate.label;}
    if(blocked.length&&gate){gate.color='#ff6a73';gate.badge='요청 차단';}
    return result;
  },[snapshot,live,scan,blocked,discord?.channel_name,manualGate]);
  const link=serviceLink(live?.service_url), running=busy||Boolean(live?.running);
  const completed = !running && (Boolean(s3Result) || live?.stage === 'done');
  const completedLink = completed ? serviceLink(s3Result?.url) || link : null;
  const rollbackPending=(live as EcsProgressStatus & {rollback_proposal?:{status?:string}} | null)?.rollback_proposal;
  const update=(key:keyof Config,value:string|number)=>{touched.current=true;if(key==='dir')dirTouched.current=true;setConfig(c=>({...c,[key]:value}));};
  const panes:Array<[Pane,string]>=[['details','상세 배포'],['history','이력·롤백'],['aws','AWS 연결'],['docker','Docker'],['security','보안'],['discord','Discord'],['status','배포 상태']];
  const drawerOpen=pane!=='canvas'||Boolean(selection);
  const targetTitle=selection==='ecs'?'ECS로 배포':selection==='s3'?'S3로 배포':'GitHub로 푸시';
  const closeDrawer=()=>{setSelection(null);setPane('canvas');};
  const configField=(key:Exclude<keyof Config,"env_vars"|"secret_refs"|"assign_public_ip"|"subnet_ids"|"security_group_ids"|"shield"|"shield_level"|"alerts"|"alert_email"|"edge"|"waf_rate_limit"|"waf_window"|"edge_cost_approved"|"https"|"custom_domain"|"database"|"db_cost_approved">,label:string)=><label key={key}>{label}<input value={config[key]} disabled={running} onChange={e=>update(key,key==='container_port'?Number(e.target.value):e.target.value)}/></label>;
  const blockedFixes=[...new Set(blocked.filter(i=>i.remediation_available&&i.proposal_id).map(i=>i.proposal_id!))];
  const notice=<>{error&&<div className="rc-note rc-error" role="alert">{error}</div>}{message&&<div className="rc-note" role="status">{message}</div>}{blockedFixes.length>1&&<button className="rc-primary" disabled={busy} onClick={()=>fixAll(blockedFixes)}>자동 수정 {blockedFixes.length}건 모두 적용</button>}{blocked.map((issue,i)=><div className="rc-finding" key={i}><b>{issue.message}</b><p>{issue.fix}</p>{issue.remediation_available&&issue.proposal_id&&<button disabled={busy} onClick={()=>fix(issue.proposal_id!)}>제안된 수정 적용</button>}</div>)}</>;
  return <section className={`rc-canvas rc-canvas-focused ${completed?'rc-canvas-completed':''}`} aria-label="배포 캔버스">
    <style>{canvasStyles}</style>
    <div className="rc-canvas-bar">
      <nav className="rc-mode-nav" aria-label="작업 전환">{navigation||<span>Deploy</span>}</nav>
      <div className="rc-canvas-context"><h1>{snapshot?.projectName || '배포 캔버스'}</h1><span>{snapshot?.git.branch || (snapshot?.workspace ? '프로젝트' : '프로젝트를 열어주세요')}</span>{(snapshot?.projects?.length||0)>1&&<select className="rc-project-select" aria-label="배포할 프로젝트" title="워크스페이스에 열린 폴더 중 배포할 프로젝트" value={snapshot?.workspace} disabled={running} onChange={e=>{const id=requestId();postMessage('canvas.selectProject',{requestId:id,path:e.target.value});}}>{snapshot!.projects!.map(f=><option key={f.path} value={f.path}>{f.name}</option>)}</select>}<button type="button" className="rc-project-change" title="다른 프로젝트 폴더로 바꾸거나 새 폴더를 추가합니다" disabled={running} onClick={()=>postMessage('canvas.pickProject',{requestId:requestId()})}>폴더 변경</button></div>
      <button className="rc-history-trigger" onClick={()=>openPane('history')}>배포 이력</button>
      <details className="rc-tool-menu rc-more-menu"><summary aria-label="캔버스 메뉴">•••</summary><div className="rc-menu-content">
        <nav aria-label="배포 도구">{panes.map(([id,title])=><button key={id} onClick={e=>{openPane(id);e.currentTarget.closest('details')?.removeAttribute('open');}}>{title}</button>)}{onOpenOperate&&<button onClick={onOpenOperate} disabled={!isOpsReady} title={isOpsReady?undefined:'AI · AWS 연결 필요'}>운영 대응</button>}</nav>
        <div className="rc-menu-options"><button onClick={()=>refresh()} disabled={loading}>{loading?'조회 중…':'새로고침'}</button></div>
      </div></details>
    </div>
    {!drawerOpen&&notice}
    {(()=>{const here=opsFor(ops,snapshot?.workspace),other=here?undefined:ops.find(o=>!o.accepted);const op=here||other;if(!op)return null;
      const label=op.action==='deploy'?'배포':'롤백';
      return <div className="rc-discord-request" role="status" data-testid="discord-request">
        <span className="rc-dr-badge" aria-hidden="true">Discord</span>
        <div className="rc-dr-copy"><strong>{op.requester}님이 {other?`${op.projectName} `:''}{label}{label==='배포'?'를':'을'} 요청했어요</strong>
          <small>{other?'다른 프로젝트의 요청이에요. 그 프로젝트로 바꾼 뒤 진행하세요.':op.accepted?'승인했어요. 결과가 나오면 Discord 채널에 알려 드려요.':op.action==='deploy'?'배포할 곳을 고르고 승인하면 진행하고, 결과를 채널에 알려 드려요.':'이력·롤백에서 되돌릴 배포를 고르면 결과를 채널에 알려 드려요.'}</small></div>
        <div className="rc-dr-actions">
          {other&&<button disabled={running} onClick={()=>postMessage('canvas.selectProject',{requestId:requestId(),path:op.workspace})}>이 프로젝트로 바꾸기</button>}
          {!other&&!op.accepted&&op.action==='rollback'&&pane!=='history'&&<button onClick={()=>openPane('history')}>이력 열기</button>}
          {!op.accepted&&<button className="rc-dr-decline" onClick={()=>replyOps(op,'declined',`${label} 요청을 거절했어요`,'VS Code에서 요청을 거절했어요.')}>거절</button>}
        </div>
      </div>;})()}
    {rollbackPending&&['pending','failed'].includes(rollbackPending.status||'')&&<div className="rc-inline-alert" role="alert"><span>롤백 승인이 필요합니다</span><button onClick={()=>openPane('details')}>확인</button></div>}
    {scan?.blocked&&<div className="rc-inline-alert" role="alert"><span>보안 게이트 차단</span><button onClick={()=>openPane('security')}>검사 결과와 조치 보기</button></div>}
    <DeploymentActivity target="s3" event={s3Progress}/>
    <div className="rc-stage">
      <FlatBoard nodes={deployment.nodes} busy={running||!snapshot?.workspace} onActivate={activate} onDrop={drop} idleHint={!snapshot?'프로젝트를 불러오는 중…':!snapshot.workspace?'VS Code에서 프로젝트 폴더를 열어주세요':running?(live?.stage_text||'배포 준비 중…'):undefined}/>
      {!completed&&(live?.deployment_id||running||Boolean(snapshot?.warnings.length))&&<button className="rc-activity" onClick={()=>openPane('status')}>{running?(live?.stage_text||'배포 준비 중…'):live?.stage==='failed'?'배포 실패 · 결과 확인':'배포 상태 확인'} →</button>}
    </div>
    {completed&&<div className="rc-result-bar" role="status"><span><strong>{s3Result?'S3 배포 완료':'배포 완료'}</strong><small>{s3Result?.bucket || snapshot?.resource?.service}</small></span><div className="rc-actions">{completedLink&&<a className="rc-service-link" href={completedLink} target="_blank" rel="noreferrer">서비스 열기 ↗</a>}<button onClick={()=>openPane('status')}>결과 상세</button></div></div>}
    <CanvasDrawer open={drawerOpen&&!plan} bare={Boolean(selection)||!PANE_HEAD[pane]} icon={PANE_HEAD[pane]?.icon} sub={PANE_HEAD[pane]?.sub} title={selection?targetTitle:PANE_HEAD[pane]?.title||panes.find(([id])=>id===pane)?.[1]||'배포'} onClose={closeDrawer} busy={busy}>
      {drawerOpen&&notice}
      {selection&&<Kit className="rc-target-setup">
        {selection==='ecs'&&<EcsSheet config={config} update={(k,v)=>update(k,v)} running={running} busy={busy} ready={Boolean(snapshot?.workspace)} onPrepare={()=>prepare()}
          protection={protection} onProtection={p=>{setProtection(p);setPlan(null);}} deployedImage={snapshot?.resource?.image}
          advanced={<><div className="rc-fields">{([['image_name','이미지 이름'],['ecs_cluster','Cluster'],['task_family','Task family'],['container_port','컨테이너 포트'],['cpu','CPU 단위'],['memory','메모리 MB']] as Array<[Exclude<keyof Config,"env_vars"|"secret_refs"|"assign_public_ip"|"subnet_ids"|"security_group_ids"|"shield"|"shield_level"|"alerts"|"alert_email"|"edge"|"waf_rate_limit"|"waf_window"|"edge_cost_approved"|"https"|"custom_domain"|"database"|"db_cost_approved">,string]>).map(([key,label])=>configField(key,label))}<label>배포 환경<select disabled={running} value={config.environment} onChange={e=>update('environment',e.target.value)}><option value="staging">staging</option><option value="production">production</option></select></label></div><EcsRuntimeEnvironment value={runtimeDraft} onChange={draft=>{setRuntimeDraft(draft);setPlan(null);}} disabled={running}/><EcsExecutionRole region={config.aws_region} disabled={!snapshot?.aws.ready||running}/></>}/>}
        {selection==='s3'&&<S3Sheet config={config} update={(k,v)=>update(k,v)} running={running} busy={busy} ready={Boolean(snapshot?.workspace)} onPrepare={()=>prepare()} autoDir={!dirTouched.current}/>}
        {selection==='github'&&snapshot&&<GitHubPanel workspace={snapshot.workspace} git={snapshot.git} onChanged={git=>{setSnapshot(s=>s?{...s,git}:s);refresh();}} onReview={()=>prepare()} onWorkflow={()=>openPane('details')} disabled={running}/>}
      </Kit>}
      {visited.has('details')&&<div className="rc-pane-details" hidden={pane!=='details'}><DeploymentCenter key={snapshot?.workspace||'none'} onOpenDocker={()=>openPane('docker')}/></div>}
      {visited.has('history')&&<div className="rc-pane-history" hidden={pane!=='history'}><Replay/></div>}
      {visited.has('aws')&&<div hidden={pane!=='aws'}><AwsConnection ecsPolicyContext={{cluster:config.ecs_cluster,service:config.ecs_service,ecrRepo:(config.image_name||config.ecs_service).trim().toLowerCase()}}/></div>}
      {visited.has('project')&&<div hidden={pane!=='project'}><ProjectSummary key={snapshot?.workspace||'none'} active={pane==='project'} branch={snapshot?.git.branch} onSpec={()=>{closeDrawer();goView('map');}} onDeploy={()=>openPane('docker')}/></div>}
      {visited.has('docker')&&<div hidden={pane!=='docker'}><ShipMode key={snapshot?.workspace||'none'} isAiReady={isAiReady} isDockerReady={isDockerReady}/></div>}
      {visited.has('security')&&<div hidden={pane!=='security'}>{manualGate&&manualGate.state!=='running'&&<div className={`rc-note ${manualGate.state==='issues'?'rc-error':''}`} role="status" style={{borderLeft:`3px solid ${gateColors[manualGate.state]}`}}><b>보안 게이트 · {manualGate.label}</b>{manualGate.notApplicable.length>0&&<small style={{display:'block'}}>{manualGate.notApplicable.map(k=>k==='trivy'?'이미지 검사(빌드된 이미지 없음)':'Dockerfile 검사(Dockerfile 없음)').join(', ')}는 대상이 없어 제외했습니다. 배포할 때 다시 검사합니다.</small>}{manualGate.state==='unverified'&&<small style={{display:'block'}}>검사하지 못한 항목은 통과로 보지 않습니다. 항목을 펼쳐 원인을 확인하세요.</small>}</div>}<SecurityScanPanel key={snapshot?.workspace||'none'} kinds={['trivy','hadolint','gitleaks']} title="지금 소스 검사" note="고친 뒤 다시 검사하세요." onChange={onScanResults}/><PolicyPanel/>{scan?.findings.map((f,i)=><div key={i} className="rc-finding"><b>{f.tool} · {f.title}</b><p>{f.fix}</p><small>{f.location}</small></div>)}{Boolean(scan?.findings.length)&&<button disabled={!isAiReady} onClick={()=>postMessage('canvas.fix',{findings:scan?.findings})}>AI 수정안 만들기</button>}</div>}
      {visited.has('discord')&&<div hidden={pane!=='discord'}><DiscordPanel state={discord} events={events} enabled={notify} onEnabled={value=>notificationSettings(value)} post={postMessage} guilds={guilds} channels={channels} error={discordError}/></div>}
      {pane==='status'&&<div>
        <h3>보안 검사</h3><div className="rc-gates" aria-label="보안 게이트 검사 단계">{(scan?.tools||['hadolint','gitleaks','trivy','OPA'].map(tool=>({tool,state:'pending',detail:''}))).map(t=><span key={t.tool} data-state={t.state} title={t.detail}>{t.tool} · {toolLabels[t.state]||t.state}</span>)}</div>
        {!scan&&<p className="rc-muted">배포를 승인하면 검사를 진행합니다.</p>}{scan&&!live?.running&&<p className="rc-muted">마지막 배포의 검사 기록입니다. 현재 소스는 다음 배포에서 다시 검사합니다.</p>}
        {snapshot?.warnings.map((warning,i)=><div key={i} className="rc-note">{warning}</div>)}
        {(live?.deployment_id||running)&&<EcsDeploymentProgress state={{...initialEcsProgress,status:live,submitting:busy}}/>}
        {live?.stage==='failed'&&<GroundedRepairPanel key={`${snapshot?.workspace}:${live.deployment_id}`} stage="ecs" log={[live.error,live.error_detail,...(live.log_tail||[])].filter(Boolean).join('\n')}/>}
        {snapshot?.topology&&<section className="rc-panel" aria-label="ECS 실시간 계층"><h3>{snapshot.topology.cluster} › {snapshot.topology.service}</h3><p>실행 {snapshot.topology.running??'미확인'} / 희망 {snapshot.topology.desired??'미확인'} · {snapshot.topology.region}</p><small>{new Date(snapshot.topology.observed_at).toLocaleTimeString('ko-KR',{hour12:false})} 조회</small><div className="rc-tree">{snapshot.topology.tasks.map(task=><div key={task.id}><b>{task.id}</b><small>{task.launch_type} · {task.status} · Health {task.health}</small>{task.images.map((image,i)=><small key={i}>{image.image}<br/>{image.digest||'이미지 digest 미확인'}</small>)}</div>)}</div>{!snapshot.topology.tasks.length&&<p>조회된 실행 태스크가 없습니다.</p>}{(snapshot.topology.desired??0)>0&&<div className="rc-actions"><button disabled={running||busy} title="태스크 수를 0으로 줄여 과금을 멈춥니다. 서비스 설정은 남습니다." onClick={()=>postMessage('canvas.ecs.stop',{requestId:requestId(),cluster:snapshot.topology!.cluster,service:snapshot.topology!.service,region:snapshot.topology!.region})}>서비스 중지 (과금 멈춤)</button></div>}{snapshot.topology.truncated&&<p>첫 100개 태스크를 표시합니다.</p>}</section>}
        {s3Result&&<section className="rc-panel" aria-label="S3 배포 결과"><h3>S3 배포 완료</h3>{serviceLink(s3Result.url)&&<a href={serviceLink(s3Result.url)!} target="_blank" rel="noreferrer">정적 사이트 열기 ↗</a>}<p>{s3Result.bucket} · {s3Result.region} · 파일 {s3Result.uploaded.length}개</p>{s3Result.index_copied_from&&<p>{s3Result.index_copied_from}를 index.html로 함께 올렸습니다.</p>}{s3Result.excluded_note&&<div className="rc-note">{s3Result.excluded_note}</div>}</section>}
      </div>}
    </CanvasDrawer>
    {plan&&<ApprovalCard plan={plan} onApprove={approve} onCancel={cancel} onFix={fix} onFixAll={fixAll}/>}
  </section>;
}
