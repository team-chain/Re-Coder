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
        <span style={{ color: "var(--vscode-textLink-foreground, #4af)" }}>진단 탭에서 확인</span>
      </div>
    );
  }

  return (
    <div style={{ fontFamily: "var(--vscode-font-family, sans-serif)", fontSize: 12, color: "var(--vscode-editor-foreground)" }}>

      {/* Step 1: Dockerfile 생성 */}
      {step === "idle" && (
        <>
          <div style={sectionHeader}>Stage 2 — Ship</div>
          <p style={{ fontSize: 11, color: "var(--vscode-descriptionForeground)", marginBottom: 10, lineHeight: 1.6 }}>
            프로젝트 스택을 자동 감지하고 Dockerfile을 생성합니다.
            보안 스캔 후 docker build / run 까지 진행합니다.
          </p>
          <button style={btnPrimary} onClick={handleGenerateDockerfile}>
            Dockerfile 생성
          </button>
        </>
      )}

      {/* Loading */}
      {(step === "generating" || step === "scanning" || step === "planning" || step === "deploying") && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 12, color: "var(--vscode-descriptionForeground)" }}>
          <div style={{ width: 14, height: 14, border: "2px solid var(--vscode-panel-border, #444)", borderTopColor: "var(--vscode-button-background, #0078d4)", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} />
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          <span>
            {step === "generating" ? "Dockerfile 생성 중…" :
             step === "scanning" ? "보안 스캔 실행 중…" :
             step === "planning" ? "배포 플랜 생성 중…" :
             "배포 실행 중…"}
          </span>
        </div>
      )}

      {/* Step 2: Dockerfile Preview */}
      {step === "preview" && proposal && (
        <>
          <div style={sectionHeader}>Dockerfile 미리보기</div>
          <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 8 }}>
            <span style={{
              background: riskColors[proposal.risk_level],
              color: "#fff", borderRadius: 3, padding: "2px 7px", fontSize: 10, fontWeight: 700,
            }}>
              {proposal.risk_level.toUpperCase()}
            </span>
            <span style={{ fontSize: 10, color: "var(--vscode-descriptionForeground)" }}>
              → {proposal.target_path}
            </span>
          </div>

          <div style={codeBlock}>{proposal.content}</div>

          {proposal.required_secrets.length > 0 && (
            <div style={{ marginTop: 8, fontSize: 10, color: "var(--vscode-editorWarning-foreground, #ff9800)" }}>
              ⚠ 필요한 환경변수: {proposal.required_secrets.join(", ")}
            </div>
          )}

          <div style={{ display: "flex", gap: 6, marginTop: 10 }}>
            <button style={btnSecondary} onClick={() => { setStep("idle"); setProposal(null); }}>
              취소
            </button>
            <button style={btnPrimary} onClick={() => {
              if (proposal.approval_level <= 1) {
                handleApproveDockerfile();
              } else {
                setApprovalContext("dockerfile");
                setShowApproval(true);
              }
            }}>
              {isDockerReady ? "저장 후 스캔" : "Dockerfile 저장"}
            </button>
          </div>
        </>
      )}

      {/* Step 3: Scan Result */}
      {step === "scanDone" && scanResult && (
        <>
          <div style={sectionHeader}>보안 스캔 결과</div>
          {renderScanSummary(scanResult)}

          {!isDockerReady ? (
            <div style={{ fontSize: 11, color: "var(--vscode-descriptionForeground)", marginTop: 8 }}>
              Docker가 설치되지 않아 build/run을 건너뜁니다. Dockerfile은 저장되었습니다.
            </div>
          ) : (
            <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
              <button style={btnSecondary} onClick={() => setStep("idle")}>
                완료
              </button>
              <button style={btnPrimary} onClick={handleCreatePlan}>
                docker build / run 진행
              </button>
            </div>
          )}
        </>
      )}

      {/* Step 4: Deploy Plan Ready */}
      {step === "planReady" && plan && (
        <>
          <div style={sectionHeader}>배포 플랜 (Approval Level 2)</div>
          <div style={{ background: "var(--vscode-sideBar-background)", border: "1px solid var(--vscode-panel-border, #333)", borderRadius: 4, padding: "8px 10px", marginBottom: 8 }}>
            <div style={{ fontSize: 11, marginBottom: 4 }}>
              <strong>이미지:</strong> {plan.image}
            </div>
            <div style={{ fontSize: 11, marginBottom: 4 }}>
              <strong>컨테이너:</strong> {plan.container_name}
            </div>
            <div style={{ fontSize: 11, marginBottom: 4 }}>
              <strong>포트:</strong>{" "}
              {Object.entries(plan.ports)
                .map(([h, c]) => `${h}→${c}`)
                .join(", ")}
            </div>
            <div style={{ marginTop: 6, fontFamily: "monospace", fontSize: 10, color: "var(--vscode-descriptionForeground)" }}>
              실행 명령 미리보기:
            </div>
            <div style={{ ...codeBlock, maxHeight: 60 }}>
              docker build -t {plan.image} . && docker run -d --name {plan.container_name}{" "}
              {Object.entries(plan.ports).map(([h, c]) => `-p ${h}:${c}`).join(" ")}{" "}
              {plan.image}
            </div>
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <button style={btnSecondary} onClick={() => setStep("idle")}>
              취소
            </button>
            <button style={btnPrimary} onClick={() => {
              setApprovalContext("deploy");
              setShowApproval(true);
            }}>
              승인 (Level 2)
            </button>
          </div>
        </>
      )}

      {/* Step: Done */}
      {step === "done" && (
        <div style={{
          marginTop: 10,
          background: "rgba(76,175,80,0.12)",
          border: "1px solid var(--vscode-testing-iconPassed, #4caf50)",
          borderRadius: 4, padding: "10px 12px",
          color: "var(--vscode-testing-iconPassed, #4caf50)", fontWeight: 600,
        }}>
          ✓ 배포 완료! Health Check 통과
          {deployResult?.deployment_id && (
            <div style={{ fontSize: 10, fontWeight: 400, marginTop: 4, color: "var(--vscode-descriptionForeground)" }}>
              deployment_id: {deployResult.deployment_id}
            </div>
          )}
          <div style={{ marginTop: 10 }}>
            <button style={btnSecondary} onClick={() => { setStep("idle"); setDeployResult(null); }}>
              새 배포
            </button>
          </div>
        </div>
      )}

      {/* Error */}
      {step === "error" && error && (
        <div style={{ marginTop: 10, background: "rgba(244,67,54,0.1)", border: "1px solid var(--vscode-editorError-foreground, #f44)", borderRadius: 4, padding: "8px 10px", color: "var(--vscode-editorError-foreground, #f44)" }}>
          {error}
          <div style={{ marginTop: 8 }}>
            <button style={btnSecondary} onClick={() => { setStep("idle"); setError(null); }}>
              다시 시도
            </button>
          </div>
        </div>
      )}

      {/* Approval Modal */}
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
