import React from "react";
import { Checkbox } from "./Check";

/**
 * ECS 배포 — 보호·운영 설정(2.0.25~).
 *  · 기본 방패: 앱 앞 nginx 가 IP 당 요청 수를 제한한다(앱이 PORT 환경변수를 읽을 때만 실제로 켜진다 — 못 켜면 결과에 이유).
 *  · 공격·장애 감지 알림: 요청 급증·차단 급증·오류 증가·태스크 부족을 VS Code·디스코드로, 메일을 넣으면 PC 가 꺼져 있어도.
 */
export type ShieldLevel = "loose" | "normal" | "strict";
export interface Protection {
  shield: boolean;
  shield_level: ShieldLevel;
  alerts: boolean;
  alert_email: string;
}

export const DEFAULT_PROTECTION: Protection = { shield: true, shield_level: "normal", alerts: true, alert_email: "" };

export const SHIELD_LEVELS: Array<{ value: ShieldLevel; label: string; rate: number; note: string }> = [
  { value: "loose", label: "느슨", rate: 50, note: "학교·회사처럼 한 IP 뒤에 사람이 많을 때" },
  { value: "normal", label: "보통", rate: 20, note: "기본값" },
  { value: "strict", label: "엄격", rate: 5, note: "공격을 받는 중일 때" },
];

const EMAIL = /^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$/;

/** 배포 요청에 넣을 값. 메일 형식이 틀리면 배포 전에 알린다. */
export function protectionRequest(p: Protection): Record<string, unknown> {
  const email = p.alert_email.trim();
  if (p.alerts && email && !EMAIL.test(email)) throw new Error("알림 메일 주소 형식이 올바르지 않습니다.");
  return { shield: p.shield, shield_level: p.shield_level, alerts: p.alerts, alert_email: p.alerts ? email : "" };
}

const muted: React.CSSProperties = { color: "var(--vscode-descriptionForeground, #999)", fontSize: 11, lineHeight: 1.5 };

export function EcsProtection({ value, onChange, disabled = false }: {
  value: Protection; onChange: (p: Protection) => void; disabled?: boolean;
}) {
  const set = <K extends keyof Protection>(k: K, v: Protection[K]) => onChange({ ...value, [k]: v });
  return (
    <fieldset disabled={disabled} data-testid="ecs-protection" style={{ marginTop: 12, minWidth: 0 }}>
      <legend>보호 · 운영</legend>
      <Checkbox checked={value.shield} disabled={disabled} onChange={v => set("shield", v)}
        label={<b>기본 방패</b>} description="앱 앞에서 한 IP 의 과도한 요청을 429 로 막습니다" />
      {value.shield && (
        <div role="radiogroup" aria-label="방패 강도" style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0,1fr))", gap: 6, margin: "6px 0 4px 24px" }}>
          {SHIELD_LEVELS.map(l => (
            <label key={l.value} data-level={l.value} style={{
              border: `1px solid ${value.shield_level === l.value ? "var(--vscode-focusBorder, #3794ff)" : "var(--vscode-panel-border, #3f3f3f)"}`,
              borderRadius: 6, padding: "6px 8px", cursor: "pointer", fontSize: 12 }}>
              <input type="radio" name="shield-level" value={l.value} checked={value.shield_level === l.value}
                onChange={() => set("shield_level", l.value)} style={{ marginRight: 6 }} />
              {l.label} <span style={muted}>· IP당 초당 {l.rate}회</span>
              <div style={muted}>{l.note}</div>
            </label>
          ))}
        </div>
      )}
      {value.shield && <p style={{ ...muted, margin: "2px 0 8px 24px" }}>앱이 포트를 PORT 환경변수로 받을 때만 켜집니다. 못 켜면 배포 결과에 이유를 알려 드려요. 초당 수만 건 규모는 대규모 방패(ALB + WAF)가 필요합니다.</p>}
      <Checkbox checked={value.alerts} disabled={disabled} onChange={v => set("alerts", v)}
        label={<b>공격·장애 감지 알림</b>} description="요청 급증 · 차단 급증 · 오류 증가 · 태스크 부족" />
      {value.alerts && (
        <label style={{ display: "block", margin: "4px 0 0 24px", fontSize: 11 }}>
          알림 메일(선택) — 넣으면 PC 가 꺼져 있어도 AWS 가 메일을 보냅니다
          <input type="email" value={value.alert_email} placeholder="me@example.com" onChange={e => set("alert_email", e.target.value)}
            style={{ display: "block", width: "100%", boxSizing: "border-box", marginTop: 4 }} />
          <span style={muted}>배포 뒤 구독 확인 메일의 링크를 한 번 눌러야 합니다.</span>
        </label>
      )}
    </fieldset>
  );
}
