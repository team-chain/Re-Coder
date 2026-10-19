/**
 * 배포 전 신뢰 점검(제로 트러스트, 2.0.35) — 승인 카드에 원칙 다섯 개로 묶어 보여 준다.
 * 이 점검은 배포를 새로 막지 않는다. "막힘"은 이미 배포를 막는 검사(Dockerfile 안전 검사)의 결과다.
 */
import React from "react";

export interface TrustItem { key: string; principle: string; status: "pass" | "warn" | "block" | "info"; title: string; detail: string; fix: string }
export interface TrustPrinciple { key: string; label: string; summary: string; status: "pass" | "warn" | "block"; items: TrustItem[] }
export interface TrustReport { target: string; principles: TrustPrinciple[]; counts: Record<"pass" | "warn" | "block" | "info", number>; scanned_files: number; truncated: boolean; note?: string }

const TONE: Record<string, { icon: string; color: string; label: string }> = {
  pass: { icon: "✓", color: "var(--vscode-testing-iconPassed, #3ddc97)", label: "통과" },
  info: { icon: "i", color: "var(--vscode-descriptionForeground, #93a8bc)", label: "참고" },
  warn: { icon: "!", color: "var(--vscode-editorWarning-foreground, #f7b955)", label: "주의" },
  block: { icon: "✕", color: "var(--vscode-editorError-foreground, #ff6573)", label: "막힘" },
};

const Mark: React.FC<{ status: string }> = ({ status }) => {
  const t = TONE[status] ?? TONE.info;
  return (
    <span aria-label={t.label} title={t.label} style={{
      display: "inline-flex", alignItems: "center", justifyContent: "center", width: 15, height: 15, flex: "0 0 15px",
      borderRadius: "50%", fontSize: 9.5, fontWeight: 800, color: "#10141a", background: t.color,
    }}>{t.icon}</span>
  );
};

export const TrustCheck: React.FC<{ report?: TrustReport | null; defaultOpen?: boolean }> = ({ report, defaultOpen }) => {
  if (!report || !report.principles?.length) return null;
  const { pass = 0, warn = 0, block = 0 } = report.counts || ({} as TrustReport["counts"]);
  const open = defaultOpen ?? (warn + block > 0);
  return (
    <section data-testid="trust-check" aria-label="제로 트러스트 점검" style={{
      border: "1px solid var(--vscode-panel-border, #3c3c3c)", borderRadius: 6, padding: "8px 10px", margin: "8px 0", fontSize: 11.5, lineHeight: 1.5,
    }}>
      <details open={open}>
        <summary style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <b>제로 트러스트 점검</b>
          <span style={{ color: TONE.pass.color }}>통과 {pass}</span>
          <span style={{ color: TONE.warn.color }}>주의 {warn}</span>
          {block > 0 && <span style={{ color: TONE.block.color }}>막힘 {block}</span>}
          <span style={{ color: "var(--vscode-descriptionForeground)", fontSize: 10.5 }}>— 이 점검은 배포를 새로 막지 않습니다</span>
        </summary>
        <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 6 }}>
          {report.principles.map((p) => (
            <div key={p.key} data-trust-principle={p.key} data-status={p.status}>
              <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                <Mark status={p.status} />
                <b>{p.label}</b>
              </div>
              <ul style={{ listStyle: "none", margin: "3px 0 0", paddingLeft: 22 }}>
                {p.items.map((i) => (
                  <li key={i.key} data-trust-item={i.key} data-status={i.status} style={{ display: "flex", gap: 6, alignItems: "flex-start", marginTop: 2 }}>
                    <Mark status={i.status} />
                    <div style={{ minWidth: 0 }}>
                      <div>{i.title}</div>
                      {i.detail && <div style={{ color: "var(--vscode-descriptionForeground)", fontSize: 10.5, wordBreak: "break-word" }}>{i.detail}</div>}
                      {i.fix && i.status !== "pass" && <div style={{ color: TONE.pass.color, fontSize: 10.5 }}>→ {i.fix}</div>}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {report.note && <div style={{ color: "var(--vscode-descriptionForeground)", fontSize: 10.5 }}>{report.note}</div>}
        </div>
      </details>
    </section>
  );
};

export default TrustCheck;
