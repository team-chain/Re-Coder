/**
 * 허브·기능 아이콘 — 이모지 대체.
 *
 * 이모지는 OS 마다 다르게 그려지고 제품 화면에서 장난감처럼 보인다. App.tsx 의
 * 인라인 SVG(Logo·Alert·Code…)와 같은 규칙으로 통일한다: 24 viewBox, stroke 1.8,
 * currentColor, round cap/join. 색은 부모가 정한다(허브 accent 등).
 */
import React from "react";

export type HubIconName =
  | "code" | "rocket" | "shield"
  | "terminal" | "bug" | "network" | "file-text"
  | "box" | "cloud-upload" | "history" | "activity"
  | "search" | "key" | "clipboard-check";

const PATHS: Record<HubIconName, React.ReactNode> = {
  // ── 허브 ──
  code: <><polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" /></>,
  rocket: <>
    <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" />
    <path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" />
    <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0" />
    <path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />
  </>,
  shield: <><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /><polyline points="9 12 11 14 15 10" /></>,
  // ── Develop ──
  terminal: <><polyline points="4 17 10 11 4 5" /><line x1="12" y1="19" x2="20" y2="19" /></>,
  bug: <>
    <path d="M8 2l1.88 1.88M14.12 3.88L16 2M9 7.13v-1a3.003 3.003 0 1 1 6 0v1" />
    <path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6z" />
    <path d="M12 20v-9M6.53 9C4.6 8.8 3 7.1 3 5M6 13H2M3 21c0-2.1 1.7-3.9 3.8-4M20.97 5c0 2.1-1.6 3.8-3.5 4M22 13h-4M17.2 17c2.1.1 3.8 1.9 3.8 4" />
  </>,
  network: <>
    <rect x="16" y="16" width="6" height="6" rx="1" /><rect x="2" y="16" width="6" height="6" rx="1" /><rect x="9" y="2" width="6" height="6" rx="1" />
    <path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3M12 12V8" />
  </>,
  "file-text": <>
    <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z" />
    <polyline points="14 2 14 8 20 8" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" /><line x1="10" y1="9" x2="8" y2="9" />
  </>,
  // ── Deploy ──
  box: <>
    <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
    <polyline points="3.27 6.96 12 12.01 20.73 6.96" /><line x1="12" y1="22.08" x2="12" y2="12" />
  </>,
  "cloud-upload": <>
    <path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242" />
    <path d="M12 12v9" /><path d="m16 16-4-4-4 4" />
  </>,
  history: <>
    <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" /><path d="M3 3v5h5" /><path d="M12 7v5l4 2" />
  </>,
  activity: <><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></>,
  // ── Security ──
  search: <><circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /></>,
  key: <>
    <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
  </>,
  "clipboard-check": <>
    <rect x="8" y="2" width="8" height="4" rx="1" ry="1" />
    <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
    <path d="m9 14 2 2 4-4" />
  </>,
};

export const HubIcon: React.FC<{ name: HubIconName; size?: number; strokeWidth?: number; style?: React.CSSProperties }> = ({ name, size = 18, strokeWidth = 1.8, style }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth}
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" style={{ flexShrink: 0, display: "block", ...style }}>
    {PATHS[name]}
  </svg>
);

/** "#rrggbb" → "rgba(r,g,b,a)". 웹뷰 Chromium 버전에 기대지 않으려고 color-mix 대신 직접 계산. */
export function tint(hex: string, alpha: number): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) { return hex; }
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

/** 허브 카드 상단의 아이콘 타일 — 은은한 accent 배경 위에 선 아이콘. */
export const HubIconTile: React.FC<{ name: HubIconName; accent: string; size?: number }> = ({ name, accent, size = 40 }) => (
  <div style={{
    width: size, height: size, borderRadius: Math.round(size * 0.28),
    background: tint(accent, 0.14),
    border: `1px solid ${tint(accent, 0.35)}`,
    color: accent, display: "flex", alignItems: "center", justifyContent: "center",
  }}>
    <HubIcon name={name} size={Math.round(size * 0.55)} strokeWidth={1.7} />
  </div>
);
