import React from "react";
import { Checkbox } from "./Check";

/**
 * ECS 배포 — 보호·운영 설정(2.0.25~).
 *  · 기본 방패: 앱 앞 nginx 가 IP 당 요청 수를 제한한다(앱이 PORT 환경변수를 읽을 때만 실제로 켜진다 — 못 켜면 결과에 이유).
 *  · 공격·장애 감지 알림: 요청 급증·차단 급증·오류 증가·태스크 부족을 VS Code·디스코드로, 메일을 넣으면 PC 가 꺼져 있어도.
 */
export type ShieldLevel = "loose" | "normal" | "strict";
export type Edge = "none" | "alb" | "alb_waf";
export interface Protection {
  shield: boolean;
  shield_level: ShieldLevel;
  alerts: boolean;
  alert_email: string;
  /** 2.0.26 대규모 방패(로드밸런서 + AWS WAF) — 켜 두기만 해도 매달 요금, 비용 확인 필요 */
  edge: Edge;
  waf_rate_limit: number;
  edge_cost_approved: boolean;
  /** 2.0.27 HTTPS 주소(CloudFront) · 내 도메인 · 관리형 DB(RDS) */
  https: boolean;
  custom_domain: string;
  database: "task" | "rds";
  db_cost_approved: boolean;
}

export const DEFAULT_PROTECTION: Protection = {
  shield: true, shield_level: "normal", alerts: true, alert_email: "",
  edge: "none", waf_rate_limit: 2000, edge_cost_approved: false,
  https: false, custom_domain: "", database: "task", db_cost_approved: false,
};
export const DB_COST =
  "관리형 DB(RDS)는 켜 두는 동안 계속 요금이 나옵니다(db.t4g.micro + 20GB, us-east-1 기준 대략 월 15달러 이상 · 새 계정 1년 무료 사용량 있음). 그만 쓰면 「AWS 리소스 정리」로 지우세요(최종 스냅숏을 남깁니다).";
const DOMAIN = /^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.){1,8}[a-z]{2,63}$/;

/** WAF 한도(5분 동안 IP 하나가 보낼 수 있는 요청 수). */
export const WAF_LIMITS = [500, 1000, 2000, 5000];
export const EDGE_COST =
  "로드밸런서는 켜 두는 시간만큼 + 처리량만큼, WAF 는 매달 고정 요금 + 요청 수만큼 나옵니다(트래픽이 거의 없어도 us-east-1 기준 대략 월 25달러 이상). 그만 쓰면 운영 화면의 「AWS 리소스 정리」로 지우세요.";

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
  //: HTTPS 주소는 로드밸런서 앞에 둔다 — 대규모 방패를 안 켰으면 로드밸런서만 켠다.
  const edge = p.edge && p.edge !== "none" ? p.edge : p.https ? "alb" : "none";
  if (edge !== "none" && !p.edge_cost_approved) throw new Error(p.https && (p.edge ?? "none") === "none"
    ? "HTTPS 주소는 로드밸런서·CloudFront 요금이 나옵니다 — 비용 확인에 체크하세요."
    : "대규모 방패는 매달 요금이 나옵니다 — 비용 확인에 체크하세요.");
  const domain = (p.custom_domain ?? "").trim().toLowerCase();
  if (p.https && domain && !DOMAIN.test(domain)) throw new Error("도메인 형식이 올바르지 않습니다(예: shop.example.com).");
  if (p.database === "rds" && !p.db_cost_approved) throw new Error("관리형 DB 는 매달 요금이 나옵니다 — 비용 확인에 체크하세요.");
  const out: Record<string, unknown> = { shield: p.shield, shield_level: p.shield_level, alerts: p.alerts, alert_email: p.alerts ? email : "" };
  if (edge !== "none") {
    out.edge = edge;
    out.waf_rate_limit = p.waf_rate_limit || 2000;
    out.waf_window = 300;
    out.edge_cost_approved = true;
  }
  if (p.https) {
    out.https = true;
    if (domain) out.custom_domain = domain;
  }
  if (p.database === "rds") {
    out.database = "rds";
    out.db_cost_approved = true;
  }
  return out;
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
      <div style={{ marginTop: 10 }} data-testid="ecs-edge">
        <Checkbox checked={(value.edge ?? "none") !== "none"} disabled={disabled}
          onChange={v => onChange({ ...value, edge: v ? "alb_waf" : "none", edge_cost_approved: false })}
          label={<b>대규모 방패 (로드밸런서 + AWS WAF)</b>}
          description="초당 수천 건 이상 몰려도 앱까지 오기 전에 막습니다 · 매달 요금이 나옵니다" />
        {(value.edge ?? "none") !== "none" && (
          <div style={{ margin: "4px 0 0 24px", fontSize: 12 }}>
            <label style={{ display: "block" }}>
              IP 하나가 5분 동안 보낼 수 있는 요청
              <select value={value.waf_rate_limit || 2000} onChange={e => set("waf_rate_limit", Number(e.target.value))}
                style={{ marginLeft: 6 }}>
                {WAF_LIMITS.map(n => <option key={n} value={n}>{n.toLocaleString()}회{n === 2000 ? " (기본)" : ""}</option>)}
              </select>
            </label>
            <p style={{ ...muted, margin: "4px 0" }}>앱은 로드밸런서에서 오는 연결만 받게 바뀌고, 주소가 로드밸런서 주소(http://…elb.amazonaws.com)로 바뀝니다. AWS 관리 규칙(일반 공격 패턴·알려진 악성 입력)도 함께 켭니다.</p>
            <p style={{ ...muted, margin: "4px 0" }} data-testid="edge-cost">{EDGE_COST}</p>
            <Checkbox checked={!!value.edge_cost_approved} disabled={disabled} tone="warn"
              onChange={v => set("edge_cost_approved", v)} label="비용이 드는 것을 확인했습니다" />
          </div>
        )}
      </div>
      <div style={{ marginTop: 10 }} data-testid="ecs-https">
        <Checkbox checked={!!value.https} disabled={disabled} onChange={v => set("https", v)}
          label={<b>HTTPS 주소 (CloudFront)</b>}
          description="https://…cloudfront.net 주소를 바로 받습니다 · 로드밸런서가 함께 켜집니다" />
        {value.https && (
          <label style={{ display: "block", margin: "4px 0 0 24px", fontSize: 11 }}>
            내 도메인(선택) — 넣으면 인증서를 요청하고, 발급된 뒤 다시 배포하면 연결됩니다
            <input value={value.custom_domain ?? ""} placeholder="shop.example.com" onChange={e => set("custom_domain", e.target.value)}
              style={{ display: "block", width: "100%", boxSizing: "border-box", marginTop: 4 }} />
            <span style={muted}>Route 53 에 그 도메인이 있으면 레코드를 자동으로 넣고, 없으면 넣을 레코드를 배포 결과에 알려 드려요.</span>
          </label>
        )}
        {value.https && (
          <p style={{ ...muted, margin: "4px 0 0 24px" }} data-testid="ecs-https-guard">
            로드밸런서는 CloudFront 를 거친 요청만 받습니다(비밀 머리글로 잠금).
            {value.edge === "alb_waf" ? " WAF 는 CloudFront 에 붙여 실제 사용자 IP 로 셉니다." : ""}
          </p>
        )}
        {(value.edge ?? "none") === "none" && value.https && (
          <div style={{ margin: "4px 0 0 24px" }}>
            <p style={{ ...muted, margin: "4px 0" }}>HTTPS 주소를 위해 로드밸런서만(WAF 없이) 켭니다. {EDGE_COST}</p>
            <Checkbox checked={!!value.edge_cost_approved} disabled={disabled} tone="warn"
              onChange={v => set("edge_cost_approved", v)} label="비용이 드는 것을 확인했습니다" />
          </div>
        )}
      </div>
      <div style={{ marginTop: 10 }} data-testid="ecs-database">
        <Checkbox checked={value.database === "rds"} disabled={disabled}
          onChange={v => onChange({ ...value, database: v ? "rds" : "task", db_cost_approved: false })}
          label={<b>관리형 DB (RDS PostgreSQL)</b>}
          description="태스크가 바뀌어도 데이터가 남습니다 · 처음 만드는 데 5~10분 · 매달 요금" />
        {value.database === "rds" && (
          <div style={{ margin: "4px 0 0 24px" }}>
            <p style={{ ...muted, margin: "4px 0" }}>인터넷에서는 접속할 수 없고 이 앱의 태스크에서만 접속합니다. DB 가 비어 있으면 처음 한 번 schema.sql 로 테이블을 만들고, 샘플 데이터는 넣지 않습니다.</p>
            <p style={{ ...muted, margin: "4px 0" }} data-testid="db-cost">{DB_COST}</p>
            <Checkbox checked={!!value.db_cost_approved} disabled={disabled} tone="warn"
              onChange={v => set("db_cost_approved", v)} label="비용이 드는 것을 확인했습니다" />
          </div>
        )}
      </div>
    </fieldset>
  );
}
