/**
 * 선 아이콘(2.0.46) — 이모지 대신 쓰는 단색 아이콘. 24×24 격자, 1.8 선, currentColor.
 * 외부에서 불러오지 않는다(웹뷰는 외부 리소스를 막는다). 기술 로고(ECS·S3·Docker·GitHub)는 canvas/brandIcons.
 */
import React from "react";

const P: Record<string, string> = {
  code: "m8 7-5 5 5 5M16 7l5 5-5 5M14 4l-4 16",
  bug: "M9 7a3 3 0 0 1 6 0M7 10h10v4a5 5 0 0 1-10 0zM12 10v9M7 13H3M21 13h-4M7 17l-3 2M17 17l3 2M8 8 5.5 5.5M16 8l2.5-2.5",
  graph: "M3 4h7v5H3zM14 4h7v5h-7zM8.5 15h7v5h-7zM6.5 9v2.5h11V9M12 11.5V15",
  flow: "M5 4v6a4 4 0 0 0 4 4h6M15 10l4 4-4 4M5 20a1.5 1.5 0 1 0 0-.01",
  doc: "M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6",
  chip: "M8 8h8v8H8zM10 3v3M14 3v3M10 18v3M14 18v3M3 10h3M3 14h3M18 10h3M18 14h3",
  box: "M12 3 4 7v10l8 4 8-4V7zM4 7l8 4 8-4M12 11v10",
  upload: "M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 8a4 4 0 0 1 0 8h-1M12 12v8M9 15l3-3 3 3",
  history: "M3 12a9 9 0 1 0 3-6.7M3 4v4h4M12 8v4l3 2",
  pulse: "M3 12h4l2-6 4 12 2-6h6",
  scan: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4M8 11h6",
  key: "M15 4a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM11 12 3 20M6 17l2 2M8 15l2 2",
  policy: "M12 3 5 6v6c0 4 3 7.5 7 9 4-1.5 7-5 7-9V6zM9 12l2 2 4-4",
  home: "M4 11 12 4l8 7M6 10v10h12V10",
  shield: "M12 3 5 6v6c0 4 3 7.5 7 9 4-1.5 7-5 7-9V6z",
  bell: "M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20a2 2 0 0 0 4 0",
  lock: "M5 11h14v9H5zM8 11V8a4 4 0 0 1 8 0v3",
  globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3",
  database: "M12 4c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3zM4 7v10c0 1.7 3.6 3 8 3s8-1.3 8-3V7M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  wall: "M3 5h18v14H3zM3 10h18M3 15h18M9 5v5M15 10v5M9 15v4",
  search: "M11 5a6 6 0 1 0 0 12 6 6 0 0 0 0-12zM20 20l-4.5-4.5",
  check: "m5 12 4.5 4.5L19 7",
  plus: "M12 5v14M5 12h14",
  chevron: "m9 6 6 6-6 6",
  close: "M6 6l12 12M18 6 6 18",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  settings: "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM19 12l2-1-1-3-2 .3-1.3-1.3.3-2-3-1-1 2h-2l-1-2-3 1 .3 2L5.3 8.3 3.3 8l-1 3 2 1v2l-2 1 1 3 2-.3 1.3 1.3-.3 2 3 1 1-2h2l1 2 3-1-.3-2 1.3-1.3 2 .3 1-3-2-1z",
  external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  branch: "M6 3v12M6 15a3 3 0 1 0 0 .01M18 9a3 3 0 1 0 0-.01M18 9c0 4-6 3-12 6",
  file: "M7 3h7l5 5v13H7zM14 3v5h5",
};

export type IconName = keyof typeof P;

export function Icon({ name, size = 16, title, style, className }: { name: IconName | string; size?: number; title?: string; style?: React.CSSProperties; className?: string }) {
  const d = P[name] ?? P.file;
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden={title ? undefined : true} role={title ? "img" : undefined} data-icon={name} style={{ flex: "none", ...style }}>
      {title && <title>{title}</title>}
      <path d={d} />
    </svg>
  );
}

export const ICON_NAMES = Object.keys(P) as IconName[];
