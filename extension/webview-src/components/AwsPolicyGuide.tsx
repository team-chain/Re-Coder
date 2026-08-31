/**
 * AWS 최소권한 권한표 — 콘솔에 복붙할 정책 JSON 과 따라 할 순서.
 *
 * 왜 이 컴포넌트가 필요한가
 *   코어에는 `GET /api/aws/policy` 가 오래전부터 있었다. 계정 ID·리전·클러스터
 *   이름까지 채워서 그대로 붙여넣을 수 있는 정책을 만들어 준다. 그런데
 *   **확장이 그 엔드포인트를 한 번도 부르지 않았다.** 저장소 전체에서 호출부가
 *   0건이었다.
 *
 *   그래서 사용자가 실제로 겪는 건 이렇다 — 배포를 누르면 "권한이 없습니다"
 *   라고만 나온다. 무엇을 허용해야 하는지는 어디에도 없다. 답은 제품 안에
 *   이미 있었는데 화면에 붙어 있지 않아서, 사용자는 AWS 문서를 뒤지거나
 *   AdministratorAccess 를 붙이는 쪽으로 간다. 후자가 훨씬 흔하다.
 */
import React, { useCallback, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

export type AwsPolicy = {
  policy_json: string;
  targets?: string[];
  action_count?: number;
  needs_manual_fill?: boolean;
  account_id?: string;
  region?: string;
  cluster?: string;
  service?: string;
  ecr_repo?: string;
  is_academy_account?: boolean;
  steps?: string[];
};

type PolicyTarget = "ecs" | "s3" | "bedrock";

const POLICY_TARGET_OPTIONS: Array<{ value: PolicyTarget; label: string }> = [
  { value: "s3", label: "S3 정적 배포" },
  { value: "ecs", label: "ECS 배포" },
  { value: "bedrock", label: "Bedrock AI" },
];

/**
 * 사용자가 이 정책을 **그대로 붙여도 되는지** 한 줄로 말해 준다.
 *
 * `needs_manual_fill` 을 조용히 넘기면 사용자는 자리표시자가 남은 정책을
 * 그대로 붙이고, 나중에 "정책을 붙였는데도 권한이 없다"는 상태에 빠진다.
 * 그 시점에는 원인이 자리표시자라는 걸 알아채기 매우 어렵다.
 */
export function describePolicyFill(policy: AwsPolicy | null): {
  blocking: boolean;
  message: string;
} {
  if (!policy) {
    return { blocking: false, message: "" };
  }
  if (policy.needs_manual_fill) {
    return {
      blocking: true,
      message:
        "계정 ID·리전 자리에 자리표시자가 남아 있습니다. 붙여 넣기 전에 " +
        "그 값을 채우세요 — 그대로 붙이면 정책은 저장되지만 권한은 안 생깁니다.",
    };
  }
  const where = [
    policy.account_id ? `계정 ${policy.account_id}` : "",
    policy.region ? `리전 ${policy.region}` : "",
  ].filter(Boolean).join(" · ");
  return {
    blocking: false,
    message: where
      ? `${where} 기준으로 채워졌습니다. 그대로 붙여 넣으면 됩니다.`
      : "그대로 붙여 넣으면 됩니다.",
  };
}

/** 정책이 허용하는 대상 요약. 무엇에 대한 권한인지 안 보이면 검토가 불가능하다. */
export function describePolicyScope(policy: AwsPolicy | null): string {
  if (!policy) { return ""; }
  const parts: string[] = [];
  if (policy.action_count) { parts.push(`액션 ${policy.action_count}개`); }
  if (policy.targets?.length) { parts.push(policy.targets.join(" · ")); }
  if (policy.cluster) { parts.push(`클러스터 ${policy.cluster}`); }
  if (policy.service) { parts.push(`서비스 ${policy.service}`); }
  if (policy.ecr_repo) { parts.push(`ECR ${policy.ecr_repo}`); }
  return parts.join(" / ");
}

export const AwsPolicyGuide: React.FC<{ region?: string }> = ({ region }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [policy, setPolicy] = useState<AwsPolicy | null>(null);
  // 정책은 선택한 기능에만 한정한다. 비우면 API의 전체 기본값(ECS·S3·Bedrock)을
  // 받아 최소 권한 안내가 오히려 과도해진다.
  const [selectedTargets, setSelectedTargets] = useState<PolicyTarget[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [open, setOpen] = useState(false);

  useMessage(useCallback(({ type, payload }) => {
    if (type === "aws.policy.result") {
      setLoading(false);
      const result = payload as { ok?: boolean; policy?: AwsPolicy; message?: string };
      if (result?.ok && result.policy) {
        setPolicy(result.policy);
        setError("");
        setOpen(true);
      } else {
        setError(result?.message ?? "권한표를 가져오지 못했습니다.");
      }
    }
    if (type === "aws.policy.copied") {
      //: 복사는 확장이 한다. 웹뷰의 navigator.clipboard 는 샌드박스에서
      //: 막히는 경우가 있어, 조용히 아무 일도 안 일어난 것처럼 보인다.
      const result = payload as { ok?: boolean };
      setCopied(Boolean(result?.ok));
      if (!result?.ok) { setError("복사하지 못했습니다. 아래 내용을 직접 선택해 복사하세요."); }
    }
  }, []));

  const request = () => {
    if (!selectedTargets.length) {
      setError("사용할 기능을 하나 이상 고르세요. 고른 기능에 필요한 권한만 만듭니다.");
      return;
    }
    setLoading(true);
    setError("");
    setCopied(false);
    postMessage("aws.policy", { targets: selectedTargets, ...(region ? { region } : {}) });
  };

  const toggleTarget = (target: PolicyTarget) => {
    // AWS identity 조회가 끝나기 전에 선택을 바꾸면 늦게 도착한 정책이
    // 이전 선택 기준인데도 새 체크박스 옆에 표시된다. 요청 중에는 고정한다.
    if (loading) { return; }
    setSelectedTargets(current => current.includes(target)
      ? current.filter(value => value !== target)
      : [...current, target]);
    // 선택을 바꾸면 이전 정책은 더 이상 현재 의도와 맞지 않는다.
    setPolicy(null);
    setOpen(false);
    setCopied(false);
    setError("");
  };

  const fill = describePolicyFill(policy);
  const scope = describePolicyScope(policy);

  const linkButton: React.CSSProperties = {
    border: "none", background: "transparent", padding: 0,
    color: "var(--vscode-textLink-foreground, #75beff)", cursor: "pointer", fontSize: 11,
  };
  const smallButton: React.CSSProperties = {
    border: "none", borderRadius: 5, padding: "6px 10px", fontSize: 11, fontWeight: 650,
    cursor: "pointer", background: "var(--vscode-button-secondaryBackground, #3a3d41)",
    color: "var(--vscode-button-secondaryForeground, #fff)",
  };

  return (
    <div style={{ marginTop: 13, paddingTop: 12, borderTop: "1px solid var(--vscode-panel-border, #3f3f3f)" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <div style={{ fontSize: 12, fontWeight: 650 }}>필요한 권한 (최소권한 정책)</div>
        {policy
          ? <button style={linkButton} onClick={() => setOpen(o => !o)}>{open ? "접기" : "펼치기"}</button>
          : <button style={linkButton} disabled={loading} onClick={request}>{loading ? "가져오는 중…" : "권한표 보기"}</button>}
      </div>

      <div style={{ marginTop: 6, fontSize: 11, lineHeight: 1.55, color: "var(--vscode-descriptionForeground, #999)" }}>
        사용할 기능만 고르면 해당 기능에 필요한 액션만 담습니다. AdministratorAccess 를 붙이지 않아도 됩니다.
      </div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 9 }}>
        {POLICY_TARGET_OPTIONS.map(option => {
          const checked = selectedTargets.includes(option.value);
          return <label key={option.value} style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, cursor: loading ? "wait" : "pointer", opacity: loading ? .65 : 1 }}>
            <input type="checkbox" checked={checked} disabled={loading} onChange={() => toggleTarget(option.value)} />
            {option.label}
          </label>;
        })}
      </div>
      {!selectedTargets.length && <div style={{ marginTop: 6, fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>권한을 넓히지 않도록 필요한 기능만 선택하세요.</div>}

      {error && <div style={{ marginTop: 9, color: "var(--vscode-errorForeground, #f48771)", fontSize: 11, lineHeight: 1.5 }}>{error}</div>}

      {policy && open && <>
        {scope && <div style={{ marginTop: 9, fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>{scope}</div>}

        {fill.message && (
          <div style={{
            marginTop: 9, padding: "8px 10px", borderRadius: 5, fontSize: 11, lineHeight: 1.55,
            background: fill.blocking ? "rgba(245, 180, 0, .12)" : "rgba(78, 201, 176, .10)",
            border: `1px solid ${fill.blocking ? "rgba(245, 180, 0, .35)" : "rgba(78, 201, 176, .32)"}`,
            color: fill.blocking ? "var(--vscode-editorWarning-foreground, #cca700)" : "var(--vscode-charts-green, #4ec9b0)",
          }}>{fill.message}</div>
        )}

        {policy.is_academy_account && (
          <div style={{ marginTop: 9, padding: "8px 10px", borderRadius: 5, fontSize: 11, lineHeight: 1.55, background: "rgba(55,148,255,.10)", border: "1px solid rgba(55,148,255,.30)", color: "var(--vscode-editorInfo-foreground, #75beff)" }}>
            학교(AWS Academy) 계정은 IAM 사용자·정책을 만들 수 없습니다. 아래 정책은 참고용이고,
            실제로는 랩이 주는 임시 자격증명을 그대로 쓰세요.
          </div>
        )}

        {policy.steps?.length ? (
          <ol style={{ margin: "11px 0 0", paddingLeft: 18, fontSize: 11, lineHeight: 1.65, color: "var(--vscode-foreground, #ddd)" }}>
            {policy.steps.map((step, i) => <li key={i} style={{ marginTop: i ? 4 : 0, whiteSpace: "pre-wrap" }}>{step}</li>)}
          </ol>
        ) : null}

        <div style={{ display: "flex", gap: 8, marginTop: 11, alignItems: "center" }}>
          <button style={smallButton} onClick={() => { setCopied(false); postMessage("aws.policy.copy", { text: policy.policy_json }); }}>정책 JSON 복사</button>
          <button style={smallButton} disabled={loading} onClick={request}>{loading ? "다시 가져오는 중…" : "다시 가져오기"}</button>
          {copied && <span style={{ fontSize: 11, color: "var(--vscode-charts-green, #4ec9b0)" }}>복사했습니다</span>}
        </div>

        <pre style={{
          marginTop: 10, maxHeight: 260, overflow: "auto", fontSize: 11, lineHeight: 1.45,
          background: "var(--vscode-textCodeBlock-background, #1e1e1e)", borderRadius: 5, padding: 10,
        }}>{policy.policy_json}</pre>
      </>}
    </div>
  );
};

export default AwsPolicyGuide;
