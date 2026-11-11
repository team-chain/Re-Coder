/**
 * 3.1 계약 대조 — 설계도(architecture.json)와 실제 코드가 같은지.
 *
 * 구조 지도는 "코드가 이렇게 생겼다" 만 보여 줘서 맞는지 알 수 없었다. 계약이 정답이므로 서비스마다 계약의 연결(DB·다른 서비스)이
 * 코드에서 실제로 쓰이는지(초록), 안 쓰이는지(회색), 계약에 없는 연결을 쓰는지(빨강)를 보이고, 근거 파일·줄로 바로 간다.
 * 데이터는 Core 의 정적 분석(contract_check) — 코드를 실행하지 않는다.
 */
import React from "react";

export interface ContractFinding {
  code: string; severity: "error" | "warning" | "info"; service: string; file: string; line: number; message: string; fix: string;
}
export interface ContractEdge { from: string; to: string; kind: "db" | "service"; status: "ok" | "unused" | "violation"; file?: string; line?: number; }
export interface ContractResult {
  available: boolean; reason?: string;
  services?: { name: string; kind: string; language: string; databases: string[]; depends_on: string[] }[];
  databases?: { name: string; engine: string }[];
  findings?: ContractFinding[]; edges?: ContractEdge[];
  summary?: { error: number; warning: number; info: number };
}

const COLOR = {
  ok: "var(--vscode-charts-green, #3fb950)", unused: "var(--vscode-descriptionForeground, #888)",
  violation: "var(--vscode-editorError-foreground, #e5534b)", warn: "var(--vscode-editorWarning-foreground, #d7a300)",
  line: "var(--vscode-panel-border, #3a3a3a)", dim: "var(--vscode-descriptionForeground, #9aa0a6)",
};
const STATUS_TEXT = { ok: "계약대로 사용", unused: "계약에 있는데 안 씀", violation: "계약에 없는 연결" } as const;
const SEVERITY_TEXT = { error: "오류", warning: "경고", info: "참고" } as const;

/** 한 줄 요약 — 오류가 있으면 배포 게이트가 막는다는 것까지 말한다. */
export function contractHeadline(r: ContractResult): string {
  if (!r.available) return r.reason ?? "아키텍처 계약이 없는 프로젝트입니다.";
  const s = r.summary ?? { error: 0, warning: 0, info: 0 };
  if (s.error) return `계약 위반 ${s.error}건 — 고치기 전에는 다중 서비스 배포가 막힙니다`;
  if (s.warning) return `계약 위반 없음 · 확인할 것 ${s.warning}건`;
  return "설계도와 코드가 일치합니다";
}

export const ContractPanel: React.FC<{ result: ContractResult; onOpen?: (file: string, line?: number) => void }> = ({ result, onOpen }) => {
  if (!result.available) return null;
  const s = result.summary ?? { error: 0, warning: 0, info: 0 };
  const findings = result.findings ?? [];
  const edges = result.edges ?? [];
  const tone = s.error ? COLOR.violation : s.warning ? COLOR.warn : COLOR.ok;
  return (
    <section data-testid="contract-panel" aria-label="계약 대조" style={{ border: `1px solid ${COLOR.line}`, borderLeft: `3px solid ${tone}`, borderRadius: 8, padding: "10px 12px", marginBottom: 12 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
        <b style={{ fontSize: 12.5 }}>계약 대조 — 설계도와 실제 코드</b>
        <span data-testid="contract-headline" style={{ fontSize: 11.5, color: tone }}>{contractHeadline(result)}</span>
      </div>
      <div style={{ fontSize: 10.5, color: COLOR.dim, margin: "4px 0 8px" }}>
        서비스가 읽는 환경 변수와 선언한 API 를 계약(architecture.json)과 비교한 정적 분석입니다. 실제 동작은 생성·배포 때 통합 테스트로 확인합니다.
      </div>
      <div role="table" aria-label="서비스별 연결" style={{ display: "grid", gap: 4, marginBottom: findings.length ? 8 : 0 }}>
        {(result.services ?? []).map((svc) => {
          const mine = edges.filter((e) => e.from === svc.name);
          return (
            <div role="row" key={svc.name} data-service={svc.name} style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 11.5 }}>
              <b style={{ minWidth: 84 }}>{svc.name}</b>
              <span style={{ color: COLOR.dim, fontSize: 10.5 }}>{svc.kind} · {svc.language}</span>
              {mine.length === 0 && <span style={{ color: COLOR.dim, fontSize: 10.5 }}>연결 없음</span>}
              {mine.map((e) => (
                <button key={`${e.kind}:${e.to}`} type="button" data-edge={`${e.from}->${e.to}`} data-status={e.status}
                  title={`${STATUS_TEXT[e.status]}${e.file ? ` · ${e.file}:${e.line}` : ""}`}
                  onClick={() => e.file && onOpen?.(e.file, e.line)}
                  style={{ fontSize: 10.5, padding: "1px 7px", borderRadius: 10, cursor: e.file ? "pointer" : "default", background: "none",
                    border: `1px solid ${COLOR[e.status]}`, color: COLOR[e.status], textDecoration: e.status === "unused" ? "line-through" : "none" }}>
                  {e.kind === "db" ? "DB " : "→ "}{e.to}
                </button>
              ))}
            </div>
          );
        })}
      </div>
      {findings.length > 0 && (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 4 }}>
          {findings.slice(0, 40).map((f, i) => (
            <li key={i} data-severity={f.severity} style={{ fontSize: 11.5 }}>
              <span style={{ color: f.severity === "error" ? COLOR.violation : f.severity === "warning" ? COLOR.warn : COLOR.dim, fontWeight: 600 }}>
                {SEVERITY_TEXT[f.severity]}
              </span>{" "}
              {f.message}
              {f.line > 0 && (
                <button type="button" onClick={() => onOpen?.(f.file, f.line)}
                  style={{ marginLeft: 6, fontSize: 10.5, background: "none", border: "none", color: "var(--vscode-textLink-foreground, #3794ff)", cursor: "pointer", padding: 0 }}>
                  {f.file}:{f.line}
                </button>
              )}
              <div style={{ fontSize: 10.5, color: COLOR.dim }}>{f.fix}</div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
};

export default ContractPanel;
