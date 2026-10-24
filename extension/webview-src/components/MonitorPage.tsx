/**
 * 2.0.48 모니터 — 한 화면에서 서비스 상태 · 밤사이 일어난 일 · 승인 대기 · 24시간 감시자를 본다.
 *
 *  · 밤에는 감시자가 감지 · 기록 · 알림 · 준비까지만 한다. 여기서 사람이 승인한 것만 실행한다.
 *  · AWS 를 바꾸는 버튼(설치 · 삭제 · 롤백 · 대응)은 모두 확인 단계를 한 번 더 거친다.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { Box, Button, Kit, Line, SettingRow } from "./ui/kit";
import { Icon } from "./ui/icons";
import { CHARACTER_SRC } from "./recoderCharacter";
import { Logo } from "./canvas/Scene";
import {
  AlertView, MonitorCurrentView, MonitorData, Offer, TimelineItem, clock, hero, kindIcon, minutesAgo, pendingAlerts, revOf,
  sourceText, timeline,
} from "./monitorModel";

const CSS = `
.rc-mon{max-width:1180px;margin:0 auto}
.rc-mon .hd{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.rc-mon .hd h1{margin:0;font-size:19px;font-weight:650}
.rc-mon .app{display:flex;align-items:center;gap:8px;height:32px;padding:0 10px 0 6px;border-radius:10px;background:var(--rk-surface);font-size:12.5px}
.rc-mon .app svg.rc-logo{width:20px;height:20px;border-radius:5px}.rc-mon .app small{color:var(--rk-fg3)}
.rc-mon .app select{all:unset;cursor:pointer;font-weight:500}
.rc-mon .pill{display:inline-flex;align-items:center;gap:7px;height:28px;padding:0 11px;border-radius:99px;font-size:12px;background:var(--rk-surface);color:var(--rk-fg2)}
.rc-mon .pill.ok{background:color-mix(in srgb,var(--rk-ok) 13%,transparent);color:var(--rk-ok)}
.rc-mon .pill.warn{background:color-mix(in srgb,var(--rk-warn) 14%,transparent);color:var(--rk-warn)}
.rc-mon .sp{flex:1}
.rc-mon .grid{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:18px;align-items:start}
.rc-mon .card{background:var(--rk-surface);border-radius:14px}
.rc-mon .hero{display:flex;align-items:center;gap:14px;padding:16px 18px}
.rc-mon .hero img{width:44px;height:44px;flex:none}
.rc-mon .hero b{font-size:15px;display:block}.rc-mon .hero p{margin:2px 0 0;color:var(--rk-fg2);font-size:12.5px}
.rc-mon .hero[data-tone=bad] b{color:var(--rk-bad)}.rc-mon .hero[data-tone=warn] b{color:var(--rk-warn)}
.rc-mon .tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:12px}
.rc-mon .tile{background:var(--rk-surface);border-radius:12px;padding:12px 14px 10px;min-width:0}
.rc-mon .tile .k{display:flex;align-items:center;gap:6px;color:var(--rk-fg2);font-size:12px}
.rc-mon .tile .v{font-size:20px;font-weight:650;margin:4px 0 2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-mon .tile .v small{font-size:12px;font-weight:400;color:var(--rk-fg3);margin-left:4px}
.rc-mon .tile .s{font-size:11.5px;color:var(--rk-fg3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-mon .bar{height:6px;border-radius:99px;background:var(--rk-surface2);overflow:hidden;margin:10px 0 8px}.rc-mon .bar i{display:block;height:100%;border-radius:99px;background:var(--rk-ok)}
.rc-mon .bar.warn i{background:var(--rk-warn)}.rc-mon .bar.bad i{background:var(--rk-bad)}
.rc-mon .spark{display:block;width:100%;height:30px;margin-top:4px}
.rc-mon .sec{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin:20px 2px 8px}.rc-mon .sec b{font-size:13px;font-weight:600}.rc-mon .sec span{font-size:12px;color:var(--rk-fg3)}
.rc-mon .tl{padding:4px 16px}
.rc-mon .ev{display:grid;grid-template-columns:48px 30px minmax(0,1fr);gap:10px;padding:11px 0;align-items:start}
.rc-mon .ev+.ev{border-top:1px solid var(--rk-hair)}
.rc-mon .ev time{color:var(--rk-fg3);font-variant-numeric:tabular-nums;font-size:12px;padding-top:5px}
.rc-mon .ib{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;background:var(--rk-surface2);color:var(--rk-fg2)}
.rc-mon .ib.bad{background:color-mix(in srgb,var(--rk-bad) 14%,transparent);color:var(--rk-bad)}
.rc-mon .ib.warn{background:color-mix(in srgb,var(--rk-warn) 14%,transparent);color:var(--rk-warn)}
.rc-mon .ib.info{background:var(--rk-accent-soft);color:var(--rk-accent)}
.rc-mon .ev b{font-weight:600}.rc-mon .ev p{margin:2px 0 0;color:var(--rk-fg2);font-size:12.5px;overflow-wrap:anywhere}
.rc-mon .ev .meta{font-size:11.5px;color:var(--rk-fg3);margin-top:3px}
.rc-mon .new{display:inline-block;margin-left:6px;padding:0 6px;border-radius:6px;font-size:10.5px;font-weight:600;background:var(--rk-accent-soft);color:var(--rk-accent);vertical-align:1px}
.rc-mon .empty{padding:26px 18px;text-align:center;color:var(--rk-fg2)}.rc-mon .empty b{display:block;color:var(--rk-fg);font-size:14px;margin:6px 0 2px}
.rc-mon .ap{padding:14px 16px}.rc-mon .ap+.ap{margin-top:10px}
.rc-mon .ap .h{display:flex;align-items:center;gap:10px}
.rc-mon .ap .h b{font-size:13.5px;display:block}.rc-mon .ap .h small{display:block;color:var(--rk-fg3);font-size:11.5px}
.rc-mon .ap p{margin:10px 0 0;color:var(--rk-fg2);font-size:12.5px}
.rc-mon .btns{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}.rc-mon .btns .rk-btn{flex:1 1 auto;width:auto;min-width:96px}
.rc-mon .confirm{margin-top:12px;padding:10px 12px;border-radius:10px;background:color-mix(in srgb,var(--rk-warn) 10%,transparent);font-size:12.5px}
.rc-mon .kv{display:flex;justify-content:space-between;gap:12px;padding:9px 0;font-size:12.5px}.rc-mon .kv+.kv{border-top:1px solid var(--rk-hair)}
.rc-mon .kv span{color:var(--rk-fg2);flex:none;white-space:nowrap}.rc-mon .kv b{flex:1;min-width:0;font-weight:500;text-align:right;overflow-wrap:anywhere}
.rc-mon .msg{margin-bottom:12px}
.rc-mon .field{display:flex;gap:8px;margin-top:8px}.rc-mon .field .rk-input{flex:1}
.rc-mon .narrow-only{display:none}
@media (max-width:900px){.rc-mon .grid{grid-template-columns:1fr}.rc-mon .narrow-only{display:block;margin-top:14px}.rc-mon .wide-only{display:none}.rc-mon .tiles{grid-template-columns:1fr 1fr}}
`;

function Spark({ values, tone = "var(--rk-accent)" }: { values?: number[]; tone?: string }) {
  if (!values || values.length < 2) return <svg className="spark" viewBox="0 0 120 30" aria-hidden="true"><line x1="0" y1="27" x2="120" y2="27" stroke="var(--rk-hair)" /></svg>;
  const max = Math.max(...values, 1);
  const pts = values.map((v, i) => `${(i * 120 / (values.length - 1)).toFixed(1)},${(28 - (v / max) * 24).toFixed(1)}`).join(" ");
  return (
    <svg className="spark" viewBox="0 0 120 30" preserveAspectRatio="none" aria-hidden="true">
      <polygon points={`0,30 ${pts} 120,30`} fill={tone} opacity=".1" />
      <polyline points={pts} fill="none" stroke={tone} strokeWidth="1.6" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
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
  onPlan?: (role: string) => void;
  onInstall?: (role: string, discord: string) => void;
  onUninstall?: () => void;
  onSettings?: (s: { discord?: string; email?: string }) => void;
  onRollback?: (o: Offer) => void;
  onAct?: (a: AlertView, s: NonNullable<AlertView["suggestions"]>[number]) => void;
}

function Tiles({ cur, now }: { cur: MonitorCurrentView; now: number }) {
  const s = cur.service_state;
  const short = s ? s.desired > 0 && s.running < s.desired : false;
  const errPct = cur.now ? cur.now.error_rate * 100 : null;
  const w = cur.watcher;
  return (
    <div className="tiles">
      <div className="tile" data-testid="tile-requests"><div className="k"><Icon name="pulse" size={13} />요청</div>
        <div className="v">{cur.now ? cur.now.rpm.toLocaleString() : "—"}<small>/분</small></div>
        {cur.series ? <Spark values={cur.series.requests} /> : <div className="s">{cur.metrics ? "지표를 읽지 못했어요" : "방패가 꺼져 요청을 셀 수 없어요"}</div>}</div>
      <div className="tile" data-testid="tile-errors"><div className="k"><Icon name="alert" size={13} />오류율</div>
        <div className="v">{errPct === null ? "—" : errPct.toFixed(errPct < 10 ? 1 : 0)}<small>% · 지금</small></div>
        {cur.series ? <Spark values={cur.series.errors} tone={errPct !== null && errPct > 5 ? "var(--rk-bad)" : "var(--rk-warn)"} /> : <div className="s">5xx 비율 · 5분 단위</div>}</div>
      <div className="tile" data-testid="tile-tasks"><div className="k"><Icon name="box" size={13} />실행 중</div>
        <div className="v">{s ? s.running : "—"}<small>/ {s ? s.desired : "—"} 태스크</small></div>
        <div className={`bar ${short ? "bad" : ""}`}><i style={{ width: s && s.desired ? `${Math.min(100, (s.running / s.desired) * 100)}%` : "0%" }} /></div>
        <div className="s">{!s ? "서비스 상태를 읽지 못했어요" : `${s.revision ? `${s.revision} · ` : ""}${short ? "덜 떠 있어요" : s.desired === 0 ? "멈춤 · 요금 없음" : "모두 떠 있어요"}`}</div></div>
      <div className="tile" data-testid="tile-watcher"><div className="k"><Icon name="shield" size={13} />감시자</div>
        <div className="v">{!w.installed ? "없음" : w.waiting_first_tick ? "준비 중" : w.healthy ? "정상" : "멈춤"}<small>{w.installed && !w.waiting_first_tick ? minutesAgo(w.last_tick, now) : ""}</small></div>
        <div className={`bar ${!w.installed ? "" : w.healthy || w.waiting_first_tick ? "" : "warn"}`}><i style={{ width: w.installed ? "100%" : "0%" }} /></div>
        <div className="s">{!w.installed ? "PC 가 꺼져 있으면 감지 · 알림이 멈춰요" : w.waiting_first_tick ? "5분 안에 첫 확인 기록이 남아요" : `5분마다 · ${(w.ticks ?? 0).toLocaleString()}번 확인`}</div></div>
    </div>
  );
}

function Timeline({ items, now }: { items: TimelineItem[]; now: Date }) {
  if (!items.length) {
    return <div className="card empty" data-testid="mon-empty-timeline"><Icon name="check" size={22} /><b>기록이 없어요</b>오류 · 요청 급증 · 태스크 이상이 생기면 여기에 쌓여요</div>;
  }
  return (
    <div className="card tl" data-testid="mon-timeline">
      {items.slice(0, 20).map((i) => (
        <div className="ev" key={i.id}>
          <time>{clock(i.at, now)}</time>
          <span className={`ib ${i.tone}`}><Icon name={kindIcon(i.kind)} size={15} /></span>
          <div><b>{i.title}</b>{i.fresh && <span className="new">새로</span>}<p>{i.detail}</p><div className="meta">{sourceText(i.source)}</div></div>
        </div>
      ))}
    </div>
  );
}

function Confirm({ text, label, busy, onYes, onNo, testId }: { text: React.ReactNode; label: string; busy?: boolean; onYes: () => void; onNo: () => void; testId?: string }) {
  return (
    <div className="confirm" role="alertdialog" data-testid={testId}>
      {text}
      <div className="btns"><Button small variant="ghost" onClick={onNo} disabled={busy}>취소</Button><Button small onClick={onYes} disabled={busy} testId={testId ? `${testId}-yes` : undefined}>{busy ? "실행 중…" : label}</Button></div>
    </div>
  );
}

function Approvals({ cur, busy, onRollback, onAct }: { cur: MonitorCurrentView; busy?: string; onRollback?: MonitorViewProps["onRollback"]; onAct?: MonitorViewProps["onAct"] }) {
  const [ask, setAsk] = useState<string>("");
  const alerts = pendingAlerts(cur);
  const total = cur.offers.length + alerts.length;
  return (
    <>
      <div className="sec" style={{ marginTop: 0 }}><b>승인 대기 {total}건</b><span>승인 전에는 아무것도 바뀌지 않아요</span></div>
      {total === 0 && <div className="card empty" data-testid="mon-no-approvals"><b>승인할 일이 없어요</b>필요하면 여기에 롤백 · 대응 제안이 올라와요</div>}
      {cur.offers.map((o) => {
        const id = `rb:${o.target}`;
        return (
          <div className="card ap" key={id} data-testid="mon-offer">
            <div className="h"><span className="ib"><Icon name="history" size={16} /></span><div><b>이전 버전으로 되돌리기</b><small>{revOf(o.from_td)} → {revOf(o.target)} · {o.title}</small></div></div>
            <p>이전 이미지로 무중단 교체해요(1~3분). 원인 코드는 그대로라 고친 뒤 다시 배포하세요.</p>
            {ask === id
              ? <Confirm testId="mon-rollback-confirm" text={<>지금 <b>{revOf(o.from_td)}</b> 을 <b>{revOf(o.target)}</b> 로 되돌립니다.</>} label="롤백 승인" busy={busy === "rollback"}
                  onYes={() => onRollback?.(o)} onNo={() => setAsk("")} />
              : <div className="btns"><Button small onClick={() => setAsk(id)} disabled={!!busy} testId="mon-rollback">{revOf(o.target)} 로 롤백</Button></div>}
          </div>
        );
      })}
      {alerts.map((a) => (
        <div className="card ap" key={a.id} data-testid="mon-alert">
          <div className="h"><span className="ib"><Icon name={kindIcon(a.kind)} size={16} /></span><div><b>{a.title}</b><small>{clock(a.at)} · VS Code 가 켜져 있을 때 감지</small></div></div>
          <p>{a.detail}</p>
          {(a.suggestions ?? []).map((s, i) => {
            const id = `al:${a.id}:${i}`;
            return ask === id
              ? <Confirm key={id} testId="mon-act-confirm" text={<><b>{s.label}</b> — {s.detail}</>} label="승인하고 적용" busy={busy === "act"}
                  onYes={() => onAct?.(a, s)} onNo={() => setAsk("")} />
              : <div className="btns" key={id}><Button small variant="ghost" onClick={() => setAsk(id)} disabled={!!busy}>{s.label}</Button></div>;
          })}
        </div>
      ))}
    </>
  );
}

function WatcherCard({ cur, plan, busy, now, onPlan, onInstall, onUninstall, onSettings }: {
  cur: MonitorCurrentView; plan: MonitorViewProps["plan"]; busy?: string; now: number;
  onPlan?: MonitorViewProps["onPlan"]; onInstall?: MonitorViewProps["onInstall"]; onUninstall?: MonitorViewProps["onUninstall"]; onSettings?: MonitorViewProps["onSettings"];
}) {
  const w = cur.watcher;
  const [role, setRole] = useState("");
  const [discord, setDiscord] = useState("");
  const [email, setEmail] = useState("");
  const [ask, setAsk] = useState(false);
  //: 설치 → 설치됨처럼 카드가 바뀌면 열려 있던 확인 단계를 닫는다(설치 확인이 '지우기' 확인으로 이어지지 않게).
  useEffect(() => { setAsk(false); }, [w.installed]);
  if (w.installed === null) {
    return <div className="card ap"><Line icon="alert" tone="warn">감시자 상태를 확인하지 못했어요 — {w.error || "AWS 연결을 확인하세요"}</Line></div>;
  }
  if (!w.installed) {
    return (
      <div className="card ap" data-testid="mon-setup">
        <div className="h"><span className="ib"><Icon name="shield" size={16} /></span><div><b>24시간 감시자</b><small>PC 가 꺼져 있어도 내 AWS 안에서</small></div></div>
        <Box>
          <SettingRow icon="pulse" title="경보 · ECS 이벤트 · 5분마다 확인" desc="오류 증가 · 요청 급증 · 컨테이너 멈춤 · 태스크 부족" />
          <SettingRow icon="bell" title="메일 · 디스코드로 알림" desc="급하면 폰에서 이전 버전 롤백만 승인" />
          <SettingRow icon="lock" title="기록은 내 AWS 에만" desc="ReCoder 서버로 보내지 않아요 · 30일 뒤 자동 삭제" />
        </Box>
        {!plan && <div className="btns"><Button onClick={() => onPlan?.(role)} disabled={!!busy} testId="mon-plan">{busy === "plan" ? "확인 중…" : "설치 내용 확인"}</Button></div>}
        {plan && (
          <div data-testid="mon-plan-detail">
            <div style={{ marginTop: 12 }}>
              {plan.resources.map((r) => <div className="kv" key={r.kind}><span>{r.kind}</span><b>{r.detail}</b></div>)}
              <div className="kv"><span>권한</span><b>{plan.permissions}</b></div>
              <div className="kv"><span>요금</span><b>{plan.cost}</b></div>
              <div className="kv"><span>역할</span><b>{plan.role.arn.split("/").pop()} {plan.role.exists === true ? "· 준비됨" : plan.role.exists === false ? "· 없음" : "· 확인 못함"}</b></div>
            </div>
            {plan.notes.map((n) => <Line key={n} icon="alert" tone="warn">{n}</Line>)}
            {plan.role.exists === false && (
              <div className="field"><input className="rk-input" aria-label="감시자 역할" placeholder="다른 역할 이름 · 학교 계정은 LabRole" value={role} onChange={(e) => setRole(e.target.value)} />
                <Button small variant="ghost" onClick={() => onPlan?.(role)} disabled={!!busy}>다시 확인</Button></div>
            )}
            <div className="field"><input className="rk-input" aria-label="디스코드 웹후크" placeholder="디스코드 웹후크 주소(선택)" value={discord} onChange={(e) => setDiscord(e.target.value)} /></div>
            {ask
              ? <Confirm testId="mon-install-confirm" text="위 리소스를 내 AWS 에 만들어요. 언제든 여기서 지울 수 있어요." label="승인하고 설치" busy={busy === "install"}
                  onYes={() => onInstall?.(role, discord)} onNo={() => setAsk(false)} />
              : <div className="btns"><Button onClick={() => setAsk(true)} disabled={!!busy || plan.role.exists === false} testId="mon-install">설치</Button></div>}
          </div>
        )}
      </div>
    );
  }
  return (
    <div className="card ap" data-testid="mon-watcher">
      <div className="h"><span className="ib"><Icon name="shield" size={16} /></span><div><b>24시간 감시자</b><small>내 AWS 안에서 · {w.waiting_first_tick ? "첫 확인 기다리는 중" : `마지막 확인 ${minutesAgo(w.last_tick, now)}`}</small></div></div>
      <div style={{ marginTop: 10 }}>
        <div className="kv"><span>알림</span><b>{[w.mail ? "메일" : "", w.discord ? "디스코드" : ""].filter(Boolean).join(" · ") || "VS Code 만"}</b></div>
        <div className="kv"><span>밤 롤백 링크</span><b>{w.approval_link ? (w.stable ? `${w.stable} 로 되돌리기` : "다음 배포부터") : "꺼짐 · VS Code 에서 승인"}</b></div>
        <div className="kv"><span>자동 실행</span><b>꺼짐 · 모두 승인 후</b></div>
      </div>
      <div className="field"><input className="rk-input" aria-label="디스코드 웹후크" placeholder={w.discord ? "새 디스코드 웹후크(비우면 그대로)" : "디스코드 웹후크 주소"} value={discord} onChange={(e) => setDiscord(e.target.value)} />
        <Button small variant="ghost" onClick={() => { onSettings?.({ discord }); setDiscord(""); }} disabled={!!busy || !discord.trim()}>저장</Button></div>
      {w.discord && <div className="btns"><Button small variant="ghost" onClick={() => onSettings?.({ discord: "" })} disabled={!!busy}>디스코드 알림 끄기</Button></div>}
      <div className="field"><input className="rk-input" aria-label="알림 메일" placeholder="알림 받을 메일 추가" value={email} onChange={(e) => setEmail(e.target.value)} />
        <Button small variant="ghost" onClick={() => { onSettings?.({ email }); setEmail(""); }} disabled={!!busy || !email.trim()}>구독</Button></div>
      {ask
        ? <Confirm testId="mon-uninstall-confirm" text="감시자 함수 · 규칙 · 기록 표 · 로그를 지워요. 기록도 함께 사라져요." label="지우기" busy={busy === "uninstall"}
            onYes={() => onUninstall?.()} onNo={() => setAsk(false)} />
        : <div className="btns"><Button small variant="ghost" onClick={() => setAsk(true)} disabled={!!busy} testId="mon-uninstall">감시자 지우기</Button></div>}
    </div>
  );
}

/** 확장과 주고받지 않는 화면(서버 렌더 테스트 · 하네스가 그대로 그린다). */
export function MonitorView(props: MonitorViewProps) {
  const { data, loading, error, message, busy, plan, onSelect, onRefresh, onOpenDeploy } = props;
  const now = props.now ?? Date.now();
  const cur = data?.current ?? null;
  const items = useMemo(() => (cur ? timeline(cur, data?.seen ?? "") : []), [cur, data?.seen]);
  const pending = cur ? cur.offers.length + pendingAlerts(cur).length : 0;
  const h = cur ? hero(cur, items, pending) : null;
  const w = cur?.watcher;
  return (
    <Kit className="rc-mon" data-testid="monitor">
      <style>{CSS}</style>
      <div className="hd">
        <h1>모니터</h1>
        {cur && (
          <span className="app"><Logo kind="ecs" />
            {data!.monitors.length > 1
              ? <select aria-label="볼 서비스" value={cur.key} onChange={(e) => onSelect?.(e.target.value)}>
                  {data!.monitors.map((m) => <option key={m.key} value={m.key}>{m.service} · {m.region}</option>)}
                </select>
              : <span>{cur.service}</span>}
            <small>{data!.monitors.length > 1 ? "ECS" : `ECS · ${cur.region}`}</small></span>
        )}
        {w && <span className={`pill ${w.installed ? (w.healthy || w.waiting_first_tick ? "ok" : "warn") : ""}`} data-testid="mon-pill">
          <Icon name="shield" size={13} />{w.installed ? (w.waiting_first_tick ? "감시자 준비 중" : w.healthy ? `감시자 동작 중 · ${minutesAgo(w.last_tick, now)} 확인` : "감시자 확인 멈춤") : "감시자 없음 · VS Code 가 켜져 있을 때만"}</span>}
        <span className="sp" />
        <Button small variant="ghost" onClick={onRefresh} disabled={loading}>{loading ? "불러오는 중…" : "새로 고침"}</Button>
      </div>
      {message && <div className="msg"><Line icon={message.tone === "ok" ? "check" : "alert"} tone={message.tone}>{message.text}</Line></div>}
      {error && <div className="msg"><Line icon="alert" tone="bad">{error}</Line></div>}
      {!cur && !loading && !error && (
        <div className="card empty" data-testid="mon-no-monitors">
          <Icon name="pulse" size={24} /><b>감시 중인 앱이 없어요</b>
          ECS 로 배포할 때 「공격·장애 감지 알림」을 켜면 여기에 나타나요.
          <div className="btns" style={{ justifyContent: "center" }}><Button small onClick={onOpenDeploy}>배포 캔버스 열기</Button></div>
        </div>
      )}
      {cur && h && (
        <div className="grid">
          <div>
            <div className="card hero" data-tone={h.tone} data-testid="mon-hero"><img src={CHARACTER_SRC} alt="" /><div><b>{h.title}</b><p>{h.sub}</p></div></div>
            {cur.gaps.map((g) => <Line key={g} icon="alert" tone="warn">{g}</Line>)}
            {/* 좁은 화면 — 승인 대기를 요약 바로 아래로(넓은 화면은 오른쪽 칸) */}
            <div className="narrow-only"><Approvals cur={cur} busy={busy} onRollback={props.onRollback} onAct={props.onAct} /></div>
            <Tiles cur={cur} now={now} />
            <div className="sec"><b>일어난 일</b><span>{items.some((i) => i.fresh) ? "마지막으로 본 뒤 생긴 것에 「새로」" : "최근 30일"}</span></div>
            <Timeline items={items} now={new Date(now)} />
          </div>
          <div>
            <div className="wide-only"><Approvals cur={cur} busy={busy} onRollback={props.onRollback} onAct={props.onAct} /></div>
            <div className="sec"><b>감시 설정</b><span>내 AWS 안에서 동작</span></div>
            <WatcherCard cur={cur} plan={plan ?? null} busy={busy} now={now} onPlan={props.onPlan} onInstall={props.onInstall}
              onUninstall={props.onUninstall} onSettings={props.onSettings} />
          </div>
        </div>
      )}
    </Kit>
  );
}

/** 확장과 주고받는 쪽. active 일 때만 1분마다 새로 고치고, 본 기록을 '봤음'으로 남긴다. */
export const MonitorPage: React.FC<{ active: boolean; onOpenDeploy: () => void }> = ({ active, onOpenDeploy }) => {
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

  const load = useCallback((k = keyRef.current) => { setLoading(true); postMessage("monitor.load", { key: k }); }, [postMessage]);
  useEffect(() => {
    if (!active) { seenAtOpen.current = {}; return; }
    load();
    const t = window.setInterval(() => load(), 60_000);
    return () => window.clearInterval(t);
  }, [active, load]);

  useMessage(useCallback((event) => {
    const p = (event.payload ?? {}) as Record<string, unknown>;
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
      setError(String(p.message ?? "모니터를 불러오지 못했습니다").replace(/^Error:\s*/, ""));
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
