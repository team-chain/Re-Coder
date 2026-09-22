import React from "react";

export interface LocalRollbackResult {
  deploymentId?: string;
  status: string;
  rolled_back_to?: string;
  warning?: string | null;
  error?: string;
  stderr?: string;
  health_ok?: boolean | null;
  health_check_url?: string | null;
  verification_resumed?: boolean;
  container_untouched?: boolean;
  restored_deployment_id?: string | null;
}
export type RollbackWatch = null | "none" | { status: string; anomalies?: unknown[] };

export function rollbackWatchId(originalId?: string, rollback?: LocalRollbackResult | null): string | undefined {
  if (!rollback) return originalId;
  if (rollback.status !== "ok" && rollback.container_untouched) return originalId;
  return rollback.status === "ok" && rollback.verification_resumed && rollback.restored_deployment_id
    ? rollback.restored_deployment_id : undefined;
}

export function rollbackBanner(result: LocalRollbackResult, watch: RollbackWatch): { ok: boolean; title: string; detail: string } {
  if (result.status !== "ok") return { ok: false, title: "롤백 실패", detail: result.error || result.warning || result.stderr || "복구 결과를 확인하지 못했습니다." };
  if (result.health_ok !== true) return { ok: false, title: "이전 이미지 실행됨 · 복구 확인 필요", detail: result.warning || "헬스 확인 결과가 없습니다. 서비스 접속을 확인하세요." };
  if (watch && watch !== "none" && (watch.status === "unstable" || watch.status === "error" || (watch.anomalies?.length ?? 0) > 0)) {
    return { ok: false, title: "롤백 후 이전 버전에서 이상 감지", detail: "아래 연속 검증 결과와 컨테이너 로그를 확인하세요." };
  }
  let monitoring = "감시 없음";
  if (result.verification_resumed && result.restored_deployment_id) {
    monitoring = watch === null ? "이전 버전 감시 상태 확인 중"
      : watch === "none" ? "이전 버전 감시 상태 없음"
      : watch.status === "running" ? "이전 버전 감시 중"
      : watch.status === "stable" ? "이전 버전 검증 완료"
      : `이전 버전 감시: ${watch.status}`;
  }
  return { ok: true, title: `롤백으로 복구됨 · ${monitoring}`, detail: result.rolled_back_to || "이전 버전" };
}

export function LocalRollbackStatus({ result, watch }: { result: LocalRollbackResult; watch: RollbackWatch }) {
  const banner = rollbackBanner(result, watch);
  const color = banner.ok ? "#22c55e" : "#f59e0b";
  return <div role="status" style={{ border: `1px solid ${color}`, borderRadius: 5, padding: "10px 12px", color, marginBottom: 10, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
    <strong>{banner.title}</strong><div style={{ fontSize: 11, marginTop: 4 }}>{banner.detail}</div>
    {result.health_check_url && <div style={{ fontSize: 11 }}>{result.health_check_url}</div>}
  </div>;
}
