/**
 * 2.0.49 Operate — 지금 괜찮은지 · 내가 할 일 · 최근 기록, 이 세 가지만 한 줄로 쭉.
 *
 *  · 처음 보이는 것은 상태 한 줄과 할 일뿐. 자세한 내용(기록 설명 · 감시 설정)은 눌러야 펼친다.
 *  · 밤에는 감시자가 감지 · 기록 · 알림 · 준비까지만 한다. AWS 를 바꾸는 버튼은 모두 확인을 한 번 더 거친다.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { Button, Kit, Line } from "./ui/kit";
import { Icon } from "./ui/icons";
import { AreaChart } from "./shell/parts";
import { Logo } from "./canvas/Scene";
import {
  AlertView, MonitorCurrentView, MonitorData, Offer, TimelineItem, clock, hero, kindIcon, minutesAgo, pendingAlerts, revOf,
  sourceText, timeline,
} from "./monitorModel";

const CSS = `
.rc-mon{max-width:1320px;margin:0 auto}
.rc-mon-grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:16px;align-items:start}
.rc-mon-grid>div>.label:first-child{margin-top:0}
@media (max-width:900px){.rc-mon-grid{grid-template-columns:1fr}}
.rc-mon .hd{display:flex;align-items:center;gap:10px;margin-bottom:16px}
.rc-mon .hd h1{margin:0;font-size:19px;font-weight:650}
.rc-mon .app{display:flex;align-items:center;gap:7px;height:30px;padding:0 10px 0 6px;border-radius:10px;background:var(--rk-surface);font-size:12.5px;min-width:0}
.rc-mon .app svg.rc-logo{width:18px;height:18px;border-radius:5px;flex:none}
.rc-mon .app select{all:unset;cursor:pointer;font-weight:500;min-width:0;overflow:hidden;text-overflow:ellipsis}
.rc-mon .sp{flex:1}
.rc-mon .icon-btn{all:unset;cursor:pointer;width:30px;height:30px;border-radius:9px;display:grid;place-items:center;color:var(--rk-fg2)}
.rc-mon .icon-btn:hover{background:var(--rk-surface)}.rc-mon .icon-btn:focus-visible{outline:2px solid var(--vscode-focusBorder,#3d8bff)}
.rc-mon .icon-btn[aria-busy=true] svg{animation:rc-mon-spin 1s linear infinite}
@keyframes rc-mon-spin{to{transform:rotate(360deg)}}
.rc-mon .card{background:var(--rc-tile,var(--rk-surface));border:1px solid var(--rc-line,transparent);border-radius:18px;overflow:hidden}
.rc-mon .hero{position:relative;padding:18px 20px 0;background:linear-gradient(165deg,rgba(53,169,209,.12),transparent 55%),var(--rc-tile,var(--rk-surface))}
.rc-mon .hero .chart{margin:10px -20px 0}
.rc-mon .hero b{font-size:20px;font-weight:700;letter-spacing:-.02em;display:block;line-height:1.35}
.rc-mon .hero p{margin:3px 0 0;color:var(--rk-fg2);font-size:12.5px}
.rc-mon .hero .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;vertical-align:2px;background:var(--rk-ok)}
.rc-mon .hero[data-tone=warn] .dot{background:var(--rk-warn)}.rc-mon .hero[data-tone=bad] .dot{background:var(--rk-bad)}.rc-mon .hero[data-tone=off] .dot{background:var(--rk-fg3)}
.rc-mon .label{font-size:12px;font-weight:500;color:var(--rk-fg2);margin:22px 4px 8px;display:flex;justify-content:space-between;gap:8px}
.rc-mon .label button{all:unset;cursor:pointer;color:var(--vscode-textLink-foreground,#3d8bff);font-weight:400}
.rc-mon .todo{display:flex;align-items:center;gap:12px;padding:12px 14px 12px 16px}
.rc-mon .todo+.todo,.rc-mon .confirm+.todo{border-top:1px solid var(--rk-hair)}
.rc-mon .ib{width:22px;height:32px;display:grid;place-items:center;flex:none;color:var(--rk-fg3)}
.rc-mon .ib.bad{color:var(--rk-bad)}
.rc-mon .ib.warn{color:var(--rk-warn)}
.rc-mon .ib.info{color:var(--rc-teal,var(--rk-accent))}
.rc-mon .tx{flex:1;min-width:0}.rc-mon .tx b{display:block;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-mon .tx small{display:block;color:var(--rk-fg3);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-mon .acts{display:flex;gap:6px;flex:none}
.rc-mon .acts .rk-btn{width:auto}
.rc-mon .confirm{margin:0 14px 12px 50px;padding:10px 12px;border-radius:11px;background:color-mix(in srgb,var(--rk-warn) 10%,transparent);font-size:12.5px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.rc-mon .confirm span{flex:1;min-width:180px}
.rc-mon .stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}
.rc-mon .stat{padding:14px 16px;min-width:0}.rc-mon .stat+.stat{border-left:1px solid var(--rk-hair)}
.rc-mon .stat .k{color:var(--rk-fg2);font-size:12px}
.rc-mon .stat .v{font-size:19px;font-weight:650;margin:2px 0 0;white-space:nowrap}.rc-mon .stat .v small{font-size:12px;font-weight:400;color:var(--rk-fg3);margin-left:3px}
.rc-mon .stat .v.bad{color:var(--rk-bad)}
.rc-mon .spark{display:block;width:100%;height:24px;margin-top:6px}
.rc-mon .ev{all:unset;box-sizing:border-box;cursor:pointer;display:flex;align-items:center;gap:12px;width:100%;padding:10px 14px 10px 16px}
.rc-mon .ev+.ev,.rc-mon .more+.ev{border-top:1px solid var(--rk-hair)}
.rc-mon .ev:hover{background:var(--rk-surface)}.rc-mon .ev:focus-visible{outline:2px solid var(--vscode-focusBorder,#3d8bff);outline-offset:-2px}
.rc-mon .ev time{width:44px;flex:none;color:var(--rk-fg3);font-variant-numeric:tabular-nums;font-size:12px}
.rc-mon .ev .ib{width:18px;height:26px}
.rc-mon .ev .new{width:6px;height:6px;border-radius:50%;background:var(--rk-accent);flex:none}
.rc-mon .more{padding:0 16px 12px 80px;color:var(--rk-fg2);font-size:12.5px;line-height:1.55;overflow-wrap:anywhere}
.rc-mon .more small{display:block;color:var(--rk-fg3);margin-top:4px}
.rc-mon .empty{padding:22px 18px;text-align:center;color:var(--rk-fg3);font-size:12.5px}
.rc-mon .empty b{display:block;color:var(--rk-fg);font-size:14px;margin:6px 0 2px}
.rc-mon .watch{display:flex;align-items:center;gap:12px;padding:12px 14px 12px 16px}
.rc-mon .panel{padding:2px 16px 14px 50px}
.rc-mon .kv{display:flex;justify-content:space-between;gap:12px;padding:8px 0;font-size:12.5px}.rc-mon .kv+.kv{border-top:1px solid var(--rk-hair)}
.rc-mon .kv span{color:var(--rk-fg2);flex:none}.rc-mon .kv b{flex:1;min-width:0;font-weight:500;text-align:right;overflow-wrap:anywhere}
.rc-mon .field{display:flex;gap:8px;margin-top:10px}.rc-mon .field .rk-input{flex:1;min-width:0}.rc-mon .field .rk-btn{width:auto;flex:none;height:34px}
.rc-mon .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.rc-mon .chip{height:26px;display:inline-flex;align-items:center;gap:6px;padding:0 10px;border-radius:8px;background:var(--rk-surface2);font-size:12px;color:var(--rk-fg2)}
.rc-mon .msg{margin:-4px 2px 12px}
.rc-mon .foot{display:flex;gap:8px;margin-top:12px}.rc-mon .foot .rk-btn{width:auto;flex:1}
@media (max-width:560px){.rc-mon .stat{padding:12px 12px}.rc-mon .stat .v{font-size:17px}
  .rc-mon .todo{flex-wrap:wrap}.rc-mon .todo .acts{width:100%;padding-left:44px}.rc-mon .todo .acts .rk-btn{flex:1}
  .rc-mon .confirm{margin-left:14px}.rc-mon .panel{padding-left:16px}.rc-mon .more{padding-left:16px}}
`;

function Spark({ values, tone = "var(--rk-accent)" }: { values?: number[]; tone?: string }) {
  if (!values || values.length < 2) return null;
  const max = Math.max(...values, 1);
  const pts = values.map((v, i) => `${(i * 120 / (values.length - 1)).toFixed(1)},${(22 - (v / max) * 19).toFixed(1)}`).join(" ");
  return (
    <svg className="spark" viewBox="0 0 120 24" preserveAspectRatio="none" aria-hidden="true">
      <polyline points={pts} fill="none" stroke={tone} strokeWidth="1.5" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export interface MonitorViewProps {
  data: MonitorData | null;
  loading?: boolean;
  error?: string;
  message?: { tone: "ok" | "bad" | "warn"; text: string } | null;
  busy?: string;
  plan?: { role: { arn: string; exists: boolean | null }; resources: Array<{ kind: string; name: string; detail: string }>; permissions: string; cost: string; notes: string[] } | null;
  now?: number;
  onSelect?: (key: string) => void;
  onRefresh?: () => void;
  onOpenDeploy?: () => void;
  onOpenOperate?: () => void;
  onPlan?: (role: string) => void;
  onInstall?: (role: string, discord: string) => void;
  onUninstall?: () => void;
  onSettings?: (s: { discord?: string; email?: string }) => void;
  onRollback?: (o: Offer) => void;
  onAct?: (a: AlertView, s: NonNullable<AlertView["suggestions"]>[number]) => void;
}

function Confirm({ text, label, busy, onYes, onNo, testId }: { text: React.ReactNode; label: string; busy?: boolean; onYes: () => void; onNo: () => void; testId?: string }) {
  return (
    <div className="confirm" role="alertdialog" data-testid={testId}>
      <span>{text}</span>
      <Button small variant="ghost" onClick={onNo} disabled={busy}>취소</Button>
      <Button small onClick={onYes} disabled={busy} testId={testId ? `${testId}-yes` : undefined}>{busy ? "실행 중…" : label}</Button>
    </div>
  );
}

/** 할 일 — 이전 버전 롤백 · 대응 제안. 한 줄에 무엇 · 왜 · 버튼 하나. */
function Todo({ cur, busy, onRollback, onAct }: { cur: MonitorCurrentView; busy?: string; onRollback?: MonitorViewProps["onRollback"]; onAct?: MonitorViewProps["onAct"] }) {
  const [ask, setAsk] = useState("");
  const alerts = pendingAlerts(cur);
  if (!cur.offers.length && !alerts.length) return null;
  return (
    <>
      <div className="label"><span>할 일</span><span>승인 전에는 바뀌지 않습니다</span></div>
      <div className="card">
        {cur.offers.map((o) => {
          const id = `rb:${o.target}`;
          return (
            <React.Fragment key={id}>
              <div className="todo" data-testid="mon-offer">
                <span className="ib warn"><Icon name="history" size={16} /></span>
                <div className="tx"><b>이전 버전으로 되돌리기</b><small>{o.title} · {revOf(o.from_td)} → {revOf(o.target)}</small></div>
                {ask !== id && <div className="acts"><Button small onClick={() => setAsk(id)} disabled={!!busy} testId="mon-rollback">되돌리기</Button></div>}
              </div>
              {ask === id && <Confirm testId="mon-rollback-confirm" text={<><b>{revOf(o.target)}</b> 로 되돌립니다 · 무중단 1~3분</>} label="승인" busy={busy === "rollback"}
                onYes={() => onRollback?.(o)} onNo={() => setAsk("")} />}
            </React.Fragment>
          );
        })}
        {alerts.map((a) => (
          <React.Fragment key={a.id}>
            <div className="todo" data-testid="mon-alert">
              <span className="ib warn"><Icon name={kindIcon(a.kind)} size={16} /></span>
              <div className="tx"><b>{a.title}</b><small>{clock(a.at)} · {a.detail}</small></div>
              <div className="acts">{(a.suggestions ?? []).slice(0, 2).map((s, i) => ask === `al:${a.id}:${i}` ? null : (
                <Button key={i} small variant={i ? "ghost" : "primary"} onClick={() => setAsk(`al:${a.id}:${i}`)} disabled={!!busy}>{s.label}</Button>))}</div>
            </div>
            {(a.suggestions ?? []).slice(0, 2).map((s, i) => ask === `al:${a.id}:${i}` &&
              <Confirm key={i} testId="mon-act-confirm" text={<><b>{s.label}</b> · {s.detail}</>} label="승인" busy={busy === "act"}
                onYes={() => onAct?.(a, s)} onNo={() => setAsk("")} />)}
          </React.Fragment>
        ))}
      </div>
    </>
  );
}

function Stats({ cur }: { cur: MonitorCurrentView }) {
  const s = cur.service_state;
  const errPct = cur.now ? cur.now.error_rate * 100 : null;
  const short = s ? s.desired > 0 && s.running < s.desired : false;
  return (
    <div className="card stats">
      <div className="stat" data-testid="stat-requests"><div className="k">요청</div>
        <div className="v">{cur.now ? cur.now.rpm.toLocaleString() : "—"}<small>/분</small></div><Spark values={cur.series?.requests} /></div>
      <div className="stat" data-testid="stat-errors"><div className="k">오류율</div>
        <div className={`v${errPct !== null && errPct > 5 ? " bad" : ""}`}>{errPct === null ? "—" : errPct.toFixed(errPct < 10 ? 1 : 0)}<small>%</small></div>
        <Spark values={cur.series?.errors} tone="var(--rk-warn)" /></div>
      <div className="stat" data-testid="stat-tasks"><div className="k">실행 중</div>
        <div className={`v${short ? " bad" : ""}`}>{s ? s.running : "—"}<small>/ {s ? s.desired : "—"}</small></div>
        <div className="k" style={{ marginTop: 6 }}>{s?.revision || ""}</div></div>
    </div>
  );
}

function Records({ items, now }: { items: TimelineItem[]; now: Date }) {
  const [open, setOpen] = useState("");
  const [all, setAll] = useState(false);
  const shown = all ? items.slice(0, 30) : items.slice(0, 5);
  return (
    <>
      <div className="label"><span>최근 기록</span>{items.length > 5 && <button type="button" onClick={() => setAll(!all)}>{all ? "접기" : `전체 ${Math.min(items.length, 30)}개`}</button>}</div>
      {!items.length
        ? <div className="card empty" data-testid="mon-empty-timeline">기록 없음</div>
        : <div className="card" data-testid="mon-timeline">
            {shown.map((i) => (
              <React.Fragment key={i.id}>
                <button type="button" className="ev" aria-expanded={open === i.id} onClick={() => setOpen(open === i.id ? "" : i.id)}>
                  <time>{clock(i.at, now)}</time>
                  <span className={`ib ${i.tone}`}><Icon name={kindIcon(i.kind)} size={14} /></span>
                  <span className="tx"><b>{i.title}</b></span>
                  {i.fresh && <span className="new" aria-label="새 기록" />}
                </button>
                {open === i.id && <div className="more">{i.detail}<small>{sourceText(i.source)}</small></div>}
              </React.Fragment>
            ))}
          </div>}
    </>
  );
}

function Watcher({ cur, plan, busy, now, onPlan, onInstall, onUninstall, onSettings }: {
  cur: MonitorCurrentView; plan: MonitorViewProps["plan"]; busy?: string; now: number;
  onPlan?: MonitorViewProps["onPlan"]; onInstall?: MonitorViewProps["onInstall"]; onUninstall?: MonitorViewProps["onUninstall"]; onSettings?: MonitorViewProps["onSettings"];
}) {
  const w = cur.watcher;
  const [open, setOpen] = useState(false);
  const [role, setRole] = useState("");
  const [discord, setDiscord] = useState("");
  const [email, setEmail] = useState("");
  const [ask, setAsk] = useState(false);
  //: 설치 → 설치됨처럼 카드가 바뀌면 열려 있던 확인 단계를 닫는다(설치 확인이 '지우기' 확인으로 이어지지 않게).
  useEffect(() => { setAsk(false); setOpen(false); }, [w.installed]);
  if (w.installed === null) {
    return <div className="card watch"><Line icon="alert" tone="warn">상시 감시 상태를 확인하지 못했습니다 — {w.error || "AWS 연결을 확인하세요"}</Line></div>;
  }
  if (!w.installed) {
    return (
      <div className="card" data-testid="mon-setup">
        <div className="watch">
          <span className="ib"><Icon name="shield" size={16} /></span>
          <div className="tx"><b>상시 감시</b><small>PC 가 꺼져 있어도 감지 · 월 $0.1 미만</small></div>
          {!plan && <div className="acts"><Button small onClick={() => onPlan?.(role)} disabled={!!busy} testId="mon-plan">{busy === "plan" ? "확인 중…" : "설치"}</Button></div>}
        </div>
        {plan && (
          <div className="panel" data-testid="mon-plan-detail">
            <div className="chips">{plan.resources.map((r) => <span className="chip" key={r.kind} title={r.detail}>{r.kind}</span>)}</div>
            <div style={{ marginTop: 8 }}>
              <div className="kv"><span>할 수 있는 일</span><b>읽기 · 기록 · 알림 · 승인한 롤백</b></div>
              <div className="kv"><span>역할</span><b>{plan.role.arn.split("/").pop()} {plan.role.exists === true ? "· 준비됨" : plan.role.exists === false ? "· 없음" : "· 확인 못함"}</b></div>
            </div>
            {plan.notes.map((n) => <Line key={n} icon="alert" tone="warn">{n}</Line>)}
            {plan.role.exists === false && (
              <div className="field"><input className="rk-input" aria-label="상시 감시 역할" placeholder="다른 역할 이름 · 학교 계정은 LabRole" value={role} onChange={(e) => setRole(e.target.value)} />
                <Button small variant="ghost" onClick={() => onPlan?.(role)} disabled={!!busy}>다시 확인</Button></div>
            )}
            <div className="field"><input className="rk-input" aria-label="디스코드 웹후크" placeholder="디스코드 웹후크(선택)" value={discord} onChange={(e) => setDiscord(e.target.value)} /></div>
            {ask
              ? <div style={{ marginTop: 12 }}><Confirm testId="mon-install-confirm" text="위 리소스를 AWS 에 만듭니다 · 언제든 지울 수 있습니다" label="승인하고 설치" busy={busy === "install"}
                  onYes={() => onInstall?.(role, discord)} onNo={() => setAsk(false)} /></div>
              : <div className="foot"><Button onClick={() => setAsk(true)} disabled={!!busy || plan.role.exists === false} testId="mon-install">설치</Button></div>}
          </div>
        )}
      </div>
    );
  }
  const state = w.waiting_first_tick ? "첫 확인을 기다리는 중" : w.healthy ? `정상 · ${minutesAgo(w.last_tick, now)} 확인` : `확인 멈춤 · ${minutesAgo(w.last_tick, now)}`;
  return (
    <div className="card" data-testid="mon-watcher">
      <div className="watch">
        <span className={`ib ${w.healthy || w.waiting_first_tick ? "" : "warn"}`}><Icon name="shield" size={16} /></span>
        <div className="tx"><b>상시 감시</b><small>{state} · {[w.mail ? "메일" : "", w.discord ? "디스코드" : ""].filter(Boolean).join(" · ") || "VS Code 알림만"}</small></div>
        <div className="acts"><Button small variant="ghost" onClick={() => setOpen(!open)} testId="mon-watch-settings">{open ? "닫기" : "설정"}</Button></div>
      </div>
      {open && (
        <div className="panel">
          <div className="kv"><span>롤백 승인 링크</span><b>{w.approval_link ? (w.stable ? `${w.stable} 로` : "다음 배포부터") : "꺼짐 · VS Code 에서 승인"}</b></div>
          <div className="kv"><span>자동 실행</span><b>꺼짐 · 모두 승인 후</b></div>
          <div className="field"><input className="rk-input" aria-label="디스코드 웹후크" placeholder={w.discord ? "새 디스코드 웹후크" : "디스코드 웹후크"} value={discord} onChange={(e) => setDiscord(e.target.value)} />
            <Button small variant="ghost" onClick={() => { onSettings?.({ discord }); setDiscord(""); }} disabled={!!busy || !discord.trim()}>저장</Button></div>
          <div className="field"><input className="rk-input" aria-label="알림 메일" placeholder="알림 받을 메일" value={email} onChange={(e) => setEmail(e.target.value)} />
            <Button small variant="ghost" onClick={() => { onSettings?.({ email }); setEmail(""); }} disabled={!!busy || !email.trim()}>구독</Button></div>
          {ask
            ? <div style={{ marginTop: 12 }}><Confirm testId="mon-uninstall-confirm" text="상시 감시와 기록을 지웁니다" label="지우기" busy={busy === "uninstall"}
                onYes={() => onUninstall?.()} onNo={() => setAsk(false)} /></div>
            : <div className="foot">
                {w.discord && <Button small variant="ghost" onClick={() => onSettings?.({ discord: "" })} disabled={!!busy}>디스코드 끄기</Button>}
                <Button small variant="ghost" onClick={() => setAsk(true)} disabled={!!busy} testId="mon-uninstall">상시 감시 지우기</Button>
              </div>}
        </div>
      )}
    </div>
  );
}

/** 확장과 주고받지 않는 화면(서버 렌더 테스트 · 하네스가 그대로 그린다). */
export function MonitorView(props: MonitorViewProps) {
  const { data, loading, error, message, busy, plan, onSelect, onRefresh, onOpenDeploy, onOpenOperate } = props;
  const now = props.now ?? Date.now();
  const cur = data?.current ?? null;
  const items = useMemo(() => (cur ? timeline(cur, data?.seen ?? "") : []), [cur, data?.seen]);
  const pending = cur ? cur.offers.length + pendingAlerts(cur).length : 0;
  const h = cur ? hero(cur, items, pending) : null;
  return (
    <Kit className="rc-mon" data-testid="monitor">
      <style>{CSS}</style>
      <div className="hd">
        <h1>Operate</h1>
        {cur && (
          <span className="app" title={`${cur.cluster} · ${cur.region}`}><Logo kind="ecs" />
            {data!.monitors.length > 1
              ? <select aria-label="볼 서비스" value={cur.key} onChange={(e) => onSelect?.(e.target.value)}>
                  {data!.monitors.map((m) => <option key={m.key} value={m.key}>{m.service}</option>)}
                </select>
              : <span>{cur.service}</span>}
          </span>
        )}
        <span className="sp" />
        <button type="button" className="icon-btn" aria-label="새로 고침" title="새로 고침" aria-busy={loading || undefined} onClick={onRefresh}><Icon name="refresh" size={16} /></button>
      </div>
      {message && <div className="msg"><Line icon={message.tone === "ok" ? "check" : "alert"} tone={message.tone}>{message.text}</Line></div>}
      {error && <div className="msg"><Line icon="alert" tone="bad">{error}</Line></div>}
      {!cur && !loading && !error && (
        <div className="card empty" data-testid="mon-no-monitors">
          <Icon name="pulse" size={22} /><b>배포된 앱이 없습니다</b>
          ECS 로 배포하면 여기에 표시됩니다
          <div className="foot" style={{ justifyContent: "center" }}><Button small onClick={onOpenDeploy}>배포 캔버스 열기</Button></div>
        </div>
      )}
      {cur && h && (
        <div className="rc-mon-grid">
          <div>
            <div className="card hero" data-tone={h.tone} data-testid="mon-hero">
              <div style={{ minWidth: 0 }}><b><span className="dot" />{h.title}</b><p>{h.sub}</p></div>
              {cur.series && cur.series.requests.length > 1 ? <div className="chart"><AreaChart values={cur.series.requests} errors={cur.series.errors} height={120} /></div> : <div style={{ height: 18 }} />}
            </div>
            {cur.gaps.map((g) => <Line key={g} icon="alert" tone="warn">{g}</Line>)}
            <div className="label"><span>지금</span><span>최근 24시간</span></div>
            <Stats cur={cur} />
            <Records items={items} now={new Date(now)} />
          </div>
          <div>
            {pending === 0 && <><div className="label"><span>할 일</span></div><div className="card empty" data-testid="mon-calm">승인할 일이 없습니다</div></>}
            <Todo cur={cur} busy={busy} onRollback={props.onRollback} onAct={props.onAct} />
            <div className="label"><span>감시</span></div>
            <Watcher cur={cur} plan={plan ?? null} busy={busy} now={now} onPlan={props.onPlan} onInstall={props.onInstall}
              onUninstall={props.onUninstall} onSettings={props.onSettings} />
            <div className="label"><span>대응</span></div>
            <div className="card watch" data-testid="mon-operate">
              <span className="ib"><Icon name="pulse" size={16} /></span>
              <div className="tx"><b>운영 대응</b><small>원인 분석 · 조치 제안</small></div>
              <div className="acts"><Button small variant="ghost" onClick={onOpenOperate} testId="mon-open-operate">열기</Button></div>
            </div>
          </div>
        </div>
      )}
      {!cur && !loading && !error && (
        <>
          <div className="label"><span>대응</span></div>
          <div className="card watch" data-testid="mon-operate">
            <span className="ib"><Icon name="pulse" size={16} /></span>
            <div className="tx"><b>운영 대응</b><small>원인 분석 · 조치 제안</small></div>
            <div className="acts"><Button small variant="ghost" onClick={onOpenOperate} testId="mon-open-operate">열기</Button></div>
          </div>
        </>
      )}
    </Kit>
  );
}

/** 확장과 주고받는 쪽. active 일 때만 1분마다 새로 고치고, 본 기록을 '봤음'으로 남긴다. */
export const MonitorPage: React.FC<{ active: boolean; onOpenDeploy: () => void; onOpenOperate?: () => void }> = ({ active, onOpenDeploy, onOpenOperate }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [data, setData] = useState<MonitorData | null>(null);
  const [key, setKey] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState<MonitorViewProps["message"]>(null);
  const [plan, setPlan] = useState<MonitorViewProps["plan"]>(null);
  //: 이번에 화면을 열었을 때의 '본 곳' — 새로 고쳐도 「새로」 표시가 바로 사라지지 않게 처음 값을 지킨다.
  const seenAtOpen = useRef<Record<string, string>>({});
  const keyRef = useRef("");
  keyRef.current = key;

  //: 3.0.2 — 홈도 monitor.load 를 부른다. 내 요청의 결과만 받는다(다른 서비스로 바뀌지 않게).
  const reqRef = useRef("");
  const load = useCallback((k = keyRef.current) => {
    reqRef.current = `mon-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    setLoading(true); postMessage("monitor.load", { key: k, requestId: reqRef.current });
  }, [postMessage]);
  useEffect(() => {
    if (!active) { seenAtOpen.current = {}; return; }
    load();
    const t = window.setInterval(() => load(), 60_000);
    return () => window.clearInterval(t);
  }, [active, load]);

  useMessage(useCallback((event) => {
    const p = (event.payload ?? {}) as Record<string, unknown>;
    if ((event.type === "monitor.result" || event.type === "monitor.error") && typeof p.requestId === "string" && p.requestId !== reqRef.current) return;
    if (event.type === "monitor.result") {
      const d = p as unknown as MonitorData;
      setLoading(false); setError("");
      const cur = d.current;
      if (cur) {
        if (!(cur.key in seenAtOpen.current)) { seenAtOpen.current[cur.key] = d.seen ?? ""; }
        setKey(cur.key);
        if (cur.watcher.installed) { setPlan(null); }
      }
      setData({ ...d, seen: cur ? seenAtOpen.current[cur.key] : "" });
    }
    if (event.type === "monitor.error") {
      //: 1분 새로 고침의 오류가 진행 중인 설치 · 롤백 버튼을 다시 켜지 않게 — 그 요청의 오류(op)일 때만 푼다.
      setLoading(false);
      if (p.op) { setBusy(""); }
      setError(String(p.message ?? "상태를 불러오지 못했습니다").replace(/^Error:\s*/, ""));
    }
    if (event.type === "monitor.plan") { setBusy(""); setError(""); setPlan(p as unknown as MonitorViewProps["plan"]); }
    if (event.type === "monitor.done") {
      setBusy("");
      const notes = Array.isArray(p.notes) ? (p.notes as string[]) : [];
      const failed = Array.isArray(p.results) ? (p.results as Array<{ ok: boolean }>).some((r) => !r.ok) : false;
      setMessage({ tone: failed ? "warn" : "ok", text: [String(p.message ?? "완료했습니다"), ...notes].join(" ") });
    }
    if (event.type === "ops.alerts.actResult") { setBusy(""); setMessage({ tone: "ok", text: String(p.message ?? "적용했습니다") }); load(); }
    if (event.type === "ops.alerts.error" && p.alertId) { setBusy(""); setMessage({ tone: "bad", text: String(p.message ?? "적용하지 못했습니다") }); }
  }, [load]));

  //: 화면에 보인 가장 새 기록을 '봤음'으로 — 다음 '밤사이 요약'의 기준.
  const newest = data?.current?.events?.[0]?.sk ?? "";
  useEffect(() => {
    if (active && data?.current && newest) { postMessage("monitor.seen", { key: data.current.key, sk: newest }); }
  }, [active, data?.current?.key, newest, postMessage]);

  const run = (op: string, type: string, payload: Record<string, unknown>) => {
    setBusy(op); setMessage(null); setError("");
    postMessage(type, { key, ...payload });
  };
  return (
    <MonitorView data={data} loading={loading && !data} error={error} message={message} busy={busy} plan={plan}
      onSelect={(k) => { setKey(k); setPlan(null); setMessage(null); load(k); }}
      onRefresh={() => load()}
      onOpenDeploy={onOpenDeploy}
      onOpenOperate={onOpenOperate}
      onPlan={(role) => run("plan", "monitor.watch.plan", { role })}
      onInstall={(role, discord) => run("install", "monitor.watch.install", { role, discord, approved: true })}
      onUninstall={() => run("uninstall", "monitor.watch.uninstall", { approved: true })}
      onSettings={(s) => run("settings", "monitor.watch.settings", s)}
      onRollback={(o) => run("rollback", "monitor.rollback", { target_td: o.target, from_td: o.from_td, approved: true })}
      onAct={(a, s) => { setBusy("act"); setMessage(null); postMessage("ops.alerts.act", { alertId: a.id, action: s.action, value: s.value, approved: true }); }}
    />
  );
};

/** 위쪽 Monitor 탭의 승인 대기 숫자 — 확장이 밤사이 요약 · 모니터 화면을 읽을 때마다 알려 준다. */
export const MonitorBadge: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [count, setCount] = useState(0);
  useEffect(() => { postMessage("monitor.badge.get"); }, [postMessage]);
  useMessage(useCallback((event) => {
    if (event.type === "monitor.badge") { setCount(Math.max(0, Number((event.payload as { count?: number })?.count) || 0)); }
  }, []));
  if (!count) return null;
  return <span className="rc-nav-badge" aria-label={`승인 대기 ${count}건`} data-testid="monitor-badge">{count > 99 ? "99+" : count}</span>;
};
