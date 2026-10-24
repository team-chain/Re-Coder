/** 3.0.2 화면 조각 — 로고 · 기술 로고 · 링 · 면 그래프 · 시간 표기. 아이콘 타일·이모지는 쓰지 않는다. */
import React from "react";
import { BRAND_GLYPHS } from "../canvas/brandIcons";

/** 확장에 들어 있는 ReCoder 로고(media/icon.png). 테스트 · 하네스처럼 경로가 없으면 같은 모양의 선 로고. */
export function BrandLogo({ size = 28 }: { size?: number }) {
  const src = typeof document !== "undefined" ? document.documentElement.dataset.recoderLogo : "";
  if (src) return <img src={src} width={size} height={size} alt="" aria-hidden="true" />;
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" fill="none" stroke="var(--rc-teal,#35A9D1)" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M 51 16 A 22 22 0 0 0 13 22" strokeWidth="3.2" /><polyline points="51,7 51,16 42,16" strokeWidth="3.2" />
      <path d="M 13 48 A 22 22 0 0 0 51 42" strokeWidth="3.2" /><polyline points="13,57 13,48 22,48" strokeWidth="3.2" />
      <line x1="22" y1="20" x2="22" y2="46" strokeWidth="3.6" /><path d="M 22 20 L 30 20 A 6 6 0 0 1 30 32 L 22 32" strokeWidth="3.6" />
      <line x1="27" y1="32" x2="35" y2="46" strokeWidth="3.6" />
    </svg>
  );
}

/** 실제 기술 로고(Docker · GitHub · S3 · ECS · Discord). */
export function TechLogo({ kind, size = 22 }: { kind: "docker" | "github" | "s3" | "ecs" | "discord"; size?: number }) {
  const g = BRAND_GLYPHS[kind];
  if (!g) return null;
  const aws = g.viewBox === "0 0 64 64";
  return (
    <svg width={size} height={size} viewBox={g.viewBox} role="img" aria-label={g.title} style={{ display: "block", flex: "none", borderRadius: aws ? 5 : 0 }}>
      {g.paths.map((p, i) => <path key={i} d={p.d} fill={p.fill || "currentColor"} fillRule={aws ? "evenodd" : undefined} />)}
    </svg>
  );
}

/** 작은 선 아이콘(경로 하나). 버튼 안 보조용으로만. */
const PATHS: Record<string, string> = {
  chevron: "m9 6 6 6-6 6",
  external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  upload: "M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 8a4 4 0 0 1 0 8h-1M12 12v8M9 15l3-3 3 3",
  lock: "M5 11h14v9H5zM8 11V8a4 4 0 0 1 8 0v3",
  branch: "M6 3v12M6 15a3 3 0 1 0 0 .01M18 9a3 3 0 1 0 0-.01M18 9c0 4-6 3-12 6",
  spark: "M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6",
  arrow: "M5 12h14M13 6l6 6-6 6",
  check: "m5 12 4.5 4.5L19 7",
  refresh: "M20 11a8 8 0 0 0-14.6-4.5M4 4v4h4M4 13a8 8 0 0 0 14.6 4.5M20 20v-4h-4",
};
export function Glyph({ name, size = 14, sw = 1.9 }: { name: keyof typeof PATHS | string; size?: number; sw?: number }) {
  const d = PATHS[name];
  if (!d) return null;
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ display: "block", flex: "none" }}><path d={d} /></svg>;
}

/** 원형 게이지 — 보안 검사처럼 "얼마나 됐나"를 한눈에. */
export function Ring({ value, size = 96, tone = "ok", children }: { value: number; size?: number; tone?: "ok" | "warn" | "bad" | "off"; children?: React.ReactNode }) {
  const r = 40, c = 2 * Math.PI * r;
  const v = Math.max(0, Math.min(1, value));
  const id = `rc-ring-${tone}`;
  const stops: Record<string, [string, string]> = { ok: ["#3DD68C", "#35A9D1"], warn: ["#F2B544", "#F29A44"], bad: ["#F27272", "#E5484D"], off: ["#67768A", "#67768A"] };
  return (
    <div style={{ position: "relative", width: size, height: size, flex: "none" }}>
      <svg width={size} height={size} viewBox="0 0 96 96" aria-hidden="true">
        <defs><linearGradient id={id} x1="0" x2="1" y1="0" y2="1"><stop offset="0" stopColor={stops[tone][0]} /><stop offset="1" stopColor={stops[tone][1]} /></linearGradient></defs>
        <circle cx="48" cy="48" r={r} fill="none" stroke="var(--rc-line2)" strokeWidth="8" />
        {v > 0 && <circle cx="48" cy="48" r={r} fill="none" stroke={`url(#${id})`} strokeWidth="8" strokeLinecap="round" strokeDasharray={`${(c * v).toFixed(1)} ${c.toFixed(1)}`} transform="rotate(-90 48 48)" />}
      </svg>
      <div style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", textAlign: "center" }}>{children}</div>
    </div>
  );
}

/** 면 그래프 — 그리드 · 그라데이션 · 강조 점 하나(가장 큰 오류). */
export function AreaChart({ values, errors, height = 140 }: { values: number[]; errors?: number[]; height?: number }) {
  const W = 560, H = 150;
  if (!values || values.length < 2) return null;
  const max = Math.max(...values, 1) * 1.15;
  const pts = values.map((v, i) => [i * W / (values.length - 1), H - 8 - (v / max) * (H - 30)] as const);
  const line = "M" + pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" L");
  const area = `${line} L${W},${H} L0,${H} Z`;
  let spike = -1;
  if (errors && errors.length === values.length) {
    const top = Math.max(...errors);
    if (top > 0) spike = errors.indexOf(top);
  }
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ display: "block", width: "100%", height }} aria-hidden="true">
      <defs>
        <linearGradient id="rc-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#35A9D1" stopOpacity=".38" /><stop offset="1" stopColor="#35A9D1" stopOpacity="0" /></linearGradient>
        <linearGradient id="rc-line" x1="0" x2="1"><stop offset="0" stopColor="#7891B4" /><stop offset=".6" stopColor="#35A9D1" /><stop offset="1" stopColor="#7FD6F2" /></linearGradient>
      </defs>
      <g stroke="var(--rc-line)"><line x1="0" x2={W} y1={H * .4} y2={H * .4} /><line x1="0" x2={W} y1={H * .7} y2={H * .7} /></g>
      <path d={area} fill="url(#rc-area)" />
      <path d={line} fill="none" stroke="url(#rc-line)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
      {spike >= 0 && <line x1={pts[spike][0]} x2={pts[spike][0]} y1={pts[spike][1]} y2={H} stroke="rgba(242,114,114,.55)" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />}
    </svg>
  );
}

export function timeAgo(iso: string | number | undefined, now = Date.now()): string {
  if (!iso) return "";
  const t = typeof iso === "number" ? iso : Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const m = Math.max(0, Math.round((now - t) / 60000));
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}시간 전`;
  return `${Math.round(h / 24)}일 전`;
}
