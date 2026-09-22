import React, { useCallback, useReducer, useRef } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

export interface ExecutionRolePlan {
  status: "missing" | "exists" | "created";
  account_id: string; caller_arn: string; role_arn: string; policy_arn: string;
  message: string; trust_policy: object; proposal_id?: string;
}
export interface ExecutionRoleState {
  pending: string; operation: "preview" | "apply" | "";
  plan: ExecutionRolePlan | null; error: string;
}
export const initialExecutionRole: ExecutionRoleState = { pending: "", operation: "", plan: null, error: "" };
type Action =
  | { type: "start"; requestId: string; operation: "preview" | "apply" }
  | { type: "result"; requestId: string; plan: ExecutionRolePlan }
  | { type: "error"; requestId: string; message: string }
  | { type: "cancel" };
export function executionRoleReducer(state: ExecutionRoleState, action: Action): ExecutionRoleState {
  if (action.type === "cancel") return state.pending ? state : initialExecutionRole;
  if (action.type === "start") return state.pending ? state : {
    ...state, pending: action.requestId, operation: action.operation,
    plan: action.operation === "preview" ? null : state.plan, error: "",
  };
  if (!state.pending || state.pending !== action.requestId) return state;
  if (action.type === "error") return { ...initialExecutionRole, error: action.message };
  const p = action.plan;
  if (!p || !["missing", "exists", "created"].includes(p.status) || !p.role_arn
      || (p.status === "missing" && !p.proposal_id)) {
    return { ...initialExecutionRole, error: "역할 확인 결과가 올바르지 않습니다. 다시 확인하세요." };
  }
  return { ...initialExecutionRole, plan: p };
}

export function ExecutionRoleView({ state, disabled = false, onPreview, onApprove, onCancel }: {
  state: ExecutionRoleState; disabled?: boolean;
  onPreview: () => void; onApprove: () => void; onCancel: () => void;
}) {
  const busy = Boolean(state.pending);
  const plan = state.plan;
  const button: React.CSSProperties = {
    padding: "7px 12px", borderRadius: 5, cursor: busy || disabled ? "default" : "pointer",
    border: "1px solid var(--vscode-panel-border, #555)",
    background: "var(--vscode-button-secondaryBackground, #333)",
    color: "var(--vscode-button-secondaryForeground, #fff)",
  };
  return <section aria-label="ECS 실행 역할 설정" style={{ marginTop: 14, padding: 12, border: "1px solid var(--vscode-panel-border, #555)", borderRadius: 6, fontSize: 12, overflowWrap: "anywhere" }}>
    <strong>ECS 실행 역할</strong>
    <p style={{ margin: "6px 0" }}>ECS가 ECR 이미지를 내려받고 CloudWatch에 로그를 쓰는 데 사용하는 역할입니다.</p>
    <button style={button} disabled={disabled || busy} onClick={onPreview}>
      {state.operation === "preview" ? "역할 확인 중…" : "실행 역할 확인"}
    </button>
    {state.error && <p role="alert" style={{ color: "var(--vscode-errorForeground, #f48771)", whiteSpace: "pre-wrap" }}>{state.error}</p>}
    {plan && <div role="status" style={{ marginTop: 10 }}>
      <p>{plan.message}</p>
      <div>계정: {plan.account_id}</div>
      <div>역할: <code>{plan.role_arn}</code></div>
      {plan.status === "missing" && <>
        <div style={{ marginTop: 6 }}>연결할 정책: <code>AmazonECSTaskExecutionRolePolicy</code></div>
        <p>현재 연결된 AWS 계정에 위 역할을 생성하고 ECR 이미지 다운로드·로그 전송 정책을 연결합니다. ECS 태스크만 이 역할을 사용할 수 있으며 현재 계정으로 제한합니다.</p>
        <details><summary>변경 내용 보기</summary>
          <p>실행 주체: <code>{plan.caller_arn}</code></p>
          <p>정책 ARN: <code>{plan.policy_arn}</code></p>
          <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(plan.trust_policy, null, 2)}</pre>
          <p>필요 권한: iam:CreateRole, iam:AttachRolePolicy. 제안은 10분 후 만료됩니다.</p>
        </details>
        <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
          <button style={button} disabled={disabled || busy} onClick={onApprove}>
            {state.operation === "apply" ? "역할 생성 중…" : "승인하고 역할 생성 (Level 4)"}
          </button>
          <button style={button} disabled={busy} onClick={onCancel}>취소</button>
        </div>
      </>}
    </div>}
  </section>;
}

export const EcsExecutionRole: React.FC<{ region: string; disabled?: boolean }> = ({ region, disabled }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [state, dispatch] = useReducer(executionRoleReducer, initialExecutionRole);
  const pending = useRef("");
  const prefix = useRef(`execution-role-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  const sequence = useRef(0);
  useMessage(useCallback(({ type, payload }) => {
    if (type !== "aws.executionRole.result" && type !== "aws.executionRole.error") return;
    const p = payload as { requestId?: string; result: ExecutionRolePlan; message?: string };
    if (!p?.requestId || p.requestId !== pending.current) return;
    pending.current = "";
    if (type === "aws.executionRole.error") dispatch({ type: "error", requestId: p.requestId, message: p.message ?? "역할 설정에 실패했습니다." });
    else dispatch({ type: "result", requestId: p.requestId, plan: p.result });
  }, []));
  const start = (operation: "preview" | "apply") => {
    if (disabled || pending.current || !region.trim()) return;
    if (operation === "apply" && (state.plan?.status !== "missing" || !state.plan.proposal_id)) return;
    const requestId = `${prefix.current}-${++sequence.current}`;
    pending.current = requestId;
    dispatch({ type: "start", requestId, operation });
    postMessage(`aws.executionRole.${operation}`, operation === "preview"
      ? { requestId, region: region.trim() }
      : { requestId, proposalId: state.plan!.proposal_id, approved: true });
  };
  return <ExecutionRoleView state={state} disabled={disabled || !region.trim()}
    onPreview={() => start("preview")} onApprove={() => start("apply")}
    onCancel={() => dispatch({ type: "cancel" })} />;
};
