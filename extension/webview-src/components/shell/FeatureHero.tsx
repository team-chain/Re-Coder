/**
 * 3.0.2 기능 화면 머리 — 제목 한 줄 · 설명 한 줄 · 무엇을 하는 화면인지 보여 주는 작은 그림.
 * 그림은 선 · 청록 그라데이션만 쓴다(캐릭터 · 이모지 · 아이콘 타일 없음).
 */
import React from "react";
import { TechLogo } from "./parts";

type Kind = "build" | "ship" | "replay" | "operate" | "models" | "adr" | "policy";

const COPY: Record<Kind, { title: string; desc: string }> = {
  build: { title: "에러 분석", desc: "오류 로그를 붙이면 원인과 수정안을 보여 줍니다. 적용은 확인한 뒤에만 합니다." },
  ship: { title: "로컬 Docker", desc: "Dockerfile 생성 → 검사 → 빌드 → 실행 → 헬스 확인. 실패하면 이전 이미지로 되돌립니다." },
  replay: { title: "배포 기록 · 롤백", desc: "지금까지의 배포와 롤백을 시간순으로. 이전 버전으로는 승인한 뒤에만 되돌립니다." },
  operate: { title: "운영 대응", desc: "장애 · 공격 신호를 모아 원인을 분석하고 조치를 제안합니다. 실행은 승인 후에만." },
  models: { title: "AI 모델 · 비용", desc: "작업마다 쓸 모델을 고르고, 같은 요청의 결과와 비용을 비교합니다." },
  adr: { title: "설계 기록", desc: "코드 생성에서 확정한 설계 결정과 이유가 docs/adr 에 쌓입니다." },
  policy: { title: "정책 게이트", desc: "배포 종류별 승인 단계와 차단 조건입니다." },
};

const G = (id: string) => (
  <defs>
    <linearGradient id={id} x1="0" x2="1" y1="0" y2="1"><stop offset="0" stopColor="#36B1D9" /><stop offset="1" stopColor="#7B8FB6" /></linearGradient>
  </defs>
);

function Art({ kind }: { kind: Kind }) {
  const box = { stroke: "var(--rc-line2)", fill: "var(--rc-solid)" } as const;
  switch (kind) {
    case "build": return (
      <svg width="250" height="96" viewBox="0 0 250 96" aria-hidden="true">{G("fh-b")}
        <rect x="1" y="6" width="150" height="84" rx="10" {...box} />
        <rect x="12" y="20" width="70" height="5" rx="2.5" fill="var(--rc-line2)" /><rect x="12" y="33" width="120" height="7" rx="3" fill="rgba(242,114,114,.35)" />
        <rect x="12" y="48" width="96" height="5" rx="2.5" fill="var(--rc-line2)" /><rect x="12" y="60" width="110" height="5" rx="2.5" fill="var(--rc-line2)" />
        <path d="M158 48h22" stroke="url(#fh-b)" strokeWidth="2" strokeLinecap="round" /><path d="m174 42 7 6-7 6" stroke="#36B1D9" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        <rect x="188" y="26" width="60" height="44" rx="10" fill="rgba(61,214,140,.08)" stroke="rgba(61,214,140,.45)" />
        <rect x="198" y="40" width="40" height="6" rx="3" fill="rgba(61,214,140,.55)" /><rect x="198" y="52" width="28" height="5" rx="2.5" fill="var(--rc-line2)" />
      </svg>);
    case "ship": return (
      <svg width="250" height="96" viewBox="0 0 250 96" aria-hidden="true">{G("fh-s")}
        {[0, 1, 2].map(i => <rect key={i} x={70 + i * 8} y={58 - i * 18} width="104" height="26" rx="8" fill="var(--rc-solid)" stroke={i === 2 ? "url(#fh-s)" : "var(--rc-line2)"} />)}
        <foreignObject x="20" y="34" width="34" height="34"><div style={{ color: "#2496ED" }}><TechLogo kind="docker" size={30} /></div></foreignObject>
        <rect x="196" y="38" width="50" height="20" rx="10" fill="rgba(61,214,140,.1)" stroke="rgba(61,214,140,.45)" />
        <circle cx="206" cy="48" r="3" fill="#3DD68C" /><rect x="214" y="45" width="24" height="6" rx="3" fill="rgba(61,214,140,.55)" />
      </svg>);
    case "replay": return (
      <svg width="250" height="96" viewBox="0 0 250 96" aria-hidden="true">{G("fh-r")}
        <path d="M18 56h214" stroke="var(--rc-line2)" strokeWidth="2" />
        {[0, 1, 2, 3].map(i => <g key={i}><circle cx={40 + i * 56} cy="56" r={i === 3 ? 9 : 7} fill={i === 3 ? "url(#fh-r)" : "var(--rc-solid)"} stroke={i === 2 ? "#F2B544" : "var(--rc-line2)"} strokeWidth="2" />
          <rect x={30 + i * 56} y="74" width="20" height="5" rx="2.5" fill="var(--rc-line2)" /></g>)}
        <path d="M208 40c-14-22-46-22-60 0" fill="none" stroke="url(#fh-r)" strokeWidth="2" strokeDasharray="4 4" /><path d="m146 34 2 7 7-2" fill="none" stroke="#7B8FB6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>);
    case "operate": return (
      <svg width="250" height="96" viewBox="0 0 250 96" aria-hidden="true">{G("fh-o")}
        <polyline points="8,62 40,58 60,64 78,30 92,70 112,56 140,60" fill="none" stroke="url(#fh-o)" strokeWidth="2.2" strokeLinejoin="round" />
        <circle cx="78" cy="30" r="5" fill="#F27272" />
        <rect x="156" y="16" width="90" height="22" rx="11" fill="rgba(242,181,68,.1)" stroke="rgba(242,181,68,.5)" /><rect x="166" y="24" width="50" height="6" rx="3" fill="rgba(242,181,68,.6)" />
        <rect x="156" y="44" width="90" height="22" rx="11" fill="var(--rc-solid)" stroke="var(--rc-line2)" /><rect x="166" y="52" width="60" height="6" rx="3" fill="var(--rc-line2)" />
        <rect x="156" y="72" width="90" height="20" rx="10" fill="url(#fh-o)" opacity=".85" />
      </svg>);
    case "models": return (
      <svg width="250" height="96" viewBox="0 0 250 96" aria-hidden="true">{G("fh-m")}
        {[["빠름", 150, 22], ["균형", 110, 48], ["저렴", 70, 74]].map(([t, w, y]) => <g key={String(t)}>
          <text x="10" y={Number(y) + 9} fontSize="11" fill="var(--rc-fg3)" fontFamily="inherit">{t}</text>
          <rect x="46" y={Number(y)} width="190" height="12" rx="6" fill="var(--rc-line)" /><rect x="46" y={Number(y)} width={Number(w)} height="12" rx="6" fill="url(#fh-m)" /></g>)}
      </svg>);
    case "adr": return (
      <svg width="250" height="96" viewBox="0 0 250 96" aria-hidden="true">{G("fh-a")}
        {[0, 1, 2].map(i => <g key={i}><rect x={60 + i * 26} y={10 + i * 8} width="96" height="76" rx="10" fill="var(--rc-solid)" stroke={i === 2 ? "url(#fh-a)" : "var(--rc-line2)"} />
          <rect x={72 + i * 26} y={24 + i * 8} width="40" height="6" rx="3" fill={i === 2 ? "#36B1D9" : "var(--rc-line2)"} /><rect x={72 + i * 26} y={38 + i * 8} width="70" height="5" rx="2.5" fill="var(--rc-line2)" /><rect x={72 + i * 26} y={50 + i * 8} width="56" height="5" rx="2.5" fill="var(--rc-line2)" /></g>)}
      </svg>);
    case "policy": return (
      <svg width="250" height="96" viewBox="0 0 250 96" aria-hidden="true">{G("fh-p")}
        <path d="M60 10 34 20v24c0 18 12 32 26 38 14-6 26-20 26-38V20z" fill="rgba(53,169,209,.08)" stroke="url(#fh-p)" strokeWidth="2" />
        <path d="m48 46 8 8 16-16" fill="none" stroke="#36B1D9" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
        {[0, 1, 2].map(i => <g key={i}><circle cx="112" cy={26 + i * 22} r="5" fill={i === 2 ? "#F2B544" : "#3DD68C"} /><rect x="124" y={23 + i * 22} width={100 - i * 18} height="6" rx="3" fill="var(--rc-line2)" /></g>)}
      </svg>);
  }
}

export const HERO_KINDS = Object.keys(COPY) as Kind[];
export const hasHero = (k: string): k is Kind => (HERO_KINDS as string[]).includes(k);

export function FeatureHero({ kind }: { kind: Kind }) {
  const c = COPY[kind];
  return (
    <div className="rc-tile rc-fhero" data-testid={`hero-${kind}`}>
      <style>{`.rc-fhero{display:flex;align-items:center;gap:24px;padding:18px 22px;margin-bottom:16px;background:linear-gradient(120deg,rgba(53,169,209,.10),transparent 60%),var(--rc-tile)}
        .rc-fhero .tx{flex:1;min-width:0}.rc-fhero h2{margin:0;font-size:20px;font-weight:700;letter-spacing:-.025em}.rc-fhero p{margin:6px 0 0;color:var(--rc-fg2);font-size:13px;line-height:1.55;max-width:560px}
        .rc-fhero svg{display:block;flex:none}@media (max-width:640px){.rc-fhero>svg{display:none}}`}</style>
      <div className="tx"><h2>{c.title}</h2><p>{c.desc}</p></div>
      <Art kind={kind} />
    </div>
  );
}
