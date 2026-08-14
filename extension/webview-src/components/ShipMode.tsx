/**
 * ReCoder — ShipMode Component
 *
 * Stage 2 Ship: Dockerfile 생성 → Trivy/Hadolint 스캔 → docker build → docker run → Health Check
 *
 * Approval Level:
 *   - Dockerfile 저장: Level 1 (단순 승인)
 *   - docker build/run: Level 2 (명령 미리보기 + 승인)
 */

import React, { useState, useCallback } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import ApprovalModal from "./ApprovalModal";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface InfraFileProposal {
  proposal_id: string;
  file_type: string;
  target_path: string;
  content: string;
  base_template?: string;
  required_secrets: string[];
  risk_level: "low" | "medium" | "high" | "critical";
  risk_reasons: string[];
  approval_level: 1 | 2 | 3 | 4;
}

interface ScanResult {
  scan_type: string;
  exit_code: number;
  findings: unknown;
  stderr?: string;
}

interface DeploymentPlan {
  plan_id: string;
  method: string;
  action: string;
  image?: string;
  container_name?: string;
  ports: Record<string, string>;
  health_check_path: string;
  risk_level: "low" | "medium" | "high" | "critical";
  risk_reasons: string[];
  approval_level: 1 | 2 | 3 | 4;
}

export type InfraFileTab = "dockerfile" | "compose" | "actions";

export const INFRA_FILE_TYPE_BY_TAB: Record<InfraFileTab, string> = {
  dockerfile: "dockerfile",
  compose: "docker_compose",
  actions: "github_actions",
};

export function generationCommandForTab(
  tab: InfraFileTab,
): "generateDockerfile" | "generateCompose" | "generateGithubActions" {
  if (tab === "compose") { return "generateCompose"; }
  if (tab === "actions") { return "generateGithubActions"; }
  return "generateDockerfile";
}

export function infraFileLabelForTab(tab: InfraFileTab): string {
  if (tab === "compose") { return "docker-compose.yml"; }
  if (tab === "actions") { return ".github/workflows/deploy.yml"; }
  return "Dockerfile";
}

export function shouldRunDockerPipeline(fileType: string): boolean {
  return fileType === INFRA_FILE_TYPE_BY_TAB.dockerfile;
}

export type Step =
  | "idle"
  | "generating"
  | "preview"
  | "saving"
  | "saved"
  | "scanning"
  | "scanDone"
  | "planning"
  | "planReady"
  | "deploying"
  | "healthCheck"
  | "done"
  | "error";

const SECURITY_SCAN_BLOCKING_STEPS: ReadonlySet<Step> = new Set([
  "generating",
  "saving",
  "scanning",
  "planning",
  "planReady",
  "deploying",
  "healthCheck",
]);

export function canRunSecurityScan(step: Step): boolean {
  return !SECURITY_SCAN_BLOCKING_STEPS.has(step);
}

// ---------------------------------------------------------------------------
// ShipMode
// ---------------------------------------------------------------------------

interface ShipModeProps {
  isAiReady: boolean;
  isDockerReady: boolean;
}

export const ShipMode: React.FC<ShipModeProps> = ({ isAiReady, isDockerReady }) => {
  const { postMessage, useMessage } = useVSCodeApi();

  const [step, setStep] = useState<Step>("idle");
  const [proposal, setProposal] = useState<InfraFileProposal | null>(null);
  const [scanResult, setScanResult] = useState<ScanResult | null>(null);
  const [plan, setPlan] = useState<DeploymentPlan | null>(null);
  const [deployResult, setDeployResult] = useState<{ status: string; deployment_id?: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showApproval, setShowApproval] = useState(false);
  const [approvalContext, setApprovalContext] = useState<"infra" | "deploy" | null>(null);
  const [activeFileTab, setActiveFileTab] = useState<InfraFileTab>("dockerfile");

  // Message listener
  useMessage(
    useCallback((msg) => {
      const { type, payload } = msg;

      if (type === "proposalReady" && (payload as { file_type?: string }).file_type) {
        setProposal(payload as InfraFileProposal);
        setStep("preview");
        setError(null);
      }

      if (type === "scanResult") {
        setScanResult(payload as ScanResult);
        setStep("scanDone");
      }

      if (type === "infraApprovalResult") {
        const result = payload as {
          status?: string;
          approved?: boolean;
          file_type?: string;
        };
        // 탭 전환 중 보낸 거절 응답은 새 탭의 상태를 바꾸면 안 된다.
        if (result.approved === true && result.status === "saved") {
          if (shouldRunDockerPipeline(result.file_type ?? "")) {
            setStep("scanning");
            postMessage("runScan", { scanType: "trivy", workspacePath: "" });
          } else {
            setStep("saved");
          }
        }
      }

      if (type === "deployResult") {
        const r = payload as { status: string; deployment_id?: string };
        setDeployResult(r);
        setStep(r.status === "success" ? "done" : "error");
        if (r.status !== "success") {
          setError("배포 실패. stderr를 확인하세요.");
        }
      }

      if (type === "errorMessage") {
        setError((payload as { message: string }).message);
        setStep("error");
      }
    }, [postMessage])
  );

  // ── Handlers ─────────────────────────────────────────────────────────────

  //: 「인프라 파일 생성」의 **현재 탭에 맞는** 파일을 만든다.
  //:
  //: 예전에는 어느 탭에 있든 Dockerfile 만 생성했다. Compose 탭은 대응
  //: 엔드포인트조차 없어서(404) 아무것도 못 했고, GitHub Actions 탭도 여기서는
  //: 호출되지 않았다. 탭이 셋인데 동작은 하나뿐이라 사용자는 "탭을 골랐는데
  //: 왜 Dockerfile 이 나오지" 상태가 됐다.
  const handleGenerateInfraFile = useCallback(() => {
    setStep("generating");
    setError(null);
    setProposal(null);
    setScanResult(null);
    setPlan(null);
    setDeployResult(null);
    const command = generationCommandForTab(activeFileTab);
    postMessage(command, { workspacePath: "" /* extension fills */ });
  }, [postMessage, activeFileTab]);

  const handleApproveInfraFile = useCallback(() => {
    if (!proposal) { return; }
    postMessage("approveDockerfile", { proposalId: proposal.proposal_id, approved: true });
    setStep("saving");
    setShowApproval(false);
  }, [proposal, postMessage]);

  const handleRejectDockerfile = useCallback(() => {
    if (!proposal) { return; }
    postMessage("approveDockerfile", { proposalId: proposal.proposal_id, approved: false });
    setStep("idle");
    setProposal(null);
    setShowApproval(false);
  }, [proposal, postMessage]);

  const handleSwitchFileTab = useCallback((tab: InfraFileTab) => {
    if (tab === activeFileTab) { return; }

    // 승인 대기 초안은 서버 메모리에도 보관된다. 탭을 바꾸며 UI에서만
    // 숨기면 나중에 다른 파일로 오인해 승인하거나 서버에 고아로 남는다.
    if (step === "preview" && proposal) {
      postMessage("approveDockerfile", {
        proposalId: proposal.proposal_id,
        approved: false,
      });
    }

    setActiveFileTab(tab);
    setProposal(null);
    setScanResult(null);
    setPlan(null);
    setDeployResult(null);
    setError(null);
    setShowApproval(false);
    setApprovalContext(null);
    setStep("idle");
  }, [activeFileTab, step, proposal, postMessage]);

  const handleDeploy = useCallback(() => {
    if (!plan) { return; }
    postMessage("executeDeployment", { planId: plan.plan_id, approved: true });
    setStep("deploying");
    setShowApproval(false);
  }, [plan, postMessage]);

  const handleCreatePlan = useCallback(() => {
    setStep("planning");
    postMessage("createDeployPlan", {
      workspacePath: "",
      method: "local_docker",
    });
  }, [postMessage]);

  // ── Styles ────────────────────────────────────────────────────────────────

  const sectionHeader: React.CSSProperties = {
    fontSize: 10,
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    color: "var(--vscode-descriptionForeground, #888)",
    marginBottom: 6,
    marginTop: 12,
  };

  const btnPrimary: React.CSSProperties = {
    background: "var(--vscode-button-background, #0078d4)",
    color: "var(--vscode-button-foreground, #fff)",
    border: "none",
    borderRadius: 4,
    padding: "6px 14px",
    fontSize: 12,
    cursor: "pointer",
    fontWeight: 600,
  };

  const btnSecondary: React.CSSProperties = {
    background: "var(--vscode-button-secondaryBackground, #3a3a3a)",
    color: "var(--vscode-button-secondaryForeground, #ccc)",
    border: "none",
    borderRadius: 4,
    padding: "6px 14px",
    fontSize: 12,
    cursor: "pointer",
  };

  const codeBlock: React.CSSProperties = {
    background: "var(--vscode-textCodeBlock-background, #1e1e1e)",
    border: "1px solid var(--vscode-panel-border, #333)",
    borderRadius: 4,
    padding: "8px 10px",
    fontFamily: "var(--vscode-editor-font-family, monospace)",
    fontSize: 11,
    overflowX: "auto",
    maxHeight: 250,
    overflowY: "auto",
    whiteSpace: "pre",
  };

  const riskColors: Record<string, string> = {
    low: "var(--vscode-testing-iconPassed, #4caf50)",
    medium: "var(--vscode-editorWarning-foreground, #ff9800)",
    high: "var(--vscode-editorError-foreground, #f44336)",
    critical: "#b71c1c",
  };

  // ── Render helpers ────────────────────────────────────────────────────────

  const renderScanSummary = (result: ScanResult) => {
    const findings = result.findings as {
      Results?: { Vulnerabilities?: unknown[] }[];
      finding_count?: number;
    };
    let criticalCount = 0;
    let highCount = 0;

    if (findings?.Results) {
      for (const r of findings.Results) {
        for (const v of (r.Vulnerabilities ?? []) as { Severity: string }[]) {
          if (v.Severity === "CRITICAL") { criticalCount++; }
          if (v.Severity === "HIGH") { highCount++; }
        }
      }
    }

    const severity = criticalCount > 0 ? "critical" : highCount > 0 ? "high" : "low";
    const label =
      criticalCount > 0
        ? `Critical: ${criticalCount}, High: ${highCount}`
        : highCount > 0
        ? `High: ${highCount}`
        : "취약점 없음 ✓";

    return (
      <div
        style={{
          padding: "6px 10px",
          borderRadius: 4,
          border: `1px solid ${riskColors[severity]}`,
          color: riskColors[severity],
          fontSize: 11,
          marginBottom: 8,
        }}
      >
        <strong>Trivy 스캔:</strong> {label}
      </div>
    );
  };

  if (!isAiReady) {
    return (
      <div style={{ padding: 16, color: "var(--vscode-descriptionForeground)", textAlign: "center", fontSize: 12 }}>
        Ship Mode는 AI Ready가 필요합니다.{" "}
        <span style={{ color: "#4a9eff" }}>진단 탭에서 확인</span>
      </div>
    );
  }

  const fileTabs: { id: InfraFileTab; label: string }[] = [
    { id: "dockerfile", label: "Dockerfile" },
    { id: "compose", label: "Compose" },
    { id: "actions", label: "GitHub Actions" },
  ];

  //: 지금 보고 있는 탭이 만들어 낸 초안만 보여 준다.
  //:
  //: 탭 이름이 아니라 **받은 제안의 file_type** 으로 판정한다. 탭만 보고
  //: 판정하면, Compose 를 생성한 뒤 Dockerfile 탭으로 옮겼을 때 compose 내용이
  //: Dockerfile 인 것처럼 표시된다.
  const matchesTab = !!proposal && proposal.file_type === INFRA_FILE_TYPE_BY_TAB[activeFileTab];
  const activeFileLabel = infraFileLabelForTab(activeFileTab);
  const canSwitchFileTab = ["idle", "preview", "saved", "done", "error"].includes(step);
  const securityScanEnabled = canRunSecurityScan(step);
  const activeContent = matchesTab ? proposal!.content : null;
  const stackComment = matchesTab
    ? `# 스택: ${proposal!.base_template ?? "auto-detected"}`
    : null;
  //: AI 를 못 써서 템플릿으로 대신 만든 경우, 그 사유를 사용자에게 보여 준다.
  //: (코어가 risk_reasons 에 담아 보낸다. 예전엔 이 상황이 500 이라 초안조차
  //: 없었다.)
  const fallbackNotes = matchesTab ? (proposal!.risk_reasons ?? []) : [];

  return (
    <div style={{ fontFamily: "var(--vscode-font-family, sans-serif)", fontSize: 12, color: "var(--vscode-editor-foreground)" }}>

      {/* ── Section header ── */}
      <div style={{ fontSize: 11, fontWeight: 600, color: "var(--vscode-descriptionForeground, #888)", marginBottom: 10 }}>
        인프라 파일 생성
      </div>

      {/* ── File type tabs ── */}
      <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
        {fileTabs.map((tab) => {
          const isActive = activeFileTab === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => handleSwitchFileTab(tab.id)}
              disabled={!canSwitchFileTab}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
                padding: "5px 11px",
                borderRadius: 5,
                border: `1.5px solid ${isActive ? "#3b82f6" : "#3f3f3f"}`,
                background: isActive ? "rgba(59,130,246,0.15)" : "#252526",
                color: isActive ? "#93c5fd" : "#888",
                fontSize: 11,
                fontWeight: isActive ? 600 : 400,
                cursor: canSwitchFileTab ? "pointer" : "not-allowed",
                opacity: canSwitchFileTab ? 1 : 0.55,
                transition: "all 0.15s",
              }}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      {/* ── Code preview card ── */}
      <div style={{
        background: "#1a1a1a",
        border: "1px solid #333",
        borderRadius: 6,
        overflow: "hidden",
        marginBottom: 10,
      }}>
        {/* Card header */}
        <div style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "6px 10px",
          background: "#252526",
          borderBottom: "1px solid #2a2a2a",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: "#aaa" }}>
            <span style={{ fontWeight: 500 }}>
              {activeFileLabel}
            </span>
          </div>
          {matchesTab && activeFileTab === "dockerfile" && (
            <button
              onClick={() => {/* preview action */}}
              style={{
                background: "#333",
                color: "#ccc",
                border: "1px solid #444",
                borderRadius: 4,
                padding: "2px 9px",
                fontSize: 10,
                cursor: "pointer",
              }}
            >
              Preview
            </button>
          )}
        </div>

        {/* Code content */}
        <div style={{
          padding: "8px 10px",
          fontFamily: "var(--vscode-editor-font-family, monospace)",
          fontSize: 11,
          lineHeight: 1.65,
          color: "#d4d4d4",
          overflowX: "auto",
          maxHeight: 220,
          overflowY: "auto",
          whiteSpace: "pre",
          minHeight: 60,
        }}>
          {activeContent ? (
            <>
              {stackComment && <span style={{ color: "#6a9955" }}>{stackComment}{"\n"}</span>}
              {activeContent.replace(/^#[^\n]*\n?/, "")}
            </>
          ) : (
            <span style={{ color: "#555", fontStyle: "italic" }}>
              {step === "generating"
                ? "생성 중…"
                : `${activeFileLabel} 생성 버튼을 누르면 여기에 표시됩니다.`}
            </span>
          )}
        </div>
      </div>

      {/* AI 를 못 써서 템플릿으로 만든 경우의 안내 — 초안은 이미 위에 있다. */}
      {fallbackNotes.length > 0 && (
        <div style={{ marginBottom: 10, border: "1px solid var(--vscode-inputValidation-warningBorder, #cca700)", background: "var(--vscode-inputValidation-warningBackground, rgba(204,167,0,.12))", borderRadius: 5, padding: "7px 9px", color: "var(--vscode-editorWarning-foreground, #cca700)", fontSize: 11, lineHeight: 1.5 }}>
          {fallbackNotes.map((note, i) => <div key={i}>{note}</div>)}
        </div>
      )}

      {/* ── Loading spinner ── */}
      {(step === "generating" || step === "saving" || step === "scanning" || step === "planning" || step === "deploying") && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10, color: "#888" }}>
          <div style={{ width: 13, height: 13, border: "2px solid #3f3f3f", borderTopColor: "#3b82f6", borderRadius: "50%", animation: "spin 0.8s linear infinite", flexShrink: 0 }} />
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          <span>
            {step === "generating" ? `${activeFileLabel} 생성 중…` :
             step === "saving" ? `${activeFileLabel} 저장 중…` :
             step === "scanning" ? "보안 스캔 실행 중…" :
             step === "planning" ? "배포 플랜 생성 중…" :
             "배포 실행 중…"}
          </span>
        </div>
      )}

      {/* ── Required secrets warning ── */}
      {matchesTab && proposal && proposal.required_secrets.length > 0 && (
        <div style={{ marginBottom: 10, fontSize: 10, color: "#f59e0b", background: "rgba(245,158,11,0.08)", border: "1px solid rgba(245,158,11,0.3)", borderRadius: 5, padding: "5px 9px" }}>
          ⚠ 필요한 환경변수: {proposal.required_secrets.join(", ")}
        </div>
      )}

      {/* ── Scan result ── */}
      {step === "scanDone" && scanResult && (
        <div style={{ marginBottom: 10 }}>
          {renderScanSummary(scanResult)}
        </div>
      )}

      {/* ── Deploy plan ── */}
      {step === "planReady" && plan && (
        <div style={{ background: "#252526", border: "1px solid #333", borderRadius: 5, padding: "8px 10px", marginBottom: 10, fontSize: 11 }}>
          <div style={{ marginBottom: 3 }}><strong>이미지:</strong> {plan.image}</div>
          <div style={{ marginBottom: 3 }}><strong>컨테이너:</strong> {plan.container_name}</div>
          <div><strong>포트:</strong> {Object.entries(plan.ports).map(([h, c]) => `${h}→${c}`).join(", ")}</div>
        </div>
      )}

      {/* ── Done banner ── */}
      {step === "done" && (
        <div style={{ background: "rgba(34,197,94,0.1)", border: "1px solid #22c55e", borderRadius: 5, padding: "10px 12px", color: "#22c55e", fontWeight: 600, marginBottom: 10 }}>
          ✓ 배포 완료! Health Check 통과
          {deployResult?.deployment_id && (
            <div style={{ fontSize: 10, fontWeight: 400, marginTop: 4, color: "#888" }}>
              deployment_id: {deployResult.deployment_id}
            </div>
          )}
        </div>
      )}

      {step === "saved" && (
        <div style={{ background: "rgba(34,197,94,0.1)", border: "1px solid #22c55e", borderRadius: 5, padding: "10px 12px", color: "#22c55e", fontWeight: 600, marginBottom: 10 }}>
          ✓ {activeFileLabel} 저장 완료
        </div>
      )}

      {/* ── Error ── */}
      {step === "error" && error && (
        <div style={{ background: "rgba(239,68,68,0.1)", border: "1px solid #ef4444", borderRadius: 5, padding: "8px 10px", color: "#ef4444", marginBottom: 10 }}>
          {error}
        </div>
      )}

      {/* ── Bottom action row ── */}
      <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
        {/* 저장 Level 1 button */}
        <button
          onClick={() => {
            if (step === "idle") {
              handleGenerateInfraFile();
            } else if (step === "preview" && proposal && matchesTab) {
              if (proposal.approval_level <= 1) {
                handleApproveInfraFile();
              } else {
                setApprovalContext("infra");
                setShowApproval(true);
              }
            } else if (step === "planReady") {
              setApprovalContext("deploy");
              setShowApproval(true);
            } else if (step === "saved" || step === "done" || step === "error") {
              setStep("idle");
              setProposal(null);
              setDeployResult(null);
              setError(null);
            }
          }}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            background: "#16a34a",
            color: "#fff",
            border: "none",
            borderRadius: 5,
            padding: "6px 12px",
            fontSize: 12,
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          <span>✅</span>
          {step === "idle"
            ? `${activeFileLabel} 생성`
            : step === "preview"
            ? `저장 Level ${proposal?.approval_level ?? 1}`
            : step === "planReady"
            ? "승인 (Level 2)"
            : step === "scanDone"
            ? "docker build / run"
            : step === "saved"
            ? "다른 파일 생성"
            : step === "done" || step === "error"
            ? "새 배포"
            : "처리 중…"}
        </button>

        {/* 보안 스캔 button */}
        {activeFileTab === "dockerfile" && <button
          onClick={() => {
            if (!securityScanEnabled) { return; }
            if (step === "preview" && proposal && matchesTab) {
              handleApproveInfraFile(); // saves then triggers scan
            } else {
              postMessage("runScan", { scanType: "trivy", workspacePath: "" });
            }
          }}
          disabled={!securityScanEnabled}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            background: "#1c1c1c",
            color: "#ccc",
            border: "1px solid #3f3f3f",
            borderRadius: 5,
            padding: "6px 12px",
            fontSize: 12,
            fontWeight: 500,
            cursor: securityScanEnabled ? "pointer" : "not-allowed",
            opacity: securityScanEnabled ? 1 : 0.5,
          }}
        >
          <span>🔒</span> 보안 스캔
        </button>}
      </div>

      {/* ── Approval Modal ── */}
      {showApproval && approvalContext === "infra" && proposal && matchesTab && (
        <div style={{ marginTop: 10 }}>
          <ApprovalModal
            level={proposal.approval_level}
            title={`파일 저장: ${proposal.target_path}`}
            summary="생성된 인프라 파일을 워크스페이스에 저장합니다."
            riskLevel={proposal.risk_level}
            riskReasons={proposal.risk_reasons}
            onApprove={handleApproveInfraFile}
            onReject={handleRejectDockerfile}
          />
        </div>
      )}

      {showApproval && approvalContext === "deploy" && plan && (
        <div style={{ marginTop: 10 }}>
          <ApprovalModal
            level={plan.approval_level}
            title="docker build / run 실행"
            summary={`이미지 ${plan.image}를 빌드하고 컨테이너 ${plan.container_name}을 실행합니다.`}
            riskLevel={plan.risk_level}
            riskReasons={plan.risk_reasons}
            commandPreview={`docker build -t ${plan.image} . && docker run -d --name ${plan.container_name} ${Object.entries(plan.ports).map(([h, c]) => `-p ${h}:${c}`).join(" ")} ${plan.image}`}
            onApprove={handleDeploy}
            onReject={() => { setShowApproval(false); }}
          />
        </div>
      )}
    </div>
  );
};

export default ShipMode;
