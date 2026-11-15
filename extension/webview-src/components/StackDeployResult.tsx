/**
 * 다중 서비스(compose) 로컬 실행 결과(4.0.0).
 *
 * 단일 앱 결과 화면을 그대로 쓰면 compose 결과에는 health_ok 가 없어 "앱 응답 확인 중 — 아직 배포 완료가 아닙니다" 로
 * 보이고, 없는 연속 검증을 계속 조회했다(실사용 2026-11-15: 실제로는 서비스 6개·DB 4개가 떠서 검사 30/32 통과).
 * Core 가 실제로 확인한 것(실행 주소·검사 수·실패 항목)을 그대로 보여 준다.
 */
import React from "react";

export interface StackFailure { kind?: string; service?: string; title: string; detail?: string }
export interface StackDeploy {
  status: string; message?: string; url?: string; deployment_id?: string; method?: string;
  verification?: { status?: string; passed?: boolean; checks?: Array<{ name: string; ok: boolean }>; failures?: StackFailure[] };
}

export const isStackDeploy = (r: unknown): boolean => (r as { method?: string } | null)?.method === "local_docker_compose";

export const StackDeployResult: React.FC<{ result: StackDeploy; onOpen?: (url: string) => void }> = ({ result, onOpen }) => {
  const checks = result.verification?.checks ?? [];
  const passed = checks.filter(c => c.ok).length;
  const failures = result.verification?.failures ?? [];
  const ok = result.status === "success" && !failures.length;
  const tone = ok ? "var(--rc-ok,#3DD68C)" : "var(--rc-warn,#F2B544)";
  const head = (result.message || "").split(" · 일부 검사 실패")[0].split(" — http")[0];
  return (
    <div data-testid="stack-result" style={{ border: `1px solid ${tone}`, borderRadius: 12, padding: "12px 14px", marginBottom: 10, background: "var(--rc-tile,rgba(148,178,210,.035))" }}>
      <div style={{ fontWeight: 650, color: tone }}>{ok ? "실행 완료" : "실행 중 · 확인 필요"} — {head || "여러 서비스 실행"}</div>
      <div style={{ fontSize: 12, marginTop: 6, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", color: "var(--rc-fg2,#9AA8B8)" }}>
        {result.url && <button type="button" onClick={() => onOpen?.(result.url!)} style={{ all: "unset", cursor: "pointer", color: "var(--rc-teal,#35A9D1)", fontFamily: "var(--rc-mono)" }}>{result.url} ↗</button>}
        {!!checks.length && <span>검사 {passed}/{checks.length} 통과</span>}
      </div>
      {!!failures.length && <ul style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 12, lineHeight: 1.6 }}>
        {failures.slice(0, 5).map((f, i) => <li key={i}><b>{f.title}</b>{f.detail ? <div style={{ color: "var(--rc-fg3,#67768A)" }}>{f.detail.slice(0, 200)}</div> : null}</li>)}
      </ul>}
    </div>
  );
};

export default StackDeployResult;
