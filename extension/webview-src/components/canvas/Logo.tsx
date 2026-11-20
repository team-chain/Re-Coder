import React from "react";
import { BRAND_GLYPHS } from "./brandIcons";

// Inline paths: never load textures, fonts, or logos from the network in a webview.
const icons: Record<string, string> = {
  project: "M9 4H7a2 2 0 0 0-2 2v3l-2 3 2 3v3a2 2 0 0 0 2 2h2M15 4h2a2 2 0 0 1 2 2v3l2 3-2 3v3a2 2 0 0 1-2 2h-2",
  folder: "M2 6h8l2 3h10v11H2V6Z", file: "M6 2h8l5 5v15H6V2Zm8 0v6h5M9 12h7M9 16h7",
  fn: "M16 3h-3c-3 0-3 3-3 6l-2 10c0 2-1 3-4 2M6 9h11",
};

export function Logo({ kind }: { kind: string }) {
  //: 실제 기술 로고 — S3·ECS 는 AWS 공식 아키텍처 아이콘(색 그대로), Docker·GitHub·Discord 는 상표 모양(노드 색).
  const brand = BRAND_GLYPHS[kind];
  if (brand) {
    const aws = brand.viewBox === "0 0 64 64";
    return <svg className={aws ? "rc-logo rc-logo-aws" : "rc-logo"} viewBox={brand.viewBox} width={aws ? 34 : 28} height={aws ? 34 : 28} role="img" aria-label={brand.title}>
      {brand.paths.map((p, i) => <path key={i} d={p.d} fill={p.fill || "currentColor"} fillRule={aws ? "evenodd" : undefined} />)}
    </svg>;
  }
  return <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true">{icons[kind] ? <path d={icons[kind]} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" /> : <text x="12" y="16" fontSize={13} fontWeight="800" fill="currentColor" textAnchor="middle">{kind === "gate" ? "·" : kind.toUpperCase()}</text>}</svg>;
}
