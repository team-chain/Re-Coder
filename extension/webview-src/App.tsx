/** ReCoder workspace and sidebar: Develop, Deploy, Security, and shared status. */

import React, { useState, useCallback, useEffect } from "react";
import { useVSCodeApi } from "./hooks/useVSCodeApi";
import { usePolling } from "./hooks/usePolling";
import { BuildMode } from "./components/BuildMode";
import CodeAgent from "./components/CodeAgent";
import type { ExternalTurn } from "./components/CodeAgent";
import { HubHome, HubPage, FeatureFrame, AdrPanel, SecurityHub, SecurityScanPanel, PolicyPanel, HUBS, hubOf, isHubView, isFeatureView } from "./components/Hubs";
import type { HubId, FeatureId, ReadyCtx } from "./components/Hubs";
import { ShipMode } from "./components/ShipMode";
import { OperateMode } from "./components/OperateMode";
import { DiagnosticsPanel } from "./components/DiagnosticsPanel";
import { CostTracker } from "./components/CostTracker";
import { Replay } from "./components/Replay";
import CodeMap from "./components/CodeMap";
import DeploymentCanvas from "./components/canvas/DeploymentCanvas";
import { AwsConnection } from './components/AwsConnection';
import { CanvasDrawer } from './components/canvas/CanvasDrawer';
import { canvasStyles } from './components/canvas/styles';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

//: 화면 구조 (A안, 2026-09-17): 홈(허브 3장) → 허브 페이지(기능 카드) → 기능 화면.
//: "build"·"ship"·"deploy"·"operate"·"replay"·"map" 은 예전 이름 그대로 두어
//: 기존 테스트·핸들러가 깨지지 않게 했다. "code" 는 CodeAgent 단독 화면(코드 생성 · 수정).
type ViewMode = "home" | `hub:${HubId}` | FeatureId;

interface DiagnosticsResult {
  core_ready: string;
  ai_ready: string;
  docker_ready: string;
  aws_deploy_ready: string;
  ops_ready: string;
}

// ---------------------------------------------------------------------------
// Inline SVG Icons (이모지 대체)
// ---------------------------------------------------------------------------

const Icon = {
  /** 로고 - 사이드바 헤더 + 작은 마크용 */
  Logo: ({ size = 28 }: { size?: number }) => (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-label="Re-Coder"
    >
      <path d="M 51 16 A 22 22 0 0 0 13 22" strokeWidth="3" />
      <polyline points="51,7 51,16 42,16" strokeWidth="3" />
      <path d="M 13 48 A 22 22 0 0 0 51 42" strokeWidth="3" />
      <polyline points="13,57 13,48 22,48" strokeWidth="3" />
      <line x1="22" y1="20" x2="22" y2="46" strokeWidth="3.5" />
      <path d="M 22 20 L 30 20 A 6 6 0 0 1 30 32 L 22 32" strokeWidth="3.5" />
      <line x1="27" y1="32" x2="35" y2="46" strokeWidth="3.5" />
      <polyline points="41,26 46,31 41,36" strokeWidth="2.5" />
    </svg>
  ),
  /** 채팅 / Discord */
  Chat: ({ size = 18 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.7a8.5 8.5 0 0 1-.9-3.8 8.38 8.38 0 0 1 8.5-8.5 8.5 8.5 0 0 1 8.5 8.5z" />
    </svg>
  ),
  /** 대시보드 (Workbench 진입) */
  Dashboard: ({ size = 16 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="7" height="9" />
      <rect x="14" y="3" width="7" height="5" />
      <rect x="14" y="12" width="7" height="9" />
      <rect x="3" y="16" width="7" height="5" />
    </svg>
  ),
  /** Chevron - 카드 우측 */
  ChevronRight: ({ size = 16 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="9 18 15 12 9 6" />
    </svg>
  ),
  /** Back arrow */
  ArrowLeft: ({ size = 14 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="19" y1="12" x2="5" y2="12" />
      <polyline points="12 19 5 12 12 5" />
    </svg>
  ),
};

// ---------------------------------------------------------------------------
// Hero (로고 + 태그라인)
// ---------------------------------------------------------------------------

const Hero: React.FC<{ onOpenWorkspace?: () => void }> = ({ onOpenWorkspace }) => (
  <div style={{ padding: "18px 16px 8px", background: "var(--vscode-sideBar-background, #1e1e1e)" }}>
    <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
      <span style={{ color: "var(--vscode-foreground, #e0e0e0)", display: "inline-flex" }}><Icon.Logo size={22} /></span>
      <span style={{ fontSize: 20, fontWeight: 600, color: "var(--vscode-foreground, #e0e0e0)", letterSpacing: "-0.01em" }}>ReCoder</span>
    </div>
    {onOpenWorkspace && (
      <button
        onClick={onOpenWorkspace}
        style={{ width: "100%", marginTop: 12, padding: "8px 10px", border: "none", borderRadius: 5, background: "var(--vscode-button-background, #0e639c)", color: "var(--vscode-button-foreground, #fff)", cursor: "pointer", fontSize: 12, fontWeight: 600 }}
      >
        ReCoder 창 열기
      </button>
    )}
  </div>
);

// ---------------------------------------------------------------------------
// Status Pills (이모지/✓✗ 대신 inline SVG 사용)
// ---------------------------------------------------------------------------

interface StatusBadgeProps {
  diagnostics: DiagnosticsResult | null;
  coreStatus: "ok" | "degraded" | "down" | null;
  expanded: boolean;
  onToggle: () => void;
  compact?: boolean;
  pending?: boolean;
  error?: string;
}

const StatusBadge: React.FC<StatusBadgeProps> = ({ diagnostics, coreStatus, expanded, onToggle, compact, pending, error }) => {
  const green = "var(--vscode-charts-green, #3fb950)";
  const amber = "var(--vscode-editorWarning-foreground, #d7a300)";
  const red = "var(--vscode-editorError-foreground, #e5534b)";
  const muted = "var(--vscode-descriptionForeground, #888)";

  const coreOk = coreStatus === "ok";
  const aiOk = diagnostics?.ai_ready === "ready";

  let label: string;
  let color: string;
  if (coreStatus === null) { label = "Core 확인 중"; color = muted; }
  else if (!coreOk) { label = "Core 연결 실패"; color = red; }
  else if (pending) { label = "AI 확인 중"; color = muted; }
  else if (error) { label = "AI 확인 실패"; color = amber; }
  else if (aiOk) { label = "AI 준비됨"; color = green; }
  else if (!diagnostics) { label = "AI 확인 중"; color = muted; }
  else { label = "AI 설정 필요"; color = amber; }

  if (compact) return <button className="rc-status-toggle" onClick={onToggle} aria-expanded={expanded} aria-controls="workspace-diagnostics" title="연결 상태와 사용량">
    <span style={{ width: 6, height: 6, borderRadius: "50%", background: color }} />{label}
  </button>;

  return (
    <div style={{
      display: "flex",
      alignItems: "center",
      padding: "9px 14px",
      background: "var(--vscode-sideBar-background, #1e1e1e)",
      borderBottom: "1px solid var(--vscode-panel-border, #2a2a2a)",
    }}>
      <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, flexShrink: 0 }} />
      <span style={{ marginLeft: 8, fontSize: 12, fontWeight: 600, color }}>{label}</span>
      <button
        onClick={onToggle}
        title={expanded ? "상태 상세 숨기기" : "상태 상세 보기"}
        style={{
          marginLeft: "auto",
          background: "none",
          border: "none",
          cursor: "pointer",
          color: muted,
          display: "flex",
          alignItems: "center",
          padding: 2,
        }}
      >
        <span style={{ display: "inline-flex", transform: expanded ? "rotate(90deg)" : "none", transition: "transform 0.12s" }}>
          <Icon.ChevronRight size={14} />
        </span>
      </button>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Sub-page header (뒤로가기)
// ---------------------------------------------------------------------------

interface WorkspaceLayoutProps {
  view: ViewMode;
  externalTurn?: ExternalTurn | null;
  diagnostics: DiagnosticsResult | null;
  coreStatus: "ok" | "degraded" | "down" | null;
  showDiagnostics: boolean;
  diagnosticsPending?: boolean;
  diagnosticsError?: string;
  isAiReady: boolean;
  isDockerReady: boolean;
  isOpsReady: boolean;
  costSummary: ReturnType<typeof usePolling>["costSummary"];
  onSelectMode: (mode: ViewMode) => void;
  onToggleDiagnostics: () => void;
  postMessage: (type: string, payload?: unknown) => void;
}

// ---------------------------------------------------------------------------
// HubRouter — 홈 / 허브 페이지 / 기능 화면을 view 값으로 골라 그린다.
// 사이드바와 Workspace 창이 같은 라우팅을 쓴다.
// ---------------------------------------------------------------------------
const HubRouter: React.FC<{ view: ViewMode; ctx: ReadyCtx; externalTurn?: ExternalTurn | null; onSelectMode: (m: ViewMode) => void; onReviewRequired: () => void; connectionPending?: boolean; connectionError?: string }> = ({ view, ctx, externalTurn, onSelectMode, onReviewRequired, connectionPending, connectionError }) => {
  const [codeVisited, setCodeVisited] = useState(view === "code");
  const deploying = view === 'hub:deploy' || view === 'deploy';
  const [deployVisited, setDeployVisited] = useState(deploying);
  const [securityVisited, setSecurityVisited] = useState(view === 'hub:security');
  useEffect(() => { if (view === "code") setCodeVisited(true); }, [view]);
  useEffect(() => { if (deploying) setDeployVisited(true); if (view === 'hub:security') setSecurityVisited(true); }, [view, deploying]);
  return <>
    <div className="rc-code-page" hidden={view !== "code"}>
      {(codeVisited || view === "code") && <FeatureFrame feature="code" onHome={() => onSelectMode("home")} onHub={h => onSelectMode(`hub:${h}`)}>
        <CodeAgent isActive={ctx.isAiReady} externalTurn={externalTurn} onReviewRequired={onReviewRequired} connectionPending={connectionPending} connectionError={connectionError} />
      </FeatureFrame>}
    </div>
    <div hidden={!deploying}>{(deployVisited || deploying) && <FeatureRouter view="hub:deploy" ctx={ctx} onSelectMode={onSelectMode} />}</div>
    <div hidden={view !== 'hub:security'}>{(securityVisited || view === 'hub:security') && <SecurityHub onHome={() => onSelectMode('home')} onHub={h => onSelectMode(`hub:${h}`)} />}</div>
    {view !== "code" && !deploying && view !== 'hub:security' && <FeatureRouter view={view} ctx={ctx} externalTurn={externalTurn} onSelectMode={onSelectMode} />}
  </>;
};

/** Connection management is reachable in every workspace mode. */
const WorkspaceConnection: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [open, setOpen] = useState(false), [visited, setVisited] = useState(false);
  const [ready, setReady] = useState<boolean | null>(null);
  useEffect(() => { postMessage('aws.status'); }, [postMessage]);
  useMessage(useCallback(({ type, payload }) => {
    const p = payload as { ready?: boolean; ok?: boolean; status?: { ready?: boolean }; snapshot?: { aws?: { ready?: boolean } } } | undefined;
    if (type === 'aws.status') setReady(Boolean(p?.ready));
    if (type === 'canvas.snapshotResult' && p?.snapshot?.aws) setReady(p.snapshot.aws.ready === true);
    if (type === 'aws.configure.result' && p?.ok) setReady(p.status?.ready === true);
    if (type === 'aws.clear.result' && p?.ok) setReady(false);
  }, []));
  return <div className="rc-workspace-connection rc-canvas">
    <style>{canvasStyles}</style>
    <button className="rc-connection-button" onClick={() => { setVisited(true); setOpen(true); }} aria-haspopup="dialog" title="AWS 계정 연결 관리"><span className="rc-connection-dot" data-ready={ready === true} />{ready === null ? 'AWS 확인 중' : ready ? 'AWS 연결됨' : 'AWS 연결'}</button>
    <CanvasDrawer open={open} title="AWS 연결" onClose={() => setOpen(false)}>{visited && <AwsConnection />}</CanvasDrawer>
  </div>;
};

const FeatureRouter: React.FC<{ view: ViewMode; ctx: ReadyCtx; externalTurn?: ExternalTurn | null; onSelectMode: (m: ViewMode) => void }> = ({ view, ctx, externalTurn, onSelectMode }) => {
  const goHome = () => onSelectMode("home");
  const goHub = (h: HubId) => onSelectMode(`hub:${h}`);
  if (view === "home") {
    return <HubHome ctx={ctx} onSelect={goHub} />;
  }
  if (isHubView(view) && view !== "hub:deploy") {
    const hub = view.slice(4) as HubId;
    return <HubPage hub={hub} ctx={ctx} onOpen={(f) => onSelectMode(f)} onHome={goHome} onHub={goHub} />;
  }
  // Home's Deploy card and the top-level tab both send hub:deploy.
  // Keep the legacy deploy entry working, but show the canvas immediately on either route.
  if (view !== "hub:deploy" && !isFeatureView(view)) { return null; }
  const feature: FeatureId = view === "hub:deploy" ? "deploy" : view;
  let body: React.ReactNode = null;
  switch (feature) {
    case "code": body = <CodeAgent isActive={ctx.isAiReady} externalTurn={externalTurn} />; break;
    case "build": body = <BuildMode isActive={ctx.isAiReady} onOpenDevelopment={() => onSelectMode("code")} />; break;
    case "map": body = <CodeMap isActive />; break;
    case "adr": body = <AdrPanel />; break;
    case "ship": body = <ShipMode isAiReady={ctx.isAiReady} isDockerReady={ctx.isDockerReady} />; break;
    case "deploy": return <DeploymentCanvas navigation={HUBS.map(h=><button key={h.id} aria-pressed={h.id==='deploy'} onClick={()=>goHub(h.id)}>{h.title}</button>)} onOpenDocker={() => onSelectMode("ship")} onOpenOperate={() => onSelectMode("operate")} isAiReady={ctx.isAiReady} isDockerReady={ctx.isDockerReady} isOpsReady={ctx.isOpsReady} />;
    case "replay": body = <Replay />; break;
    case "operate": body = <OperateMode isActive={ctx.isOpsReady} />; break;
    case "scan": body = <SecurityScanPanel kinds={["trivy", "hadolint"]} title="취약점 스캔" note="이미지·의존성은 Trivy, Dockerfile 은 Hadolint 로 검사합니다. 결과는 배포 승인 카드의 위험도에 반영돼요." />; break;
    case "secrets": body = <SecurityScanPanel kinds={["gitleaks"]} title="시크릿 검사" note="저장소에 API 키·비밀번호 같은 값이 평문으로 남았는지 gitleaks 규칙으로 확인합니다. 찾은 값의 원문은 화면에 표시하지 않아요." />; break;
    case "policy": body = <PolicyPanel />; break;
  }
  return <FeatureFrame feature={feature} onHome={goHome} onHub={goHub}>{body}</FeatureFrame>;
};

//: 테스트에서 직접 렌더할 수 있도록 export 한다. 이 레이아웃에서 설계 결정
//: 경로(CodeAgent)가 살아있는지가 회귀 대상이다 — 예전에 여기서만 숨겨져서
//: Workspace 창에서 결정 카드가 뜨지 않는 버그가 있었다.
export const WorkspaceLayout: React.FC<WorkspaceLayoutProps> = ({
  view, externalTurn, diagnostics, coreStatus, showDiagnostics, diagnosticsPending, diagnosticsError, isAiReady, isDockerReady, isOpsReady,
  costSummary, onSelectMode, onToggleDiagnostics,
}) => {
  const activeHub = isHubView(view) ? view.slice(4) : isFeatureView(view) ? hubOf(view) : '';
  const showReview = useCallback(() => onSelectMode("code"), [onSelectMode]);
  return (
    <div className="rc-workspace">
      <style>{`
        .rc-workspace{height:100vh;display:grid;grid-template-columns:minmax(0,1fr);overflow:hidden;position:relative;background:var(--vscode-editor-background,#181818);color:var(--vscode-foreground,#e0e0e0)}
        .rc-workspace-header{display:flex;align-items:center;gap:12px;min-height:62px;padding:10px 24px;border-bottom:1px solid var(--vscode-panel-border,#2a2a2a);flex-wrap:wrap}
        .rc-workspace-header button{font:inherit;font-size:12px;cursor:pointer;color:inherit;background:transparent;border:1px solid var(--vscode-panel-border,#333);border-radius:6px;padding:6px 10px}
        .rc-workspace-header .rc-brand{display:flex;align-items:center;gap:8px;border:0;padding-left:0;font-size:15px;font-weight:600}
        .rc-workspace-header .rc-status-toggle{display:flex;align-items:center;gap:7px;color:var(--vscode-descriptionForeground,#aaa);border:0;font-size:11px;padding:6px}
        .rc-global-nav{display:flex;align-items:center;gap:4px;margin-left:24px}.rc-workspace-header .rc-global-nav button{border:0;padding:9px 14px;color:var(--vscode-descriptionForeground,#929ba7)}.rc-workspace-header .rc-global-nav button[aria-current=page]{background:var(--vscode-list-activeSelectionBackground,#263748);color:var(--vscode-list-activeSelectionForeground,#eef5ff)}
        .rc-workspace-connection{margin-left:auto}.rc-workspace-header .rc-connection-button{display:flex;align-items:center;gap:7px;border:0;background:transparent;font-size:11px;color:var(--vscode-descriptionForeground,#aaa)}.rc-connection-dot{height:6px;width:6px;border-radius:50%;background:#d2a760}.rc-connection-dot[data-ready=true]{background:#56bf91}
        .rc-workspace .rc-local-hub-nav,.rc-workspace .rc-mode-nav{display:none!important}.rc-workspace [hidden]{display:none!important}
        .rc-workspace-content{flex:1;min-height:0;overflow:auto;padding:28px 32px}
        .rc-workspace button:focus-visible{outline:2px solid var(--vscode-focusBorder,#75b9ef);outline-offset:3px}
        .rc-code-page{width:100%;max-width:1080px;margin:0 auto}
        @media(max-width:850px){.rc-workspace-content{padding:16px}.rc-workspace-header{padding:10px 16px}}
        @media(max-width:720px){.rc-global-nav{order:5;width:100%;margin:0;justify-content:center;border-top:1px solid var(--vscode-panel-border,#333);padding-top:8px}.rc-workspace-header{gap:6px}.rc-workspace-header .rc-status-toggle{max-width:18px;overflow:hidden;white-space:nowrap;padding:4px}.rc-status-toggle>span{flex-shrink:0}.rc-workspace-header .rc-brand{font-size:13px}.rc-workspace-header .rc-connection-button{font-size:10px;padding:4px}.rc-workspace-header>button:last-child{font-size:11px}}
      `}</style>
      <section style={{ minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column", borderRight: "1px solid var(--vscode-panel-border, #333)" }}>
        <div className="rc-workspace-header">
          <button className="rc-brand" onClick={() => onSelectMode("home")} aria-label="ReCoder 홈"><Icon.Logo size={22} />ReCoder</button>
          <nav className="rc-global-nav" aria-label="작업 선택">{HUBS.map(h => <button key={h.id} aria-current={activeHub === h.id ? 'page' : undefined} onClick={() => onSelectMode(`hub:${h.id}`)}>{h.title}</button>)}</nav>
          <WorkspaceConnection />
          <StatusBadge pending={diagnosticsPending} error={diagnosticsError} compact diagnostics={diagnostics} coreStatus={coreStatus} expanded={showDiagnostics} onToggle={onToggleDiagnostics} />
        </div>

        {showDiagnostics && <div id="workspace-diagnostics" style={{ borderBottom: "1px solid var(--vscode-panel-border, #333)", maxHeight: 260, overflowY: "auto" }}><DiagnosticsPanel diagnostics={diagnostics} /><CostTracker costSummary={costSummary} /></div>}

        <div className="rc-workspace-content">
          <HubRouter view={view} ctx={{ isAiReady, isDockerReady, isOpsReady }} externalTurn={externalTurn} onSelectMode={onSelectMode} onReviewRequired={showReview} connectionPending={diagnosticsPending || coreStatus === null} connectionError={diagnosticsError || (coreStatus && coreStatus !== "ok" ? "Core에 연결할 수 없습니다." : "")} />
        </div>
      </section>

    </div>
  );
};

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

const App: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();
  const isWorkspacePanel = typeof document !== "undefined" && document.documentElement.dataset.recoderLayout === "workspace";
  const { coreHealth, costSummary } = usePolling(4000);

  const [view, setView] = useState<ViewMode>(isWorkspacePanel ? "hub:deploy" : "home");
  //: 채팅 승인 카드에서 넘어온 코드 생성 요청. Build 화면을 열고 CodeAgent 에 넘긴다.
  const [externalTurn, setExternalTurn] = useState<ExternalTurn | null>(null);
  const [diagnostics, setDiagnostics] = useState<DiagnosticsResult | null>(null);
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const [diagnosticsPending, setDiagnosticsPending] = useState(true);
  const [diagnosticsError, setDiagnosticsError] = useState("");

  useMessage(
    useCallback((msg) => {
      const { type, payload } = msg;
      // Core 는 Ready 상태를 "ok" 로 보내고 UI 는 "ready" 로 비교한다 → 정규화로 맞춘다.
      const normDiag = (d: DiagnosticsResult): DiagnosticsResult => {
        const fix = (s?: string) => (s === "ok" ? "ready" : s);
        return {
          ...d,
          core_ready: fix(d.core_ready),
          ai_ready: fix(d.ai_ready),
          docker_ready: fix(d.docker_ready),
          aws_deploy_ready: fix(d.aws_deploy_ready),
          ops_ready: fix(d.ops_ready),
        } as DiagnosticsResult;
      };
      if (type === "stateUpdate") {
        const state = payload as { currentMode?: string; diagnostics?: DiagnosticsResult };
        if (state.diagnostics) { setDiagnostics(normDiag(state.diagnostics)); }
      }
      if (type === "diagnosticsUpdate") {
        setDiagnostics(normDiag(payload as DiagnosticsResult));
        setDiagnosticsPending(false); setDiagnosticsError("");
      }
      if (type === "diagnostics.status") {
        const p = payload as { pending?: boolean; error?: string };
        setDiagnosticsPending(!!p.pending); setDiagnosticsError(p.error ?? "");
      }
      if (type === "diagnostics.error") {
        setDiagnosticsPending(false); setDiagnosticsError((payload as {message?: string})?.message || "연결 확인에 실패했습니다.");
      }
      if (type === "chat.actionAccepted") {
        const p = payload as Partial<ExternalTurn>;
        if (typeof p.requestId === "number" && p.instruction) {
          setExternalTurn({ requestId: p.requestId, instruction: p.instruction, targetFolder: p.targetFolder ?? "", contextFiles: p.contextFiles ?? [] });
          setView("code");
        }
      }
    }, [])
  );

  useEffect(() => {
    postMessage("webview.ready", { layout: isWorkspacePanel ? "workspace" : "sidebar" });
  }, [postMessage]);

  // 사이드바는 retainContextWhenHidden 옵션으로 닫혀도 React가 유지된다.
  // 다시 보이는 순간을 직접 알리면 "큰 창 닫기 → ReCoder 아이콘 클릭"도
  // 항상 Workspace를 다시 여는 동작으로 연결할 수 있다.
  useEffect(() => {
    if (isWorkspacePanel) { return; }
    const notifyVisible = () => {
      if (document.visibilityState === "visible") {
        postMessage("sidebar.visible", {});
      }
    };
    document.addEventListener("visibilitychange", notifyVisible);
    notifyVisible();
    return () => document.removeEventListener("visibilitychange", notifyVisible);
  }, [isWorkspacePanel, postMessage]);

  const isAiReady = diagnostics ? diagnostics.ai_ready === "ready" : false;
  const isDockerReady = diagnostics ? diagnostics.docker_ready === "ready" : false;
  const isOpsReady = diagnostics
    ? diagnostics.ai_ready === "ready" &&
      diagnostics.aws_deploy_ready === "ready" &&
      diagnostics.ops_ready === "ready"
    : false;


  // 개발 대화와 설계·변경 검토는 코드 생성·수정 화면에서 함께 진행한다.
  if (isWorkspacePanel) {
    return <WorkspaceLayout
      view={view}
      externalTurn={externalTurn}
      diagnostics={diagnostics}
      coreStatus={coreHealth?.status ?? null}
      showDiagnostics={showDiagnostics}
      diagnosticsPending={diagnosticsPending}
      diagnosticsError={diagnosticsError}
      isAiReady={isAiReady}
      isDockerReady={isDockerReady}
      isOpsReady={isOpsReady}
      costSummary={costSummary}
      onSelectMode={setView}
      onToggleDiagnostics={() => {
        setShowDiagnostics((v) => !v);

      }}
      postMessage={postMessage}
    />;
  }

  return <div style={{ height: "100vh", background: "var(--vscode-sideBar-background, #1e1e1e)" }}>
    <Hero onOpenWorkspace={() => postMessage("openWorkbench")} />
    <StatusBadge diagnostics={diagnostics} coreStatus={coreHealth?.status ?? null} pending={diagnosticsPending} error={diagnosticsError} expanded={showDiagnostics} onToggle={() => setShowDiagnostics(v => !v)} />
    {showDiagnostics && <DiagnosticsPanel diagnostics={diagnostics} />}
    <p style={{ padding: "12px 16px", fontSize: 12, lineHeight: 1.7, color: "var(--vscode-descriptionForeground, #999)" }}>개발, 배포, 보안 작업을 ReCoder 창에서 이어가세요.</p>
  </div>;
};

export default App;
