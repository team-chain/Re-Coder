/**
 * DiscordIntegration.tsx — Discord 봇 연동 설정 UI
 *
 * [새 흐름 — 초대 링크 방식]
 *   1단계: Discord 서버 초대 링크(discord.gg/xxx) 입력
 *          → Extension이 Discord API를 호출해 서버 이름·ID 자동 확인
 *          → 해당 서버가 미리 선택된 OAuth2 봇 초대 링크 생성
 *          → 사용자가 클릭해서 봇을 서버에 추가
 *   2단계: 알림 채널 설정 (선택)
 *   3단계: 저장 및 완료
 *
 * 메시지 프로토콜 (webview → extension):
 *   discord.loadConfig      저장된 설정 요청
 *   discord.saveConfig      설정 저장 { guildId, guildName, ... }
 *   discord.openInvite      OAuth2 초대 URL을 브라우저에서 열기
 *   discord.resolveInvite   { inviteUrl } → 초대 링크를 서버 ID로 변환 요청
 *
 * 메시지 프로토콜 (extension → webview):
 *   discord.configLoaded    { guildId, guildName, ..., connected: boolean }
 *   discord.saved           { ok: boolean }
 *   discord.inviteResolved  { ok, guildId, guildName, guildIcon, memberCount, error? }
 */

import React, { useState, useEffect, useCallback } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

// ── 상수 ──────────────────────────────────────────────────────────────────────

const BOT_CLIENT_ID = "1508077108310835240";
const BOT_PERMISSIONS = "2147568640";

function makeBotInviteUrl(guildId?: string): string {
  const base =
    `https://discord.com/api/oauth2/authorize` +
    `?client_id=${BOT_CLIENT_ID}` +
    `&permissions=${BOT_PERMISSIONS}` +
    `&scope=bot%20applications.commands`;
  if (guildId) {
    return base + `&guild_id=${guildId}&disable_guild_select=true`;
  }
  return base;
}

// ── 타입 ──────────────────────────────────────────────────────────────────────

interface DiscordConfig {
  guildId: string;
  guildName: string;
  deployChannelId: string;
  incidentChannelId: string;
  standupChannelId: string;
  standupCron: string;
}

interface ResolvedGuild {
  guildId: string;
  guildName: string;
  guildIcon: string | null;
  memberCount: number | null;
}

type Step = 1 | 2 | 3;

// ── 아이콘 ────────────────────────────────────────────────────────────────────

const DiscordIcon = ({ size = 20 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
    <path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z" />
  </svg>
);

const CheckIcon = ({ size = 14 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="20 6 9 17 4 12" />
  </svg>
);

const ExternalLinkIcon = ({ size = 12 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
    <polyline points="15 3 21 3 21 9" />
    <line x1="10" y1="14" x2="21" y2="3" />
  </svg>
);

const SearchIcon = ({ size = 14 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="11" cy="11" r="8" />
    <line x1="21" y1="21" x2="16.65" y2="16.65" />
  </svg>
);

const SpinnerIcon = ({ size = 14 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
    <path d="M21 12a9 9 0 1 1-6.219-8.56" />
    <style>{`@keyframes spin{to{transform:rotate(360deg)}}svg{animation:spin .8s linear infinite;transform-origin:center}`}</style>
  </svg>
);

const ServerIcon = ({ size = 16 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <rect x="2" y="2" width="20" height="8" rx="2" ry="2" />
    <rect x="2" y="14" width="20" height="8" rx="2" ry="2" />
    <line x1="6" y1="6" x2="6.01" y2="6" />
    <line x1="6" y1="18" x2="6.01" y2="18" />
  </svg>
);

// ── 스타일 ────────────────────────────────────────────────────────────────────

const S = {
  section: {
    background: "var(--vscode-input-background, #252526)",
    border: "1px solid var(--vscode-panel-border, #333)",
    borderRadius: 8,
    padding: "14px 14px 12px",
    marginBottom: 10,
  } as React.CSSProperties,

  label: {
    fontSize: 11,
    fontWeight: 600,
    color: "var(--vscode-descriptionForeground, #888)",
    textTransform: "uppercase" as const,
    letterSpacing: "0.06em",
    display: "block",
    marginBottom: 6,
  } as React.CSSProperties,

  input: {
    width: "100%",
    padding: "7px 10px",
    borderRadius: 5,
    border: "1px solid var(--vscode-panel-border, #444)",
    background: "var(--vscode-input-background, #1e1e1e)",
    color: "var(--vscode-input-foreground, #ccc)",
    fontSize: 12,
    fontFamily: "var(--vscode-editor-font-family, monospace)",
    outline: "none",
    boxSizing: "border-box" as const,
  } as React.CSSProperties,

  hint: {
    fontSize: 10,
    color: "var(--vscode-descriptionForeground, #777)",
    marginTop: 4,
    lineHeight: 1.5,
  } as React.CSSProperties,

  btn: (variant: "primary" | "secondary" | "danger" | "ghost" | "invite" | "discord") => ({
    padding: variant === "ghost" ? "4px 0" : "8px 14px",
    borderRadius: 5,
    border: "none",
    fontSize: 12,
    fontWeight: 600,
    cursor: "pointer",
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    background:
      variant === "primary"  ? "var(--vscode-button-background, #0e639c)" :
      variant === "danger"   ? "#7f1d1d" :
      variant === "ghost"    ? "transparent" :
      variant === "invite"   ? "#5865f2" :
      variant === "discord"  ? "rgba(88,101,242,0.15)" :
      "var(--vscode-button-secondaryBackground, #3a3d41)",
    color:
      variant === "ghost"   ? "var(--vscode-textLink-foreground, #4a9eff)" :
      variant === "discord" ? "#5865f2" :
      "#fff",
    border: variant === "discord" ? "1px solid rgba(88,101,242,0.4)" : "none",
    transition: "opacity 0.12s",
  } as React.CSSProperties),
};

// ── 스텝 인디케이터 ────────────────────────────────────────────────────────────

const StepIndicator: React.FC<{ current: Step; completed: Set<Step> }> = ({ current, completed }) => {
  const steps: { n: Step; label: string }[] = [
    { n: 1, label: "서버 연결" },
    { n: 2, label: "채널 설정" },
    { n: 3, label: "완료" },
  ];

  return (
    <div style={{ display: "flex", alignItems: "center", marginBottom: 16 }}>
      {steps.map((s, i) => {
        const done = completed.has(s.n);
        const active = s.n === current;
        const color = done ? "#22c55e" : active
          ? "var(--vscode-textLink-foreground, #4a9eff)"
          : "var(--vscode-descriptionForeground, #555)";

        return (
          <React.Fragment key={s.n}>
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", flex: 1 }}>
              <div style={{
                width: 26, height: 26, borderRadius: "50%",
                border: `2px solid ${color}`,
                background: done ? color : "transparent",
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 11, fontWeight: 700, color: done ? "#fff" : color,
                transition: "all 0.2s",
              }}>
                {done ? <CheckIcon size={12} /> : s.n}
              </div>
              <div style={{ fontSize: 9, color, marginTop: 3, fontWeight: active ? 700 : 400 }}>
                {s.label}
              </div>
            </div>
            {i < steps.length - 1 && (
              <div style={{
                height: 2, flex: 2, marginBottom: 14,
                background: done ? "#22c55e" : "var(--vscode-panel-border, #333)",
                transition: "background 0.2s",
              }} />
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
};

// ── 연결 상태 배지 ─────────────────────────────────────────────────────────────

const ConnectionBadge: React.FC<{ connected: boolean; guildName: string; guildId: string }> = ({
  connected, guildName, guildId
}) => (
  <div style={{
    display: "flex", alignItems: "center", gap: 8,
    padding: "8px 12px", borderRadius: 6,
    background: connected ? "rgba(34,197,94,0.1)" : "rgba(107,114,128,0.12)",
    border: `1px solid ${connected ? "#22c55e40" : "#6b728030"}`,
    marginBottom: 12,
  }}>
    <div style={{ position: "relative", width: 10, height: 10 }}>
      <div style={{
        width: 10, height: 10, borderRadius: "50%",
        background: connected ? "#22c55e" : "#6b7280",
      }} />
      {connected && (
        <div style={{
          position: "absolute", top: 0, left: 0, width: 10, height: 10,
          borderRadius: "50%", background: "#22c55e",
          animation: "pulse 1.5s ease-out infinite", opacity: 0.6,
        }} />
      )}
      <style>{`@keyframes pulse{0%{transform:scale(1);opacity:.6}100%{transform:scale(2.5);opacity:0}}`}</style>
    </div>
    <div style={{ flex: 1 }}>
      <div style={{
        fontSize: 12, fontWeight: 600,
        color: connected ? "#22c55e" : "var(--vscode-descriptionForeground, #888)",
      }}>
        {connected ? (guildName || "봇 연동됨") : "연동 안 됨"}
      </div>
      {connected && guildId && (
        <div style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #777)", marginTop: 1 }}>
          Guild ID: {guildId}
        </div>
      )}
    </div>
    <DiscordIcon size={14} />
  </div>
);

// ── 서버 카드 (resolve 성공 후 표시) ────────────────────────────────────────────

const ServerCard: React.FC<{ guild: ResolvedGuild; onInvite: () => void; inviteConfirmed: boolean; onConfirm: () => void }> = ({
  guild, onInvite, inviteConfirmed, onConfirm
}) => (
  <div style={{
    padding: "12px 14px", borderRadius: 8, marginBottom: 12,
    background: "rgba(88,101,242,0.08)",
    border: "1px solid rgba(88,101,242,0.25)",
  }}>
    {/* 서버 정보 */}
    <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
      {guild.guildIcon ? (
        <img
          src={guild.guildIcon}
          alt=""
          style={{ width: 40, height: 40, borderRadius: 10, objectFit: "cover" }}
        />
      ) : (
        <div style={{
          width: 40, height: 40, borderRadius: 10,
          background: "rgba(88,101,242,0.2)", color: "#5865f2",
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          <ServerIcon size={20} />
        </div>
      )}
      <div>
        <div style={{ fontSize: 13, fontWeight: 700, color: "var(--vscode-foreground, #e0e0e0)" }}>
          {guild.guildName}
        </div>
        {guild.memberCount !== null && (
          <div style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #888)", marginTop: 2 }}>
            멤버 {guild.memberCount.toLocaleString()}명
          </div>
        )}
      </div>
      <div style={{
        marginLeft: "auto", padding: "3px 8px", borderRadius: 999,
        background: "rgba(34,197,94,0.12)", color: "#22c55e",
        fontSize: 10, fontWeight: 600,
      }}>
        서버 확인됨
      </div>
    </div>

    {/* 봇 추가 버튼 */}
    <button
      onClick={onInvite}
      style={{
        ...S.btn("invite"),
        width: "100%", justifyContent: "center",
        padding: "10px 14px", marginBottom: 10,
      }}
    >
      <DiscordIcon size={16} />
      봇을 "{guild.guildName}"에 추가하기
      <ExternalLinkIcon size={12} />
    </button>

    {/* 추가 완료 확인 */}
    <label style={{
      display: "flex", alignItems: "center", gap: 8,
      cursor: "pointer", padding: "7px 10px", borderRadius: 5,
      background: inviteConfirmed ? "rgba(34,197,94,0.08)" : "rgba(255,255,255,0.03)",
      border: `1px solid ${inviteConfirmed ? "#22c55e30" : "var(--vscode-panel-border, #333)"}`,
      userSelect: "none",
    }}>
      <div
        onClick={onConfirm}
        style={{
          width: 16, height: 16, borderRadius: 3, flexShrink: 0, cursor: "pointer",
          border: `2px solid ${inviteConfirmed ? "#22c55e" : "var(--vscode-panel-border, #555)"}`,
          background: inviteConfirmed ? "#22c55e" : "transparent",
          display: "flex", alignItems: "center", justifyContent: "center",
        }}
      >
        {inviteConfirmed && <CheckIcon size={10} />}
      </div>
      <span style={{ fontSize: 11, color: "var(--vscode-foreground, #ccc)" }}>
        봇을 서버에 추가했습니다
      </span>
    </label>
  </div>
);

// ── 메인 컴포넌트 ──────────────────────────────────────────────────────────────

export const DiscordIntegration: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();

  const [step, setStep] = useState<Step>(1);
  const [completedSteps, setCompletedSteps] = useState<Set<Step>>(new Set());

  // 1단계: 초대 링크 입력 및 서버 확인
  const [inviteInput, setInviteInput] = useState("");
  const [resolving, setResolving] = useState(false);
  const [resolveError, setResolveError] = useState("");
  const [resolvedGuild, setResolvedGuild] = useState<ResolvedGuild | null>(null);
  const [inviteConfirmed, setInviteConfirmed] = useState(false);

  // 2단계: 채널 ID
  const [deployChannelId, setDeployChannelId] = useState("");
  const [incidentChannelId, setIncidentChannelId] = useState("");
  const [standupChannelId, setStandupChannelId] = useState("");
  const [standupCron, setStandupCron] = useState("0 9 * * 1-5");

  // 저장 상태
  const [connected, setConnected] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  // ── 마운트 시 설정 로드 ──────────────────────────────────────────────────────
  useEffect(() => {
    postMessage("discord.loadConfig", {});
  }, [postMessage]);

  // ── 메시지 수신 ──────────────────────────────────────────────────────────────
  useMessage(
    useCallback((msg: { type: string; payload: any }) => {
      const { type, payload } = msg;

      if (type === "discord.configLoaded") {
        const cfg = payload as DiscordConfig & { connected?: boolean };
        setDeployChannelId(cfg.deployChannelId || "");
        setIncidentChannelId(cfg.incidentChannelId || "");
        setStandupChannelId(cfg.standupChannelId || "");
        setStandupCron(cfg.standupCron || "0 9 * * 1-5");
        setConnected(!!cfg.connected);

        // 기존 설정이 있으면 복원
        if (cfg.guildId) {
          setResolvedGuild({
            guildId: cfg.guildId,
            guildName: cfg.guildName || `서버 (${cfg.guildId})`,
            guildIcon: null,
            memberCount: null,
          });
          setInviteConfirmed(true);
          const completed = new Set<Step>([1, 2]);
          setCompletedSteps(completed);
          setStep(3);
        }
      }

      if (type === "discord.inviteResolved") {
        setResolving(false);
        if (payload.ok) {
          setResolvedGuild({
            guildId: payload.guildId,
            guildName: payload.guildName,
            guildIcon: payload.guildIcon,
            memberCount: payload.memberCount,
          });
          setResolveError("");
        } else {
          setResolveError(payload.error || "서버 확인에 실패했습니다.");
          setResolvedGuild(null);
        }
      }

      if (type === "discord.saved") {
        setSaving(false);
        if (payload.ok) {
          setConnected(true);
          setSaved(true);
          setTimeout(() => setSaved(false), 2500);
        }
      }
    }, [])
  );

  // ── 핸들러 ───────────────────────────────────────────────────────────────────

  const handleResolveInvite = () => {
    if (!inviteInput.trim()) return;
    setResolving(true);
    setResolveError("");
    setResolvedGuild(null);
    setInviteConfirmed(false);
    postMessage("discord.resolveInvite", { inviteUrl: inviteInput.trim() });
  };

  const handleOpenBotInvite = () => {
    const url = makeBotInviteUrl(resolvedGuild?.guildId);
    postMessage("discord.openInvite", { url });
  };

  const handleSave = () => {
    if (!resolvedGuild) return;
    setSaving(true);
    postMessage("discord.saveConfig", {
      guildId: resolvedGuild.guildId,
      guildName: resolvedGuild.guildName,
      deployChannelId: deployChannelId.trim(),
      incidentChannelId: incidentChannelId.trim(),
      standupChannelId: standupChannelId.trim(),
      standupCron: standupCron.trim() || "0 9 * * 1-5",
    });
  };

  const markDone = (s: Step) => {
    setCompletedSteps((prev) => new Set([...prev, s]));
    if (s < 3) setStep((s + 1) as Step);
  };

  // ── 단계별 렌더 ───────────────────────────────────────────────────────────────

  // 1단계: 서버 초대 링크 입력
  const renderStep1 = () => (
    <div style={S.section}>
      <label style={S.label}>1단계 — Discord 서버 초대 링크 입력</label>

      <p style={{ ...S.hint, marginBottom: 12, fontSize: 11, color: "var(--vscode-foreground, #bbb)" }}>
        봇을 추가할 Discord 서버의 초대 링크를 붙여넣으세요.
        서버 이름과 ID를 자동으로 확인합니다.
      </p>

      {/* 링크 입력 + 확인 버튼 */}
      <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
        <input
          type="text"
          value={inviteInput}
          onChange={(e) => {
            setInviteInput(e.target.value);
            setResolveError("");
          }}
          onKeyDown={(e) => { if (e.key === "Enter") handleResolveInvite(); }}
          placeholder="discord.gg/abcdefg 또는 https://discord.gg/..."
          style={{ ...S.input, flex: 1 }}
        />
        <button
          onClick={handleResolveInvite}
          disabled={!inviteInput.trim() || resolving}
          style={{
            ...S.btn("secondary"),
            flexShrink: 0,
            opacity: !inviteInput.trim() || resolving ? 0.5 : 1,
          }}
        >
          {resolving ? <SpinnerIcon size={12} /> : <SearchIcon size={12} />}
          {resolving ? "확인 중" : "서버 확인"}
        </button>
      </div>

      {/* 에러 */}
      {resolveError && (
        <div style={{
          padding: "7px 10px", borderRadius: 5, marginBottom: 10,
          background: "rgba(248,81,73,0.1)", border: "1px solid rgba(248,81,73,0.3)",
          fontSize: 11, color: "#f85149",
        }}>
          {resolveError}
        </div>
      )}

      {/* 서버 확인 성공 카드 */}
      {resolvedGuild && (
        <ServerCard
          guild={resolvedGuild}
          onInvite={handleOpenBotInvite}
          inviteConfirmed={inviteConfirmed}
          onConfirm={() => setInviteConfirmed((v) => !v)}
        />
      )}

      {/* 다음 버튼 */}
      <div style={{ marginTop: 4 }}>
        <button
          onClick={() => markDone(1)}
          disabled={!inviteConfirmed || !resolvedGuild}
          style={{
            ...S.btn("primary"),
            opacity: (!inviteConfirmed || !resolvedGuild) ? 0.45 : 1,
          }}
        >
          <CheckIcon size={12} /> 다음 →
        </button>
      </div>
    </div>
  );

  // 2단계: 채널 설정
  const renderStep2 = () => (
    <div style={S.section}>
      <label style={S.label}>2단계 — 알림 채널 설정 (선택)</label>
      <p style={{ ...S.hint, marginBottom: 12 }}>
        각 채널 <b>우클릭</b> → "채널 ID 복사". 비워두면 해당 알림을 보내지 않습니다.
        (먼저 Discord 설정 → 고급 → <b>개발자 모드</b>를 켜세요)
      </p>

      {[
        { key: "deploy",   label: "배포 알림 채널",   value: deployChannelId,   setter: setDeployChannelId,   ph: "배포 시작·완료·실패 알림" },
        { key: "incident", label: "인시던트 채널",    value: incidentChannelId, setter: setIncidentChannelId, ph: "새벽 긴급 인시던트 알림" },
        { key: "standup",  label: "Standup 채널",    value: standupChannelId,  setter: setStandupChannelId,  ph: "매일 아침 운영 브리핑" },
      ].map(({ key, label, value, setter, ph }) => (
        <div key={key} style={{ marginBottom: 10 }}>
          <label style={{ ...S.label, fontSize: 10, marginBottom: 4 }}>{label}</label>
          <input
            type="text"
            value={value}
            onChange={(e) => setter(e.target.value.replace(/\D/g, ""))}
            placeholder={ph}
            style={S.input}
            maxLength={20}
          />
        </div>
      ))}

      <div style={{ marginBottom: 12 }}>
        <label style={{ ...S.label, fontSize: 10, marginBottom: 4 }}>Standup 스케줄 (cron)</label>
        <input
          type="text"
          value={standupCron}
          onChange={(e) => setStandupCron(e.target.value)}
          placeholder="0 9 * * 1-5"
          style={{ ...S.input, fontFamily: "monospace" }}
        />
        <p style={S.hint}>기본값: 평일 오전 9시 (Asia/Seoul)</p>
      </div>

      <div style={{ display: "flex", gap: 8 }}>
        <button onClick={() => markDone(2)} style={S.btn("primary")}>
          <CheckIcon size={12} /> 다음 →
        </button>
        <button onClick={() => setStep(1)} style={S.btn("ghost")}>← 이전</button>
      </div>
    </div>
  );

  // 3단계: 저장 및 완료
  const renderStep3 = () => (
    <div style={S.section}>
      <label style={{ ...S.label, marginBottom: 10 }}>연동 설정 완료</label>

      <ConnectionBadge connected={connected} guildName={resolvedGuild?.guildName || ""} guildId={resolvedGuild?.guildId || ""} />

      {/* 설정 요약 */}
      {resolvedGuild && (
        <div style={{
          padding: "10px 12px", borderRadius: 6, marginBottom: 12,
          background: "rgba(255,255,255,0.03)",
          border: "1px solid var(--vscode-panel-border, #333)",
          fontSize: 11,
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
            <span style={{ color: "var(--vscode-descriptionForeground, #888)" }}>서버</span>
            <span style={{ fontWeight: 600 }}>{resolvedGuild.guildName}</span>
          </div>
          {deployChannelId && (
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
              <span style={{ color: "var(--vscode-descriptionForeground, #888)" }}>배포 채널</span>
              <span style={{ fontFamily: "monospace", fontSize: 10 }}>{deployChannelId}</span>
            </div>
          )}
          {incidentChannelId && (
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
              <span style={{ color: "var(--vscode-descriptionForeground, #888)" }}>인시던트 채널</span>
              <span style={{ fontFamily: "monospace", fontSize: 10 }}>{incidentChannelId}</span>
            </div>
          )}
          {standupChannelId && (
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--vscode-descriptionForeground, #888)" }}>Standup 채널</span>
              <span style={{ fontFamily: "monospace", fontSize: 10 }}>{standupChannelId}</span>
            </div>
          )}
        </div>
      )}

      <button
        onClick={handleSave}
        disabled={saving || !resolvedGuild}
        style={{
          ...S.btn(saved ? "secondary" : "primary"),
          width: "100%", justifyContent: "center",
          opacity: (saving || !resolvedGuild) ? 0.5 : 1,
        }}
      >
        {saving
          ? <><SpinnerIcon size={12} /> 저장 중...</>
          : saved
          ? <><CheckIcon size={12} /> 저장 완료!</>
          : "설정 저장"}
      </button>

      <div style={{ display: "flex", justifyContent: "center", marginTop: 8 }}>
        <button onClick={() => setStep(2)} style={S.btn("ghost")}>← 채널 설정 수정</button>
      </div>
    </div>
  );

  // ── 렌더 ──────────────────────────────────────────────────────────────────────

  return (
    <div style={{
      padding: "14px 12px 16px",
      color: "var(--vscode-foreground, #e0e0e0)",
      fontFamily: "var(--vscode-font-family, sans-serif)",
    }}>
      {/* 헤더 */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
        <div style={{
          width: 34, height: 34, borderRadius: 8,
          background: "rgba(88,101,242,0.18)", color: "#5865f2",
          display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
        }}>
          <DiscordIcon size={20} />
        </div>
        <div>
          <div style={{ fontSize: 13, fontWeight: 700 }}>Discord 연동</div>
          <div style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #888)", marginTop: 1 }}>
            서버 초대 링크를 붙여넣어 봇을 연결하세요
          </div>
        </div>
      </div>

      {/* 스텝 인디케이터 */}
      <StepIndicator current={step} completed={completedSteps} />

      {/* 현재 단계 */}
      {step === 1 && renderStep1()}
      {step === 2 && renderStep2()}
      {step === 3 && renderStep3()}

      {/* 완료된 단계 목록 */}
      {completedSteps.size > 0 && (
        <div style={{ marginBottom: 10 }}>
          {([1, 2] as Step[])
            .filter((s) => completedSteps.has(s) && s !== step)
            .map((s) => (
              <button
                key={s}
                onClick={() => setStep(s)}
                style={{
                  width: "100%", textAlign: "left",
                  padding: "7px 12px", borderRadius: 6, marginBottom: 4,
                  border: "1px solid var(--vscode-panel-border, #333)",
                  background: "rgba(34,197,94,0.05)",
                  cursor: "pointer", display: "flex", alignItems: "center", gap: 8,
                  fontSize: 11, color: "#22c55e",
                }}
              >
                <CheckIcon size={11} />
                {s === 1 && `서버 연결 — ${resolvedGuild?.guildName || "완료"}`}
                {s === 2 && "채널 설정 완료"}
                <span style={{ marginLeft: "auto", fontSize: 10, color: "var(--vscode-textLink-foreground, #4a9eff)" }}>
                  수정
                </span>
              </button>
            ))}
        </div>
      )}

      {/* 사용 안내 (연동 완료 후) */}
      {connected && (
        <div style={{
          padding: "10px 12px", borderRadius: 6, marginTop: 4,
          background: "rgba(88,101,242,0.08)",
          border: "1px solid rgba(88,101,242,0.2)",
          fontSize: 11, color: "var(--vscode-descriptionForeground, #8b949e)",
          lineHeight: 1.6,
        }}>
          Discord에서{" "}
          <code style={{ color: "#5865f2", fontFamily: "monospace" }}>/recoder</code>{" "}
          명령으로 ReCoder를 사용하세요.<br />
          <span style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #666)" }}>
            preflight · status · deploy · rollback · code
          </span>
        </div>
      )}
    </div>
  );
};

export default DiscordIntegration;
