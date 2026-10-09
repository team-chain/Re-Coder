/**
 * 배포 진행 애니메이션 — 리코더(Recoder Character)가 박스(빌드한 앱)를 들고 배포 단계를 따라 옮긴다.
 *
 * 위치는 **실제 진행 단계**(코어의 SSE 진행 이벤트)로만 정한다. 한 단계 안에서는 다음 단계 직전까지
 * 천천히 다가가기만 하고(가짜로 다음 단계에 도착하지 않는다), 이벤트가 오면 그 단계로 이동한다.
 * 걷는 빠르기는 최근 단계가 얼마나 빨리 끝났는지에 맞춘다 — 빨리 진행되면 빨리, 오래 걸리면 천천히.
 * 실패하면 박스를 내려놓고 멈추며, 완료되면 목적지에 박스를 넣는다.
 */
import React, { useEffect, useRef, useState } from "react";
import { characterImg, CHARACTER_NAME } from "./recoderCharacter";

export type DeliveryState = "running" | "pending" | "done" | "failed";

export interface DeliveryTrackProps {
  /** 지금 진행 중인 단계 번호(0부터). 완료면 stages.length. */
  index: number;
  stages: string[];
  state: DeliveryState;
  /** 목적지 이름(예: "Docker", "S3", "ECS"). */
  destination: string;
  /** 예전 호출 호환용(무시). 배달은 항상 리코더가 한다. */
  animal?: string;
}

const css = `
.rc-dt{position:relative;margin:4px 0 10px;padding:30px 44px 6px 6px;height:76px;box-sizing:border-box}
.rc-dt-line{position:absolute;left:14px;right:52px;top:62px;height:3px;border-radius:2px;background:var(--vscode-panel-border,#3a3f4b)}
.rc-dt-fill{position:absolute;left:0;top:0;bottom:0;border-radius:2px;background:var(--vscode-progressBar-background,#4faff0);transition:width .9s cubic-bezier(.25,.8,.3,1)}
.rc-dt.failed .rc-dt-fill{background:var(--vscode-errorForeground,#f38a91)}
.rc-dt.done .rc-dt-fill{background:var(--vscode-charts-green,#73d39b)}
.rc-dt-cp{position:absolute;top:58px;width:11px;height:11px;margin-left:-5px;border-radius:50%;background:var(--vscode-editorWidget-background,#1e232b);border:2px solid var(--vscode-panel-border,#4a5160);box-sizing:border-box}
.rc-dt-cp.ok{border-color:var(--vscode-charts-green,#73d39b);background:var(--vscode-charts-green,#73d39b)}
.rc-dt-cp.now{border-color:var(--vscode-progressBar-background,#4faff0)}
.rc-dt-dest{position:absolute;right:4px;top:30px;width:40px;text-align:center;font-size:9.5px;color:var(--vscode-descriptionForeground,#99a9b9)}
.rc-dt-dest svg{display:block;margin:0 auto 1px}
.rc-dt-walker{position:absolute;top:14px;width:40px;margin-left:-20px;transition:left .9s cubic-bezier(.25,.8,.3,1)}
.rc-dt-walker .body{display:block;width:40px;height:40px}
.rc-dt-walker .box{position:absolute;left:11px;top:-9px;width:18px;height:15px}
.rc-dt.running .rc-dt-walker .body,.rc-dt.pending .rc-dt-walker .body{animation:rcDtStep var(--rc-dt-step,.5s) ease-in-out infinite alternate}
.rc-dt.running .rc-dt-walker .box{animation:rcDtBox var(--rc-dt-step,.5s) ease-in-out infinite alternate}
.rc-dt.failed .rc-dt-walker .box{top:30px;left:30px;transform:rotate(18deg);transition:all .5s ease-in}
.rc-dt.failed .rc-dt-walker .body{transform:rotate(-8deg);filter:grayscale(.6)}
.rc-dt.done .rc-dt-walker .box{opacity:0;transition:opacity .4s .6s}
.rc-dt.done .rc-dt-dest .drop{animation:rcDtDrop .5s .7s both}
@keyframes rcDtStep{from{transform:translateY(0) rotate(-3deg)}to{transform:translateY(-3px) rotate(3deg)}}
@keyframes rcDtBox{from{transform:translateY(0)}to{transform:translateY(-2px)}}
@keyframes rcDtDrop{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion: reduce){.rc-dt *{animation:none!important;transition:none!important}}
`;

const BOX = `<svg viewBox="0 0 18 15" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M1 4h16v10H1z" fill="#c9965a" stroke="#8a5a2b" stroke-width="1"/><path d="M1 4l3-3h10l3 3" fill="#e0b277" stroke="#8a5a2b" stroke-width="1"/><path d="M7 4v4h4V4" fill="#f3e3c4"/></svg>`;

function DestIcon({ done }: { done: boolean }) {
  return (
    <svg width="26" height="24" viewBox="0 0 26 24" aria-hidden="true">
      <rect x="2" y="6" width="22" height="16" rx="2" fill="var(--vscode-editorWidget-background,#1e232b)" stroke="var(--vscode-descriptionForeground,#99a9b9)" strokeWidth="1.4" />
      <path d="M2 10h22" stroke="var(--vscode-descriptionForeground,#99a9b9)" strokeWidth="1.2" />
      <circle cx="6" cy="8" r="1" fill="var(--vscode-descriptionForeground,#99a9b9)" />
      {done && <g className="drop"><rect x="9" y="12" width="8" height="7" rx="1" fill="#c9965a" /><path d="M10.5 15.5l2 2 3.5-4" stroke="#fff" strokeWidth="1.4" fill="none" /></g>}
    </svg>
  );
}

/** 단계 안에서 다음 단계 직전까지 천천히 다가가는 정도(0~0.8). 다음 단계 이벤트가 오기 전에는 넘지 않는다. */
export function creepFor(elapsedMs: number, typicalMs: number): number {
  const t = Math.max(0, elapsedMs) / Math.max(1500, typicalMs);
  return 0.8 * (1 - Math.exp(-1.6 * t));
}

/** 걷는 한 걸음 시간(초) — 최근 단계가 빨리 끝나면 빨리, 오래 걸리면 느리게. */
export function stepSeconds(lastSegmentMs: number | null): number {
  if (lastSegmentMs === null) return 0.5;
  return Math.min(0.9, Math.max(0.22, lastSegmentMs / 12000));
}

export function DeliveryTrack({ index, stages, state, destination }: DeliveryTrackProps) {
  const n = Math.max(1, stages.length);
  const clamped = Math.max(0, Math.min(index, n));
  const enteredAt = useRef<number>(Date.now());
  const lastIndex = useRef<number>(clamped);
  const [segmentMs, setSegmentMs] = useState<number | null>(null);
  const [typical, setTypical] = useState<number>(8000);
  const [now, setNow] = useState<number>(Date.now());

  useEffect(() => {
    if (clamped !== lastIndex.current) {
      const spent = Date.now() - enteredAt.current;
      setSegmentMs(spent);
      setTypical(t => Math.round(t * 0.5 + Math.max(1500, spent) * 0.5));
      enteredAt.current = Date.now();
      lastIndex.current = clamped;
    }
  }, [clamped]);

  useEffect(() => {
    if (state !== "running") return;
    const timer = setInterval(() => setNow(Date.now()), 400);
    return () => clearInterval(timer);
  }, [state]);

  const creep = state === "running" && clamped < n ? creepFor(now - enteredAt.current, typical) : 0;
  const ratio = state === "done" ? 1 : Math.min(1, (clamped + creep) / n);
  const label = state === "done" ? `${destination}에 배달 완료`
    : state === "failed" ? `${stages[Math.min(clamped, n - 1)] ?? ""} 단계에서 멈췄어요`
    : state === "pending" ? "도착해서 응답을 기다리는 중"
    : `${stages[Math.min(clamped, n - 1)] ?? ""} 중`;

  return (
    <div className={`rc-dt ${state}`} data-testid="delivery-track" data-state={state} data-ratio={ratio.toFixed(3)}
      role="img" aria-label={`${CHARACTER_NAME}가 배포 박스를 옮기는 중 — ${label}`}
      style={{ ["--rc-dt-step" as string]: `${stepSeconds(segmentMs)}s` } as React.CSSProperties}>
      <style>{css}</style>
      <div className="rc-dt-line"><div className="rc-dt-fill" style={{ width: `${ratio * 100}%` }} /></div>
      {stages.map((name, i) => (
        <span key={name} title={name} className={`rc-dt-cp${i < clamped || state === "done" ? " ok" : i === clamped ? " now" : ""}`}
          style={{ left: `calc(14px + (100% - 66px) * ${i / n})` }} />
      ))}
      <div className="rc-dt-walker" style={{ left: `calc(14px + (100% - 66px) * ${ratio})` }} title={`${CHARACTER_NAME} · ${label}`}>
        <span className="body" dangerouslySetInnerHTML={{ __html: characterImg(40) }} />
        <span className="box" dangerouslySetInnerHTML={{ __html: BOX }} />
      </div>
      <div className="rc-dt-dest"><DestIcon done={state === "done"} />{destination}</div>
    </div>
  );
}
