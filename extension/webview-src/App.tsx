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

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ViewMode = "home" | "build" | "ship" | "operate";

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
};

// ---------------------------------------------------------------------------
// Hero (로고 + 태그라인)
// ---------------------------------------------------------------------------

const Hero: React.FC = () => (
  <div style={{
    padding: "16px 14px 12px",
    background: "var(--vscode-sideBar-background, #1e1e1e)",
    borderBottom: "1px solid var(--vscode-panel-border, #2a2a2a)",
  }}>
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <div style={{ color: "var(--vscode-textLink-foreground, #4a9eff)" }}>
        <Icon.Logo size={32} />
      </div>
      <div>
        <div style={{
          fontSize: 16,
          fontWeight: 700,
          color: "var(--vscode-foreground, #e0e0e0)",
          letterSpacing: "-0.01em",
          lineHeight: 1.1,
        }}>
          Re-Coder
        </div>
        <div style={{
          fontSize: 10,
          fontWeight: 500,
          color: "var(--vscode-descriptionForeground, #888)",
          letterSpacing: "0.04em",
          marginTop: 2,
        }}>
          Remember. Return. Re-Code.
        </div>
      </div>
    </div>
  </div>
);

// ---------------------------------------------------------------------------
// Status Pills (이모지/✓✗ 대신 inline SVG 사용)
// ---------------------------------------------------------------------------

interface StatusPillsProps {
  diagnostics: DiagnosticsResult | null;
  coreStatus: "ok" | "degraded" | "down" | null;
}

const StatusPills: React.FC<StatusPillsProps> = ({ diagnostics, coreStatus }) => {
  const green = "#22c55e";
  const red = "#ef4444";
  const gray = "#6b7280";

  const coreOk = coreStatus === "ok";
  const aiOk = diagnostics?.ai_ready === "ready";
  const dockerOk = diagnostics?.docker_ready === "ready";

  const pillStyle = (state: "ok" | "fail" | "pending"): React.CSSProperties => ({
    display: "inline-flex",
    alignItems: "center",
    gap: 5,
    padding: "3px 9px",
    borderRadius: 999,
    border: `1px solid ${state === "ok" ? green : state === "fail" ? red : gray}40`,
    background: state === "ok" ? `${green}15` : state === "fail" ? `${red}15` : `${gray}15`,
    color: state === "ok" ? green : state === "fail" ? red : gray,
    fontSize: 11,
    fontWeight: 600,
    cursor: "default",
    userSelect: "none",
  });

  const renderPill = (label: string, isOk: boolean | null) => {
    const state: "ok" | "fail" | "pending" = isOk === null ? "pending" : isOk ? "ok" : "fail";
    return (
      <span style={pillStyle(state)}>
        {state === "ok" ? <Icon.Check size={10} /> : state === "fail" ? <Icon.Cross size={10} /> : <span style={{ width: 10, height: 10, display: "inline-block" }} />}
        {label}
      </span>
    );
  };

  return (
    <div style={{
      display: "flex",
      gap: 6,
      padding: "10px 14px",
      background: "var(--vscode-sideBar-background, #1e1e1e)",
      flexWrap: "wrap",
    }}>
      {renderPill("Core", coreOk)}
      {renderPill("AI", aiOk ?? null)}
      {renderPill("Docker", dockerOk ?? null)}
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
}

const Home: React.FC<HomeProps> = ({ isAiReady, isDockerReady, isOpsReady, onSelectMode }) => (
  <div style={{ padding: "14px 12px 10px", display: "flex", flexDirection: "column", gap: 9 }}>
    <div style={{
      fontSize: 11,
      fontWeight: 600,
      letterSpacing: "0.08em",
      textTransform: "uppercase",
      color: "var(--vscode-descriptionForeground, #888)",
      marginBottom: 2,
    }}>
      무엇을 도와드릴까요?
    </div>

    <ActionCard
      icon={<Icon.Alert size={18} />}
      title="에러 분석"
      description="터미널 에러를 자동 분석하고 코드 수정안을 제안합니다."
      accent="#ef4444"
      enabled={isAiReady}
      disabledReason="AI 설정 필요 — 시스템 진단에서 확인하세요"
      onClick={() => onSelectMode("build")}
    />

    <ActionCard
      icon={<Icon.Container size={18} />}
      title="Dockerfile · 배포"
      description="스택을 감지해 Dockerfile을 만들고 docker build/run · Health Check 수행."
      accent="#3b82f6"
      enabled={isAiReady}
      disabledReason={!isAiReady ? "AI 설정 필요" : !isDockerReady ? "Docker 미감지 — 생성만 가능" : undefined}
      onClick={() => onSelectMode("ship")}
    />

    <ActionCard
      icon={<Icon.Cloud size={18} />}
      title="운영 대응"
      description="EC2 incident 조회 → AI 분석 → 승인 기반 원격 명령 실행."
      accent="#22c55e"
      enabled={isOpsReady}
      disabledReason="2학기 — AWS Deploy + Ops 설정 필요"
      onClick={() => onSelectMode("operate")}
    />
  </div>
);

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
// App
// ---------------------------------------------------------------------------

const App: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();
  const { coreHealth, costSummary } = usePolling(4000);

  const [view, setView] = useState<ViewMode>("home");
  const [diagnostics, setDiagnostics] = useState<DiagnosticsResult | null>(null);
  const [showDiagnostics, setShowDiagnostics] = useState(false);

  useMessage(
    useCallback((msg) => {
      const { type, payload } = msg;
      if (type === "stateUpdate") {
        const state = payload as { currentMode?: string; diagnostics?: DiagnosticsResult };
        if (state.diagnostics) setDiagnostics(state.diagnostics);
      }
      if (type === "diagnosticsUpdate") {
        setDiagnostics(payload as DiagnosticsResult);
      }
    }, [])
  );

  useEffect(() => {
    postMessage("runDiagnostics", {});
  }, [postMessage]);

  const isAiReady = diagnostics ? diagnostics.ai_ready === "ready" : false;
  const isDockerReady = diagnostics ? diagnostics.docker_ready === "ready" : false;
  const isOpsReady = diagnostics
    ? diagnostics.ai_ready === "ready" &&
      diagnostics.aws_deploy_ready === "ready" &&
      diagnostics.ops_ready === "ready"
    : false;

  const subTitle = view === "build" ? "에러 분석" : view === "ship" ? "Dockerfile · 배포" : view === "operate" ? "운영 대응" : "";

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
      {view === "home" && <Hero />}
      {view !== "home" && <SubHeader title={subTitle} onBack={() => setView("home")} />}

      {/* Status pills */}
      <StatusPills diagnostics={diagnostics} coreStatus={coreHealth?.status ?? null} />

      {/* 진단 토글 */}
      <div style={{ display: "flex", justifyContent: "flex-end", padding: "0 12px 4px" }}>
        <button
          onClick={() => {
            setShowDiagnostics((v) => !v);
            if (!showDiagnostics) postMessage("runDiagnostics", {});
          }}
          style={{
            background: "none",
            border: "none",
            color: "var(--vscode-textLink-foreground, #4a9eff)",
            fontSize: 10,
            cursor: "pointer",
            padding: "2px 0",
            opacity: 0.7,
          }}
        >
          {showDiagnostics ? "진단 숨기기" : "시스템 진단"}
        </button>
      </div>

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
        {view === "operate" && (
          <div style={{ padding: "10px 10px 8px" }}>
            <OperateMode isActive={isOpsReady} />
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
        <div style={{
          fontSize: 10,
          color: "var(--vscode-descriptionForeground, #888)",
          textAlign: "center",
          marginTop: 5,
        }}>
          넓은 대시보드 · 4탭 · 실시간 로그
        </div>
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
