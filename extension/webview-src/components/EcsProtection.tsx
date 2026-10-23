import React from "react";
import { Checkbox, Switch } from "./Check";
import { Box, Collapse, Section, Segmented, SettingRow } from "./ui/kit";

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

export const ALB_COST =
  "로드밸런서는 켜 두는 시간만큼 + 처리량만큼 나옵니다(트래픽이 거의 없어도 us-east-1 기준 대략 월 18달러 이상). 그만 쓰면 운영 화면의 「AWS 리소스 정리」로 지우세요.";

export const SHIELD_LEVELS: Array<{ value: ShieldLevel; label: string; rate: number; note: string }> = [
  { value: "loose", label: "느슨", rate: 50, note: "학교·회사처럼 한 IP 뒤에 사람이 많을 때" },
  { value: "normal", label: "보통", rate: 20, note: "기본값" },
  { value: "strict", label: "엄격", rate: 5, note: "공격을 받는 중일 때" },
];

/** 요금 안내 — 한 줄만 보이고, 자세한 설명은 「자세히」 안에. */
function CostNote({ short, full, testId }: { short: string; full: string; testId?: string }) {
  return (
    <details className="rk-costnote" data-testid={testId}>
      <summary><span>{short}</span><em>자세히</em></summary>
      <p>{full}</p>
    </details>
  );
}

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

/** 켜 둔 유료 항목 이름(접힌 줄에 보인다). */
export function paidSummary(p: Protection): string[] {
  const on: string[] = [];
  if (p.https) on.push("HTTPS 주소");
  if ((p.edge ?? "none") !== "none") on.push("대규모 방패");
  if (p.database === "rds") on.push("관리형 DB");
  return on;
}

/** 배포 요약에 보일 대략의 월 추가 요금(USD, 표시용). 켠 것이 없으면 0. */
export function monthlyExtra(p: Protection): number {
  //: 대규모 방패(로드밸런서 + WAF) 약 $25, HTTPS 만이면 로드밸런서만 약 $18.
  const lb = (p.edge ?? "none") !== "none" ? 25 : p.https ? 18 : 0;
  return lb + (p.database === "rds" ? 15 : 0);
}

const help = "rk-help";

/**
 * 보호 · 운영(2.0.46 새 모양) — 기본 방패·감지 알림은 늘 보이는 두 줄, 요금이 드는 셋(HTTPS·대규모 방패·관리형 DB)은
 * 한 줄로 접어 둔다. 하나라도 켜져 있으면 펼친 채로 보인다.
 */
export function EcsProtection({ value, onChange, disabled = false }: {
  value: Protection; onChange: (p: Protection) => void; disabled?: boolean;
}) {
  const set = <K extends keyof Protection>(k: K, v: Protection[K]) => onChange({ ...value, [k]: v });
  const paid = paidSummary(value);
  const edgeOn = (value.edge ?? "none") !== "none";
  return (
    <div data-testid="ecs-protection" aria-disabled={disabled || undefined}>
      <Section title="보호">
        <Box>
          <SettingRow icon="shield" title="기본 방패" desc="한 IP 의 과도한 요청을 429 로 막음"
            trailing={<Switch checked={value.shield} disabled={disabled} onChange={v => set("shield", v)} ariaLabel="기본 방패" />}>
            {value.shield && <>
              <Segmented name="shield-level" label="방패 강도" value={value.shield_level} disabled={disabled} onChange={v => set("shield_level", v)}
                options={SHIELD_LEVELS.map(l => ({ value: l.value, label: <>{l.label} <span style={{ opacity: .7 }}>{l.rate}/초</span></>, title: `IP당 초당 ${l.rate}회 · ${l.note}` }))} />
              <p className={help}>{SHIELD_LEVELS.find(l => l.value === value.shield_level)?.note} · IP당 초당 {SHIELD_LEVELS.find(l => l.value === value.shield_level)?.rate}회. 앱이 PORT 환경변수로 포트를 받을 때만 켜집니다.</p>
            </>}
          </SettingRow>
          <SettingRow icon="bell" title="공격·장애 감지 알림" desc="요청·차단 급증 · 오류 증가 · 태스크 부족"
            trailing={<Switch checked={value.alerts} disabled={disabled} onChange={v => set("alerts", v)} ariaLabel="공격·장애 감지 알림" />}>
            {value.alerts && <>
              <input className="rk-input" type="email" value={value.alert_email} disabled={disabled} aria-label="알림 메일"
                placeholder="알림 메일 (선택) — PC 가 꺼져 있어도 받습니다" onChange={e => set("alert_email", e.target.value)} />
              {value.alert_email.trim() && <p className={help}>배포 뒤 구독 확인 메일의 링크를 한 번 눌러야 합니다.</p>}
            </>}
          </SettingRow>
        </Box>
        <Collapse testId="ecs-paid" open={paid.length > 0} summary={paid.length ? `켜짐: ${paid.join(" · ")}` : "HTTPS 주소 · 대규모 방패 · 관리형 DB"} aside="매달 요금">
          <div data-testid="ecs-https">
            <SettingRow icon="lock" title="HTTPS 주소" desc="CloudFront · https://…cloudfront.net 주소, 내 도메인도 연결" cost="월 약 $18+"
              trailing={<Switch checked={!!value.https} disabled={disabled} onChange={v => set("https", v)} ariaLabel="HTTPS 주소" />}>
              {value.https && <>
                <input className="rk-input" value={value.custom_domain ?? ""} disabled={disabled} aria-label="내 도메인"
                  placeholder="내 도메인 (선택) · shop.example.com" onChange={e => set("custom_domain", e.target.value)} />
                <p className={help} data-testid="ecs-https-guard">인증서가 나온 뒤 다시 배포하면 도메인이 붙습니다 · CloudFront 를 거친 요청만 받음(비밀 머리글로 잠금).
                  {value.edge === "alb_waf" ? " WAF 는 CloudFront 에 붙여 실제 사용자 IP 로 셉니다." : ""}</p>
                {!edgeOn && <>
                  <CostNote short="로드밸런서만(WAF 없이) 함께 켬 · 월 약 $18+" full={ALB_COST} />
                  <Checkbox checked={!!value.edge_cost_approved} disabled={disabled} tone="warn" onChange={v => set("edge_cost_approved", v)} label="비용이 드는 것을 확인했습니다" />
                </>}
              </>}
            </SettingRow>
          </div>
          <div data-testid="ecs-edge">
            <SettingRow icon="wall" title="대규모 방패" desc="로드밸런서 + AWS WAF · 초당 수천 건도 앱 앞에서 막음" cost="월 약 $25+"
              trailing={<Switch checked={edgeOn} disabled={disabled} onChange={v => onChange({ ...value, edge: v ? "alb_waf" : "none", edge_cost_approved: false })} ariaLabel="대규모 방패" />}>
              {edgeOn && <>
                <label className={help} style={{ display: "flex", alignItems: "center", gap: 8 }}>IP 하나가 5분 동안 보낼 수 있는 요청
                  <select className="rk-input" style={{ width: "auto", height: 30 }} value={value.waf_rate_limit || 2000} disabled={disabled} onChange={e => set("waf_rate_limit", Number(e.target.value))}>
                    {WAF_LIMITS.map(n => <option key={n} value={n}>{n.toLocaleString()}회{n === 2000 ? " (기본)" : ""}</option>)}
                  </select></label>
                <CostNote testId="edge-cost" short="AWS 관리 규칙도 함께 켬 · 월 약 $25+" full={`주소가 로드밸런서 주소로 바뀌고 AWS 관리 규칙(일반 공격 패턴)도 함께 켭니다. ${EDGE_COST}`} />
                <Checkbox checked={!!value.edge_cost_approved} disabled={disabled} tone="warn" onChange={v => set("edge_cost_approved", v)} label="비용이 드는 것을 확인했습니다" />
              </>}
            </SettingRow>
          </div>
          <div data-testid="ecs-database">
            <SettingRow icon="database" title="관리형 DB" desc="RDS PostgreSQL · 다시 배포해도 데이터가 남음" cost="월 약 $15+"
              trailing={<Switch checked={value.database === "rds"} disabled={disabled} onChange={v => onChange({ ...value, database: v ? "rds" : "task", db_cost_approved: false })} ariaLabel="관리형 DB" />}>
              {value.database === "rds" && <>
                <CostNote testId="db-cost" short="이 앱에서만 접속 · schema.sql 로 테이블 생성 · 처음 5~10분" full={DB_COST} />
                <Checkbox checked={!!value.db_cost_approved} disabled={disabled} tone="warn" onChange={v => set("db_cost_approved", v)} label="비용이 드는 것을 확인했습니다" />
              </>}
            </SettingRow>
          </div>
        </Collapse>
      </Section>
    </div>
  );
}
