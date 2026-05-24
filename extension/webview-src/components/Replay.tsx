/**
 * Replay.tsx — Deploy Replay 재생 UI (설계서 §38.2)
 *
 * 기능:
 *   - 배포 타임라인 이벤트를 시각적으로 재생
 *   - 속도 조절: 0.5x / 1x / 2x (§38.2)
 *   - 시점 점프: 이벤트 클릭으로 즉시 이동 (§38.2)
 *   - 이벤트 종류별 색상 구분 및 상세 패널
 *   - Postmortem 다운로드 (§38.4)
 *   - 학습/포트폴리오 목적 공유 링크 생성
 */

import React, { useState, useEffect, useRef, useCallback } from "react";

// ── 타입 정의 ────────────────────────────────────────────────────────────

interface ReplayEvent {
  ts: string;
  ts_unix: number;
  kind: string;
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

interface ReplayProps {
  deployId?: string;
  timeline?: ReplayTimeline;
  onClose?: () => void;
}

// ── 상수 ─────────────────────────────────────────────────────────────────

const EVENT_COLORS: Record<string, string> = {
  DEPLOY_START: "#3b82f6",   // blue
  APPROVAL: "#10b981",       // green
  ROLLBACK: "#f59e0b",       // amber
  INCIDENT: "#ef4444",       // red
  LLM_CALL: "#8b5cf6",       // violet
  GIT_COMMIT: "#6b7280",     // gray
  METRIC_SPIKE: "#f97316",   // orange
};

const SEVERITY_COLORS: Record<string, string> = {
  INFO: "#3b82f6",
  WARN: "#f59e0b",
  ERROR: "#ef4444",
  CRITICAL: "#7f1d1d",
};

const SEVERITY_ICONS: Record<string, string> = {
  INFO: "ℹ️",
  WARN: "⚠️",
  ERROR: "❌",
  CRITICAL: "🚨",
};

const SPEED_OPTIONS = [0.5, 1, 2] as const;
type Speed = typeof SPEED_OPTIONS[number];

// ── 유틸리티 ──────────────────────────────────────────────────────────────

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return m > 0 ? `${m}분 ${s}초` : `${s}초`;
}

function formatTs(ts: string): string {
  try {
    return new Date(ts).toLocaleString("ko-KR", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

// ── 컴포넌트 ──────────────────────────────────────────────────────────────

const EventDot: React.FC<{
  event: ReplayEvent;
  position: number; // 0~100%
  isActive: boolean;
  isCurrent: boolean;
  onClick: () => void;
}> = ({ event, position, isActive, isCurrent, onClick }) => {
  const color = EVENT_COLORS[event.kind] || "#6b7280";
  const size = isCurrent ? 14 : isActive ? 10 : 8;

  return (
    <div
      className="absolute transform -translate-x-1/2 -translate-y-1/2 cursor-pointer group"
      style={{ left: `${position}%`, top: "50%" }}
      onClick={onClick}
      title={event.title}
    >
      {/* 툴팁 */}
      <div
        className="absolute bottom-6 left-1/2 transform -translate-x-1/2
                   bg-gray-900 text-white text-xs rounded px-2 py-1 whitespace-nowrap
                   opacity-0 group-hover:opacity-100 transition-opacity z-10 pointer-events-none"
        style={{ maxWidth: 200 }}
      >
        <div className="font-semibold">{event.kind}</div>
        <div className="text-gray-300">{event.title.slice(0, 40)}</div>
        <div className="text-gray-400">{formatTs(event.ts)}</div>
      </div>

      {/* 도트 */}
      <div
        style={{
          width: size,
          height: size,
          backgroundColor: color,
          borderRadius: "50%",
          border: isCurrent ? "2px solid white" : "none",
          boxShadow: isCurrent ? `0 0 8px ${color}` : "none",
          transition: "all 0.2s ease",
        }}
      />
    </div>
  );
};

const EventDetail: React.FC<{ event: ReplayEvent }> = ({ event }) => {
  const color = SEVERITY_COLORS[event.severity] || "#3b82f6";
  const icon = SEVERITY_ICONS[event.severity] || "ℹ️";

  return (
    <div
      className="rounded-lg p-4 mb-2"
      style={{
        backgroundColor: "var(--vscode-editor-background)",
        border: `1px solid ${color}40`,
        borderLeft: `4px solid ${color}`,
      }}
    >
      <div className="flex items-start gap-2">
        <span>{icon}</span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className="text-xs px-2 py-0.5 rounded font-mono"
              style={{
                backgroundColor: (EVENT_COLORS[event.kind] || "#6b7280") + "30",
                color: EVENT_COLORS[event.kind] || "#6b7280",
              }}
            >
              {event.kind}
            </span>
            <span className="text-xs" style={{ color: "var(--vscode-descriptionForeground)" }}>
              {formatTs(event.ts)}
            </span>
            {event.actor && (
              <span className="text-xs" style={{ color: "var(--vscode-descriptionForeground)" }}>
                by {event.actor}
              </span>
            )}
          </div>
          <div
            className="mt-1 font-medium text-sm truncate"
            style={{ color: "var(--vscode-foreground)" }}
          >
            {event.title}
          </div>
          {event.detail && (
            <pre
              className="mt-2 text-xs whitespace-pre-wrap break-words"
              style={{
                color: "var(--vscode-descriptionForeground)",
                fontFamily: "var(--vscode-editor-font-family, monospace)",
                maxHeight: 120,
                overflow: "auto",
              }}
            >
              {event.detail.slice(0, 400)}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
};

// ── 메인 Replay 컴포넌트 ──────────────────────────────────────────────────

const Replay: React.FC<ReplayProps> = ({ deployId, timeline: initialTimeline, onClose }) => {
  const [timeline, setTimeline] = useState<ReplayTimeline | null>(initialTimeline ?? null);
  const [loading, setLoading] = useState(!initialTimeline && !!deployId);
  const [error, setError] = useState<string | null>(null);

  // 재생 상태
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentIndex, setCurrentIndex] = useState(0);
  const [speed, setSpeed] = useState<Speed>(1);
  const [selectedEvent, setSelectedEvent] = useState<ReplayEvent | null>(null);
  const [showPostmortem, setShowPostmortem] = useState(false);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── 데이터 로드 ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!deployId || initialTimeline) return;
    setLoading(true);

    // VSCode extension API를 통해 Local Core에서 타임라인 로드
    const vscode = (window as any).acquireVsCodeApi?.();
    if (vscode) {
      vscode.postMessage({ type: "REPLAY_LOAD", deployId });
    }

    const handler = (event: MessageEvent) => {
      if (event.data?.type === "REPLAY_DATA") {
        setTimeline(event.data.timeline);
        setLoading(false);
      } else if (event.data?.type === "REPLAY_ERROR") {
        setError(event.data.message);
        setLoading(false);
      }
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [deployId, initialTimeline]);

  // ── 재생 엔진 ──────────────────────────────────────────────────────────
  const events = timeline?.events ?? [];

  const play = useCallback(() => {
    if (!events.length || currentIndex >= events.length - 1) return;
    setIsPlaying(true);
  }, [events.length, currentIndex]);

  const pause = useCallback(() => {
    setIsPlaying(false);
  }, []);

  const reset = useCallback(() => {
    setIsPlaying(false);
    setCurrentIndex(0);
    setSelectedEvent(null);
  }, []);

  const jumpTo = useCallback((index: number) => {
    setCurrentIndex(index);
    setSelectedEvent(events[index] ?? null);
    setIsPlaying(false);
  }, [events]);

  // 재생 타이머
  useEffect(() => {
    if (!isPlaying) {
      if (intervalRef.current) clearInterval(intervalRef.current);
      return;
    }

    // 이벤트 간 실제 시간 간격 기반 재생 (최소 200ms, 최대 3000ms)
    const getNextDelay = (idx: number): number => {
      if (idx >= events.length - 1) return 1000;
      const diff = (events[idx + 1].ts_unix - events[idx].ts_unix) * 1000;
      const scaled = diff / speed;
      return Math.max(200, Math.min(scaled, 3000));
    };

    let currentIdx = currentIndex;

    const tick = () => {
      if (currentIdx >= events.length - 1) {
        setIsPlaying(false);
        return;
      }
      currentIdx += 1;
      setCurrentIndex(currentIdx);
      setSelectedEvent(events[currentIdx]);

      // 다음 이벤트까지 딜레이 조정
      if (intervalRef.current) clearInterval(intervalRef.current);
      intervalRef.current = setTimeout(tick, getNextDelay(currentIdx));
    };

    intervalRef.current = setTimeout(tick, getNextDelay(currentIdx));
    return () => {
      if (intervalRef.current) clearTimeout(intervalRef.current);
    };
  }, [isPlaying, speed, events]); // currentIndex는 의도적으로 제외

  // ── 포스트모텀 다운로드 ────────────────────────────────────────────────
  const downloadPostmortem = () => {
    if (!timeline?.postmortem_md) return;
    const blob = new Blob([timeline.postmortem_md], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `postmortem-${timeline.deploy_id.slice(0, 8)}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // ── 렌더링 ───────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <div className="text-2xl mb-2">⏳</div>
          <div style={{ color: "var(--vscode-descriptionForeground)" }}>
            타임라인 로딩 중...
          </div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 rounded" style={{ backgroundColor: "#ef444420", border: "1px solid #ef4444" }}>
        <div className="font-semibold text-red-400">❌ 로드 실패</div>
        <div className="text-sm mt-1" style={{ color: "var(--vscode-descriptionForeground)" }}>
          {error}
        </div>
      </div>
    );
  }

  if (!timeline) {
    return (
      <div className="p-8 text-center" style={{ color: "var(--vscode-descriptionForeground)" }}>
        배포 ID를 입력하거나 타임라인 데이터를 전달하세요.
      </div>
    );
  }

  const progressPercent =
    events.length > 1 ? (currentIndex / (events.length - 1)) * 100 : 0;

  return (
    <div
      className="flex flex-col h-full"
      style={{
        backgroundColor: "var(--vscode-sideBar-background)",
        color: "var(--vscode-foreground)",
        fontFamily: "var(--vscode-font-family, sans-serif)",
      }}
    >
      {/* ── 헤더 ── */}
      <div
        className="flex items-center justify-between px-4 py-3"
        style={{ borderBottom: "1px solid var(--vscode-panel-border)" }}
      >
        <div>
          <div className="font-semibold text-sm flex items-center gap-2">
            🎬 Deploy Replay
            <span
              className="text-xs px-2 py-0.5 rounded font-mono"
              style={{
                backgroundColor: "var(--vscode-badge-background)",
                color: "var(--vscode-badge-foreground)",
              }}
            >
              {timeline.service}
            </span>
          </div>
          <div className="text-xs mt-0.5" style={{ color: "var(--vscode-descriptionForeground)" }}>
            {timeline.cluster} · {timeline.region} · {events.length}개 이벤트 ·{" "}
            {formatDuration(timeline.duration_seconds)}
            {!timeline.otel_available && (
              <span className="ml-2 text-yellow-500">⚠️ OTel 미연결</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowPostmortem(!showPostmortem)}
            className="text-xs px-3 py-1.5 rounded"
            style={{
              backgroundColor: "var(--vscode-button-secondaryBackground)",
              color: "var(--vscode-button-secondaryForeground)",
            }}
          >
            📋 Postmortem
          </button>
          {onClose && (
            <button
              onClick={onClose}
              className="text-xs px-2 py-1 rounded opacity-60 hover:opacity-100"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* ── Postmortem 패널 ── */}
      {showPostmortem && timeline.postmortem_md && (
        <div
          className="px-4 py-3"
          style={{
            backgroundColor: "var(--vscode-editor-background)",
            borderBottom: "1px solid var(--vscode-panel-border)",
            maxHeight: 300,
            overflow: "auto",
          }}
        >
          <div className="flex items-center justify-between mb-2">
            <div className="font-semibold text-sm">§38.4 Postmortem</div>
            <button
              onClick={downloadPostmortem}
              className="text-xs px-3 py-1 rounded"
              style={{
                backgroundColor: "var(--vscode-button-background)",
                color: "var(--vscode-button-foreground)",
              }}
            >
              ⬇️ 다운로드 .md
            </button>
          </div>
          <pre
            className="text-xs whitespace-pre-wrap"
            style={{
              color: "var(--vscode-descriptionForeground)",
              fontFamily: "var(--vscode-editor-font-family, monospace)",
            }}
          >
            {timeline.postmortem_md.slice(0, 1000)}
            {timeline.postmortem_md.length > 1000 && "\n...(다운로드하여 전체 확인)"}
          </pre>
        </div>
      )}

      {/* ── 타임라인 바 ── */}
      <div className="px-4 py-4" style={{ borderBottom: "1px solid var(--vscode-panel-border)" }}>
        <div className="relative h-8">
          {/* 배경 바 */}
          <div
            className="absolute left-0 right-0 top-1/2 transform -translate-y-1/2 rounded-full"
            style={{ height: 4, backgroundColor: "var(--vscode-panel-border)" }}
          />
          {/* 진행 바 */}
          <div
            className="absolute left-0 top-1/2 transform -translate-y-1/2 rounded-full transition-all"
            style={{
              width: `${progressPercent}%`,
              height: 4,
              backgroundColor: "#3b82f6",
            }}
          />
          {/* 이벤트 도트 */}
          {events.map((event, idx) => {
            const position =
              timeline.duration_seconds > 0
                ? ((event.ts_unix - events[0].ts_unix) / timeline.duration_seconds) * 100
                : (idx / Math.max(events.length - 1, 1)) * 100;
            return (
              <EventDot
                key={`${event.ts}-${idx}`}
                event={event}
                position={position}
                isActive={idx <= currentIndex}
                isCurrent={idx === currentIndex}
                onClick={() => jumpTo(idx)}
              />
            );
          })}
        </div>

        {/* 시간 레이블 */}
        <div className="flex justify-between mt-1 text-xs" style={{ color: "var(--vscode-descriptionForeground)" }}>
          <span>{formatTs(timeline.start_ts)}</span>
          <span>{timeline.end_ts ? formatTs(timeline.end_ts) : "진행 중"}</span>
        </div>
      </div>

      {/* ── 컨트롤 ── */}
      <div
        className="flex items-center gap-3 px-4 py-3"
        style={{ borderBottom: "1px solid var(--vscode-panel-border)" }}
      >
        {/* 재생/일시정지 */}
        <button
          onClick={isPlaying ? pause : play}
          disabled={events.length === 0 || currentIndex >= events.length - 1}
          className="px-4 py-1.5 rounded text-sm font-medium"
          style={{
            backgroundColor: "var(--vscode-button-background)",
            color: "var(--vscode-button-foreground)",
            opacity: events.length === 0 ? 0.5 : 1,
          }}
        >
          {isPlaying ? "⏸ 일시정지" : "▶ 재생"}
        </button>

        {/* 리셋 */}
        <button
          onClick={reset}
          className="px-3 py-1.5 rounded text-sm"
          style={{
            backgroundColor: "var(--vscode-button-secondaryBackground)",
            color: "var(--vscode-button-secondaryForeground)",
          }}
        >
          ⏮ 처음
        </button>

        {/* 속도 선택 (§38.2) */}
        <div className="flex items-center gap-1 ml-2">
          <span className="text-xs" style={{ color: "var(--vscode-descriptionForeground)" }}>
            속도:
          </span>
          {SPEED_OPTIONS.map((s) => (
            <button
              key={s}
              onClick={() => setSpeed(s)}
              className="px-2.5 py-1 rounded text-xs"
              style={{
                backgroundColor:
                  speed === s
                    ? "var(--vscode-button-background)"
                    : "var(--vscode-button-secondaryBackground)",
                color:
                  speed === s
                    ? "var(--vscode-button-foreground)"
                    : "var(--vscode-button-secondaryForeground)",
              }}
            >
              {s}x
            </button>
          ))}
        </div>

        {/* 진행 상태 */}
        <div className="ml-auto text-xs" style={{ color: "var(--vscode-descriptionForeground)" }}>
          {currentIndex + 1} / {events.length}
        </div>
      </div>

      {/* ── 현재 이벤트 상세 + 이벤트 목록 ── */}
      <div className="flex flex-1 overflow-hidden">
        {/* 현재 이벤트 상세 */}
        <div
          className="flex-1 overflow-auto p-4"
          style={{ minWidth: 0 }}
        >
          <div
            className="text-xs font-semibold mb-2 uppercase tracking-wide"
            style={{ color: "var(--vscode-descriptionForeground)" }}
          >
            현재 이벤트
          </div>
          {selectedEvent ? (
            <EventDetail event={selectedEvent} />
          ) : (
            <div
              className="text-sm"
              style={{ color: "var(--vscode-descriptionForeground)" }}
            >
              ▶ 재생을 시작하거나 타임라인의 이벤트를 클릭하세요.
            </div>
          )}

          {/* 근본 원인 / 재발 방지 */}
          {(timeline.root_cause || timeline.prevention) && (
            <div className="mt-4">
              {timeline.root_cause && (
                <div className="mb-3">
                  <div className="text-xs font-semibold mb-1 text-yellow-500">🔍 근본 원인</div>
                  <div className="text-sm">{timeline.root_cause}</div>
                </div>
              )}
              {timeline.prevention && (
                <div>
                  <div className="text-xs font-semibold mb-1 text-green-500">🛡️ 재발 방지</div>
                  <div className="text-sm">{timeline.prevention}</div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* 이벤트 목록 (우측 사이드) */}
        <div
          className="w-48 overflow-auto border-l p-2"
          style={{
            borderColor: "var(--vscode-panel-border)",
            backgroundColor: "var(--vscode-editor-background)",
          }}
        >
          <div
            className="text-xs font-semibold mb-2 uppercase tracking-wide px-1"
            style={{ color: "var(--vscode-descriptionForeground)" }}
          >
            이벤트 목록
          </div>
          {events.map((event, idx) => (
            <div
              key={`list-${idx}`}
              onClick={() => jumpTo(idx)}
              className="flex items-center gap-1.5 px-2 py-1.5 rounded cursor-pointer mb-0.5"
              style={{
                backgroundColor:
                  idx === currentIndex
                    ? "var(--vscode-list-activeSelectionBackground)"
                    : idx < currentIndex
                    ? "var(--vscode-list-inactiveSelectionBackground)"
                    : "transparent",
                color:
                  idx === currentIndex
                    ? "var(--vscode-list-activeSelectionForeground)"
                    : "var(--vscode-foreground)",
              }}
            >
              <div
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: "50%",
                  backgroundColor: EVENT_COLORS[event.kind] || "#6b7280",
                  flexShrink: 0,
                }}
              />
              <div className="text-xs truncate flex-1">{event.title.slice(0, 24)}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

export default Replay;
