/**
 * ReCoder — OperateMode Component
 *
 * Stage 3 Operate:
 *   EC2 incident 조회 (SSH 기반) → Bedrock 분석 → ResponseProposal → 사용자 승인 → SSH 원격 실행
 *
 * Approval Level:
 *   - 컨테이너 재시작: Level 3 (영향 대상 + 실행 명령 + 롤백 경로 + 리스크 표시)
 *   - 롤백/env 변경: Level 3~4
 *
 * 핵심 원칙: 감지는 Watchdog이, 분석은 Local Core가, 실행은 사용자 승인 후.
 */

import React, { useState, useCallback } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import ApprovalModal from "./ApprovalModal";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface AlertRecord {
  alert_id: string;
  alert_type: string;
  severity: "low" | "medium" | "high" | "critical";
  container_name?: string;
  environment: string;
  host?: string;
  detected_at: string;
  logs_excerpt?: string;
}

interface ResponseProposal {
  alert_id: string;
  action_type: string;
  target_container?: string;
  parameters: { reasoning?: string; rollback_feasible?: boolean; rollback_blocker?: string };
  risk_level: "low" | "medium" | "high" | "critical";
  risk_reasons: string[];
  approval_level: 1 | 2 | 3 | 4;
}

// ---------------------------------------------------------------------------
// OperateMode
// ---------------------------------------------------------------------------

interface OperateModeProps {
  isActive: boolean;
}

export const OperateMode: React.FC<OperateModeProps> = ({ isActive }) => {
  const { postMessage, useMessage } = useVSCodeApi();

  const [host, setHost] = useState("");
  const [sshKeyPath, setSshKeyPath] = useState("");
  const [incidents, setIncidents] = useState<AlertRecord[]>([]);
  const [selectedAlert, setSelectedAlert] = useState<AlertRecord | null>(null);
  const [proposal, setProposal] = useState<ResponseProposal | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isFetching, setIsFetching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ status: string } | null>(null);
  const [showApproval, setShowApproval] = useState(false);

  // Message listener
  useMessage(
    useCallback((msg) => {
      const { type, payload } = msg;

      if (type === "stateUpdate") {
        const state = payload as { incidents?: AlertRecord[] };
        if (state.incidents) {
          setIncidents(state.incidents);
          setIsFetching(false);
        }
      }

      if (type === "proposalReady" && (payload as ResponseProposal).alert_id) {
        setProposal(payload as ResponseProposal);
        setIsLoading(false);
        setError(null);
      }

      if (type === "opsResult") {
        setResult(payload as { status: string });
        setShowApproval(false);
        setIsLoading(false);
      }

      if (type === "errorMessage") {
        setError((payload as { message: string }).message);
        setIsLoading(false);
        setIsFetching(false);
      }
    }, [])
  );

  // ── Handlers ─────────────────────────────────────────────────────────────

  const handleFetchIncidents = useCallback(() => {
    if (!host.trim()) { return; }
    setIsFetching(true);
    setIncidents([]);
    setSelectedAlert(null);
    setProposal(null);
    setError(null);
    setResult(null);
    postMessage("fetchIncidents", { host: host.trim(), sshKeyPath: sshKeyPath.trim() });
  }, [host, sshKeyPath, postMessage]);

  const handleAnalyzeAlert = useCallback((alert: AlertRecord) => {
    setSelectedAlert(alert);
    setProposal(null);
    setIsLoading(true);
    setError(null);
    postMessage("analyzeIncident", { alertId: alert.alert_id });
  }, [postMessage]);

  const handleApproveResponse = useCallback(() => {
    if (!proposal) { return; }
    setIsLoading(true);
    postMessage("approveResponse", { proposalId: proposal.alert_id, approved: true });
    setShowApproval(false);
  }, [proposal, postMessage]);

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

  const inputStyle: React.CSSProperties = {
    width: "100%",
    boxSizing: "border-box",
    background: "var(--vscode-input-background, #1e1e1e)",
    color: "var(--vscode-input-foreground, #ccc)",
    border: "1px solid var(--vscode-input-border, #555)",
    borderRadius: 3,
    padding: "5px 8px",
    fontSize: 11,
    fontFamily: "var(--vscode-editor-font-family, monospace)",
    marginBottom: 6,
    outline: "none",
  };

  const btnPrimary: React.CSSProperties = {
    background: "var(--vscode-button-background, #0078d4)",
    color: "var(--vscode-button-foreground, #fff)",
    border: "none",
    borderRadius: 4,
    padding: "5px 12px",
    fontSize: 12,
    cursor: "pointer",
    fontWeight: 600,
  };

  const btnSecondary: React.CSSProperties = {
    background: "var(--vscode-button-secondaryBackground, #3a3a3a)",
    color: "var(--vscode-button-secondaryForeground, #ccc)",
    border: "none",
    borderRadius: 4,
    padding: "5px 12px",
    fontSize: 12,
    cursor: "pointer",
  };

  const riskColors: Record<string, string> = {
    low: "var(--vscode-testing-iconPassed, #4caf50)",
    medium: "var(--vscode-editorWarning-foreground, #ff9800)",
    high: "var(--vscode-editorError-foreground, #f44336)",
    critical: "#b71c1c",
  };

  const actionLabel: Record<string, string> = {
    docker_restart: "컨테이너 재시작",
    ssh_docker_restart: "원격 컨테이너 재시작",
    ssh_docker_rollback: "이전 버전 롤백",
    ssh_env_update: "환경변수 변경",
    no_action: "조치 없음 (모니터링)",
  };

  if (!isActive) {
    return (
      <div style={{ padding: 16, color: "var(--vscode-descriptionForeground)", textAlign: "center", fontSize: 12, lineHeight: 1.7 }}>
        Operate Mode는 다음이 필요합니다:<br />
        AI Ready · AWS Deploy Ready · Ops Ready<br />
        <span style={{ color: "var(--vscode-textLink-foreground, #4af)" }}>진단 탭에서 확인하세요</span>
      </div>
    );
  }

  return (
    <div style={{ fontFamily: "var(--vscode-font-family, sans-serif)", fontSize: 12, color: "var(--vscode-editor-foreground)" }}>

      {/* EC2 연결 정보 */}
      <div style={sectionHeader}>EC2 연결 정보</div>
      <input
        style={inputStyle}
        placeholder="EC2 호스트 (예: 52.1.2.3)"
        value={host}
        onChange={(e) => setHost(e.target.value)}
      />
      <input
        style={inputStyle}
        placeholder="SSH 키 경로 (예: ~/.ssh/id_rsa)"
        value={sshKeyPath}
        onChange={(e) => setSshKeyPath(e.target.value)}
      />
      <button
        style={{ ...btnPrimary, opacity: (!host.trim() || isFetching) ? 0.6 : 1 }}
        onClick={handleFetchIncidents}
        disabled={!host.trim() || isFetching}
      >
        {isFetching ? "조회 중…" : "운영 상태 조회"}
      </button>

      {/* 에러 표시 */}
      {error && (
        <div style={{ marginTop: 8, background: "rgba(244,67,54,0.1)", border: "1px solid var(--vscode-editorError-foreground, #f44)", borderRadius: 4, padding: "6px 8px", color: "var(--vscode-editorError-foreground, #f44)" }}>
          {error}
        </div>
      )}

      {/* 인시던트 목록 */}
      {incidents.length > 0 && (
        <>
          <div style={sectionHeader}>인시던트 ({incidents.length}건)</div>
          {incidents.map((alert, i) => (
            <div
              key={alert.alert_id}
              style={{
                background:
                  selectedAlert?.alert_id === alert.alert_id
                    ? "var(--vscode-list-activeSelectionBackground, #094771)"
                    : "var(--vscode-sideBar-background, #252526)",
                border: `1px solid ${
                  alert.severity === "critical" || alert.severity === "high"
                    ? riskColors[alert.severity]
                    : "var(--vscode-panel-border, #333)"
                }`,
                borderRadius: 4,
                padding: "7px 10px",
                marginBottom: 6,
                cursor: "pointer",
              }}
              onClick={() => handleAnalyzeAlert(alert)}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 3 }}>
                <span style={{ fontWeight: 600, fontSize: 11 }}>
                  {alert.alert_type.replace(/_/g, " ")}
                </span>
                <span style={{
                  background: riskColors[alert.severity],
                  color: "#fff", borderRadius: 3, padding: "1px 6px", fontSize: 9, fontWeight: 700,
                }}>
                  {alert.severity.toUpperCase()}
                </span>
              </div>
              <div style={{ fontSize: 10, color: "var(--vscode-descriptionForeground)" }}>
                {alert.container_name && <span>{alert.container_name} · </span>}
                {alert.environment} · {new Date(alert.detected_at).toLocaleString("ko-KR")}
              </div>
            </div>
          ))}
        </>
      )}

      {incidents.length === 0 && !isFetching && host && (
        <div style={{ marginTop: 10, fontSize: 11, color: "var(--vscode-testing-iconPassed, #4caf50)" }}>
          ✓ 인시던트 없음
        </div>
      )}

      {/* 로딩 (분석 중) */}
      {isLoading && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 12, color: "var(--vscode-descriptionForeground)" }}>
          <div style={{ width: 14, height: 14, border: "2px solid var(--vscode-panel-border)", borderTopColor: "var(--vscode-button-background, #0078d4)", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} />
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          <span>AI가 인시던트를 분석 중…</span>
        </div>
      )}

      {/* ResponseProposal */}
      {proposal && !showApproval && (
        <>
          <div style={sectionHeader}>AI 대응 제안 (Approval Level {proposal.approval_level})</div>
          <div style={{
            background: "var(--vscode-sideBar-background)",
            border: `1px solid ${riskColors[proposal.risk_level]}`,
            borderRadius: 4,
            padding: "10px 12px",
            marginBottom: 10,
          }}>
            <div style={{ fontWeight: 700, marginBottom: 6 }}>
              {actionLabel[proposal.action_type] ?? proposal.action_type}
            </div>
            {proposal.parameters.reasoning && (
              <div style={{ fontSize: 11, color: "var(--vscode-descriptionForeground)", marginBottom: 8, lineHeight: 1.6 }}>
                {proposal.parameters.reasoning}
              </div>
            )}
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 6 }}>
              <span style={{ background: riskColors[proposal.risk_level], color: "#fff", borderRadius: 3, padding: "2px 7px", fontSize: 10, fontWeight: 700 }}>
                {proposal.risk_level.toUpperCase()}
              </span>
              {proposal.target_container && (
                <span style={{ fontSize: 10, color: "var(--vscode-descriptionForeground)", fontFamily: "monospace" }}>
                  대상: {proposal.target_container}
                </span>
              )}
            </div>
            {proposal.risk_reasons.length > 0 && (
              <ul style={{ margin: "6px 0 0", paddingLeft: 16, fontSize: 10, color: "var(--vscode-descriptionForeground)", lineHeight: 1.6 }}>
                {proposal.risk_reasons.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            )}
            {!proposal.parameters.rollback_feasible && (
              <div style={{ marginTop: 8, fontSize: 10, color: "var(--vscode-editorWarning-foreground, #ff9800)" }}>
                ⚠ 롤백 불완전: {proposal.parameters.rollback_blocker}
              </div>
            )}
          </div>

          <div style={{ display: "flex", gap: 6 }}>
            <button style={btnSecondary} onClick={() => { setProposal(null); setSelectedAlert(null); }}>
              거절
            </button>
            <button style={btnPrimary} onClick={() => setShowApproval(true)}>
              승인 (Level {proposal.approval_level})
            </button>
          </div>
        </>
      )}

      {/* 실행 결과 */}
      {result && (
        <div style={{
          marginTop: 10,
          background: result.status === "executed" ? "rgba(76,175,80,0.12)" : "rgba(244,67,54,0.1)",
          border: `1px solid ${result.status === "executed" ? "var(--vscode-testing-iconPassed, #4caf50)" : "var(--vscode-editorError-foreground, #f44)"}`,
          borderRadius: 4, padding: "8px 10px",
          color: result.status === "executed" ? "var(--vscode-testing-iconPassed, #4caf50)" : "var(--vscode-editorError-foreground, #f44)",
          fontWeight: 600,
        }}>
          {result.status === "executed" ? "✓ 원격 명령 실행 완료" : `실행 실패: ${result.status}`}
          <div style={{ marginTop: 8 }}>
            <button style={btnSecondary} onClick={() => { setResult(null); setProposal(null); setSelectedAlert(null); }}>
              닫기
            </button>
          </div>
        </div>
      )}

      {/* Approval Modal (Level 3) */}
      {showApproval && proposal && (
        <div style={{ marginTop: 10 }}>
          <ApprovalModal
            level={proposal.approval_level}
            title={actionLabel[proposal.action_type] ?? proposal.action_type}
            summary={proposal.parameters.reasoning ?? ""}
            riskLevel={proposal.risk_level}
            riskReasons={proposal.risk_reasons}
            commandPreview={proposal.target_container
              ? `ssh ec2-user@host 'docker restart ${proposal.target_container}'`
              : undefined}
            rollbackPath={
              proposal.parameters.rollback_feasible
                ? "DeploymentRecord의 rollback_image로 복원 가능"
                : `롤백 불완전: ${proposal.parameters.rollback_blocker ?? "알 수 없음"}`
            }
            onApprove={handleApproveResponse}
            onReject={() => setShowApproval(false)}
          />
        </div>
      )}
    </div>
  );
};

export default OperateMode;
