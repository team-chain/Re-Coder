/**
 * 3.0.2 홈 — 글을 읽지 않아도 지금 상태가 보이게(벤토).
 *
 *  ┌ 지금 서비스 중인 앱(미리보기) ┐┌ 요청 · 오류 그래프 ┐
 *  │                              │├ 보안 링 ┬ 승인 대기 ┤
 *  ├ 배포 경로(실제 로고) ─────────┤├ 바뀐 코드 ─────────┤
 *
 * 모든 칸은 실제 데이터만 그린다(배포 기록 · 캔버스 스냅숏 · 모니터 · git 변경). 데이터가 없으면 무엇을 하면
 * 채워지는지 한 줄과 버튼 하나를 보여 준다. AWS 를 바꾸는 버튼은 여기 없다 — 해당 화면으로 옮겨 갈 뿐이다.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useVSCodeApi } from "../../hooks/useVSCodeApi";
import type { Snapshot } from "../canvas/model";
import type { MonitorCurrentView } from "../monitorModel";
import { pendingAlerts, revOf } from "../monitorModel";
import { AreaChart, Glyph, Ring, TechLogo, timeAgo } from "./parts";

export interface HistoryEntryLite {
  key: string; source: "local" | "ecs"; deployment_id: string; target: string; region?: string; status: string; status_text: string;
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
.rc-home2{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));grid-template-rows:240px 170px 168px;gap:12px;max-width:1320px;margin:0 auto}
.rc-home2 .app{grid-column:span 2;grid-row:span 2;background:linear-gradient(165deg,rgba(53,169,209,.13),transparent 55%),var(--rc-tile)}
.rc-home2 .wide{grid-column:span 2}
.rc-win{position:absolute;left:22px;right:22px;top:44px;bottom:92px;border-radius:12px;background:var(--rc-solid);border:1px solid var(--rc-line2);box-shadow:var(--rc-shadow);overflow:hidden}
.rc-win .bar{height:28px;display:flex;align-items:center;gap:6px;padding:0 10px;border-bottom:1px solid var(--rc-line)}
.rc-win .bar i{width:8px;height:8px;border-radius:50%;background:var(--rc-line2)}
.rc-win .bar span{margin-left:8px;flex:1;min-width:0;display:flex;align-items:center;gap:6px;font:11px var(--rc-mono);color:var(--rc-fg3);background:var(--rc-tile2);border-radius:6px;padding:3px 9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-win .view{position:absolute;inset:28px 0 0 0;overflow:hidden}
.rc-win iframe{border:0;width:200%;height:200%;transform:scale(.5);transform-origin:0 0;pointer-events:none;background:#fff}
.rc-win .poster{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;padding:0 30px;background:radial-gradient(420px 260px at 0 0,rgba(53,169,209,.24),transparent),var(--rc-solid)}
.rc-win .poster small{font-size:10.5px;letter-spacing:.1em;color:var(--rc-teal);font-weight:650;text-transform:uppercase}
.rc-win .poster b{font-size:30px;letter-spacing:-.035em;line-height:1.15;margin-top:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rc-win .poster span{color:var(--rc-fg3);font-size:12px;margin-top:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-win.ghost{border-style:dashed;background:transparent}
.rc-meta{position:absolute;left:22px;right:22px;bottom:16px}
.rc-meta .r1{display:flex;align-items:center;gap:8px}.rc-meta .r1 b{font-size:17px;letter-spacing:-.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.rc-meta .r1 .sp{flex:1}
.rc-meta .r2{display:flex;align-items:center;gap:12px;margin-top:6px;color:var(--rc-fg3);font-size:12.5px;white-space:nowrap;overflow:hidden}
.rc-meta .r2>span{display:flex;align-items:center;gap:6px;min-width:0;overflow:hidden;text-overflow:ellipsis}
.rc-num{display:flex;align-items:baseline;gap:7px;padding:8px 18px 0}.rc-num b{font-size:30px;letter-spacing:-.03em;font-weight:700}.rc-num small{color:var(--rc-fg3);font-size:12.5px}
.rc-sub{padding:2px 18px 0;font-size:12px;color:var(--rc-fg3)}.rc-sub b{color:var(--rc-fg);font-weight:600}
.rc-chart{position:absolute;left:0;right:0;bottom:0}
.rc-ghost-chart{position:absolute;left:0;right:0;bottom:0;opacity:.35;filter:grayscale(1)}
.rc-tile .center-note{position:absolute;left:18px;right:18px;bottom:18px;color:var(--rc-fg2);font-size:12.5px}
.rc-ring-row{display:flex;align-items:center;gap:18px;padding:12px 18px 0}
.rc-ring-row ul{margin:0;padding:0;list-style:none;display:grid;gap:9px;font-size:12.5px;color:var(--rc-fg2);flex:1}
.rc-ring-row li{display:flex;align-items:center;gap:8px}.rc-ring-row li b{margin-left:auto;color:var(--rc-fg);font-weight:600;font-family:var(--rc-mono);font-size:12px}
.rc-ring-row li i{width:6px;height:6px;border-radius:50%;background:var(--rc-ok);flex:none}.rc-ring-row li i.warn{background:var(--rc-warn)}.rc-ring-row li i.bad{background:var(--rc-bad)}.rc-ring-row li i.off{background:var(--rc-fg3)}
.rc-ap{border-color:rgba(242,181,68,.24);background:linear-gradient(170deg,rgba(242,181,68,.10),transparent 55%),var(--rc-tile)}
.rc-ap .rc-tile-hd{color:var(--rc-warn)}
.rc-stack{position:absolute;left:12px;right:12px;bottom:12px;display:grid;gap:6px}
.rc-stack>div{height:44px;border-radius:12px;background:color-mix(in srgb,var(--rc-bg) 55%,transparent);border:1px solid var(--rc-line);display:flex;align-items:center;padding:0 8px 0 12px;font-size:12.5px;gap:10px;min-width:0}
.rc-stack>div>span{min-width:0;overflow:hidden}.rc-stack b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.rc-stack small{display:block;color:var(--rc-fg3);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-stack .rc-btn{margin-left:auto}
.rc-calm{position:absolute;left:18px;right:18px;bottom:18px;display:flex;align-items:center;gap:12px;color:var(--rc-fg2);font-size:12.5px}
.rc-calm span.c{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;color:var(--rc-ok);background:rgba(61,214,140,.12);flex:none}
.rc-path{position:absolute;left:24px;right:24px;top:46px;display:flex;justify-content:space-between;gap:10px}
.rc-path:before{content:"";position:absolute;left:60px;right:60px;top:42px;height:1px;background:linear-gradient(90deg,var(--rc-line2),rgba(53,169,209,.7) 70%,var(--rc-line))}
.rc-node{all:unset;box-sizing:border-box;cursor:pointer;position:relative;z-index:1;flex:1;max-width:136px;min-width:0;height:88px;border-radius:14px;padding:11px 12px;background:var(--rc-solid);border:1px solid var(--rc-line2);box-shadow:var(--rc-shadow)}
.rc-node b{display:block;margin-top:9px;font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-node small{display:flex;align-items:center;gap:5px;color:var(--rc-fg3);font-size:11px;white-space:nowrap;overflow:hidden}
.rc-node small i{width:5px;height:5px;border-radius:50%;background:var(--rc-ok);flex:none}.rc-node small i.off{background:var(--rc-fg3)}.rc-node small i.warn{background:var(--rc-warn)}.rc-node small i.bad{background:var(--rc-bad)}
.rc-node.live{border-color:rgba(53,169,209,.6);box-shadow:0 0 0 4px rgba(53,169,209,.1),0 18px 40px -14px rgba(53,169,209,.45)}
.rc-node.dim{border-style:dashed}.rc-node.dim>*{opacity:.5}
.rc-node:focus-visible{outline:2px solid var(--rc-teal);outline-offset:2px}
.rc-diff{position:absolute;left:16px;right:156px;top:42px;bottom:14px;border-radius:12px;background:color-mix(in srgb,var(--rc-bg) 70%,transparent);border:1px solid var(--rc-line);padding:6px 0;font:11.5px/1.62 var(--rc-mono);overflow:hidden}
.rc-diff .fn{padding:0 12px 4px;color:var(--rc-fg3);font-size:11px;border-bottom:1px solid var(--rc-line);margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-diff div{padding:0 12px;white-space:pre;overflow:hidden;text-overflow:ellipsis}
.rc-diff .add{background:rgba(61,214,140,.09);color:color-mix(in srgb,var(--rc-ok) 70%,var(--rc-fg))}.rc-diff .del{background:rgba(242,114,114,.09);color:color-mix(in srgb,var(--rc-bad) 65%,var(--rc-fg))}.rc-diff .ctx{color:var(--rc-fg3)}
.rc-cside{position:absolute;right:16px;top:42px;bottom:14px;width:126px;display:flex;flex-direction:column}
.rc-cside b{font-size:28px;letter-spacing:-.03em;line-height:1}.rc-cside b small{font-size:13px;color:var(--rc-fg3);font-weight:500}
.rc-cside .pm{color:var(--rc-fg3);font-size:12px;margin-top:6px}
.rc-cside .bar{display:flex;height:5px;border-radius:3px;overflow:hidden;margin-top:10px;background:var(--rc-line)}.rc-cside .bar i{background:var(--rc-ok)}.rc-cside .bar u{background:var(--rc-bad)}
.rc-cside .rc-btn{margin-top:auto}
@media (max-width:1100px){.rc-home2{grid-template-columns:repeat(2,minmax(0,1fr));grid-template-rows:300px 180px 170px 170px 170px}.rc-home2 .app{grid-row:span 1}}
@media (max-width:620px){.rc-home2{grid-template-columns:1fr;grid-template-rows:none;grid-auto-rows:minmax(170px,auto)}.rc-home2 .app,.rc-home2 .wide{grid-column:span 1}.rc-home2 .app{min-height:300px}
  .rc-diff{right:16px;bottom:62px}.rc-cside{left:16px;right:16px;top:auto;width:auto;flex-direction:row;align-items:center;gap:12px}.rc-cside .bar{display:none}.rc-cside .rc-btn{margin:0 0 0 auto}
  .rc-path{flex-wrap:wrap}.rc-path:before{display:none}}
`;

function Production({ data, go, open }: { data: HomeData; go: Go; open: (url: string) => void }) {
  const entries = data.history ?? [];
  const live = entries.find(e => isOk(e) && e.service_url) ?? entries.find(isOk) ?? null;
  const last = entries[0] ?? null;
  const project = data.snapshot?.projectName || "프로젝트";
  if (!live) {
    return (
      <div className="rc-tile app" data-testid="home-app">
        <div className="rc-tile-hd">배포된 앱</div>
        <div className="rc-win ghost"><div className="view"><div className="poster" style={{ background: "transparent" }}>
          <small>{project}</small><b>아직 배포한 버전이 없습니다</b><span>{last && FAIL.has(last.status) ? `마지막 시도: ${last.status_text} · ${timeAgo(last.started_at, data.now)}` : "로컬 Docker 나 AWS 로 배포하면 여기에 실행 중인 화면이 보입니다."}</span>
        </div></div></div>
        <div className="rc-meta"><div className="r1"><b>{project}</b><span className="sp" />
          <button className="rc-btn" onClick={() => go("ship")}>로컬 Docker</button>
          <button className="rc-btn p" onClick={() => go("hub:deploy")}><Glyph name="upload" />배포 캔버스</button></div></div>
      </div>
    );
  }
  const url = live.service_url || "";
  const where = live.source === "ecs" ? `ECS ${live.region || ""}`.trim() : "로컬 Docker";
  const failedAfter = last && last !== live && FAIL.has(last.status);
  return (
    <div className="rc-tile app" data-testid="home-app">
      <div className="rc-tile-hd">Production<span className="rc-chip"><i />실행 중</span>
        {failedAfter && <span className="rc-chip bad"><i />최근 배포 실패</span>}
        <button className="rc-more" onClick={() => go("replay")}>배포 기록<Glyph name="chevron" size={12} /></button></div>
      <div className="rc-win"><div className="bar"><i /><i /><i /><span><Glyph name="lock" size={11} />{hostOf(url) || "주소 없음"}</span></div>
        <div className="view">
          <div className="poster"><small>{live.source === "ecs" ? "Live · ECS" : "Live · 로컬 Docker"}</small><b>{project}</b><span className="rc-mono">{hostOf(url) || live.target}</span></div>
          {previewable(url) && <iframe title={`${project} 미리보기`} src={url} sandbox="allow-scripts" tabIndex={-1} loading="lazy" referrerPolicy="no-referrer" />}
        </div></div>
      <div className="rc-meta">
        <div className="r1"><b>{project}</b><span className="sp" />
          {url && <button className="rc-btn" onClick={() => open(url)}><Glyph name="external" />열기</button>}
          <button className="rc-btn p" onClick={() => go("hub:deploy")}><Glyph name="upload" />배포</button></div>
        <div className="r2">
          {data.snapshot?.git.branch && <span><Glyph name="branch" size={13} /><span className="rc-mono">{data.snapshot.git.branch}{data.snapshot.git.head ? ` · ${data.snapshot.git.head.slice(0, 7)}` : ""}</span></span>}
          <span>{live.source === "ecs" ? <TechLogo kind="ecs" size={14} /> : <TechLogo kind="docker" size={14} />}{where}</span>
          <span>{timeAgo(live.started_at, data.now)}</span>
        </div>
      </div>
    </div>
  );
}

function Traffic({ data, go }: { data: HomeData; go: Go }) {
  const m = data.monitor;
  const s = m?.series;
  if (!m || !m.metrics || !s || (s.requests?.length ?? 0) < 2) {
    const ghost = Array.from({ length: 48 }, (_, i) => 40 + 14 * Math.sin(i / 6) + 6 * Math.sin(i / 2.3));
    return (
      <div className="rc-tile wide" data-testid="home-traffic">
        <div className="rc-tile-hd">요청<button className="rc-more" onClick={() => go("hub:monitor")}>Operate<Glyph name="chevron" size={12} /></button></div>
        <div className="rc-ghost-chart"><AreaChart values={ghost} height={120} /></div>
        <div className="center-note">{data.snapshot?.aws.ready ? "ECS 에 배포하면 요청 · 오류가 여기에 표시됩니다." : "AWS 를 연결하고 배포하면 요청 · 오류가 여기에 표시됩니다."}</div>
      </div>
    );
  }
  const rate = (m.now?.error_rate ?? 0) * 100;
  return (
    <div className="rc-tile wide" data-testid="home-traffic">
      <div className="rc-tile-hd">요청 · 24시간<button className="rc-more" onClick={() => go("hub:monitor")}>Operate<Glyph name="chevron" size={12} /></button></div>
      <div className="rc-num"><b>{(m.now?.rpm ?? 0).toLocaleString()}</b><small>/분</small></div>
      <div className="rc-sub">오류율 <b style={{ color: rate > 5 ? "var(--rc-bad)" : undefined }}>{rate.toFixed(rate < 10 ? 1 : 0)}%</b>{m.service_state ? <> · 실행 <b>{m.service_state.running}/{m.service_state.desired}</b></> : null}</div>
      <div className="rc-chart"><AreaChart values={s.requests} errors={s.errors} height={112} /></div>
    </div>
  );
}

export function securitySummary(snapshot: Snapshot | null): { tone: "ok" | "warn" | "bad" | "off"; label: string; vulns: number; secrets: number; unverified: number } {
  const scan = snapshot?.scan;
  if (!scan) return { tone: "off", label: "검사 전", vulns: 0, secrets: 0, unverified: 0 };
  const vulns = scan.findings.filter(f => f.tool !== "gitleaks").length;
  const secrets = scan.findings.filter(f => f.tool === "gitleaks").length;
  const unverified = scan.tools.filter(t => ["unverified", "not_run", "pending"].includes(t.state)).length;
  if (scan.blocked) return { tone: "bad", label: "차단", vulns, secrets, unverified };
  if (vulns || secrets) return { tone: "warn", label: "확인 필요", vulns, secrets, unverified };
  if (unverified) return { tone: "warn", label: "일부 미확인", vulns, secrets, unverified };
  return { tone: "ok", label: "통과", vulns, secrets, unverified };
}

function Security({ data, go }: { data: HomeData; go: Go }) {
  const s = securitySummary(data.snapshot);
  const tools = data.snapshot?.scan?.tools.length ?? 0;
  const ratio = s.tone === "off" ? 0 : tools ? (tools - s.unverified) / tools : 1;
  const dot = (n: number) => (s.tone === "off" ? "off" : n ? "warn" : "");
  return (
    <div className="rc-tile" data-testid="home-security">
      <div className="rc-tile-hd">보안 검사<button className="rc-more" onClick={() => go("hub:security")}>{s.tone === "off" ? "검사하기" : "자세히"}<Glyph name="chevron" size={12} /></button></div>
      <div className="rc-ring-row">
        <Ring value={ratio} tone={s.tone} size={92}><b style={{ fontSize: s.label.length > 3 ? 13 : 19, letterSpacing: "-.02em", color: s.tone === "bad" ? "var(--rc-bad)" : undefined }}>{s.label}</b></Ring>
        <ul>
          <li><i className={dot(s.vulns)} />취약점<b>{s.tone === "off" ? "–" : s.vulns}</b></li>
          <li><i className={dot(s.secrets)} />시크릿<b>{s.tone === "off" ? "–" : s.secrets}</b></li>
          <li><i className={s.tone === "off" ? "off" : s.unverified ? "warn" : ""} />미확인<b>{s.tone === "off" ? "–" : s.unverified}</b></li>
        </ul>
      </div>
    </div>
  );
}

function Approvals({ data, go }: { data: HomeData; go: Go }) {
  const m = data.monitor;
  const offers = m?.offers ?? [];
  const alerts = m ? pendingAlerts(m) : [];
  const n = offers.length + alerts.length;
  if (!n) {
    return (
      <div className="rc-tile" data-testid="home-approvals">
        <div className="rc-tile-hd">승인 대기<button className="rc-more" onClick={() => go("hub:monitor")}>Operate<Glyph name="chevron" size={12} /></button></div>
        <div className="rc-calm"><span className="c"><Glyph name="check" size={16} sw={2.4} /></span><span>{m ? "승인할 일이 없습니다." : "배포한 서비스의 롤백 · 차단 제안이 여기에 모입니다."}</span></div>
      </div>
    );
  }
  const rows = [
    ...offers.map(o => ({ key: `o:${o.target}`, title: `${revOf(o.target)} 으로 되돌리기`, sub: `${o.title} · 지금 ${revOf(o.from_td)}` })),
    ...alerts.map(a => ({ key: `a:${a.id}`, title: a.suggestions?.[0]?.label ?? a.title, sub: a.title })),
  ].slice(0, 2);
  return (
    <div className="rc-tile rc-ap" data-testid="home-approvals">
      <div className="rc-tile-hd">승인 대기 · {n}<button className="rc-more" onClick={() => go("hub:monitor")}>Operate<Glyph name="chevron" size={12} /></button></div>
      <div className="rc-stack">{rows.map(r => <div key={r.key}><span><b>{r.title}</b><small>{r.sub}</small></span><button className="rc-btn sm" onClick={() => go("hub:monitor")}>보기</button></div>)}</div>
    </div>
  );
}

function DeployPath({ data, go }: { data: HomeData; go: Go }) {
  const snap = data.snapshot;
  const entries = data.history ?? [];
  const local = entries.find(e => e.source === "local");
  const ecs = entries.find(e => e.source === "ecs");
  const git = snap?.git;
  const topo = snap?.topology;
  const ecsLive = !!topo && (topo.running ?? 0) > 0;
  const localLive = isOk(local) && !ecsLive;
  const nodes: Array<{ kind: "github" | "docker" | "ecs" | "s3"; name: string; sub: string; tone: string; live?: boolean; dim?: boolean }> = [
    { kind: "github", name: "GitHub", sub: git?.repository ? (git.branch || "연결됨") : "연결 안 됨", tone: git?.repository ? (git.dirty ? "warn" : "") : "off", dim: !git?.repository },
    { kind: "docker", name: "Docker", sub: local ? local.status_text : "아직 실행 안 함", tone: local ? (isOk(local) ? "" : "bad") : "off", live: localLive },
    { kind: "ecs", name: topo ? "ECS" : "ECS", sub: !snap?.aws.ready ? "AWS 연결 필요" : topo ? `${topo.running ?? "?"}/${topo.desired ?? "?"} 실행 중` : ecs ? ecs.status_text : "배포 전", tone: topo ? ((topo.running ?? 0) >= (topo.desired ?? 1) ? "" : "warn") : ecs && FAIL.has(ecs.status) ? "bad" : "off", live: ecsLive, dim: !snap?.aws.ready },
    { kind: "s3", name: "S3", sub: snap?.s3 ? snap.s3.bucket : "연결 안 됨", tone: snap?.s3 ? "" : "off", dim: !snap?.s3 },
  ];
  return (
    <div className="rc-tile wide" data-testid="home-path">
      <div className="rc-tile-hd">배포 경로<button className="rc-more" onClick={() => go("hub:deploy")}>Deploy<Glyph name="chevron" size={12} /></button></div>
      <div className="rc-path">{nodes.map(n => (
        <button key={n.kind} className={`rc-node${n.live ? " live" : ""}${n.dim ? " dim" : ""}`} onClick={() => go(n.kind === "docker" ? "ship" : "hub:deploy")} aria-label={`${n.name} · ${n.sub}`}>
          <TechLogo kind={n.kind} size={24} /><b>{n.name}</b><small><i className={n.tone} />{n.sub}</small>
        </button>))}</div>
    </div>
  );
}

function Changes({ data, go }: { data: HomeData; go: Go }) {
  const c = data.changes;
  const count = (c?.files.length ?? 0) + (c?.untracked ?? 0);
  if (!c || !count) {
    return (
      <div className="rc-tile wide" data-testid="home-changes">
        <div className="rc-tile-hd">바뀐 코드<button className="rc-more" onClick={() => go("code")}>Develop<Glyph name="chevron" size={12} /></button></div>
        <div className="rc-calm">{!c?.error && data.snapshot?.git.initialized !== false && <span className="c"><Glyph name="check" size={16} sw={2.4} /></span>}<span>{c?.error || (data.snapshot?.git.initialized === false ? "Git 저장소가 아닙니다 · 배포 캔버스에서 GitHub 를 연결하세요." : "커밋하지 않은 변경이 없습니다.")}</span>
          <button className="rc-btn sm" style={{ marginLeft: "auto" }} onClick={() => go("code")}>새 요청</button></div>
      </div>
    );
  }
  const total = Math.max(1, c.added + c.removed);
  return (
    <div className="rc-tile wide" data-testid="home-changes">
      <div className="rc-tile-hd">바뀐 코드<span className="rc-chip warn"><i />커밋 전</span><button className="rc-more" onClick={() => go("code")}>Develop<Glyph name="chevron" size={12} /></button></div>
      <div className="rc-diff">
        {c.snippet ? <><div className="fn">{c.snippet.file}</div>{c.snippet.lines.map((l, i) => <div key={i} className={l.kind}>{l.kind === "add" ? "+ " : l.kind === "del" ? "- " : "  "}{l.text}</div>)}</>
          : <><div className="fn">새 파일</div>{c.files.slice(0, 5).map(f => <div key={f.path} className="ctx">{f.path}</div>)}</>}
      </div>
      <div className="rc-cside"><b>{count}<small> 파일</small></b>
        <div className="pm"><span style={{ color: "var(--rc-ok)" }}>+{c.added}</span> <span style={{ color: "var(--rc-bad)" }}>−{c.removed}</span>{c.untracked ? ` · 새 ${c.untracked}` : ""}</div>
        <div className="bar"><i style={{ flex: c.added / total }} /><u style={{ flex: c.removed / total }} /></div>
        <button className="rc-btn p" onClick={() => go("hub:deploy")}><Glyph name="upload" />GitHub 에 올리기</button></div>
    </div>
  );
}

/** 확장과 주고받지 않는 화면(서버 렌더 테스트 · 하네스가 그대로 그린다). */
export function HomeView({ data, go, open }: { data: HomeData; go: Go; open: (url: string) => void }) {
  return (
    <section className="rc-home2" aria-label="홈">
      <style>{CSS}</style>
      <Production data={data} go={go} open={open} />
      <Traffic data={data} go={go} />
      <Security data={data} go={go} />
      <Approvals data={data} go={go} />
      <DeployPath data={data} go={go} />
      <Changes data={data} go={go} />
    </section>
  );
}

/** 확장과 주고받는 쪽 — 보이는 동안만 1분마다 새로 읽는다. */
export const HomeBento: React.FC<{ active: boolean; go: Go }> = ({ active, go }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [data, setData] = useState<HomeData>({ snapshot: null, history: null, monitor: null, changes: null });
  const reqs = useRef({ snap: "", hist: "", mon: "" });
  const load = useCallback(() => {
    const t = Date.now();
    reqs.current = { snap: `home-snap-${t}`, hist: `home-hist-${t}`, mon: `home-mon-${t}` };
    postMessage("canvas.snapshot", { requestId: reqs.current.snap });
    postMessage("replay.history", { requestId: reqs.current.hist });
    postMessage("home.changes");
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
      setData(d => ({ ...d, history: h?.entries ?? [] }));
    }
    if (type === "replay.historyError" && p.requestId === reqs.current.hist) setData(d => ({ ...d, history: [] }));
    if (type === "home.changesResult") setData(d => ({ ...d, changes: p as unknown as ChangesLite }));
    if (type === "monitor.result" && p.requestId === reqs.current.mon) setData(d => ({ ...d, monitor: (p.current as MonitorCurrentView) ?? null }));
  }, [postMessage]));
  return <HomeView data={data} go={go} open={(url) => postMessage("webview.open.external", { url })} />;
};
