/**
 * ReCoder — Replay Component (§38)
 *
 * Deploy Replay: 배포 이벤트 타임라인을 영상처럼 재생하는 UI.
 *
 * 기능:
 *   - 속도 조절: 0.5x / 1x / 2x
 *   - 시점 점프: 타임라인 바 클릭 또는 이벤트 클릭
 *   - 이벤트 종류별 아이콘/색상 구분
 *   - Postmortem 자동 생성 내용 표시 (§38.4)
 */

import React, { useState, useEffect, useRef, useCallback } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

// ---------------------------------------------------------------------------
// Types (ReplayTimeline과 동일한 구조 — timeline_builder.py 참조)
// ---------------------------------------------------------------------------

interface ReplayEvent {
  ts: string;
  ts_unix: number;
  kind:
    | "DEPLOY_START"
    | "APPROVAL"
    | "ROLLBACK"
    | "INCIDENT"
    | "LLM_CALL"
    | "GIT_COMMIT"
    | "METRIC_SPIKE";
  title: string;
  detail: string;
  actor: string;
  severity: "INFO" | "WARN" | "ERROR" | "CRITICAL";
  metadata: Record<string, unknown>;
}

interface ReplayTimeline {
  deploy_id: string;
  service: string;
  cluster: string;
  region: string;
  start_ts: string;
  end_ts: string | null;
  duration_seconds: number;
  events: ReplayEvent[];
  otel_available: boolean;
  root_cause: string;
  prevention: string;
  postmortem_md: string;
}

type PlaybackSpeed = 0.5 | 1 | 2;
type ReplayState = "idle" | "loading" | "ready" | "playing" | "paused" | "done" | "error";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const KIND_META: Record<
  ReplayEvent["kind"],
  { icon: string; color: string; label: string }
> = {
  DEPLOY_START:  { icon: "🚀", color: "#3b82f6", label: "배포 시작" },
  APPROVAL:      { icon: "✅", color: "#22c55e", label: "승인" },
  ROLLBACK:      { icon: "↩️", color: "#f59e0b", label: "롤백" },
  INCIDENT:      { icon: "🚨", color: "#ef4444", label: "인시던트" },
  LLM_CALL:      { icon: "🤖", color: "#a78bfa", label: "AI 호출" },
  GIT_COMMIT:    { icon: "📝", color: "#64748b", label: "커밋" },
  METRIC_SPIKE:  { icon: "📈", color: "#fb923c", label: "메트릭 스파이크" },
};

const SEVERITY_COLOR: Record<ReplayEvent["severity"], string> = {
  INFO:     "var(--vscode-editor-foreground, #ccc)",
  WARN:     "#f59e0b",
  ERROR:    "#ef4444",
  CRITICAL: "#b91c1c",
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDuration(seconds: number): string {
  if (seconds < 60) { return `${Math.round(seconds)}s`; }
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

function formatTs(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString("ko-KR", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts.slice(11, 19);
  }
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

const EventDot: React.FC<{
  event: ReplayEvent;
  position: number; // 0~100%
  isActive: boolean;
  onClick: () => void;
}> = ({ event, position, isActive, onClick }) => {
  const meta = KIND_META[event.kind] ?? KIND_META.GIT_COMMIT;
  return (
    <div
      title={`${meta.icon} ${event.title}`}
      onClick={onClick}
      style={{
        position: "absolute",
        left: `${position}%`,
        top: "50%",
        transform: "translate(-50%, -50%)",
        width: isActive ? 14 : 10,
        height: isActive ? 14 : 10,
        borderRadius: "50%",
        background: meta.color,
        border: `2px solid ${isActive ? "#fff" : "transparent"}`,
        cursor: "pointer",
        transition: "all 0.15s",
        zIndex: isActive ? 10 : 5,
        boxShadow: isActive ? `0 0 6px ${meta.color}` : "none",
      }}
    />
  );
};

const EventRow: React.FC<{
  event: ReplayEvent;
  isActive: boolean;
  onClick: () => void;
}> = ({ event, isActive, onClick }) => {
  const meta = KIND_META[event.kind] ?? KIND_META.GIT_COMMIT;
  return (
    <div
      onClick={onClick}
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 8,
        padding: "6px 8px",
        borderRadius: 5,
        background: isActive
          ? `${meta.color}22`
          : "transparent",
        border: `1px solid ${isActive ? meta.color : "transparent"}`,
        cursor: "pointer",
        marginBottom: 3,
        transition: "all 0.12s",
      }}
    >
      <span style={{ fontSize: 14, lineHeight: 1.4, flexShrink: 0 }}>{meta.icon}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 11,
            fontWeight: isActive ? 600 : 400,
            color: isActive ? meta.color : "var(--vscode-editor-foreground, #ccc)",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {event.title}
        </div>
        {isActive && (
          <div
            style={{
              fontSize: 10,
              color: "var(--vscode-descriptionForeground, #888)",
              marginTop: 2,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {event.detail.slice(0, 200)}
          </div>
        )}
      </div>
      <div
        style={{
          fontSize: 10,
          color: SEVERITY_COLOR[event.severity],
          flexShrink: 0,
          fontFamily: "monospace",
        }}
      >
        {formatTs(event.ts)}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export const Replay: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();

  const [state, setState] = useState<ReplayState>("idle");
  const [timeline, setTimeline] = useState<ReplayTimeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deployId, setDeployId] = useState("");

  // 재생 상태
  const [currentIdx, setCurrentIdx] = useState(0);
  const [speed, setSpeed] = useState<PlaybackSpeed>(1);
  const [showPostmortem, setShowPostmortem] = useState(false);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // ── 메시지 수신 ────────────────────────────────────────────────────────

  useMessage(
    useCallback((msg) => {
      const { type, payload } = msg as { type: string; payload: unknown };
      if (type === "replayTimeline") {
        setTimeline(payload as ReplayTimeline);
        setState("ready");
        setCurrentIdx(0);
        setError(null);
      }
      if (type === "errorMessage") {
        setError((payload as { message: string }).message);
        setState("error");
      }
    }, [])
  );

  // ── 재생 로직 ──────────────────────────────────────────────────────────

  const stopInterval = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  const play = useCallback(() => {
    if (!timeline || timeline.events.length === 0) { return; }
    setState("playing");

    // 이벤트 간 실제 시간 간격을 speed 배율로 재생
    const advance = () => {
      setCurrentIdx((prev) => {
        const next = prev + 1;
        if (next >= timeline.events.length) {
          stopInterval();
          setState("done");
          return prev;
        }
        return next;
      });
    };

    // 평균 간격 기반 tick (최소 400ms, 최대 3000ms)
    const avgInterval = timeline.duration_seconds > 0
      ? Math.min(3000, Math.max(400, (timeline.duration_seconds * 1000) / timeline.events.length / speed))
      : 800 / speed;

    intervalRef.current = setInterval(advance, avgInterval);
  }, [timeline, speed, stopInterval]);

  const pause = useCallback(() => {
    stopInterval();
    setState("paused");
  }, [stopInterval]);

  const reset = useCallback(() => {
    stopInterval();
    setCurrentIdx(0);
    setState("ready");
  }, [stopInterval]);

  // speed 변경 시 재생 중이면 재시작
  useEffect(() => {
    if (state === "playing") {
      stopInterval();
      play();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [speed]);

  // 현재 이벤트로 스크롤
  useEffect(() => {
    if (!listRef.current) { return; }
    const rows = listRef.current.querySelectorAll("[data-event-row]");
    if (rows[currentIdx]) {
      (rows[currentIdx] as HTMLElement).scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [currentIdx]);

  // unmount 시 정리
  useEffect(() => () => stopInterval(), [stopInterval]);

  // ── 핸들러 ────────────────────────────────────────────────────────────

  const handleLoad = () => {
    if (!deployId.trim()) { return; }
    setState("loading");
    setError(null);
    setTimeline(null);
    postMessage("loadReplay", { deployId: deployId.trim() });
  };

  // ── 스타일 상수 ───────────────────────────────────────────────────────

  const card: React.CSSProperties = {
    background: "var(--vscode-editorWidget-background, #252526)",
    border: "1px solid var(--vscode-panel-border, #333)",
    borderRadius: 6,
    padding: "10px 12px",
    marginBottom: 10,
  };

  const btnBase: React.CSSProperties = {
    border: "none",
    borderRadius: 4,
    padding: "5px 12px",
    fontSize: 11,
    fontWeight: 600,
    cursor: "pointer",
    transition: "opacity 0.1s",
  };

  // ── 렌더 ─────────────────────────────────────────────────────────────

  const events = timeline?.events ?? [];
  const progressPct = events.length > 1 ? (currentIdx / (events.length - 1)) * 100 : 0;

  return (
    <div
      style={{
        fontFamily: "var(--vscode-font-family, sans-serif)",
        fontSize: 12,
        color: "var(--vscode-editor-foreground, #ccc)",
        padding: 2,
      }}
    >
      {/* ── 헤더 ── */}
      <div
        style={{
          fontSize: 11,
          fontWeight: 700,
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          color: "var(--vscode-descriptionForeground, #888)",
          marginBottom: 10,
        }}
      >
        🎬 Deploy Replay
      </div>

      {/* ── Deploy ID 입력 ── */}
      <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
        <input
          value={deployId}
          onChange={(e) => setDeployId(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleLoad()}
          placeholder="Deploy ID 입력..."
          style={{
            flex: 1,
            background: "var(--vscode-input-background, #3c3c3c)",
            border: "1px solid var(--vscode-input-border, #555)",
            borderRadius: 4,
            padding: "5px 8px",
            fontSize: 11,
            color: "var(--vscode-input-foreground, #ccc)",
            outline: "none",
          }}
        />
        <button
          onClick={handleLoad}
          disabled={state === "loading" || !deployId.trim()}
          style={{
            ...btnBase,
            background: "var(--vscode-button-background, #0078d4)",
            color: "var(--vscode-button-foreground, #fff)",
            opacity: state === "loading" || !deployId.trim() ? 0.5 : 1,
          }}
        >
          {state === "loading" ? "로딩 중…" : "불러오기"}
        </button>
      </div>

      {/* ── 에러 ── */}
      {state === "error" && error && (
        <div
          style={{
            background: "rgba(239,68,68,0.1)",
            border: "1px solid #ef4444",
            borderRadius: 5,
            padding: "7px 10px",
            color: "#ef4444",
            marginBottom: 10,
            fontSize: 11,
          }}
        >
          {error}
        </div>
      )}

      {/* ── 타임라인 메타 ── */}
      {timeline && (
        <div style={card}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: "3px 12px",
              fontSize: 11,
              marginBottom: 8,
            }}
          >
            {[
              ["서비스", timeline.service],
              ["클러스터", timeline.cluster],
              ["리전", timeline.region],
              ["소요 시간", formatDuration(timeline.duration_seconds)],
              ["이벤트 수", `${events.length}개`],
              ["OTel", timeline.otel_available ? "✅ 연결됨" : "⚪ 미연결"],
            ].map(([k, v]) => (
              <div key={k} style={{ display: "flex", gap: 4 }}>
                <span style={{ color: "var(--vscode-descriptionForeground, #888)" }}>
                  {k}:
                </span>
                <span style={{ fontWeight: 500 }}>{v}</span>
              </div>
            ))}
          </div>

          {/* ── 진행 바 ── */}
          <div
            style={{
              position: "relative",
              height: 20,
              background: "var(--vscode-scrollbarSlider-background, #333)",
              borderRadius: 10,
              marginBottom: 8,
              cursor: "pointer",
            }}
            onClick={(e) => {
              const rect = e.currentTarget.getBoundingClientRect();
              const pct = (e.clientX - rect.left) / rect.width;
              const idx = Math.round(pct * (events.length - 1));
              setCurrentIdx(Math.max(0, Math.min(idx, events.length - 1)));
              if (state === "done") { setState("paused"); }
            }}
          >
            {/* 채워진 바 */}
            <div
              style={{
                position: "absolute",
                left: 0,
                top: 0,
                height: "100%",
                width: `${progressPct}%`,
                background: "var(--vscode-progressBar-background, #0078d4)",
                borderRadius: 10,
                transition: "width 0.2s",
              }}
            />
            {/* 이벤트 점 */}
            {events.map((ev, i) => (
              <EventDot
                key={i}
                event={ev}
                position={(i / Math.max(1, events.length - 1)) * 100}
                isActive={i === currentIdx}
                onClick={(e?: React.MouseEvent) => {
                  e?.stopPropagation?.();
                  setCurrentIdx(i);
                  if (state === "done") { setState("paused"); }
                }}
              />
            ))}
          </div>

          {/* ── 재생 컨트롤 ── */}
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {/* Play/Pause/Reset */}
            {state === "playing" ? (
              <button
                onClick={pause}
                style={{ ...btnBase, background: "#374151", color: "#f9fafb" }}
              >
                ⏸ 일시정지
              </button>
            ) : state === "done" ? (
              <button
                onClick={reset}
                style={{ ...btnBase, background: "#374151", color: "#f9fafb" }}
              >
                🔁 다시보기
              </button>
            ) : (
              <button
                onClick={play}
                disabled={events.length === 0}
                style={{
                  ...btnBase,
                  background: "var(--vscode-button-background, #0078d4)",
                  color: "var(--vscode-button-foreground, #fff)",
                  opacity: events.length === 0 ? 0.4 : 1,
                }}
              >
                ▶ 재생
              </button>
            )}

            {/* 속도 버튼 */}
            <div style={{ display: "flex", gap: 3, marginLeft: 4 }}>
              {([0.5, 1, 2] as PlaybackSpeed[]).map((s) => (
                <button
                  key={s}
                  onClick={() => setSpeed(s)}
                  style={{
                    ...btnBase,
                    padding: "4px 8px",
                    background: speed === s ? "#1d4ed8" : "#2d2d2d",
                    color: speed === s ? "#fff" : "#9ca3af",
                    border: `1px solid ${speed === s ? "#3b82f6" : "#444"}`,
                  }}
                >
                  {s}x
                </button>
              ))}
            </div>

            {/* 현재 위치 표시 */}
            <span
              style={{
                marginLeft: "auto",
                fontSize: 10,
                color: "var(--vscode-descriptionForeground, #888)",
                fontFamily: "monospace",
              }}
            >
              {events.length > 0
                ? `${currentIdx + 1} / ${events.length}`
                : "—"}
            </span>
          </div>
        </div>
      )}

      {/* ── 현재 이벤트 상세 ── */}
      {events[currentIdx] && (
        <div
          style={{
            ...card,
            borderColor: KIND_META[events[currentIdx].kind]?.color ?? "#333",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
            <span style={{ fontSize: 16 }}>
              {KIND_META[events[currentIdx].kind]?.icon}
            </span>
            <span style={{ fontWeight: 700, fontSize: 12 }}>
              {events[currentIdx].title}
            </span>
            <span
              style={{
                marginLeft: "auto",
                fontSize: 10,
                color: SEVERITY_COLOR[events[currentIdx].severity],
                fontWeight: 600,
              }}
            >
              {events[currentIdx].severity}
            </span>
          </div>
          <div
            style={{
              fontSize: 11,
              color: "var(--vscode-descriptionForeground, #888)",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              maxHeight: 80,
              overflowY: "auto",
            }}
          >
            {events[currentIdx].detail}
          </div>
          <div
            style={{
              marginTop: 4,
              fontSize: 10,
              color: "var(--vscode-descriptionForeground, #666)",
            }}
          >
            {formatTs(events[currentIdx].ts)}
            {events[currentIdx].actor && ` · ${events[currentIdx].actor}`}
          </div>
        </div>
      )}

      {/* ── 이벤트 목록 ── */}
      {events.length > 0 && (
        <div
          ref={listRef}
          style={{
            maxHeight: 240,
            overflowY: "auto",
            marginBottom: 10,
          }}
        >
          {events.map((ev, i) => (
            <div key={i} data-event-row>
              <EventRow
                event={ev}
                isActive={i === currentIdx}
                onClick={() => {
                  setCurrentIdx(i);
                  if (state === "done") { setState("paused"); }
                }}
              />
            </div>
          ))}
        </div>
      )}

      {/* ── Postmortem ── */}
      {timeline?.postmortem_md && (
        <div>
          <button
            onClick={() => setShowPostmortem((v) => !v)}
            style={{
              ...btnBase,
              background: "transparent",
              color: "var(--vscode-descriptionForeground, #888)",
              border: "1px solid var(--vscode-panel-border, #333)",
              width: "100%",
              textAlign: "left",
              padding: "6px 10px",
            }}
          >
            {showPostmortem ? "▾" : "▸"} Postmortem 보기 (§38.4 자동 생성)
          </button>
          {showPostmortem && (
            <div
              style={{
                background: "var(--vscode-textCodeBlock-background, #1a1a1a)",
                border: "1px solid var(--vscode-panel-border, #333)",
                borderRadius: "0 0 5px 5px",
                padding: "10px 12px",
                fontSize: 11,
                fontFamily: "var(--vscode-editor-font-family, monospace)",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                maxHeight: 300,
                overflowY: "auto",
                color: "var(--vscode-editor-foreground, #ccc)",
              }}
            >
              {timeline.postmortem_md}
            </div>
          )}
        </div>
      )}

      {/* ── 빈 상태 ── */}
      {state === "idle" && (
        <div
          style={{
            textAlign: "center",
            color: "var(--vscode-descriptionForeground, #666)",
            padding: "24px 0",
            fontSize: 11,
          }}
        >
          Deploy ID를 입력하고 재생해보세요.
          <br />
          <span style={{ fontSize: 10 }}>
            예: <code>dep_e09cdf77</code>
          </span>
        </div>
      )}
    </div>
  );
};

export default Replay;
