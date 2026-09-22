import React from "react";

export interface EcsProgressStatus {
  warnings?: string[];
  running: boolean;
  stage: string;
  stage_text?: string;
  deployment_id?: string;
  observed_at?: string;
  steps?: Array<{
    key: string; label: string;
    status: "pending" | "running" | "done" | "failed" | "cancelled" | "skipped" | "warning";
    started_at?: string | null; finished_at?: string | null;
  }>;
  error?: string;
  error_detail?: string;
  remedy?: string;
  service_url?: string;
  log_tail?: string[];
  image_uri?: string;
  task_def_arn?: string;
  started_at?: string;
  finished_at?: string;
}

export interface EcsProgressState {
  status: EcsProgressStatus | null;
  requestError: string;
  pollError: string;
  submitting: boolean;
  expectedId: string;
}
export const initialEcsProgress: EcsProgressState = {
  status: null, requestError: "", pollError: "", submitting: false, expectedId: "",
};
type Action =
  | { type: "start" }
  | { type: "accepted"; deploymentId?: string }
  | { type: "rejected"; message?: string }
  | { type: "pollError"; message: string; deploymentId?: string }
  | { type: "status"; status: EcsProgressStatus };

export function ecsProgressReducer(state: EcsProgressState, action: Action): EcsProgressState {
  switch (action.type) {
    case "start": return { ...initialEcsProgress, submitting: true };
    case "accepted": return { ...state, submitting: false, expectedId: action.deploymentId ?? "" };
    case "rejected": return { ...state, submitting: false, expectedId: "", requestError: action.message ?? "" };
    case "pollError":
      if (state.submitting || (state.expectedId && action.deploymentId !== state.expectedId)) return state;
      return { ...state, pollError: action.message };
    case "status": {
      if (state.submitting || (state.expectedId && action.status.deployment_id !== state.expectedId)) return state;
      const current = state.status;
      if (current?.deployment_id === action.status.deployment_id) {
        if (current?.observed_at && action.status.observed_at && Date.parse(action.status.observed_at) < Date.parse(current.observed_at)) return state;
        if (current?.finished_at && action.status.running) return state;
      }
      // A successful poll may clear a connection error, never a rejected deployment.
      return { ...state, status: action.status, pollError: "" };
    }
  }
}

export function ecsProgressBusy(state: EcsProgressState): boolean {
  return state.submitting || Boolean(state.status?.running)
    || Boolean(state.expectedId && state.status?.deployment_id !== state.expectedId);
}

export function serviceLink(value?: string): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

const labels = { pending: "대기", running: "진행 중", done: "완료", failed: "실패", cancelled: "취소", skipped: "생략", warning: "확인 필요" };
const colors = { pending: "var(--vscode-descriptionForeground, #999)", running: "var(--vscode-textLink-foreground, #75beff)", done: "var(--vscode-charts-green, #4ec9b0)", failed: "var(--vscode-errorForeground, #f48771)", cancelled: "var(--vscode-editorWarning-foreground, #cca700)", skipped: "var(--vscode-descriptionForeground, #999)", warning: "var(--vscode-editorWarning-foreground, #cca700)" };
const box: React.CSSProperties = { marginTop: 12, padding: 12, border: "1px solid var(--vscode-panel-border, #444)", borderRadius: 6, fontSize: 12, lineHeight: 1.6, overflowWrap: "anywhere" };

export function EcsDeploymentProgress({ state }: { state: EcsProgressState }) {
  const status = state.status;
  const link = serviceLink(status?.service_url);
  const detail = status?.error_detail || status?.log_tail?.filter(line => line.startsWith("detail:")).map(line => line.replace(/^detail:\s*/, "")).join("\n");
  const title = status?.stage_text || (status?.stage === "done" ? "완료" : status?.stage === "failed" ? "실패" : status?.running ? "배포 중" : "대기");
  const failure = status?.error || (status?.stage === "failed" ? "배포가 완료되지 않았습니다." : "");
  return (
    <section aria-label="ECS 배포 진행 및 결과" style={box}>
      <strong>배포 진행 · 결과</strong>
      {state.requestError && <div role="alert" style={{ marginTop: 8, color: colors.failed, whiteSpace: "pre-wrap" }}>배포 요청 실패: {state.requestError}</div>}
      {state.pollError && <div role="alert" style={{ marginTop: 8, color: colors.warning, whiteSpace: "pre-wrap" }}>상태 조회 실패 — 마지막 확인 결과를 표시합니다. {state.pollError}</div>}
      {state.submitting && <p role="status">배포 요청을 전송하는 중…</p>}
      {!state.submitting && !status && <p role="status">{state.expectedId ? "새 배포의 상태를 확인하는 중…" : "배포 상태를 확인하는 중…"}</p>}
      {status && <>
        <div role="status" style={{ marginTop: 6, fontWeight: 600 }}>{status.stage === "idle" && !status.deployment_id ? "아직 ECS 배포 기록이 없습니다." : title}</div>
        {status.started_at && <div style={{ color: colors.pending }}>시작: {new Date(status.started_at).toLocaleString()}</div>}
        {!!status.steps?.length && <ol style={{ padding: 0, margin: "10px 0", listStyle: "none", display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 5 }}>
          {status.steps.map((step, index) => <li key={step.key} data-step={step.key} data-status={step.status} aria-current={step.status === "running" ? "step" : undefined} style={{ color: colors[step.status], padding: "4px 7px", borderLeft: `2px solid ${colors[step.status]}` }}>
            {index + 1}. {step.label} <b style={{ marginLeft: 5 }}>{step.status === "pending" && !status.running && status.stage !== "idle" ? "미실행" : labels[step.status]}</b>
          </li>)}
        </ol>}
        {!status.steps?.length && (status.deployment_id || status.stage !== "idle") && <p style={{ color: colors.pending }}>이 배포에는 세부 단계 기록이 없습니다.</p>}
        {failure && <div role="alert" style={{ marginTop: 8, padding: 9, borderLeft: `3px solid ${colors.failed}`, whiteSpace: "pre-wrap" }}>
          <strong>{failure}</strong>{detail && <div>{detail}</div>}{status.remedy && <div style={{ marginTop: 5 }}>조치: {status.remedy}</div>}
        </div>}
        {!!status.warnings?.length && <div style={{ marginTop: 8, color: colors.warning }}><b>확인할 사항</b><ul style={{ margin: "4px 0", paddingLeft: 18 }}>{status.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div>}
        {link && <div style={{ marginTop: 10 }}><a href={link} target="_blank" rel="noreferrer">서비스 URL 열기 ↗</a><div><code>{link}</code></div></div>}
        {!link && status.stage === "done" && <p>접속 URL이 없습니다. 아래 배포 로그에서 태스크 수와 네트워크 안내를 확인하세요.</p>}
        {!!status.log_tail?.length && <details style={{ marginTop: 10 }}><summary>배포 로그 · 리소스 정보</summary><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 220, overflow: "auto", fontSize: 11 }}>{status.log_tail.join("\n")}</pre></details>}
        {status.deployment_id && <div style={{ marginTop: 8, fontSize: 10, color: colors.pending }}>배포 ID: {status.deployment_id}</div>}
      </>}
    </section>
  );
}
