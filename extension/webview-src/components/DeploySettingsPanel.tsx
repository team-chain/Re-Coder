/**
 * 로컬 Docker 배포의 "필요한 설정" — 앱이 시작할 때 요구하는 값을 빌드 **전에** 채운다.
 *
 *  · 앱 내부 서명 키(JWT_SECRET 등)는 코어가 로컬 배포용으로 자동 생성한다(표시만).
 *  · 외부 서비스 키(Stripe 등)는 여기서 입력한다. 값은 코어가 사용자 홈(~/.recoder)에만 보관하고
 *    화면으로 되돌려 보내지 않는다.
 *  · 앱이 모의 결제를 지원하면 "키 없이 결제를 끈 로컬 데모로 실행" 버튼으로 바로 띄울 수 있다.
 */
import React, { useEffect, useState } from "react";

export interface DeploySetting {
  name: string; label: string; hint: string; status: "ready" | "missing"; source: string; secret: boolean; min_length?: number;
}
export interface DeployDemo { available: boolean; enabled: boolean; label: string; note: string; unavailable_reason?: string }

const SOURCE_TEXT: Record<string, string> = {
  provided: "PC 의 .env · DB 자동", generated: "자동 생성", saved: "입력함", demo: "데모 값",
};

const S = {
  box: { border: "1px solid var(--vscode-panel-border,#3b3b3b)", borderRadius: 6, padding: "9px 11px", margin: "0 0 10px", fontSize: 11, lineHeight: 1.5, background: "var(--vscode-editorWidget-background,#1f1f1f)" } as React.CSSProperties,
  row: { display: "flex", alignItems: "center", gap: 8, padding: "4px 0", borderTop: "1px solid var(--vscode-panel-border,#2c2c2c)", flexWrap: "wrap" } as React.CSSProperties,
  chip: (ok: boolean): React.CSSProperties => ({ fontSize: 10, padding: "1px 7px", borderRadius: 99, whiteSpace: "nowrap",
    color: ok ? "var(--vscode-charts-green,#73d39b)" : "var(--vscode-editorWarning-foreground,#cca700)",
    border: `1px solid ${ok ? "rgba(115,211,155,.45)" : "rgba(204,167,0,.5)"}` }),
  input: { flex: "1 1 220px", minWidth: 0, background: "var(--vscode-input-background,#2a2a2a)", color: "var(--vscode-input-foreground,#ddd)", border: "1px solid var(--vscode-input-border,#444)", borderRadius: 4, padding: "4px 7px", fontFamily: "var(--vscode-editor-font-family,monospace)", fontSize: 11 } as React.CSSProperties,
  primary: { background: "var(--vscode-button-background,#0e639c)", color: "var(--vscode-button-foreground,#fff)", border: 0, borderRadius: 4, padding: "5px 11px", fontSize: 11.5, fontWeight: 600, cursor: "pointer" } as React.CSSProperties,
  demo: { background: "transparent", color: "var(--vscode-textLink-foreground,#4daafc)", border: "1px solid var(--vscode-textLink-foreground,#4daafc)", borderRadius: 4, padding: "5px 11px", fontSize: 11.5, fontWeight: 600, cursor: "pointer" } as React.CSSProperties,
};

export const DeploySettingsPanel: React.FC<{
  settings: DeploySetting[];
  missing: string[];
  demo: DeployDemo | null | undefined;
  busy?: boolean;
  error?: string;
  onSave: (values: Record<string, string>) => void;
  onDemo: (enabled: boolean) => void;
}> = ({ settings, missing, demo, busy, error, onSave, onDemo }) => {
  const [values, setValues] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<string[]>([]);
  useEffect(() => { setValues({}); setEditing([]); }, [missing.join(","), demo?.enabled]);
  if (!settings.length) return null;
  const inputs = settings.filter(s => s.status === "missing" || editing.includes(s.name));
  const short = inputs.filter(s => (values[s.name] ?? "").trim() && (values[s.name] ?? "").trim().length < (s.min_length ?? 0)).map(s => s.name);
  const canSave = inputs.length > 0 && inputs.every(s => (values[s.name] ?? "").trim()) && short.length === 0 && !busy;
  const ready = missing.length === 0;
  return (
    <section aria-label="필요한 설정" data-testid="deploy-settings" style={S.box}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <b style={{ fontSize: 12 }}>필요한 설정</b>
        <span style={S.chip(ready)}>{ready ? "모두 준비됨" : `${missing.length}개 입력 필요`}</span>
        {demo?.enabled && <span style={{ ...S.chip(true), color: "var(--vscode-textLink-foreground,#4daafc)", borderColor: "rgba(77,170,252,.5)" }}>로컬 데모 · 결제 꺼짐</span>}
      </div>
      <div style={{ color: "var(--vscode-descriptionForeground,#999)", marginBottom: 4 }}>
        이 앱은 시작할 때 아래 값이 없으면 바로 종료합니다. 값은 이 PC 에만 보관하고, 이미지·프로젝트 파일·배포 기록에는 남기지 않습니다.
      </div>
      {settings.map(s => {
        const input = s.status === "missing" || editing.includes(s.name);
        return (
          <div key={s.name} style={S.row} data-setting={s.name} data-status={s.status}>
            <div style={{ flex: "0 1 210px", minWidth: 0 }}>
              <div style={{ fontWeight: 600 }}>{s.label !== s.name ? s.label : s.name}</div>
              {s.label !== s.name && <code style={{ fontSize: 10, color: "var(--vscode-descriptionForeground,#999)" }}>{s.name}</code>}
            </div>
            {input ? (
              <input type="password" autoComplete="off" spellCheck={false} aria-label={`${s.name} 값`} style={S.input}
                placeholder="값을 붙여 넣으세요" value={values[s.name] ?? ""} disabled={busy}
                onChange={e => setValues(v => ({ ...v, [s.name]: e.target.value }))} />
            ) : (
              <>
                <span style={S.chip(true)}>{SOURCE_TEXT[s.source] ?? "준비됨"}</span>
                {s.source === "saved" && <button type="button" onClick={() => setEditing(e => [...e, s.name])} disabled={busy}
                  style={{ background: "transparent", border: 0, color: "var(--vscode-textLink-foreground,#4daafc)", cursor: "pointer", fontSize: 11 }}>바꾸기</button>}
              </>
            )}
            {input && s.hint && <div style={{ flexBasis: "100%", color: "var(--vscode-descriptionForeground,#999)", fontSize: 10.5 }}>{s.hint}{s.min_length ? ` · ${s.min_length}자 이상` : ""}</div>}
          </div>
        );
      })}
      {short.length > 0 && <div role="alert" style={{ color: "var(--vscode-errorForeground,#f48771)", marginTop: 4 }}>{short.join(", ")} 값이 너무 짧습니다.</div>}
      {error && <div role="alert" style={{ color: "var(--vscode-errorForeground,#f48771)", marginTop: 4 }}>{error}</div>}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
        {inputs.length > 0 && <button type="button" style={{ ...S.primary, opacity: canSave ? 1 : 0.5, cursor: canSave ? "pointer" : "not-allowed" }}
          disabled={!canSave} onClick={() => onSave(Object.fromEntries(inputs.map(s => [s.name, (values[s.name] ?? "").trim()])))}>
          {busy ? "저장 중…" : "저장하고 계속"}
        </button>}
        {demo?.available && !demo.enabled && !ready && (
          <button type="button" style={S.demo} disabled={busy} onClick={() => onDemo(true)} data-testid="demo-button">
            {demo.label || "키 없이 결제를 끈 로컬 데모로 실행"}
          </button>
        )}
        {demo?.enabled && (
          <button type="button" style={{ ...S.demo, borderStyle: "dashed" }} disabled={busy} onClick={() => onDemo(false)}>
            실제 키로 실행하기
          </button>
        )}
      </div>
      {demo && !demo.available && !ready && demo.unavailable_reason && (
        <div data-testid="demo-unavailable" style={{ marginTop: 6, color: "var(--vscode-descriptionForeground,#999)", fontSize: 10.5 }}>
          {demo.unavailable_reason}
        </div>
      )}
      {demo?.available && (!ready || demo.enabled) && demo.note && (
        <div style={{ marginTop: 6, color: "var(--vscode-descriptionForeground,#999)", fontSize: 10.5 }}>{demo.note}</div>
      )}
    </section>
  );
};
