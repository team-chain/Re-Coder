import React from "react";

export type RuntimeDraft = { environment: string; secrets: string; network?: string };
export function parseRuntimeEnvironment(draft: RuntimeDraft) {
  const parse = (text: string, label: string): Record<string, string> => {
    let value: unknown;
    try { value = JSON.parse(text.trim() || "{}"); }
    catch { throw new Error(`${label}: JSON 객체를 입력하세요.`); }
    if (!value || Array.isArray(value) || typeof value !== "object"
      || Object.entries(value).some(([key, item]) => !/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) || typeof item !== "string")) {
      throw new Error(`${label}: 환경변수 이름과 문자열 값으로 입력하세요.`);
    }
    return value as Record<string, string>;
  };
  const env_vars = parse(draft.environment, "일반 환경변수");
  const secret_refs = parse(draft.secrets, "비밀값 참조");
  for (const [name, arn] of Object.entries(secret_refs)) {
    if (!/^arn:(aws|aws-us-gov|aws-cn):(secretsmanager|ssm):[a-z0-9-]+:[0-9]{12}:(secret:|parameter\/)[^\s]+$/.test(arn)) {
      throw new Error(`${name}: 비밀값 대신 Secrets Manager 또는 SSM ARN을 입력하세요.`);
    }
    if (name === "PORT" || name === "ENVIRONMENT" || Object.prototype.hasOwnProperty.call(env_vars, name)) {
      throw new Error(`${name}: 일반 환경변수나 예약된 변수와 중복됩니다.`);
    }
  }
  let network: {target_group_arn?: string; cloudfront_domain?: string; assign_public_ip?: boolean; subnet_ids?: string[]; security_group_ids?: string[]} = {};
  try { network = JSON.parse(draft.network?.trim() || "{}"); }
  catch { throw new Error("네트워크·HTTPS: JSON 객체를 입력하세요."); }
  const allowed = ['target_group_arn', 'cloudfront_domain', 'assign_public_ip', 'subnet_ids', 'security_group_ids'];
  if (!network || Array.isArray(network) || typeof network !== 'object' || Object.keys(network).some(k => !allowed.includes(k))) throw new Error("네트워크·HTTPS: 지원하는 설정만 입력하세요.");
  if (network.cloudfront_domain && !/^[a-z0-9-]+\.cloudfront\.net$/.test(network.cloudfront_domain)) throw new Error("CloudFront 도메인을 입력하세요 (https:// 제외).");
  if (network.target_group_arn && !/^arn:(aws|aws-us-gov|aws-cn):elasticloadbalancing:[a-z0-9-]+:[0-9]{12}:targetgroup\/[A-Za-z0-9-]+\/[a-f0-9]+$/.test(network.target_group_arn)) throw new Error("ALB 대상 그룹 ARN을 입력하세요.");
  if (network.assign_public_ip !== undefined && typeof network.assign_public_ip !== 'boolean') throw new Error("assign_public_ip는 true/false여야 합니다.");
  for (const key of ['subnet_ids','security_group_ids'] as const) {
    const ids = network[key];
    if (ids !== undefined && (!Array.isArray(ids) || !ids.every(id => typeof id === 'string' && (key === 'subnet_ids' ? /^subnet-[a-f0-9]+$/ : /^sg-[a-f0-9]+$/).test(id)))) throw new Error("올바른 서브넷·보안 그룹 ID 배열을 입력하세요.");
  }
  return { env_vars, secret_refs, ...network };
}

export function EcsRuntimeEnvironment({ value, onChange, disabled = false }: {
  value: RuntimeDraft; onChange: (draft: RuntimeDraft) => void; disabled?: boolean;
}) {
  return <fieldset disabled={disabled} style={{ marginTop: 12, minWidth: 0 }}>
    <legend>앱 실행 설정</legend>
    <p>비밀번호·API 키는 AWS에 저장하고 아래에는 ARN만 입력하세요. 실행 역할에 해당 비밀값을 읽는 권한이 필요합니다.</p>
    <label>일반 환경변수 (JSON)<textarea aria-label="일반 환경변수 JSON" rows={3} style={{ width: "100%", boxSizing: "border-box" }}
      placeholder={'{"NODE_ENV":"production"}'} value={value.environment} onChange={e => onChange({ ...value, environment: e.target.value })} /></label>
    <label>비밀값 참조 (JSON)<textarea aria-label="비밀값 참조 JSON" rows={3} style={{ width: "100%", boxSizing: "border-box" }}
      placeholder={'{"DATABASE_URL":"arn:aws:secretsmanager:리전:계정:secret:이름"}'} value={value.secrets} onChange={e => onChange({ ...value, secrets: e.target.value })} /></label>
    <details><summary>네트워크·HTTPS</summary>
      <p>HTTPS 인프라의 대상 그룹 ARN, CloudFront 도메인, 서브넷·보안 그룹 ID를 입력하세요. 공인 IP를 끄면 이미지 다운로드와 로그 전송용 NAT 또는 VPC 엔드포인트가 필요합니다.</p>
      <textarea aria-label="네트워크 HTTPS JSON" rows={5} style={{ width: "100%", boxSizing: "border-box" }} value={value.network || ""}
        placeholder={'{"target_group_arn":"arn:aws:elasticloadbalancing:...","cloudfront_domain":"d123.cloudfront.net","subnet_ids":["subnet-..."],"security_group_ids":["sg-..."],"assign_public_ip":true}'}
        onChange={e => onChange({ ...value, network: e.target.value })} />
    </details>
  </fieldset>;
}
