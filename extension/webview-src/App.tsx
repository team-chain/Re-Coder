/**
 * ReCoder Webview — Root App (사이드바)
 *
 * 디자인 원칙:
 *   - 로고를 헤더에 두어 전문 서비스 인상 (Kiro 의 유령 같은 브랜드 마크)
 *   - 사용자가 "지금 뭘 할 수 있는지" 카드 하나로 파악 가능
 *   - 이모지 / 유니코드 장식 문자 금지 — 모두 inline SVG
 *
 * 구성:
 *   ┌──────────────────────────┐
 *   │  [Logo] Re-Coder          │  Hero
 *   │  Remember. Return.        │
 *   │  Re-Code.                 │
 *   ├──────────────────────────┤
 *   │  Core / AI / Docker pills │  Status
 *   ├──────────────────────────┤
 *   │  [icon] 에러 분석          │  Action cards (각 카드에 설명)
 *   │  [icon] Dockerfile 생성    │
 *   │  [icon] 배포 + 운영        │
 *   ├──────────────────────────┤
 *   │  [Workbench 열기] (CTA)   │
 *   ├──────────────────────────┤
 *   │  $0.00 / $3.00            │  Cost
 *   └──────────────────────────┘
 */

import React, { useState, useCallback, useEffect } from "react";
import { useVSCodeApi } from "./hooks/useVSCodeApi";
import { usePolling } from "./hooks/usePolling";
import { BuildMode } from "./components/BuildMode";
import { ShipMode } from "./components/ShipMode";
import { OperateMode } from "./components/OperateMode";
import { DiagnosticsPanel } from "./components/DiagnosticsPanel";
import { CostTracker } from "./components/CostTracker";
import { Replay } from "./components/Replay";
import CodeMap from "./components/CodeMap";
import ChatPanel from "./components/ChatPanel";
import DeploymentCenter from "./components/DeploymentCenter";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ViewMode = "home" | "build" | "ship" | "deploy" | "operate" | "replay" | "map";

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
  /** 에러 / 알림 - 삼각형 + 느낌표 */
  Alert: ({ size = 18 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  ),
  /** 코드 / Build - 꺾쇠 */
  Code: ({ size = 18 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="16 18 22 12 16 6" />
      <polyline points="8 6 2 12 8 18" />
    </svg>
  ),
  /** Git / GitHub */
  Git: ({ size = 18 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="6" r="2.2" /><circle cx="6" cy="18" r="2.2" /><circle cx="18" cy="9" r="2.2" />
      <path d="M6 8.2v7.6M18 11.2a6 6 0 0 1-6 6H8.5" />
    </svg>
  ),
  /** 채팅 / Discord */
  Chat: ({ size = 18 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.7a8.5 8.5 0 0 1-.9-3.8 8.38 8.38 0 0 1 8.5-8.5 8.5 8.5 0 0 1 8.5 8.5z" />
    </svg>
  ),
  /** 컨테이너 / Ship - 박스 + 화살표 */
  Container: ({ size = 18 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
      <line x1="12" y1="22.08" x2="12" y2="12" />
    </svg>
  ),
  /** 운영 / 클라우드 - 클라우드 + 게이지 */
  Cloud: ({ size = 18 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z" />
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
  /** Check */
  Check: ({ size = 12 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  ),
  /** Cross */
  Cross: ({ size = 12 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  ),
  /** Back arrow */
  ArrowLeft: ({ size = 14 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="19" y1="12" x2="5" y2="12" />
      <polyline points="12 19 5 12 12 5" />
    </svg>
  ),
  /** Replay — 재생 버튼 */
  Replay: ({ size = 18 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="5 3 19 12 5 21 5 3" />
      <line x1="19" y1="20" x2="19" y2="4" />
    </svg>
  ),
  /** Map — 노드 그래프 (구조 지도) */
  Map: ({ size = 18 }: { size?: number }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="5" r="2.2" /><circle cx="18" cy="9" r="2.2" /><circle cx="7" cy="19" r="2.2" />
      <line x1="7.6" y1="6.6" x2="16.2" y2="7.7" /><line x1="6.4" y1="7.1" x2="6.8" y2="16.8" />
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
}

const StatusBadge: React.FC<StatusBadgeProps> = ({ diagnostics, coreStatus, expanded, onToggle }) => {
  const green = "var(--vscode-charts-green, #3fb950)";
  const amber = "var(--vscode-editorWarning-foreground, #d7a300)";
  const red = "var(--vscode-editorError-foreground, #e5534b)";
  const muted = "var(--vscode-descriptionForeground, #888)";

  const coreOk = coreStatus === "ok";
  const aiOk = diagnostics?.ai_ready === "ready";

  let label: string;
  let color: string;
  if (coreStatus === null) { label = "확인 중"; color = muted; }
  else if (!coreOk) { label = "연결 안 됨"; color = red; }
  else if (aiOk) { label = "준비됨"; color = green; }
  else { label = "설정 필요"; color = amber; }

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
// Action Card (사이드바의 핵심 — 사용자가 한눈에 뭘 할 수 있는지 파악)
// ---------------------------------------------------------------------------

interface ActionCardProps {
  icon: React.ReactNode;
  title: string;
  description: string;
  accent: string; // CSS color
  enabled: boolean;
  disabledReason?: string;
  onClick: () => void;
}

const ActionCard: React.FC<ActionCardProps> = ({ icon, title, description, accent, enabled, disabledReason, onClick }) => {
  const [hover, setHover] = useState(false);

  return (
    <button
      onClick={enabled ? onClick : undefined}
      disabled={!enabled}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      title={!enabled ? disabledReason : undefined}
      style={{
        width: "100%",
        display: "flex",
        alignItems: "flex-start",
        gap: 11,
        padding: "12px 12px",
        background: enabled
          ? hover
            ? "var(--vscode-list-hoverBackground, rgba(255,255,255,0.05))"
            : "var(--vscode-input-background, #252526)"
          : "var(--vscode-input-background, #252526)",
        border: `1px solid ${enabled && hover ? accent : "var(--vscode-panel-border, #333)"}`,
        borderLeftWidth: 3,
        borderLeftColor: accent,
        borderRadius: 6,
        cursor: enabled ? "pointer" : "not-allowed",
        textAlign: "left",
        opacity: enabled ? 1 : 0.55,
        transition: "all 0.12s ease-out",
        outline: "none",
      }}
    >
      <div
        style={{
          width: 32,
          height: 32,
          borderRadius: 6,
          background: `${accent}18`,
          color: accent,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          flexShrink: 0,
        }}
      >
        {icon}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontSize: 13,
          fontWeight: 600,
          color: "var(--vscode-foreground, #e0e0e0)",
          marginBottom: 2,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}>
          <span>{title}</span>
          {enabled && (
            <span style={{ color: "var(--vscode-descriptionForeground, #888)", opacity: hover ? 1 : 0.45, transition: "opacity 0.12s" }}>
              <Icon.ChevronRight size={14} />
            </span>
          )}
        </div>
        <div style={{
          fontSize: 11,
          color: "var(--vscode-descriptionForeground, #999)",
          lineHeight: 1.45,
        }}>
          {description}
        </div>
      </div>
    </button>
  );
};

// ---------------------------------------------------------------------------
// Home (3개 카드)
// ---------------------------------------------------------------------------

interface HomeProps {
  isAiReady: boolean;
  isDockerReady: boolean;
  isOpsReady: boolean;
  onSelectMode: (mode: ViewMode) => void;
  postMessage: (type: string, payload?: unknown) => void;
  awsReady: boolean;
  githubReady: boolean;
  showMap?: boolean;
}

const Home: React.FC<HomeProps> = ({ isAiReady, isDockerReady, isOpsReady, onSelectMode, postMessage, awsReady, githubReady, showMap = false }) => {
  const accent = "var(--vscode-textLink-foreground, #4a9eff)";
  const green = "var(--vscode-charts-green, #3fb950)";
  const muted = "var(--vscode-descriptionForeground, #888)";
  const fg = "var(--vscode-foreground, #e0e0e0)";

  const sectionLabel: React.CSSProperties = {
    fontSize: 11, fontWeight: 400, color: muted, marginBottom: 6, marginLeft: 2,
  };

  const StepRow: React.FC<{
    icon: React.ReactNode; label: string;
    enabled: boolean; hint?: string; onClick: () => void; last?: boolean;
  }> = ({ icon, label, enabled, hint, onClick, last }) => (
    <button
      onClick={enabled ? onClick : undefined}
      disabled={!enabled}
      style={{
        width: "100%", display: "flex", alignItems: "center", gap: 12,
        padding: "11px 2px", background: "none", border: "none",
        borderBottom: last ? "none" : "0.5px solid var(--vscode-panel-border, #2a2a2a)",
        textAlign: "left", cursor: enabled ? "pointer" : "default",
        opacity: enabled ? 1 : 0.55,
      }}
    >
      <span style={{ color: enabled ? fg : muted, display: "inline-flex", flexShrink: 0 }}>{icon}</span>
      <span style={{ flex: 1, minWidth: 0, fontSize: 13.5, color: enabled ? fg : muted }}>{label}</span>
      {enabled
        ? <span style={{ color: muted, display: "inline-flex", flexShrink: 0 }}><Icon.ChevronRight size={15} /></span>
        : <span style={{ fontSize: 11, color: muted, flexShrink: 0 }}>{hint}</span>}
    </button>
  );

  const ConnRow: React.FC<{
    icon: React.ReactNode; label: string; connected: boolean; actionLabel?: string; onConnect: () => void; last?: boolean;
  }> = ({ icon, label, connected, actionLabel, onConnect, last }) => (
    <button
      onClick={onConnect}
      style={{
        width: "100%", display: "flex", alignItems: "center", gap: 12,
        padding: "11px 2px", background: "none", border: "none",
        borderBottom: last ? "none" : "0.5px solid var(--vscode-panel-border, #2a2a2a)",
        textAlign: "left", cursor: "pointer",
      }}
    >
      <span style={{ color: fg, display: "inline-flex", flexShrink: 0 }}>{icon}</span>
      <span style={{ flex: 1, minWidth: 0, fontSize: 13.5, color: fg }}>{label}</span>
      <span style={{ fontSize: 12, color: connected ? muted : accent, flexShrink: 0 }}>
        {connected ? "연결됨" : (actionLabel ?? "연결")}
      </span>
    </button>
  );

  return (
    <div style={{ padding: "14px 12px 10px", display: "flex", flexDirection: "column", gap: 16 }}>

      {/* 구조 지도 — 홈에서 바로 표시 (정적 분석이라 AI 없이 동작) */}
      {showMap ? (
        <div>
          <div style={sectionLabel}>구조 지도</div>
          <CodeMap isActive={true} />
        </div>
      ) : (
        <div>
          <div style={sectionLabel}>구조 지도</div>
          <StepRow icon={<Icon.Map size={19} />} label="전체 아키텍처 보기" enabled onClick={() => onSelectMode("map")} last />
        </div>
      )}

      {/* 워크플로 */}
      <div>
        <div style={sectionLabel}>워크플로</div>
        <StepRow
          icon={<Icon.Code size={19} />}
          label="Build"
          enabled={isAiReady} hint="AI 필요"
          onClick={() => onSelectMode("build")}
        />
        <StepRow
          icon={<Icon.Container size={19} />}
          label="Deploy"
          enabled={isAiReady} hint="AI 필요"
          onClick={() => onSelectMode("ship")}
        />
        <StepRow
          icon={<Icon.Dashboard size={19} />}
          label="배포 센터"
          enabled={true}
          onClick={() => onSelectMode("deploy")}
        />
        <StepRow
          icon={<Icon.Cloud size={19} />}
          label="Operate"
          enabled={isOpsReady} hint="대기"
          onClick={() => onSelectMode("operate")}
          last
        />
      </div>

      {/* 연결 */}
      <div>
        <div style={sectionLabel}>연결</div>
        <ConnRow icon={<Icon.Git size={19} />} label="GitHub" connected={githubReady}
          onConnect={() => postMessage("webview.diagnostics.fix", { key: "github_ready" })} />
        <ConnRow icon={<Icon.Cloud size={19} />} label="AWS" connected={awsReady}
          onConnect={() => postMessage("webview.diagnostics.fix", { key: "aws_deploy_ready" })} />
        <ConnRow icon={<Icon.Chat size={19} />} label="Discord" connected={false} actionLabel="봇 초대"
          onConnect={() => postMessage("workbench.open", {})} last />
      </div>

      {/* Deploy Replay (보조) */}
      <button
        onClick={() => onSelectMode("replay")}
        style={{
          width: "100%", display: "flex", alignItems: "center", gap: 8,
          padding: "9px 2px", background: "none", border: "none",
          borderTop: "1px solid var(--vscode-panel-border, #2a2a2a)",
          color: muted, fontSize: 12, cursor: "pointer", textAlign: "left",
        }}
      >
        <Icon.Replay size={16} />
        <span style={{ flex: 1 }}>Deploy Replay</span>
        <Icon.ChevronRight size={14} />
      </button>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Sub-page header (뒤로가기)
// ---------------------------------------------------------------------------

const SubHeader: React.FC<{ title: string; onBack: () => void }> = ({ title, onBack }) => (
  <div style={{
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "10px 12px",
    background: "var(--vscode-sideBar-background, #1e1e1e)",
    borderBottom: "1px solid var(--vscode-panel-border, #2a2a2a)",
  }}>
    <button
      onClick={onBack}
      style={{
        background: "none",
        border: "none",
        color: "var(--vscode-foreground, #ccc)",
        cursor: "pointer",
        display: "flex",
        alignItems: "center",
        gap: 4,
        fontSize: 12,
        padding: "3px 6px",
        borderRadius: 4,
      }}
      onMouseEnter={(e) => { e.currentTarget.style.background = "var(--vscode-list-hoverBackground, rgba(255,255,255,0.05))"; }}
      onMouseLeave={(e) => { e.currentTarget.style.background = "none"; }}
    >
      <Icon.ArrowLeft size={14} />
      뒤로
    </button>
    <span style={{ fontSize: 12, fontWeight: 600, color: "var(--vscode-foreground, #e0e0e0)" }}>
      {title}
    </span>
  </div>
);

// ---------------------------------------------------------------------------
// ReCoder 작업 화면 — 왼쪽 작업 영역 + 오른쪽 고정 AI 대화
// ---------------------------------------------------------------------------

interface WorkspaceLayoutProps {
  view: ViewMode;
  diagnostics: DiagnosticsResult | null;
  coreStatus: "ok" | "degraded" | "down" | null;
  showDiagnostics: boolean;
  isAiReady: boolean;
  isDockerReady: boolean;
  isOpsReady: boolean;
  costSummary: ReturnType<typeof usePolling>["costSummary"];
  onSelectMode: (mode: ViewMode) => void;
  onToggleDiagnostics: () => void;
  postMessage: (type: string, payload?: unknown) => void;
}

//: 테스트에서 직접 렌더할 수 있도록 export 한다. 이 레이아웃에서 설계 결정
//: 경로(CodeAgent)가 살아있는지가 회귀 대상이다 — 예전에 여기서만 숨겨져서
//: Workspace 창에서 결정 카드가 뜨지 않는 버그가 있었다.
export const WorkspaceLayout: React.FC<WorkspaceLayoutProps> = ({
  view, diagnostics, coreStatus, showDiagnostics, isAiReady, isDockerReady, isOpsReady,
  costSummary, onSelectMode, onToggleDiagnostics, postMessage,
}) => {
  const subTitle = view === "build" ? "에러 분석" : view === "ship" ? "로컬 Docker 배포" : view === "deploy" ? "배포 센터" : view === "operate" ? "운영 대응" : view === "replay" ? "Deploy Replay" : view === "map" ? "구조 지도" : "";

  return (
    <div style={{ height: "100vh", display: "grid", gridTemplateColumns: "minmax(0, 1.65fr) minmax(330px, .85fr)", overflow: "hidden", background: "var(--vscode-editor-background, #1e1e1e)", color: "var(--vscode-foreground, #e0e0e0)" }}>
      <section style={{ minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column", borderRight: "1px solid var(--vscode-panel-border, #333)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "12px 18px", borderBottom: "1px solid var(--vscode-panel-border, #333)", background: "var(--vscode-sideBar-background, #181818)" }}>
          <Icon.Logo size={22} />
          <strong style={{ fontSize: 16 }}>ReCoder</strong>
          <span style={{ color: "var(--vscode-descriptionForeground, #8b8b8b)", fontSize: 12 }}>Workspace</span>
          <button onClick={() => onSelectMode("map")} style={{ marginLeft: "auto", border: "1px solid var(--vscode-button-border, transparent)", borderRadius: 4, padding: "4px 9px", background: "transparent", color: "var(--vscode-textLink-foreground, #4a9eff)", cursor: "pointer", fontSize: 11 }}>구조 지도</button>
          <button onClick={() => onSelectMode("deploy")} style={{ border: "1px solid var(--vscode-button-border, transparent)", borderRadius: 4, padding: "4px 9px", background: "transparent", color: "var(--vscode-textLink-foreground, #4a9eff)", cursor: "pointer", fontSize: 11 }}>배포 센터</button>
          <button onClick={() => onSelectMode("home")} style={{ border: "1px solid var(--vscode-button-border, transparent)", borderRadius: 4, padding: "4px 9px", background: "var(--vscode-button-secondaryBackground, #3a3d41)", color: "var(--vscode-button-secondaryForeground, #fff)", cursor: "pointer", fontSize: 11 }}>홈</button>
        </div>

        <StatusBadge diagnostics={diagnostics} coreStatus={coreStatus} expanded={showDiagnostics} onToggle={onToggleDiagnostics} />
        {showDiagnostics && <div style={{ borderBottom: "1px solid var(--vscode-panel-border, #333)", maxHeight: 220, overflowY: "auto" }}><DiagnosticsPanel diagnostics={diagnostics} /></div>}

        <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: view === "home" ? "0 14px 16px" : "14px 18px 18px" }}>
          {view === "home" && <Home isAiReady={isAiReady} isDockerReady={isDockerReady} isOpsReady={isOpsReady} onSelectMode={onSelectMode} postMessage={postMessage} awsReady={diagnostics?.aws_deploy_ready === "ready"} githubReady={(diagnostics as unknown as { github_ready?: string })?.github_ready === "ready"} showMap={false} />}
          {view === "build" && <><SubHeader title={subTitle} onBack={() => onSelectMode("home")} /><BuildMode isActive={isAiReady} /></>}
          {view === "ship" && <><SubHeader title={subTitle} onBack={() => onSelectMode("home")} /><ShipMode isAiReady={isAiReady} isDockerReady={isDockerReady} /></>}
          {view === "deploy" && <><SubHeader title={subTitle} onBack={() => onSelectMode("home")} /><DeploymentCenter onOpenDocker={() => onSelectMode("ship")} /></>}
          {view === "operate" && <><SubHeader title={subTitle} onBack={() => onSelectMode("home")} /><OperateMode isActive={isOpsReady} /></>}
          {view === "replay" && <><SubHeader title={subTitle} onBack={() => onSelectMode("home")} /><Replay /></>}
          {view === "map" && <><SubHeader title={subTitle} onBack={() => onSelectMode("home")} /><CodeMap isActive /></>}
        </div>
        <div style={{ borderTop: "1px solid var(--vscode-panel-border, #333)", padding: "4px 14px", background: "var(--vscode-sideBar-background, #181818)" }}><CostTracker costSummary={costSummary} /></div>
      </section>

      <aside style={{ minWidth: 0, minHeight: 0, height: "100%", display: "flex", flexDirection: "column", overflow: "hidden", background: "var(--vscode-sideBar-background, #1e1e1e)" }}>
        <div style={{ padding: "14px 16px 12px", borderBottom: "1px solid var(--vscode-panel-border, #333)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}><Icon.Chat size={19} /><strong style={{ fontSize: 15 }}>AI와 대화</strong><span style={{ marginLeft: "auto", color: "var(--vscode-charts-green, #4ade80)", fontSize: 11 }}>AI-DLC</span></div>
          <div style={{ marginTop: 5, color: "var(--vscode-descriptionForeground, #999)", fontSize: 11 }}>요청을 입력하면 설계 결정부터 코드 적용까지 함께 진행합니다.</div>
        </div>
        <div style={{ flex: 1, minHeight: 0, display: "flex", overflow: "hidden" }}>
          <ChatPanel isAiReady={isAiReady} />
        </div>
      </aside>
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

  const [view, setView] = useState<ViewMode>("home");
  const [diagnostics, setDiagnostics] = useState<DiagnosticsResult | null>(null);
  const [showDiagnostics, setShowDiagnostics] = useState(false);

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
        if (state.diagnostics) setDiagnostics(normDiag(state.diagnostics));
      }
      if (type === "diagnosticsUpdate") {
        setDiagnostics(normDiag(payload as DiagnosticsResult));
      }
    }, [])
  );

  useEffect(() => {
    postMessage("runDiagnostics", {});
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

  const subTitle = view === "build" ? "에러 분석" : view === "ship" ? "로컬 Docker 배포" : view === "deploy" ? "배포 센터" : view === "operate" ? "운영 대응" : view === "replay" ? "Deploy Replay" : view === "map" ? "구조 지도" : "";

  // WebviewPanel 로 열리는 ReCoder 작업 화면에서는 현재 사이드바 기능을
  // 왼쪽에, 대화형 코드 에이전트를 오른쪽에 동시에 표시한다.
  if (isWorkspacePanel) {
    return <WorkspaceLayout
      view={view}
      diagnostics={diagnostics}
      coreStatus={coreHealth?.status ?? null}
      showDiagnostics={showDiagnostics}
      isAiReady={isAiReady}
      isDockerReady={isDockerReady}
      isOpsReady={isOpsReady}
      costSummary={costSummary}
      onSelectMode={setView}
      onToggleDiagnostics={() => {
        setShowDiagnostics((v) => !v);
        if (!showDiagnostics) postMessage("runDiagnostics", {});
      }}
      postMessage={postMessage}
    />;
  }

  return (
    <div style={{
      display: "flex",
      flexDirection: "column",
      height: "100vh",
      overflow: "hidden",
      background: "var(--vscode-sideBar-background, #1e1e1e)",
      color: "var(--vscode-foreground, #e0e0e0)",
      fontFamily: "var(--vscode-font-family)",
    }}>
      {/* Hero (로고 + 브랜드) */}
      {view === "home" && <Hero onOpenWorkspace={() => postMessage("workbench.open", {})} />}
      {view !== "home" && <SubHeader title={subTitle} onBack={() => setView("home")} />}

      {/* Status badge (펼치면 진단 상세) */}
      <StatusBadge
        diagnostics={diagnostics}
        coreStatus={coreHealth?.status ?? null}
        expanded={showDiagnostics}
        onToggle={() => {
          setShowDiagnostics((v) => !v);
          if (!showDiagnostics) postMessage("runDiagnostics", {});
        }}
      />

      {showDiagnostics && (
        <div style={{
          borderBottom: "1px solid var(--vscode-panel-border, #333)",
          maxHeight: 220,
          overflowY: "auto",
        }}>
          <DiagnosticsPanel diagnostics={diagnostics} />
        </div>
      )}

      {/* Main content */}
      <div style={{ flex: 1, overflowY: "auto" }}>
        {view === "home" && (
          <Home
            isAiReady={isAiReady}
            isDockerReady={isDockerReady}
            isOpsReady={isOpsReady}
            onSelectMode={setView}
            postMessage={postMessage}
            awsReady={diagnostics?.aws_deploy_ready === "ready"}
            githubReady={(diagnostics as unknown as { github_ready?: string })?.github_ready === "ready"}
          />
        )}
        {view === "build" && (
          <div style={{ padding: "10px 10px 8px" }}>
            <BuildMode isActive={isAiReady} />
          </div>
        )}
        {view === "ship" && (
          <div style={{ padding: "10px 10px 8px" }}>
            <ShipMode isAiReady={isAiReady} isDockerReady={isDockerReady} />
          </div>
        )}
        {view === "deploy" && (
          <div style={{ padding: "10px 10px 8px" }}>
            <DeploymentCenter onOpenDocker={() => setView("ship")} />
          </div>
        )}
        {view === "operate" && (
          <div style={{ padding: "10px 10px 8px" }}>
            <OperateMode isActive={isOpsReady} />
          </div>
        )}
        {view === "replay" && (
          <div style={{ padding: "10px 10px 8px" }}>
            <Replay />
          </div>
        )}
        {view === "map" && (
          <div style={{ padding: "10px 10px 8px" }}>
            <CodeMap isActive={view === "map"} />
          </div>
        )}
      </div>

      {/* Workbench CTA */}
      <div style={{
        padding: "10px 12px 8px",
        background: "var(--vscode-sideBar-background, #1e1e1e)",
        borderTop: "1px solid var(--vscode-panel-border, #2a2a2a)",
      }}>
        <button
          onClick={() => postMessage("workbench.open", {})}
          style={{
            width: "100%",
            padding: "10px 12px",
            border: "none",
            borderRadius: 6,
            background: "var(--vscode-button-background, #0e639c)",
            color: "var(--vscode-button-foreground, #ffffff)",
            fontSize: 12.5,
            fontWeight: 600,
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 8,
            transition: "background-color 0.15s",
          }}
          onMouseEnter={(e) => { e.currentTarget.style.background = "var(--vscode-button-hoverBackground, #1177bb)"; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "var(--vscode-button-background, #0e639c)"; }}
          title="Editor Area에 ReCoder Workbench 풀스크린 탭을 엽니다"
        >
          <Icon.Dashboard size={14} />
          Workbench 열기
        </button>
      </div>

      {/* Cost tracker */}
      <div style={{
        borderTop: "1px solid var(--vscode-panel-border, #2a2a2a)",
        padding: "3px 12px",
        background: "var(--vscode-sideBar-background, #1e1e1e)",
      }}>
        <CostTracker costSummary={costSummary} />
      </div>
    </div>
  );
};

export default App;
