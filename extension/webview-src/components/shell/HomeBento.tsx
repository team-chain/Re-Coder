/**
 * 홈 — 지금 돌고 있는 버전 하나와 그 근거(Vercel 프로젝트 개요 구성).
 *
 *  프로젝트 이름 ─────────────────────── [저장소] [사이트 열기] [배포]
 *  ┌ 미리보기 ┬ 주소 · 상태 · 시각 · 대상 · 소스 ┐
 *  │          ├ 보안 · 요청 · 승인 · 바뀐 코드 한 줄 ┤
 *  최근 배포 3줄 ─────────── 배포 경로(실제 로고) ─
 *
 * 모든 칸은 실제 데이터만 그린다(배포 기록 · 캔버스 스냅숏 · 모니터 · git 변경). 데이터가 없으면 무엇을 하면
 * 채워지는지 한 줄과 버튼 하나를 보여 준다. AWS 를 바꾸는 버튼은 여기 없다 — 해당 화면으로 옮겨 갈 뿐이다.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useVSCodeApi } from "../../hooks/useVSCodeApi";
import type { Snapshot } from "../canvas/model";
import type { MonitorCurrentView } from "../monitorModel";
import { pendingAlerts, revOf } from "../monitorModel";
import { AreaChart, Glyph, TechLogo, timeAgo } from "./parts";

export interface HistoryEntryLite {
  key: string; source: "local" | "ecs" | "stack"; deployment_id: string; target: string; region?: string; status: string; status_text: string;
  started_at: string; image?: string; service_url?: string; error?: string;
}
export interface ChangesLite {
  files: Array<{ path: string; added: number; removed: number; binary: boolean }>; added: number; removed: number; untracked: number;
  snippet: null | { file: string; lines: Array<{ kind: "add" | "del" | "ctx"; text: string }> }; error?: string;
}
export interface HomeData {
  snapshot: Snapshot | null;
  history: HistoryEntryLite[] | null;
  monitor: MonitorCurrentView | null;
  changes: ChangesLite | null;
  now?: number;
  /** 배포 기록을 읽지 못했다(Core 가 막 켜지는 중 등) — 「배포 없음」 으로 오해하게 그리지 않는다. */
  historyError?: boolean;
  /** 진행 흐름의 보안 검사 단계(보안 화면에서 한 검사) — ECS 배포 기록에 검사가 없을 때 홈 「보안」 칸에 쓴다. */
  journeySecurity?: { status: string; summary: string } | null;
}
type Go = (view: string) => void;

const OK = new Set(["success", "succeeded"]);
const FAIL = new Set(["failed", "circuit_breaker_triggered", "cancelled"]);
export const isOk = (e?: HistoryEntryLite | null) => !!e && OK.has(e.status);

/** 미리보기로 띄워도 되는 주소 — https 또는 내 PC(localhost). 웹뷰는 https 안에서 http 사이트를 막는다. */
export function previewable(url?: string): boolean {
  if (!url) return false;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || (u.protocol === "http:" && (u.hostname === "localhost" || u.hostname === "127.0.0.1"));
  } catch { return false; }
}

function hostOf(url?: string): string {
  if (!url) return "";
  try { return new URL(url).host; } catch { return url; }
}

const CSS = `
.rc-home3{max-width:1120px;margin:0 auto;color:var(--rc-fg)}
.rc-home3 .hd{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.rc-home3 .hd h1{margin:0 auto 0 0;font-size:26px;font-weight:750;letter-spacing:-.03em;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rc-home3 .hd .rc-btn{display:inline-flex;align-items:center;gap:7px}
.rc-ov{margin-top:18px;display:grid;grid-template-columns:minmax(0,420px) minmax(0,1fr);border:1px solid var(--rc-line2);border-radius:16px;background:var(--rc-tile);overflow:hidden;animation:rc-h-up .45s cubic-bezier(.2,.8,.2,1) both}
.rc-ov .pv{padding:16px;border-right:1px solid var(--rc-line)}
.rc-win{position:relative;height:236px;border-radius:12px;background:var(--rc-solid);border:1px solid var(--rc-line2);box-shadow:var(--rc-shadow);overflow:hidden}
.rc-win .bar{height:28px;display:flex;align-items:center;gap:6px;padding:0 10px;border-bottom:1px solid var(--rc-line)}
.rc-win .bar i{width:8px;height:8px;border-radius:50%;background:var(--rc-line2)}
.rc-win .bar span{margin-left:8px;flex:1;min-width:0;display:flex;align-items:center;gap:6px;font:11px var(--rc-mono);color:var(--rc-fg3);background:var(--rc-tile2);border-radius:6px;padding:3px 9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-win .view{position:absolute;inset:28px 0 0 0;overflow:hidden}
.rc-win iframe{border:0;width:200%;height:200%;transform:scale(.5);transform-origin:0 0;pointer-events:none;background:#fff}
.rc-win .poster{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;padding:0 26px;background:radial-gradient(420px 260px at 0 0,rgba(53,169,209,.22),transparent),var(--rc-solid)}
.rc-win .poster small{font-size:10.5px;letter-spacing:.1em;color:var(--rc-teal);font-weight:650;text-transform:uppercase}
.rc-win .poster b{font-size:24px;letter-spacing:-.03em;line-height:1.2;margin-top:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rc-win .poster span{color:var(--rc-fg3);font-size:12px;margin-top:8px;line-height:1.5}
.rc-win.ghost{border-style:dashed;background:transparent;box-shadow:none}
.rc-ov .kv{padding:20px 24px;display:grid;grid-template-columns:1fr 1fr;gap:16px 24px;align-content:start}
.rc-ov .kv>div{min-width:0}.rc-ov .kv .full{grid-column:1/-1}
.rc-ov .kv label{display:block;font-size:12.5px;color:var(--rc-fg3);margin-bottom:5px}
.rc-ov .kv .v{display:flex;align-items:center;gap:7px;font-size:14px;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-ov .kv .v .rc-mono{overflow:hidden;text-overflow:ellipsis}
.rc-ov .kv .dim{color:var(--rc-fg3)}
.rc-dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--rc-fg3)}.rc-dot.ok{background:var(--rc-ok)}.rc-dot.bad{background:var(--rc-bad)}.rc-dot.warn{background:var(--rc-warn)}
.rc-dot.live{box-shadow:0 0 0 0 color-mix(in srgb,var(--rc-ok) 60%,transparent);animation:rc-h-live 2s ease-out infinite}
.rc-strip{grid-column:1/-1;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-top:1px solid var(--rc-line)}
.rc-stat{all:unset;box-sizing:border-box;cursor:pointer;padding:14px 18px;border-left:1px solid var(--rc-line);min-width:0;position:relative;overflow:hidden}
.rc-stat:first-child{border-left:0}.rc-stat:hover{background:var(--rc-tile2)}.rc-stat:focus-visible{outline:2px solid var(--rc-teal);outline-offset:-2px}
.rc-stat small{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--rc-fg3)}
.rc-stat b{display:flex;align-items:baseline;gap:6px;margin-top:4px;font-size:20px;font-weight:700;letter-spacing:-.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-stat b em{font-style:normal;font-size:12px;font-weight:500;color:var(--rc-fg3)}
.rc-stat[data-t=bad] b{color:var(--rc-bad)}.rc-stat[data-t=warn] b{color:var(--rc-warn)}.rc-stat[data-t=ok] b{color:var(--rc-ok)}.rc-stat[data-t=off] b{color:var(--rc-fg3)}
.rc-stat .spark{position:absolute;left:0;right:0;bottom:0;height:34px;opacity:.55;pointer-events:none}
.rc-low{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;margin-top:22px;align-items:start}
.rc-low h3{display:flex;align-items:center;margin:0 0 10px;font-size:14px;font-weight:650}.rc-low h3 .rc-more{margin-left:auto}
.rc-more{all:unset;cursor:pointer;display:inline-flex;align-items:center;gap:3px;font-size:12.5px;color:var(--rc-fg3)}.rc-more:hover{color:var(--rc-fg)}
.rc-recent{border:1px solid var(--rc-line);border-radius:12px;background:var(--rc-tile);overflow:hidden}
.rc-recent button{all:unset;box-sizing:border-box;cursor:pointer;width:100%;display:grid;grid-template-columns:16px 110px minmax(0,1fr) auto;align-items:center;gap:12px;padding:12px 16px;border-top:1px solid var(--rc-line);font-size:13px;animation:rc-h-up .4s cubic-bezier(.2,.8,.2,1) both}
.rc-recent button:first-child{border-top:0}.rc-recent button:hover{background:var(--rc-tile2)}.rc-recent button:focus-visible{outline:2px solid var(--rc-teal);outline-offset:-2px}
.rc-recent .t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--rc-fg2)}.rc-recent .w{color:var(--rc-fg3);font-size:12px;white-space:nowrap}
.rc-recent .empty{padding:22px 16px;color:var(--rc-fg3);font-size:13px}
.rc-side{display:grid;gap:12px}
.rc-ap{border:1px solid color-mix(in srgb,var(--rc-warn) 30%,transparent);border-radius:12px;background:color-mix(in srgb,var(--rc-warn) 7%,var(--rc-tile));padding:12px 14px}
.rc-ap h4{margin:0 0 8px;font-size:13px;color:var(--rc-warn);display:flex;align-items:center;gap:6px}
.rc-ap>div{display:flex;align-items:center;gap:10px;padding:6px 0;border-top:1px solid var(--rc-line);font-size:12.5px;min-width:0}
.rc-ap>div>span{min-width:0;flex:1}.rc-ap b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.rc-ap small{display:block;color:var(--rc-fg3);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-path{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}
.rc-node{all:unset;box-sizing:border-box;cursor:pointer;min-width:0;border-radius:12px;padding:10px;background:var(--rc-tile);border:1px solid var(--rc-line);display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center}
.rc-node:hover{background:var(--rc-tile2)}.rc-node:focus-visible{outline:2px solid var(--rc-teal);outline-offset:2px}
.rc-node b{font-size:12px;font-weight:600}
.rc-node small{display:flex;align-items:center;gap:5px;color:var(--rc-fg3);font-size:11px;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}
.rc-node small i{width:5px;height:5px;border-radius:50%;background:var(--rc-ok);flex:none}.rc-node small i.off{background:var(--rc-fg3)}.rc-node small i.warn{background:var(--rc-warn)}.rc-node small i.bad{background:var(--rc-bad)}
.rc-node.live{border-color:color-mix(in srgb,var(--rc-teal) 60%,transparent);box-shadow:0 0 0 3px color-mix(in srgb,var(--rc-teal) 12%,transparent)}
.rc-node.dim{border-style:dashed}.rc-node.dim>*{opacity:.55}
@keyframes rc-h-up{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes rc-h-live{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--rc-ok) 55%,transparent)}100%{box-shadow:0 0 0 7px transparent}}
@media(prefers-reduced-motion:reduce){.rc-home3 *{animation:none!important}}
@media(max-width:900px){.rc-ov{grid-template-columns:1fr}.rc-ov .pv{border-right:0;border-bottom:1px solid var(--rc-line)}.rc-low{grid-template-columns:1fr}}
@media(max-width:560px){.rc-ov .kv{grid-template-columns:1fr}.rc-strip{grid-template-columns:1fr 1fr}.rc-stat:nth-child(3){border-left:0}.rc-stat:nth-child(n+3){border-top:1px solid var(--rc-line)}.rc-recent button{grid-template-columns:16px minmax(0,1fr) auto}.rc-recent .t{display:none}}
`;

const statusOf = (e?: HistoryEntryLite | null) => !e ? "off" : isOk(e) ? "ok" : FAIL.has(e.status) ? "bad" : "warn";
const whereOf = (e: HistoryEntryLite) => e.source === "ecs" ? `ECS ${e.region || ""}`.trim() : e.source === "stack" ? "로컬 다중 서비스" : "로컬 Docker";

const JOURNEY_SEC: Record<string, { tone: "ok" | "warn" | "bad"; label: string }> = {
  done: { tone: "ok", label: "검사함" }, failed: { tone: "bad", label: "다시 필요" }, stale: { tone: "warn", label: "코드 바뀜 · 다시 검사" },
  unverified: { tone: "warn", label: "일부 미확인" }, cancelled: { tone: "warn", label: "중단됨" },
};
export function securitySummary(snapshot: Snapshot | null, journey?: { status: string; summary: string } | null): { tone: "ok" | "warn" | "bad" | "off"; label: string; vulns: number; secrets: number; unverified: number; note?: string } {
  const scan = snapshot?.scan;
  //: 배포 기록의 검사가 없으면 보안 화면에서 한 검사(진행 흐름 기록)를 쓴다 — 실사용(2026-11-21): 진행 흐름은 「보안 검사 완료」 인데 홈은 「검사 전」.
  if (!scan) {
    const j = journey && JOURNEY_SEC[journey.status];
    return j ? { ...j, vulns: 0, secrets: 0, unverified: 0, note: journey!.summary } : { tone: "off", label: "검사 전", vulns: 0, secrets: 0, unverified: 0 };
  }
  const vulns = scan.findings.filter(f => f.tool !== "gitleaks").length;
  const secrets = scan.findings.filter(f => f.tool === "gitleaks").length;
  const unverified = scan.tools.filter(t => ["unverified", "not_run", "pending"].includes(t.state)).length;
  if (scan.blocked) return { tone: "bad", label: "차단", vulns, secrets, unverified };
  if (vulns || secrets) return { tone: "warn", label: "확인 필요", vulns, secrets, unverified };
  if (unverified) return { tone: "warn", label: "일부 미확인", vulns, secrets, unverified };
  return { tone: "ok", label: "통과", vulns, secrets, unverified };
}

function Overview({ data, go, open }: { data: HomeData; go: Go; open: (url: string) => void }) {
  const entries = data.history ?? [];
  //: 가장 최근에 성공한 배포 — 주소가 있는 옛 배포(예: 40일 전 ECS)를 최근 로컬 배포보다 앞세우지 않는다(실사용 2026-11-21).
  const live = entries.find(isOk) ?? null;
  const last = entries[0] ?? null;
  const project = data.snapshot?.projectName || "프로젝트";
  const url = live?.service_url || "";
  const failedAfter = !!last && last !== live && FAIL.has(last.status);
  const git = data.snapshot?.git;
  const repoUrl = git?.repository ? (/^https?:\/\//.test(git.repository) ? git.repository : `https://github.com/${git.repository}`) : "";
  const sec = securitySummary(data.snapshot, data.journeySecurity);
  //: 「실행 중」 은 확인한 것만 — ECS 실시간 조회가 실행 중 태스크를 보여 줄 때. 나머지는 기록의 상태 글자.
  const topoNow = data.snapshot?.topology as { running?: number } | null | undefined;
  const liveNow = !!live && live.source === "ecs" && (topoNow?.running ?? 0) > 0;
  const m = data.monitor;
  const s = m?.series;
  const hasTraffic = !!m && !!m.metrics && !!s && (s.requests?.length ?? 0) >= 2;
  const rate = (m?.now?.error_rate ?? 0) * 100;
  const offers = m?.offers ?? [];
  const alerts = m ? pendingAlerts(m) : [];
  const pending = offers.length + alerts.length;
  const c = data.changes;
  const changed = (c?.files.length ?? 0) + (c?.untracked ?? 0);
  return <>
    <div className="hd">
      <h1>{project}</h1>
      {repoUrl && <button className="rc-btn" onClick={() => open(repoUrl)}><TechLogo kind="github" size={15} />저장소</button>}
      {url && <button className="rc-btn" onClick={() => open(url)}>사이트 열기<Glyph name="external" /></button>}
      <button className="rc-btn p" onClick={() => go("hub:deploy")}><Glyph name="upload" />{live ? "배포" : "배포 캔버스"}</button>
    </div>
    <div className="rc-ov" data-testid="home-app">
      <div className="pv">
        {live ? <div className="rc-win"><div className="bar"><i /><i /><i /><span><Glyph name="lock" size={11} />{hostOf(url) || "주소 없음"}</span></div>
          <div className="view">
            <div className="poster"><small>{live.source === "ecs" ? "Live · ECS" : "Live · 로컬 Docker"}</small><b>{project}</b><span className="rc-mono">{hostOf(url) || live.target}</span></div>
            {previewable(url) && <iframe title={`${project} 미리보기`} src={url} sandbox="allow-scripts" tabIndex={-1} loading="lazy" referrerPolicy="no-referrer" />}
          </div></div>
          : data.history === null ? <div className="rc-win ghost" data-testid="home-history-wait"><div className="view" style={{ inset: 0 }}><div className="poster" style={{ background: "transparent" }}>
            <small>{project}</small><b>{data.historyError ? "배포 기록을 불러오지 못했어요" : "배포 기록을 불러오는 중…"}</b><span>{data.historyError ? "ReCoder Core 가 준비되는 중일 수 있어요. 잠시 뒤 다시 읽어요." : "실행 중인 앱이 있으면 여기에 보여요."}</span>
          </div></div></div>
          : <div className="rc-win ghost"><div className="view" style={{ inset: 0 }}><div className="poster" style={{ background: "transparent" }}>
            <small>{project}</small><b>아직 배포한 버전이 없습니다</b><span>{last && FAIL.has(last.status) ? `마지막 시도: ${last.status_text} · ${timeAgo(last.started_at, data.now)}` : "로컬 Docker 나 AWS 로 배포하면 여기에 실행 중인 화면이 보입니다."}</span>
          </div></div></div>}
      </div>
      <div className="kv">
        <div><label>배포</label><div className="v">{url ? <span className="rc-mono">{hostOf(url)}</span> : <span className="dim">{live ? live.target : "없음"}</span>}</div></div>
        <div><label>상태</label><div className="v">{data.history === null ? <span className="dim">{data.historyError ? "읽지 못함" : "확인 중"}</span> : live ? <><i className={`rc-dot ok${liveNow ? " live" : ""}`} />{liveNow ? "실행 중" : live.status_text}{failedAfter && <span className="dim"> · 최근 배포 실패</span>}</> : last ? <><i className={`rc-dot ${statusOf(last)}`} />{last.status_text}</> : <><i className="rc-dot" />배포 전</>}</div></div>
        <div><label>배포한 시각</label><div className="v">{live ? timeAgo(live.started_at, data.now) || "시각 미기록" : <span className="dim">—</span>}</div></div>
        <div><label>대상</label><div className="v">{live ? <>{live.source === "ecs" ? <TechLogo kind="ecs" size={15} /> : <TechLogo kind="docker" size={15} />}{whereOf(live)}</> : <span className="dim">—</span>}</div></div>
        <div className="full"><label>소스</label><div className="v">{git?.branch ? <><Glyph name="branch" size={14} /><span className="rc-mono">{git.branch}{git.head ? ` · ${git.head.slice(0, 7)}` : ""}</span>{git.dirty && <span className="dim">· 커밋 전 변경 있음</span>}</> : <span className="dim">{git?.initialized === false ? "Git 저장소가 아닙니다" : "브랜치 정보 없음"}</span>}</div></div>
      </div>
      <div className="rc-strip">
        <button className="rc-stat" data-testid="home-security" data-t={sec.tone} onClick={() => go("hub:security")}>
          <small>보안</small><b title={sec.note || undefined}>{sec.label}{sec.tone !== "off" && (sec.vulns + sec.secrets) > 0 && <em>{sec.vulns + sec.secrets}건</em>}</b></button>
        <button className="rc-stat" data-testid="home-traffic" data-t={hasTraffic ? (rate > 5 ? "bad" : "") : "off"} onClick={() => go("hub:monitor")}>
          <small>요청{hasTraffic ? " · 24시간" : ""}</small>
          {hasTraffic ? <b>{(m!.now?.rpm ?? 0).toLocaleString()}<em>/분 · 오류율 {rate.toFixed(rate < 10 ? 1 : 0)}%</em></b> : <b title={data.snapshot?.aws.ready ? "ECS 에 배포하면 요청 · 오류가 표시됩니다." : "AWS 를 연결하고 배포하면 요청 · 오류가 표시됩니다."}>—<em>배포 후 표시</em></b>}
          {hasTraffic && <div className="spark"><AreaChart values={s!.requests} errors={s!.errors} height={34} /></div>}
        </button>
        <button className="rc-stat" data-testid="home-approvals" data-t={pending ? "warn" : "off"} onClick={() => go("hub:monitor")}>
          <small>승인 대기</small><b>{pending ? `승인 대기 · ${pending}` : "없음"}</b></button>
        <button className="rc-stat" data-testid="home-changes" data-t={changed ? "warn" : "off"} onClick={() => go("code")}>
          <small>바뀐 코드</small>{changed && c ? <b>{changed}<em>파일 · <span style={{ color: "var(--rc-ok)" }}>+{c.added}</span> <span style={{ color: "var(--rc-bad)" }}>−{c.removed}</span></em></b> : <b title={c?.error}>0<em>{c?.error ? "읽지 못함" : "커밋하지 않은 변경이 없습니다"}</em></b>}</button>
      </div>
    </div>
  </>;
}

function Recent({ data, go }: { data: HomeData; go: Go }) {
  const entries = (data.history ?? []).slice(0, 4);
  return <div>
    <h3>최근 배포<button className="rc-more" onClick={() => go("replay")}>모두 보기<Glyph name="chevron" size={12} /></button></h3>
    <div className="rc-recent">
      {data.history === null && <div className="empty">{data.historyError ? "배포 기록을 불러오지 못했어요 · 잠시 뒤 다시 읽어요" : "불러오는 중…"}</div>}
      {data.history !== null && !entries.length && <div className="empty">배포하면 여기에 쌓여요.</div>}
      {entries.map((e, i) => <button key={e.key} onClick={() => go("replay")} style={{ animationDelay: `${0.1 + i * 0.05}s` }} aria-label={`${e.status_text} · ${e.target}`}>
        <i className={`rc-dot ${statusOf(e)}`} /><span>{e.status_text}</span><span className="t">{whereOf(e)} · {e.image || e.target}</span><span className="w">{timeAgo(e.started_at, data.now) || "시각 미기록"}</span>
      </button>)}
    </div>
  </div>;
}

function Side({ data, go }: { data: HomeData; go: Go }) {
  const snap = data.snapshot;
  const entries = data.history ?? [];
  const local = entries.find(e => e.source === "local" || e.source === "stack");
  const ecs = entries.find(e => e.source === "ecs");
  const git = snap?.git;
  const topo = snap?.topology;
  const ecsLive = !!topo && (topo.running ?? 0) > 0;
  const localLive = isOk(local) && !ecsLive;
  const m = data.monitor;
  const rows = m ? [
    ...(m.offers ?? []).map(o => ({ key: `o:${o.target}`, title: `${revOf(o.target)} 으로 되돌리기`, sub: `${o.title} · 지금 ${revOf(o.from_td)}` })),
    ...pendingAlerts(m).map(a => ({ key: `a:${a.id}`, title: a.suggestions?.[0]?.label ?? a.title, sub: a.title })),
  ].slice(0, 2) : [];
  const nodes: Array<{ kind: "github" | "docker" | "ecs" | "s3"; name: string; sub: string; tone: string; live?: boolean; dim?: boolean }> = [
    { kind: "github", name: "GitHub", sub: git?.repository ? (git.branch || "연결됨") : "연결 안 됨", tone: git?.repository ? (git.dirty ? "warn" : "") : "off", dim: !git?.repository },
    { kind: "docker", name: "Docker", sub: data.history === null ? "확인 중" : local ? local.status_text : "실행 전", tone: local ? (isOk(local) ? "" : "bad") : "off", live: localLive },
    { kind: "ecs", name: "ECS", sub: !snap?.aws.ready ? "AWS 연결 필요" : topo ? `${topo.running ?? "?"}/${topo.desired ?? "?"} 실행 중` : ecs ? ecs.status_text : "배포 전", tone: topo ? ((topo.running ?? 0) >= (topo.desired ?? 1) ? "" : "warn") : ecs && FAIL.has(ecs.status) ? "bad" : "off", live: ecsLive, dim: !snap?.aws.ready },
    { kind: "s3", name: "S3", sub: snap?.s3 ? snap.s3.bucket : "연결 안 됨", tone: snap?.s3 ? "" : "off", dim: !snap?.s3 },
  ];
  return <div className="rc-side">
    {rows.length > 0 && <div className="rc-ap"><h4><Glyph name="alert" size={13} />승인할 일</h4>{rows.map(r => <div key={r.key}><span><b>{r.title}</b><small>{r.sub}</small></span><button className="rc-btn sm" onClick={() => go("hub:monitor")}>보기</button></div>)}</div>}
    <div data-testid="home-path"><h3>배포 경로<button className="rc-more" onClick={() => go("hub:deploy")}>Deploy<Glyph name="chevron" size={12} /></button></h3>
      <div className="rc-path">{nodes.map(n => (
        <button key={n.kind} className={`rc-node${n.live ? " live" : ""}${n.dim ? " dim" : ""}`} onClick={() => go("hub:deploy")} aria-label={`${n.name} · ${n.sub}`}>
          <TechLogo kind={n.kind} size={22} /><b>{n.name}</b><small><i className={n.tone} />{n.sub}</small>
        </button>))}</div></div>
  </div>;
}

/** 확장과 주고받지 않는 화면(서버 렌더 테스트 · 하네스가 그대로 그린다). */
export function HomeView({ data, go, open }: { data: HomeData; go: Go; open: (url: string) => void }) {
  return (
    <section className="rc-home3" aria-label="홈">
      <style>{CSS}</style>
      <Overview data={data} go={go} open={open} />
      <div className="rc-low"><Recent data={data} go={go} /><Side data={data} go={go} /></div>
    </section>
  );
}

/** 확장과 주고받는 쪽 — 보이는 동안만 1분마다 새로 읽는다. */
export const HomeBento: React.FC<{ active: boolean; go: Go }> = ({ active, go }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [data, setData] = useState<HomeData>({ snapshot: null, history: null, monitor: null, changes: null });
  const reqs = useRef({ snap: "", hist: "", mon: "" });
  //: 읽기 실패(Core 시작 중 등)는 몇 초 뒤 다시 — 1분 뒤까지 「배포 없음」 으로 두지 않는다.
  const retry = useRef<{ timer?: number; tries: number }>({ tries: 0 });
  const load = useCallback(() => {
    const t = Date.now();
    reqs.current = { snap: `home-snap-${t}`, hist: `home-hist-${t}`, mon: `home-mon-${t}` };
    postMessage("canvas.snapshot", { requestId: reqs.current.snap });
    postMessage("replay.history", { requestId: reqs.current.hist });
    postMessage("home.changes");
    postMessage("journey.get");
  }, [postMessage]);
  useEffect(() => {
    if (!active) return undefined;
    load();
    const id = window.setInterval(load, 60_000);
    return () => window.clearInterval(id);
  }, [active, load]);
  useMessage(useCallback(({ type, payload }) => {
    const p = (payload ?? {}) as Record<string, unknown>;
    if (type === "canvas.snapshotResult" && p.requestId === reqs.current.snap) {
      const snapshot = p.snapshot as Snapshot;
      setData(d => ({ ...d, snapshot }));
      //: 모니터는 AWS 가 연결됐을 때만 묻는다(아니면 매번 오류만 돌아온다).
      if (snapshot?.aws?.ready) postMessage("monitor.load", { key: "", requestId: reqs.current.mon });
    }
    if (type === "replay.historyResult" && p.requestId === reqs.current.hist) {
      const h = p.history as { entries?: HistoryEntryLite[] } | undefined;
      retry.current.tries = 0;
      setData(d => ({ ...d, history: h?.entries ?? [], historyError: false }));
    }
    if (type === "replay.historyError" && p.requestId === reqs.current.hist) {
      setData(d => ({ ...d, history: d.historyError === undefined || d.history === null ? null : d.history, historyError: true }));
      const r = retry.current;
      if (r.tries < 8 && typeof window !== "undefined") {
        window.clearTimeout(r.timer);
        r.timer = window.setTimeout(() => load(), Math.min(30_000, 3_000 * 2 ** r.tries));
        r.tries++;
      }
    }
    if (type === "journey.state") {
      const st = (p.steps as Array<{ key: string; status: string; summary: string }> | undefined)?.find(x => x.key === "security");
      setData(d => ({ ...d, journeySecurity: st ? { status: st.status, summary: st.summary } : null }));
    }
    if (type === "home.changesResult") setData(d => ({ ...d, changes: p as unknown as ChangesLite }));
    if (type === "monitor.result" && p.requestId === reqs.current.mon) setData(d => ({ ...d, monitor: (p.current as MonitorCurrentView) ?? null }));
  }, [postMessage, load]));
  useEffect(() => () => { if (typeof window !== "undefined") window.clearTimeout(retry.current.timer); }, []);
  return <HomeView data={data} go={go} open={(url) => postMessage("webview.open.external", { url })} />;
};
