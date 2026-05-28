/**
 * ReCoder — DiscordMode component
 *
 * 모바일 Discord에서 "tetris.html 만들어줘" 같은 메시지를 보내면 봇이 Bedrock을
 * 호출해서 노트북 VSCode에 실시간으로 코드를 삽입한다. 이 화면은 그 다리(Bridge)
 * 의 채널 설정과 연결 상태를 관리한다.
 *
 * 메시지 프로토콜 (extension host와):
 *   webview → host: "wb.bridge.getStatus" / "wb.bridge.setChannel"
 *   host → webview: "wb.bridge.status"  { ok, active_channel_id, channel_name,
 *                                          guild_name, connected_clients, error }
 */

import React, { useCallback, useEffect, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

// ── Types ──────────────────────────────────────────────────────────────────

interface BridgeStatus {
  ok: boolean;
  active_channel_id?: string;
  channel_name?: string | null;
  guild_name?: string | null;
  connected_clients?: number;
  error?: string;
}

// ── DiscordMode ────────────────────────────────────────────────────────────

export const DiscordMode: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();

  const [status, setStatus] = useState<BridgeStatus | null>(null);
  const [input, setInput] = useState("");
  const [saving, setSaving] = useState(false);

  useMessage(useCallback((msg) => {
    if (msg.type === "wb.bridge.status") {
      setStatus((msg.payload as BridgeStatus) ?? null);
      setSaving(false);
    }
  }, []));

  // 초기 조회 + 8초 폴링
  useEffect(() => {
    postMessage("wb.bridge.getStatus");
    const id = setInterval(() => postMessage("wb.bridge.getStatus"), 8000);
    return () => clearInterval(id);
  }, [postMessage]);

  const handleSave = useCallback(() => {
    setSaving(true);
    postMessage("wb.bridge.setChannel", { channelId: input.trim() });
  }, [input, postMessage]);

  const handleClear = useCallback(() => {
    setSaving(true);
    setInput("");
    postMessage("wb.bridge.setChannel", { channelId: "" });
  }, [postMessage]);

  const isConfigured = !!status?.active_channel_id;
  const isConnected = (status?.connected_clients ?? 0) > 0;
  const botUnreachable = status && !status.ok;

  // ── 공통 스타일 ──────────────────────────────────────────────────────────
  const card: React.CSSProperties = {
    background: "var(--vscode-editor-background, #1e1e1e)",
    border: "1px solid var(--vscode-panel-border, #333)",
    borderRadius: 6,
    padding: "12px 14px",
    marginBottom: 12,
  };
  const cardAccent: React.CSSProperties = {
    ...card,
    background: "rgba(88,101,242,0.06)",
    border: "1px solid rgba(88,101,242,0.35)",
  };
  const label: React.CSSProperties = {
    fontSize: 10,
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    color: "var(--vscode-descriptionForeground, #9ca3af)",
    marginBottom: 4,
  };
  const inputStyle: React.CSSProperties = {
    flex: 1,
    background: "var(--vscode-input-background, #252526)",
    color: "var(--vscode-input-foreground, #ccc)",
    border: "1px solid var(--vscode-input-border, #3f3f3f)",
    borderRadius: 4,
    padding: "6px 9px",
    fontSize: 12,
    fontFamily: "var(--vscode-editor-font-family, monospace)",
    outline: "none",
  };
  const btnPrimary: React.CSSProperties = {
    background: "#5865f2",
    color: "#fff",
    border: "none",
    borderRadius: 4,
    padding: "6px 12px",
    fontSize: 11,
    fontWeight: 600,
    cursor: saving ? "wait" : "pointer",
    opacity: saving ? 0.6 : 1,
  };
  const btnSecondary: React.CSSProperties = {
    background: "transparent",
    color: "var(--vscode-foreground, #ccc)",
    border: "1px solid var(--vscode-input-border, #3f3f3f)",
    borderRadius: 4,
    padding: "6px 12px",
    fontSize: 11,
    cursor: "pointer",
  };

  return (
    <div style={{ padding: "12px 12px 16px", fontFamily: "var(--vscode-font-family, sans-serif)", color: "var(--vscode-foreground, #e0e0e0)" }}>

      {/* ── 상단: 채널 설정 카드 ───────────────────────────────────────── */}
      <div style={cardAccent}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <span style={{ fontSize: 16 }}>📱</span>
          <strong style={{ fontSize: 13 }}>ReCoder Bridge</strong>
          <span style={{
            marginLeft: "auto",
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            fontSize: 11,
            fontWeight: 600,
            color: isConnected ? "#22c55e" : isConfigured ? "#f59e0b" : botUnreachable ? "#ef4444" : "#9ca3af",
          }}>
            <span style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: isConnected ? "#22c55e" : isConfigured ? "#f59e0b" : botUnreachable ? "#ef4444" : "#6b7280",
            }} />
            {botUnreachable ? "봇 오프라인" : isConnected ? "연결됨" : isConfigured ? "대기 중" : "미설정"}
          </span>
        </div>

        {/* 현재 채널 표시 */}
        <div style={{ marginBottom: 12, fontSize: 12, color: "var(--vscode-descriptionForeground, #9ca3af)", lineHeight: 1.5 }}>
          {isConfigured ? (
            <>
              현재 채널:{" "}
              <strong style={{ color: "var(--vscode-foreground, #e6edf3)", fontFamily: "monospace", fontSize: 12 }}>
                {status?.channel_name ? `#${status.channel_name}` : status?.active_channel_id}
              </strong>
              {status?.guild_name && (
                <span style={{ color: "var(--vscode-descriptionForeground, #6b7280)" }}> · {status.guild_name}</span>
              )}
            </>
          ) : (
            <span>채널이 설정되지 않았습니다. 디스코드 채널 ID를 입력해 시작하세요.</span>
          )}
        </div>

        <div style={label}>채널 ID</div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input
            type="text"
            value={input}
            placeholder={isConfigured ? "새 채널 ID로 변경" : "예) 123456789012345678"}
            onChange={(e) => setInput(e.target.value.replace(/[^0-9]/g, ""))}
            onKeyDown={(e) => { if (e.key === "Enter" && input) handleSave(); }}
            style={inputStyle}
            spellCheck={false}
          />
          <button style={btnPrimary} disabled={saving || !input} onClick={handleSave}>
            {saving ? "저장 중…" : "저장"}
          </button>
          {isConfigured && (
            <button style={btnSecondary} disabled={saving} onClick={handleClear}>
              해제
            </button>
          )}
        </div>

        <div style={{ marginTop: 8, fontSize: 11, color: "var(--vscode-descriptionForeground, #6b7280)", lineHeight: 1.5 }}>
          Discord 설정 → 고급 → <strong>개발자 모드 ON</strong> → 채널 우클릭 → "ID 복사"
        </div>

        {botUnreachable && (
          <div style={{
            marginTop: 10,
            fontSize: 11,
            color: "#ef4444",
            background: "rgba(239,68,68,0.08)",
            border: "1px solid rgba(239,68,68,0.25)",
            padding: "6px 9px",
            borderRadius: 4,
            lineHeight: 1.5,
          }}>
            <strong>봇과 연결되지 않습니다.</strong><br />
            {status?.error ?? "discord-bot 프로세스가 실행 중인지 확인하세요."}
          </div>
        )}
      </div>

      {/* ── 사용법 카드 ───────────────────────────────────────────────── */}
      <div style={card}>
        <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 8, color: "var(--vscode-foreground, #e6edf3)" }}>
          사용법
        </div>
        <ol style={{ paddingLeft: 18, margin: 0, fontSize: 11.5, lineHeight: 1.7, color: "var(--vscode-descriptionForeground, #b1bac4)" }}>
          <li>노트북에서 봇 실행: <code style={{ background: "rgba(255,255,255,0.06)", padding: "1px 5px", borderRadius: 3, fontSize: 11 }}>cd discord-bot && python bot.py</code></li>
          <li>이 화면에서 채널 ID 저장</li>
          <li>VSCode에서 워크스페이스 폴더가 열려있어야 합니다 (생성된 파일이 저장될 곳)</li>
          <li>핸드폰 Discord 앱에서 그 채널에 메시지:
            <ul style={{ paddingLeft: 18, marginTop: 4 }}>
              <li><code style={{ background: "rgba(255,255,255,0.06)", padding: "1px 5px", borderRadius: 3, fontSize: 11 }}>tetris.html 만들어줘</code></li>
              <li><code style={{ background: "rgba(255,255,255,0.06)", padding: "1px 5px", borderRadius: 3, fontSize: 11 }}>app.py 간단한 Flask 서버</code></li>
            </ul>
          </li>
          <li>위 상태 도트가 <strong style={{ color: "#22c55e" }}>연결됨</strong>으로 바뀌면서 VSCode에 코드가 실시간으로 흘러들어옵니다</li>
        </ol>
      </div>

      {/* ── 모델/상태 카드 ─────────────────────────────────────────────── */}
      <div style={card}>
        <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 8, color: "var(--vscode-foreground, #e6edf3)" }}>
          현재 설정
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 12px", fontSize: 11, lineHeight: 1.6 }}>
          <span style={{ color: "var(--vscode-descriptionForeground, #9ca3af)" }}>모델</span>
          <span style={{ fontFamily: "monospace" }}>core/llm/bedrock_provider.py와 공유</span>

          <span style={{ color: "var(--vscode-descriptionForeground, #9ca3af)" }}>채널</span>
          <span style={{ fontFamily: "monospace" }}>
            {isConfigured ? (status?.channel_name ? `#${status.channel_name}` : status?.active_channel_id) : "미설정"}
          </span>

          <span style={{ color: "var(--vscode-descriptionForeground, #9ca3af)" }}>연결된 확장</span>
          <span style={{ fontFamily: "monospace" }}>{status?.connected_clients ?? 0}개</span>

          <span style={{ color: "var(--vscode-descriptionForeground, #9ca3af)" }}>봇 API</span>
          <span style={{ fontFamily: "monospace", color: botUnreachable ? "#ef4444" : "#22c55e" }}>
            {botUnreachable ? "응답 없음" : status ? "정상" : "확인 중…"}
          </span>
        </div>
      </div>

    </div>
  );
};

export default DiscordMode;
