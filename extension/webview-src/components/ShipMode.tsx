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

type Step =
  | "idle"
  | "generating"
  | "preview"
  | "scanning"
  | "scanDone"
  | "planning"
  | "planReady"
  | "deploying"
  | "healthCheck"
  | "done"
  | "error";

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
  const [approvalContext, setApprovalContext] = useState<"dockerfile" | "deploy" | null>(null);
  const [activeFileTab, setActiveFileTab] = useState<"dockerfile" | "compose" | "actions">("dockerfile");

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
    }, [])
  );

  // ── Handlers ─────────────────────────────────────────────────────────────

  const handleGenerateDockerfile = useCallback(() => {
    setStep("generating");
    setError(null);
    setProposal(null);
    setScanResult(null);
    setPlan(null);
    setDeployResult(null);
    postMessage("generateDockerfile", { workspacePath: "" /* extension fills */ });
  }, [postMessage]);

  const handleApproveDockerfile = useCallback(() => {
    if (!proposal) { return; }
    postMessage("approveDockerfile", { proposalId: proposal.proposal_id, approved: true });
    setStep("scanning");
    setShowApproval(false);
    // Trigger Trivy scan after saving
    setTimeout(() => {
      postMessage("runScan", { scanType: "trivy", workspacePath: "" });
    }, 500);
  }, [proposal, postMessage]);

  const handleRejectDockerfile = useCallback(() => {
    if (!proposal) { return; }
    postMessage("approveDockerfile", { proposalId: proposal.proposal_id, approved: false });
    setStep("idle");
    setProposal(null);
    setShowApproval(false);
  }, [proposal, postMessage]);

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

  const fileTabs: { id: "dockerfile" | "compose" | "actions"; label: string }[] = [
    { id: "dockerfile", label: "Dockerfile" },
    { id: "compose", label: "Compose" },
    { id: "actions", label: "GitHub Actions" },
  ];

  // Derive displayed content for the active file tab
  const activeContent = activeFileTab === "dockerfile" && proposal ? proposal.content : null;
  const stackComment = activeFileTab === "dockerfile" && proposal
    ? `# 스택: ${proposal.base_template ?? "auto-detected"}`
    : null;

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
              onClick={() => setActiveFileTab(tab.id)}
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
                cursor: "pointer",
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
              {activeFileTab === "dockerfile" ? "Dockerfile" : activeFileTab === "compose" ? "docker-compose.yml" : ".github/workflows/deploy.yml"}
            </span>
          </div>
          {proposal && activeFileTab === "dockerfile" && (
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
                : activeFileTab === "dockerfile"
                ? "Dockerfile 생성 버튼을 누르면 여기에 표시됩니다."
                : "Dockerfile 생성 후 활성화됩니다."}
            </span>
          )}
        </div>
      </div>

      {/* ── Loading spinner ── */}
      {(step === "generating" || step === "scanning" || step === "planning" || step === "deploying") && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10, color: "#888" }}>
          <div style={{ width: 13, height: 13, border: "2px solid #3f3f3f", borderTopColor: "#3b82f6", borderRadius: "50%", animation: "spin 0.8s linear infinite", flexShrink: 0 }} />
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          <span>
            {step === "generating" ? "Dockerfile 생성 중…" :
             step === "scanning" ? "보안 스캔 실행 중…" :
             step === "planning" ? "배포 플랜 생성 중…" :
             "배포 실행 중…"}
          </span>
        </div>
      )}

      {/* ── Required secrets warning ── */}
      {proposal && proposal.required_secrets.length > 0 && (
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
              handleGenerateDockerfile();
            } else if (step === "preview" && proposal) {
              if (proposal.approval_level <= 1) {
                handleApproveDockerfile();
              } else {
                setApprovalContext("dockerfile");
                setShowApproval(true);
              }
            } else if (step === "planReady") {
              setApprovalContext("deploy");
              setShowApproval(true);
            } else if (step === "done" || step === "error") {
              setStep("idle");
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
            ? "Dockerfile 생성"
            : step === "preview"
            ? `저장 Level ${proposal?.approval_level ?? 1}`
            : step === "planReady"
            ? "승인 (Level 2)"
            : step === "scanDone"
            ? "docker build / run"
            : step === "done" || step === "error"
            ? "새 배포"
            : "처리 중…"}
        </button>

        {/* 보안 스캔 button */}
        <button
          onClick={() => {
            if (step === "preview" && proposal) {
              handleApproveDockerfile(); // saves then triggers scan
            } else {
              postMessage("runScan", { scanType: "trivy", workspacePath: "" });
            }
          }}
          disabled={step === "generating" || step === "scanning"}
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
            cursor: step === "generating" || step === "scanning" ? "not-allowed" : "pointer",
            opacity: step === "generating" || step === "scanning" ? 0.5 : 1,
          }}
        >
          <span>🔒</span> 보안 스캔
        </button>
      </div>

      {/* ── Approval Modal ── */}
      {showApproval && approvalContext === "dockerfile" && proposal && (
        <div style={{ marginTop: 10 }}>
          <ApprovalModal
            level={proposal.approval_level}
            title={`Dockerfile 저장: ${proposal.target_path}`}
            summary="AI가 생성한 Dockerfile을 워크스페이스에 저장합니다."
            riskLevel={proposal.risk_level}
            riskReasons={proposal.risk_reasons}
            onApprove={handleApproveDockerfile}
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
