/**
 * 2.0.25 공격·장애 감지 알림 — 코어가 실제 지표로 판정한 알림과 대응 제안. 대응은 승인 카드에서 고른 것만 적용한다.
 */
import React, { useCallback, useEffect, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

export interface OpsAlertView {
  id: string; at: string; kind: string; severity: string; title: string; detail: string;
  region: string; cluster: string; service: string; status?: string; resolution?: string;
  sources?: { ips?: Array<{ value: string; count: number }>; paths?: Array<{ value: string; count: number }> };
  suggestions?: Array<{ action: string; value: unknown; label: string; detail: string }>;
}

const box: React.CSSProperties = { border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 8, padding: 12, marginBottom: 10 };
const muted: React.CSSProperties = { color: "var(--vscode-descriptionForeground, #999)", fontSize: 11, lineHeight: 1.5 };
const btn: React.CSSProperties = { padding: "6px 12px", borderRadius: 5, border: "1px solid var(--vscode-button-border, transparent)",
  background: "var(--vscode-button-secondaryBackground, #3a3d41)", color: "var(--vscode-button-secondaryForeground, #fff)", cursor: "pointer", fontSize: 12 };
const primary: React.CSSProperties = { ...btn, background: "var(--vscode-button-background, #0e639c)", color: "var(--vscode-button-foreground, #fff)" };

export function OpsAlertCard({ alert, onChoose }: { alert: OpsAlertView; onChoose: (s: NonNullable<OpsAlertView["suggestions"]>[number]) => void }) {
  const open = (alert.status ?? "open") === "open";
  const tone = alert.severity === "critical" ? "var(--vscode-errorForeground, #f48771)" : "var(--vscode-editorWarning-foreground, #cca700)";
  return (
    <div style={{ ...box, borderColor: open ? tone : "var(--vscode-panel-border, #3f3f3f)" }} data-testid="ops-alert">
      <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
        <b style={{ color: tone }}>{alert.title}</b>
        <span>{alert.service}</span>
        <span style={muted}>{alert.region} · {new Date(alert.at).toLocaleString("ko-KR")}</span>
        {!open && <span style={muted}>· {alert.status === "applied" ? "대응함" : "닫음"}</span>}
      </div>
      <div style={{ marginTop: 4, fontSize: 12 }}>{alert.detail}</div>
      {(alert.sources?.ips?.length || alert.sources?.paths?.length) ? (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 8, fontSize: 11 }}>
          <div><div style={muted}>상위 IP</div>{(alert.sources?.ips ?? []).map(r => <div key={r.value}><code>{r.value}</code> {r.count.toLocaleString()}회</div>)}</div>
          <div><div style={muted}>많이 맞은 경로</div>{(alert.sources?.paths ?? []).map(r => <div key={r.value}><code>{r.value}</code> {r.count.toLocaleString()}회</div>)}</div>
        </div>
      ) : null}
      {alert.resolution && <div style={{ ...muted, marginTop: 6 }}>{alert.resolution}</div>}
      {open && (alert.suggestions?.length ?? 0) > 0 && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 10 }}>
          {alert.suggestions!.map(s => <button key={s.action} style={btn} onClick={() => onChoose(s)}>{s.label}</button>)}
        </div>
      )}
    </div>
  );
}

export const OpsAlertsPanel: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [alerts, setAlerts] = useState<OpsAlertView[]>([]);
  const [monitors, setMonitors] = useState(0);
  const [pending, setPending] = useState<{ alert: OpsAlertView; s: NonNullable<OpsAlertView["suggestions"]>[number] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  const load = useCallback(() => postMessage("ops.alerts.list"), [postMessage]);
  useEffect(() => { load(); const t = window.setInterval(load, 60_000); return () => window.clearInterval(t); }, [load]);
  useMessage(useCallback((event) => {
    const p = (event.payload ?? {}) as { alerts?: OpsAlertView[]; monitors?: unknown[]; message?: string; applied?: boolean };
    if (event.type === "ops.alerts.result") { setAlerts([...(p.alerts ?? [])].reverse()); setMonitors((p.monitors ?? []).length); }
    if (event.type === "ops.alerts.actResult") { setBusy(false); setPending(null); setNote(p.message ?? ""); }
    if (event.type === "ops.alerts.error") { setBusy(false); setNote(p.message ?? "알림을 불러오지 못했습니다"); }
  }, []));

  const decide = (approved: boolean) => {
    if (!pending) return;
    setBusy(true);
    postMessage("ops.alerts.act", { alertId: pending.alert.id, action: pending.s.action, value: pending.s.value, approved });
  };

  return (
    <section data-testid="ops-alerts" style={{ marginBottom: 18 }}>
      <h3 style={{ margin: "0 0 4px" }}>공격·장애 감지 알림</h3>
      <p style={muted}>
        {monitors ? `ECS 서비스 ${monitors}개를 1분마다 봅니다 — 요청 급증 · 차단 급증 · 오류 증가 · 태스크 부족.`
          : "감시 중인 서비스가 없습니다. ECS 배포에서 「공격·장애 감지 알림」을 켜면 여기에 나타납니다."}
        {" "}대응은 승인한 것만 적용합니다.
      </p>
      {note && <div style={{ ...box, fontSize: 12 }}>{note}</div>}
      {pending && (
        <div style={{ ...box, borderColor: "var(--vscode-focusBorder, #3794ff)" }} data-testid="ops-approval">
          <b>승인 필요 — {pending.s.label}</b>
          <div style={{ fontSize: 12, margin: "6px 0" }}>{pending.alert.service} ({pending.alert.region}) · {pending.s.detail}</div>
          <div style={{ display: "flex", gap: 6 }}>
            <button style={primary} disabled={busy} onClick={() => decide(true)}>{busy ? "적용 중…" : "승인하고 적용"}</button>
            <button style={btn} disabled={busy} onClick={() => decide(false)}>적용 안 함</button>
          </div>
        </div>
      )}
      {alerts.length === 0 ? <div style={muted}>아직 알림이 없습니다.</div>
        : alerts.slice(0, 20).map(a => <OpsAlertCard key={a.id} alert={a} onChoose={s => { setNote(""); setPending({ alert: a, s }); }} />)}
    </section>
  );
};
