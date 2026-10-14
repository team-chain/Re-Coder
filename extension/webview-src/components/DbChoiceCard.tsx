/**
 * DB 테이블 구조가 앱과 다를 때 — 조용히 쓰지 않고 묻는다.
 *  · 배포 전(stage: db_schema): [새 DB로 시작 (기존 DB는 지우지 않고 남겨 둠)] / [그대로 사용]
 *  · 배포 후 조회 API 가 5xx(app_check): "배포는 됐지만 앱이 오류를 냄" + 원인 + (DB 구조 문제면) [새 DB로 시작]
 */
import React from "react";

export interface DbDiagnosis { code: string; title: string; cause: string; fix?: string; lines?: string[] }

const box = (tone: "warn" | "bad" | "info"): React.CSSProperties => ({
  border: `1px solid ${tone === "bad" ? "var(--vscode-errorForeground,#f48771)" : tone === "info" ? "var(--vscode-panel-border,#555)" : "var(--vscode-editorWarning-foreground,#cca700)"}`,
  background: tone === "bad" ? "rgba(244,135,113,.08)" : tone === "info" ? "transparent" : "rgba(204,167,0,.08)",
  borderRadius: 6, padding: "9px 11px", margin: "0 0 10px", fontSize: 11, lineHeight: 1.55,
});
const primary: React.CSSProperties = { background: "var(--vscode-button-background,#0e639c)", color: "var(--vscode-button-foreground,#fff)", border: 0, borderRadius: 4, padding: "5px 11px", fontSize: 11.5, fontWeight: 600, cursor: "pointer" };
const secondary: React.CSSProperties = { background: "transparent", color: "var(--vscode-foreground,#ddd)", border: "1px solid var(--vscode-panel-border,#555)", borderRadius: 4, padding: "5px 11px", fontSize: 11.5, cursor: "pointer" };

function Lines({ lines }: { lines?: string[] }) {
  if (!lines?.length) return null;
  return <pre style={{ margin: "6px 0 0", padding: "6px 8px", background: "var(--vscode-textCodeBlock-background,#1e1e1e)", borderRadius: 4, whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: "var(--vscode-editor-font-family,monospace)", fontSize: 10.5 }}>{lines.join("\n")}</pre>;
}

/** 배포 전 — DB 구조가 다르다. 선택하면 같은 승인으로 다시 배포한다. */
export const DbSchemaChoice: React.FC<{ diagnosis: DbDiagnosis; busy?: boolean; error?: string; onChoose: (choice: "new" | "keep") => void }> = ({ diagnosis, busy, error, onChoose }) => (
  <section role="alert" aria-label="DB 구조 확인" data-testid="db-schema-choice" style={box("warn")}>
    <strong style={{ fontSize: 12, color: "var(--vscode-editorWarning-foreground,#cca700)" }}>{diagnosis.title}</strong>
    <div style={{ marginTop: 4 }}>{diagnosis.cause}</div>
    <Lines lines={diagnosis.lines} />
    {error && <div role="alert" style={{ marginTop: 6, color: "var(--vscode-errorForeground,#f48771)" }}>{error}</div>}
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
      <button type="button" style={primary} disabled={busy} onClick={() => onChoose("new")}>새 DB로 시작 (기존 DB는 지우지 않고 남겨 둠)</button>
      <button type="button" style={secondary} disabled={busy} onClick={() => onChoose("keep")}>그대로 사용</button>
    </div>
    <div style={{ marginTop: 6, color: "var(--vscode-descriptionForeground,#999)", fontSize: 10.5 }}>
      기존 컨테이너는 그대로입니다. 고르면 같은 승인으로 다시 배포합니다.
    </div>
  </section>
);

/** 배포 후 — 컨테이너는 떴지만 조회 API 가 5xx. */
export const AppCheckWarning: React.FC<{ check: { path?: string; http_status?: number; diagnosis?: DbDiagnosis }; busy?: boolean; onNewDb?: () => void }> = ({ check, busy, onNewDb }) => (
  <section role="alert" aria-label="앱 응답 확인" data-testid="app-check-error" style={box("bad")}>
    <strong style={{ fontSize: 12, color: "var(--vscode-errorForeground,#f48771)" }}>배포는 됐지만 앱이 오류를 냄</strong>
    <div style={{ marginTop: 4 }}>{check.diagnosis?.title ? `${check.diagnosis.title} — ` : ""}{check.diagnosis?.cause ?? `${check.path} 가 ${check.http_status} 를 냈습니다.`}</div>
    {check.diagnosis?.fix && <div style={{ marginTop: 4, color: "var(--vscode-foreground,#ddd)" }}><b>해결:</b> {check.diagnosis.fix}</div>}
    <Lines lines={check.diagnosis?.lines} />
    {check.diagnosis?.code === "DB_SCHEMA_MISMATCH" && onNewDb && (
      <div style={{ marginTop: 8 }}>
        <button type="button" style={primary} disabled={busy} onClick={onNewDb}>새 DB로 시작 (기존 DB는 지우지 않고 남겨 둠)</button>
      </div>
    )}
  </section>
);

/** 데모 상품을 넣지 못했다 — 진행 문구로 지나가지 않게 결과에 남긴다. */
export const DemoSeedWarning: React.FC<{ seed: { ok: boolean; script?: string; message?: string } }> = ({ seed }) => seed.ok ? null : (
  <section role="status" aria-label="데모 상품" data-testid="demo-seed-warning" style={box("warn")}>
    <strong style={{ color: "var(--vscode-editorWarning-foreground,#cca700)" }}>데모 상품을 넣지 못했습니다</strong>
    <div style={{ marginTop: 4 }}>앱은 실행됐지만 상품 목록이 비어 있을 수 있습니다. {seed.script ? `${seed.script} 실행 결과:` : ""}</div>
    <Lines lines={seed.message ? seed.message.split("\n").filter(Boolean).slice(-6) : []} />
  </section>
);

/** 첫 배포에서 앱의 샘플 데이터 스크립트(npm run db:seed 등)를 돌린 결과 — 관리자 계정·메뉴가 여기서 생긴다. */
export const AppSeedNote: React.FC<{ seed: { ok: boolean; script?: string; log?: string } }> = ({ seed }) => (
  <section role="status" aria-label="샘플 데이터" data-testid="app-seed-note" style={box(seed.ok ? "info" : "warn")}>
    <strong>{seed.ok ? "처음 배포 — 앱의 샘플 데이터를 넣었습니다" : "앱의 샘플 데이터를 넣지 못했습니다"}</strong>
    <div style={{ marginTop: 4 }}>
      {seed.ok
        ? `빈 DB 라서 ${seed.script ?? "샘플 데이터"} 를 한 번 실행했습니다(관리자 계정·기본 메뉴 등은 앱 README 를 확인하세요). 다시 배포할 때는 실행하지 않습니다.`
        : `앱은 실행됐지만 관리자 계정·기본 데이터가 없을 수 있습니다. ${seed.script ?? ""} 실행 결과:`}
    </div>
    {!seed.ok && <Lines lines={seed.log ? seed.log.split("\n").filter(Boolean).slice(-6) : []} />}
  </section>
);
