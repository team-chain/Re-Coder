import React from "react";

export type RuntimeDraft = { environment: string; secrets: string };
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
  return { env_vars, secret_refs };
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
  </fieldset>;
}
