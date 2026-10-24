/** 3.0.2 — ✓ ✕ ✗ 같은 글자 기호 대신 쓰는 작은 선 표시(SVG). 색은 부모 글자색을 따른다. */
import React from "react";

const D: Record<string, string> = { ok: "m5 12 4.5 4.5L19 7", bad: "M7 7l10 10M17 7 7 17", skip: "M7 12h10", warn: "M12 6v8M12 18v.01" };

export function MarkGlyph({ kind, size = 12, label }: { kind: "ok" | "bad" | "skip" | "warn"; size?: number; label?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={kind === "ok" ? 3 : 2.8}
      strokeLinecap="round" strokeLinejoin="round" role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true}
      style={{ display: "inline-block", verticalAlign: "-0.12em", flex: "none" }}>
      <path d={D[kind]} />
    </svg>
  );
}
