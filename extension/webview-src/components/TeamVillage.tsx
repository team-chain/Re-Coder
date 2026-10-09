/**
 * 팀 작업 마을 — 팀 모드 생성 진행을 "작업장" 으로 보여 준다.
 *
 * 관통 원칙: **움직임은 코어의 실제 진행 이벤트(teamState.reduceTeam 결과)로만 일어난다.**
 *  · 설계 리코더: 설계 중이면 설계실에서 작업, 끝나면 작업 카드가 대기열에 있다(카드 수 = 남은 파일 수).
 *  · 개발 리코더: 파일 작성을 시작(file_start)하면 대기열로 가서 카드를 집어 자기 작업대로 돌아온다.
 *    이어 쓰기(file_part)는 작업대 화면에 조각 수, 자동 검사 교정(fixing)은 **작성한 리코더 자신의** 작업대가
 *    노랗게 바뀐다(교정은 별도 검토 에이전트가 아니라 작성한 슬롯이 한다 — 엔진 구조 그대로).
 *    완료(file_done)하면 카드가 완성 선반으로 간다.
 *  · 검토 리코더: 마지막 전체 점검(consistency) 때만 가운데로 나온다.
 *  · 단계 문: 공통 기반 → 기능 → 화면. 앞 단계 파일이 모두 끝나야 다음 칸이 열린다(엔진의 wave 순서).
 * 지어낸 연출(가짜 대화·가짜 진행률)은 넣지 않는다.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { characterImg } from "./recoderCharacter";
import type { TeamMember, TeamView } from "./teamState";

export const VILLAGE_HEIGHT = 368;
const LAYER = ["공통 기반", "기능", "화면"];
const LAYER_COLOR = ["#4f8cff", "#2fbfa5", "#b08cff"];

export interface Rect { x: number; y: number; w: number; h: number }
export interface VillageLayout { W: number; H: number; plan: Rect; review: Rect; queue: Rect; qcols: Rect[]; desks: Rect[]; shelf: Rect; segs: Rect[] }

/** 화면 폭과 개발 에이전트 수로 위치를 정한다(순수 함수 — 테스트 가능). */
export function villageLayout(W: number, devs: number, H = VILLAGE_HEIGHT): VillageLayout {
  const n = Math.max(1, devs);
  const side = Math.max(110, Math.min(170, W * 0.19));
  const plan = { x: 10, y: 10, w: side, h: 104 };
  const review = { x: W - 10 - side, y: 10, w: side, h: 104 };
  const queue = { x: plan.x + side + 10, y: 10, w: Math.max(120, review.x - plan.x - side - 20), h: 104 };
  const qw = (queue.w - 16 - 12) / 3;
  const qcols = [0, 1, 2].map(i => ({ x: queue.x + 8 + i * (qw + 6), y: queue.y + 22, w: qw, h: queue.h - 30 }));
  const gap = n > 4 ? 8 : 12;
  const dw = Math.max(64, Math.min(170, (W - 20 - (n - 1) * gap) / n));
  const total = n * dw + (n - 1) * gap;
  const sx = Math.max(10, (W - total) / 2);
  const desks = Array.from({ length: n }, (_, i) => ({ x: sx + i * (dw + gap), y: 200, w: dw, h: 60 }));
  const shelf = { x: 10, y: H - 70, w: W - 20, h: 60 };
  const sw = (shelf.w - 12) / 3;
  const segs = [0, 1, 2].map(i => ({ x: shelf.x + i * (sw + 6), y: shelf.y, w: sw, h: shelf.h }));
  return { W, H, plan, review, queue, qcols, desks, shelf, segs };
}

const css = `
.rc-vl{position:relative;height:${VILLAGE_HEIGHT}px;border-radius:10px;overflow:hidden;border:1px solid var(--vscode-panel-border,#2a2e35);
  background:radial-gradient(ellipse at 50% 120%,rgba(55,148,255,.10),transparent 60%),linear-gradient(rgba(127,127,127,.06) 1px,transparent 1px) 0 0/24px 24px,linear-gradient(90deg,rgba(127,127,127,.06) 1px,transparent 1px) 0 0/24px 24px,var(--vscode-editor-background,#191b1f)}
.rc-vl .zn{position:absolute;border:1px dashed var(--vscode-panel-border,#3a404a);border-radius:10px}
.rc-vl .zn>span{position:absolute;left:8px;top:4px;font-size:10px;color:var(--vscode-descriptionForeground,#8b95a3)}
.rc-vl .qc{position:absolute;border-radius:7px;background:rgba(0,0,0,.16)}
.rc-vl .qc>span{position:absolute;left:0;right:0;bottom:2px;text-align:center;font-size:9.5px;color:var(--vscode-descriptionForeground,#8b95a3)}
.rc-vl .qc.locked::after{content:"🔒 앞 단계가 끝나면 열림";position:absolute;inset:0;z-index:4;display:flex;align-items:center;justify-content:center;text-align:center;padding:0 4px;font-size:9.5px;color:var(--vscode-descriptionForeground,#8b95a3);background:rgba(10,12,15,.5);border-radius:7px}
.rc-vl .cd{position:absolute;width:26px;height:17px;border-radius:3px;z-index:3;box-shadow:0 1px 0 rgba(0,0,0,.35)}
.rc-vl .cd::before{content:"";position:absolute;left:4px;right:4px;top:4px;height:2px;background:rgba(255,255,255,.55);box-shadow:0 4px 0 rgba(255,255,255,.35)}
.rc-vl .more{position:absolute;font-size:9.5px;color:var(--vscode-descriptionForeground,#8b95a3);z-index:3}
.rc-vl .dk{position:absolute;border-radius:9px;background:var(--vscode-editorWidget-background,#23272e);border:1px solid var(--vscode-panel-border,#333a44);transition:border-color .3s,box-shadow .3s}
.rc-vl .dk.busy{border-color:rgba(55,148,255,.55);box-shadow:0 0 16px rgba(55,148,255,.15)}
.rc-vl .dk.fix{border-color:rgba(204,167,0,.7);box-shadow:0 0 16px rgba(204,167,0,.2)}
.rc-vl .dk .mon{position:absolute;left:7px;right:7px;top:6px;height:30px;border-radius:5px;background:rgba(0,0,0,.35);border:1px solid var(--vscode-panel-border,#2d333c);font:10px/1.3 var(--vscode-editor-font-family,monospace);color:#7fd1ff;padding:2px 6px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.rc-vl .dk .mon i{font-style:normal;color:var(--vscode-descriptionForeground,#8b95a3)}
.rc-vl .dk .bar{position:absolute;left:7px;right:7px;bottom:8px;height:4px;border-radius:2px;background:rgba(127,127,127,.2);overflow:hidden}
.rc-vl .dk.busy .bar i{position:absolute;top:0;bottom:0;width:35%;background:var(--vscode-progressBar-background,#3794ff);animation:rcVlScan 1.4s ease-in-out infinite}
.rc-vl .dk .lb{position:absolute;left:6px;right:6px;bottom:-16px;display:flex;justify-content:space-between;font-size:9.5px;color:var(--vscode-descriptionForeground,#8b95a3);white-space:nowrap}
.rc-vl .dk .lb b{font-weight:500;color:var(--vscode-charts-green,#73d39b)}
.rc-vl .sg{position:absolute;border-radius:8px;border:1px solid var(--vscode-panel-border,#2f353e);background:rgba(127,127,127,.04)}
.rc-vl .sg>span{position:absolute;left:8px;top:3px;font-size:9.5px;color:var(--vscode-descriptionForeground,#8b95a3)}
.rc-vl .sg>b{position:absolute;right:8px;top:3px;font-size:9.5px;font-weight:500;color:var(--vscode-charts-green,#73d39b)}
.rc-vl .sg .st{position:absolute;width:16px;height:6px;border-radius:2px}
.rc-vl .ag{position:absolute;width:54px;margin-left:-27px;z-index:5;transition:left .9s cubic-bezier(.35,.6,.35,1),top .9s cubic-bezier(.35,.6,.35,1);pointer-events:none}
.rc-vl .ag .bw{position:relative;width:40px;height:40px;margin:0 auto}
.rc-vl .ag .bw img{filter:drop-shadow(0 3px 4px rgba(0,0,0,.4))}
.rc-vl .ag.walk .bw{animation:rcVlWalk .3s ease-in-out infinite alternate}
.rc-vl .ag.type .bw{animation:rcVlType .26s ease-in-out infinite alternate}
.rc-vl .ag.wait .bw{opacity:.6;filter:grayscale(.6)}
.rc-vl .ag.cheer .bw{animation:rcVlCheer .45s ease-in-out 4 alternate}
.rc-vl .ag .bd{display:block;margin:1px auto 0;width:fit-content;font-size:9.5px;font-weight:700;padding:0 6px;border-radius:99px;white-space:nowrap;background:var(--vscode-editorWidget-background,#2a2f37);border:1px solid var(--vscode-panel-border,#3d4550);color:var(--vscode-foreground,#cfd6df)}
.rc-vl .ag.planner .bd{border-color:rgba(79,140,255,.6)}
.rc-vl .ag.review .bd{border-color:rgba(204,167,0,.6)}
.rc-vl .ag .cr{position:absolute;left:7px;top:-11px;width:26px;height:17px;border-radius:3px}
.rc-vl .bb{position:absolute;bottom:58px;left:50%;transform:translateX(-50%);max-width:190px;overflow:hidden;text-overflow:ellipsis;background:#f3f5f8;color:#1b1f24;font-size:10px;font-weight:600;padding:2px 8px;border-radius:9px;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.35)}
.rc-vl .bb::after{content:"";position:absolute;left:50%;bottom:-4px;margin-left:-4px;border:4px solid transparent;border-bottom:0;border-top-color:#f3f5f8}
.rc-vl .bb.warn{background:#fff3c4}.rc-vl .bb.warn::after{border-top-color:#fff3c4}
.rc-vl .bb.ok{background:#d9f7e5}.rc-vl .bb.ok::after{border-top-color:#d9f7e5}
.rc-vl .fly{position:absolute;width:26px;height:17px;border-radius:3px;z-index:6;transition:left .7s cubic-bezier(.3,.7,.3,1),top .7s cubic-bezier(.3,.7,.3,1),opacity .2s .65s}
@keyframes rcVlWalk{from{transform:translateY(0) rotate(-4deg)}to{transform:translateY(-4px) rotate(4deg)}}
@keyframes rcVlType{from{transform:translateY(0)}to{transform:translateY(-2px)}}
@keyframes rcVlCheer{from{transform:translateY(0)}to{transform:translateY(-8px)}}
@keyframes rcVlScan{from{left:-35%}to{left:100%}}
@media (prefers-reduced-motion: reduce){.rc-vl .ag,.rc-vl .fly{transition:none!important}.rc-vl .ag .bw,.rc-vl .dk .bar i{animation:none!important}}
`;

const base = (p?: string) => (p ?? "").split("/").pop() || "";
const center = (r: Rect) => ({ x: r.x + r.w / 2, y: r.y + r.h / 2 });

interface Fly { id: number; layer: number; from: { x: number; y: number }; to: { x: number; y: number } }

function FlyingCard({ fly, onDone }: { fly: Fly; onDone: (id: number) => void }) {
  const [at, setAt] = useState(fly.from);
  const [gone, setGone] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => { setAt(fly.to); setGone(true); });
    const t = setTimeout(() => onDone(fly.id), 900);
    return () => { cancelAnimationFrame(raf); clearTimeout(t); };
  }, [fly, onDone]);
  return <div className="fly" style={{ left: at.x, top: at.y, background: LAYER_COLOR[fly.layer] ?? LAYER_COLOR[1], opacity: gone ? 0 : 1 }} />;
}

export interface TeamVillageProps {
  roster: TeamMember[];
  view: TeamView;
  width: number;
  paused?: boolean;
}

export function TeamVillage({ roster, view, width, paused }: TeamVillageProps) {
  const devs = useMemo(() => roster.filter(m => m.role === "dev"), [roster]);
  const L = useMemo(() => villageLayout(width, devs.length), [width, devs.length]);
  const layerOf = useMemo(() => new Map(view.files.map(f => [f.file, f.layer])), [view.files]);

  //: 파일 작성 시작(에이전트의 file 이 바뀜) → 잠깐 대기열로 가서 카드를 집어 온다. 완료(done 증가) → 카드가 선반으로.
  const prev = useRef<Record<string, { file?: string; done: number }>>({});
  const [fetching, setFetching] = useState<Record<string, number>>({});
  const [flies, setFlies] = useState<Fly[]>([]);
  const flyId = useRef(0);
  useEffect(() => {
    const now: Record<string, { file?: string; done: number }> = {};
    const newFetch: Record<string, number> = {};
    const newFlies: Fly[] = [];
    devs.forEach((m, i) => {
      const a = view.agents[m.id];
      const cur = { file: a?.file, done: a?.done ?? 0 };
      const was = prev.current[m.id];
      if (was && cur.file && cur.file !== was.file) {
        const l = layerOf.get(cur.file) ?? 1;
        newFetch[m.id] = l;
      }
      if (was && cur.done > was.done) {
        const d = L.desks[i];
        const l = was.file ? (layerOf.get(was.file) ?? 1) : 1;
        const seg = L.segs[l] ?? L.segs[1];
        newFlies.push({ id: ++flyId.current, layer: l, from: { x: d.x + d.w / 2 - 13, y: d.y + 8 }, to: { x: seg.x + seg.w / 2, y: seg.y + 26 } });
      }
      now[m.id] = cur;
    });
    prev.current = now;
    if (Object.keys(newFetch).length) {
      setFetching(f => ({ ...f, ...newFetch }));
      const ids = Object.keys(newFetch);
      setTimeout(() => setFetching(f => { const n = { ...f }; ids.forEach(id => delete n[id]); return n; }), 1000);
    }
    if (newFlies.length) setFlies(f => [...f, ...newFlies].slice(-12));
  }, [view.agents, view.files, devs, layerOf, L]);
  const dropFly = React.useCallback((id: number) => setFlies(f => f.filter(x => x.id !== id)), []);

  const perLayer = [0, 1, 2].map(l => {
    const files = view.files.filter(f => f.layer === l);
    return {
      total: files.length,
      waiting: files.filter(f => f.state === "waiting").length,
      done: files.filter(f => f.state === "done" || f.state === "issue").length,
    };
  });
  const layerDone = perLayer.map(p => p.total > 0 && p.done === p.total);
  const unlocked = (l: number) => view.files.length === 0 || l === 0 || (view.layer !== null && view.layer >= l) || layerDone.slice(0, l).every(Boolean);

  const checking = view.phase === "checking";
  const finished = view.phase === "done";
  const planning = view.phase === "planning";
  const gather = (k: number, count: number) => ({ x: L.W / 2 + (k - (count - 1) / 2) * 50, y: L.desks[0].y - 64 });
  const everyone = roster.length;

  const agentNode = (m: TeamMember, k: number) => {
    let pos: { x: number; y: number };
    let cls = "ag " + m.role;
    let bubble: { text: string; tone?: string } | null = null;
    let carry: number | null = null;
    if (m.role === "planner") {
      const c = center(L.plan); pos = { x: c.x, y: L.plan.y + 30 };
      if (planning && !paused) { cls += " type"; bubble = { text: "요청을 작업으로 나누는 중" }; }
      if (checking || finished) pos = gather(k, everyone);
    } else if (m.role === "review") {
      const c = center(L.review); pos = { x: c.x, y: L.review.y + 30 };
      if (checking) { pos = gather(k, everyone); cls += paused ? "" : " type"; bubble = { text: "전체 점검 — 빌드로 확인", tone: "warn" }; }
      else if (finished) pos = gather(k, everyone);
    } else {
      const i = devs.findIndex(d => d.id === m.id);
      const d = L.desks[i];
      const a = view.agents[m.id];
      pos = { x: d.x + d.w / 2, y: d.y - 58 };
      const fetchLayer = fetching[m.id];
      if (fetchLayer !== undefined) {
        const q = L.qcols[fetchLayer] ?? L.qcols[1];
        //: 여럿이 같은 칸에서 동시에 카드를 집으면 한 점에 겹친다 — 작업대 순서대로 칸 안에서 옆으로 벌려 선다.
        const slots = Math.max(1, devs.length);
        pos = { x: q.x + (q.w * (i + 1)) / (slots + 1), y: L.queue.y + L.queue.h - 8 + (i % 2) * 14 };
        cls += " walk"; carry = fetchLayer;
      } else if (checking || finished) {
        pos = gather(k, everyone);
      } else if (a?.state === "writing" || a?.state === "parts") {
        cls += paused ? "" : " type";
        bubble = { text: a.state === "parts" && a.part ? `이어 쓰는 중 · ${a.part}조각` : `${base(a.file)} 작성 중` };
      } else if (a?.state === "fixing") {
        cls += paused ? "" : " type"; bubble = { text: "자동 검사 → 고치는 중", tone: "warn" };
      } else if (a?.state === "waiting") {
        cls += " wait"; bubble = { text: "⏳ 잠시 대기", tone: "warn" };
      } else if (view.layer === 0 && i > 0 && !finished && !checking && view.files.length) {
        cls += " wait"; bubble = { text: "공통 기반 먼저 — 대기" };
      }
    }
    if (finished && !paused) cls += " cheer";
    if (paused) cls += " wait";
    return (
      <div key={m.id} className={cls} style={{ left: pos.x, top: pos.y }} data-agent={m.id}>
        {bubble && <div className={`bb ${bubble.tone ?? ""}`}>{bubble.text}</div>}
        <div className="bw">
          {carry !== null && <span className="cr" style={{ background: LAYER_COLOR[carry] }} />}
          <span dangerouslySetInnerHTML={{ __html: characterImg(40) }} />
        </div>
        <span className="bd">{m.role === "planner" ? "설계" : m.role === "review" ? "검토" : `개발 ${devs.findIndex(d => d.id === m.id) + 1}`}</span>
      </div>
    );
  };

  return (
    <div className="rc-vl" data-testid="team-village" role="img" aria-label={`팀 작업 — 파일 ${view.done}/${view.total || "?"}`}>
      <style>{css}</style>
      <div className="zn" style={{ left: L.plan.x, top: L.plan.y, width: L.plan.w, height: L.plan.h }}><span>설계실</span></div>
      <div className="zn" style={{ left: L.review.x, top: L.review.y, width: L.review.w, height: L.review.h }}><span>검토 · 마지막 전체 점검</span></div>
      <div className="zn" style={{ left: L.queue.x, top: L.queue.y, width: L.queue.w, height: L.queue.h, borderStyle: "solid" }}><span>작업 대기열</span></div>
      {L.qcols.map((q, l) => {
        const per = Math.max(1, Math.floor((q.w - 6) / 30));
        const rows = 3;
        const shown = Math.min(perLayer[l].waiting, per * rows);
        return (
          <React.Fragment key={l}>
            <div className={`qc${unlocked(l) ? "" : " locked"}`} style={{ left: q.x, top: q.y, width: q.w, height: q.h }}><span>{LAYER[l]}</span></div>
            {Array.from({ length: shown }, (_, k) => (
              <div key={k} className="cd" style={{ left: q.x + 4 + (k % per) * 30, top: q.y + q.h - 32 - Math.floor(k / per) * 20, background: LAYER_COLOR[l] }} />
            ))}
            {perLayer[l].waiting > shown && <div className="more" style={{ left: q.x + q.w - 26, top: q.y + 2 }}>+{perLayer[l].waiting - shown}</div>}
          </React.Fragment>
        );
      })}
      {devs.map((m, i) => {
        const d = L.desks[i];
        const a = view.agents[m.id];
        const busy = a && (a.state === "writing" || a.state === "parts");
        return (
          <div key={m.id} className={`dk${busy ? " busy" : ""}${a?.state === "fixing" ? " fix" : ""}`} style={{ left: d.x, top: d.y, width: d.w, height: d.h }}>
            <div className="mon">{a?.file ? <>{a.file}<br /><i>{a.state === "parts" && a.part ? `${a.part}조각째${a.lines ? ` · ${a.lines}줄` : ""}` : a.state === "fixing" ? "자동 검사 결과로 고치는 중" : "작성 중…"}</i></> : <i>{a?.done ? "다음 작업 대기" : "대기 중"}</i>}</div>
            <div className="bar"><i /></div>
            <div className="lb"><span>개발 {i + 1} 작업대</span>{a?.done ? <b>완료 {a.done}</b> : null}</div>
          </div>
        );
      })}
      {L.segs.map((g, l) => {
        const n = perLayer[l].done;
        const per = Math.max(1, Math.floor((g.w - 14) / 20));
        const shown = Math.min(n, per * 4);
        return (
          <div key={l} className="sg" style={{ left: g.x, top: g.y, width: g.w, height: g.h }}>
            <span>완성 · {LAYER[l]}</span>{n ? <b>{n}/{perLayer[l].total}</b> : null}
            {Array.from({ length: shown }, (_, k) => (
              <i key={k} className="st" style={{ left: 8 + (k % per) * 20, top: 20 + Math.floor(k / per) * 9, background: LAYER_COLOR[l] }} />
            ))}
          </div>
        );
      })}
      {roster.map((m, k) => agentNode(m, k))}
      {flies.map(f => <FlyingCard key={f.id} fly={f} onDone={dropFly} />)}
    </div>
  );
}
