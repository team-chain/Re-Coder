/** AWS BYO 계정 연결 — 키는 검증 뒤 VS Code SecretStorage에만 저장된다. */
import React, { useCallback, useEffect, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

type AwsStatus = {
  ready: boolean;
  identity?: { account?: string; arn?: string } | null;
  region?: string;
  access_key_last4?: string;
  message?: string;
};

const input: React.CSSProperties = {
  width: "100%", boxSizing: "border-box", marginTop: 5, padding: "8px 9px", borderRadius: 5,
  color: "var(--vscode-input-foreground, #ddd)", background: "var(--vscode-input-background, #252526)",
  border: "1px solid var(--vscode-input-border, #3f3f3f)", fontSize: 12,
};

export const AwsConnection: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [accessKeyId, setAccessKeyId] = useState("");
  const [secretAccessKey, setSecretAccessKey] = useState("");
  const [region, setRegion] = useState("ap-northeast-2");
  const [status, setStatus] = useState<AwsStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { postMessage("aws.status"); }, [postMessage]);
  useMessage(useCallback(({ type, payload }) => {
    if (type === "aws.status") { setStatus(payload as AwsStatus); }
    if (type === "aws.configure.result") {
      const result = payload as { ok: boolean; status?: AwsStatus; message?: string };
      setBusy(false);
      if (result.ok) {
        setStatus(result.status ?? null);
        // 성공 후에는 렌더링 메모리에도 비밀키를 더 보관하지 않는다.
        setAccessKeyId("");
        setSecretAccessKey("");
        setError("");
      } else {
        setError(result.message ?? "AWS 자격증명을 확인하지 못했습니다.");
      }
    }
    if (type === "aws.clear.result") {
      const result = payload as { ok: boolean; message?: string };
      setBusy(false);
      if (result.ok) { setStatus({ ready: false, message: "AWS 연결이 해제되었습니다." }); setError(""); }
      else { setError(result.message ?? "AWS 연결 해제에 실패했습니다."); }
    }
  }, []));

  const connect = () => {
    if (!accessKeyId.trim() || !secretAccessKey.trim()) {
      setError("Access Key ID와 Secret Access Key를 모두 입력하세요.");
      return;
    }
    setBusy(true);
    setError("");
    postMessage("aws.configure", {
      accessKeyId: accessKeyId.trim(), secretAccessKey: secretAccessKey.trim(), region: region.trim() || "ap-northeast-2",
    });
  };

  const card: React.CSSProperties = {
    border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 8, padding: 15,
    background: "var(--vscode-editorWidget-background, #252526)",
  };
  const button: React.CSSProperties = {
    border: "none", borderRadius: 5, padding: "8px 12px", fontSize: 12, fontWeight: 650, cursor: busy ? "wait" : "pointer",
    background: "var(--vscode-button-background, #0e639c)", color: "var(--vscode-button-foreground, #fff)", opacity: busy ? .7 : 1,
  };

  if (status?.ready) {
    return <div style={card}>
      <div style={{ color: "var(--vscode-charts-green, #4ec9b0)", fontSize: 15, fontWeight: 700 }}>✓ AWS 연결됨</div>
      <div style={{ marginTop: 9, fontSize: 12, lineHeight: 1.6 }}>
        <div>계정: <b>{status.identity?.account ?? "확인됨"}</b></div>
        <div>리전: <b>{status.region || "ap-northeast-2"}</b>{status.access_key_last4 ? ` · 키 끝 ${status.access_key_last4}` : ""}</div>
      </div>
      <div style={{ marginTop: 11, color: "var(--vscode-descriptionForeground, #999)", fontSize: 11, lineHeight: 1.5 }}>키는 VS Code의 OS 보안 금고에 암호화되어 저장되며 프로젝트 파일에는 기록되지 않습니다.</div>
      <button disabled={busy} onClick={() => { setBusy(true); setError(""); postMessage("aws.clear"); }} style={{ ...button, marginTop: 13, background: "var(--vscode-button-secondaryBackground, #3a3d41)", color: "var(--vscode-button-secondaryForeground, #fff)" }}>연결 해제</button>
      {error && <div style={{ marginTop: 10, color: "var(--vscode-errorForeground, #f48771)", fontSize: 12 }}>{error}</div>}
    </div>;
  }

  return <div style={card}>
    <div style={{ fontSize: 15, fontWeight: 700 }}>AWS 계정 연결</div>
    <p style={{ margin: "7px 0 14px", color: "var(--vscode-descriptionForeground, #999)", fontSize: 12, lineHeight: 1.5 }}>배포는 본인의 AWS 계정에서 실행됩니다. 입력한 키는 검증 후 VS Code 보안 금고에만 저장합니다.</p>
    <label style={{ display: "block", fontSize: 12 }}>Access Key ID
      <input autoComplete="off" spellCheck={false} value={accessKeyId} onChange={e => setAccessKeyId(e.target.value)} placeholder="AKIA..." style={input} />
    </label>
    <label style={{ display: "block", marginTop: 11, fontSize: 12 }}>Secret Access Key
      <input autoComplete="off" spellCheck={false} type="password" value={secretAccessKey} onChange={e => setSecretAccessKey(e.target.value)} placeholder="비밀 키를 붙여 넣으세요" style={input} />
    </label>
    <label style={{ display: "block", marginTop: 11, fontSize: 12 }}>리전
      <input autoComplete="off" spellCheck={false} value={region} onChange={e => setRegion(e.target.value)} placeholder="ap-northeast-2" style={input} />
    </label>
    {error && <div role="alert" style={{ marginTop: 10, color: "var(--vscode-errorForeground, #f48771)", fontSize: 12, lineHeight: 1.45 }}>{error}</div>}
    {status?.message && !error && <div style={{ marginTop: 10, color: "var(--vscode-descriptionForeground, #999)", fontSize: 11 }}>{status.message}</div>}
    <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 15 }}>
      <button disabled={busy} onClick={connect} style={button}>{busy ? "STS로 확인 중…" : "연결"}</button>
      <a href="https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_access-keys.html" target="_blank" rel="noreferrer" style={{ color: "var(--vscode-textLink-foreground, #75beff)", fontSize: 11 }}>키 만드는 법 보기</a>
    </div>
  </div>;
};

export default AwsConnection;
