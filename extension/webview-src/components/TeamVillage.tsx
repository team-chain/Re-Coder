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
 *  · 말풍선은 리코더마다 따로 띄운다(각자 움직이는 개체). 이웃끼리 높이를 엇갈리게 두고 폭을 작업대 간격에 맞춰 겹치지 않게 한다.
 *  · 같은 때 카드를 집으러 가는 리코더는 차례로 간다(대기열 앞에 동시에 몰려 겹치지 않게).
 *  · 완성 카드는 선반의 자기 칸으로 날아가 그 칸 크기로 줄어들며 내려앉고, 그때 선반 숫자가 +1 된다.
 *  · 마지막 전체 점검이 다듬는 완성 파일은 완료로 센 채 "다듬는 중" 만 표시한다(완료 수가 줄지 않는다).
 * 지어낸 연출(가짜 대화·가짜 진행률)은 넣지 않는다.
 */
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { characterImg } from "./recoderCharacter";
import { polishingCount } from "./teamState";
import type { TeamMember, TeamView } from "./teamState";

export const VILLAGE_HEIGHT = 392;
/** 작업대 위 말풍선 두 줄(엇갈림) 높이. */
const BUBBLE_TIER = 26;
/** 동시에 날아가는 완성 카드 수 상한 — 넘으면 오래된 것부터 바로 선반에 내려놓는다. */
export const MAX_FLIES = 4;
/** 같은 이유로 기다리는 리코더의 말 — 상태(공통 기반 대기)는 같고 말투만 다르다. 첫 문구가 기본. */
export const WAIT_TEXT = ["공통 기반 먼저 — 대기", "기반 끝나면 시작", "앞 단계 기다리는 중", "곧 내 차례", "기반 완성 대기", "준비하고 대기"];
const LAYER = ["공통 기반", "기능", "화면"];
const LAYER_COLOR = ["#4f8cff", "#2fbfa5", "#b08cff"];

export interface Rect { x: number; y: number; w: number; h: number }
export interface VillageLayout { W: number; H: number; plan: Rect; review: Rect; queue: Rect; qcols: Rect[]; desks: Rect[]; shelf: Rect; segs: Rect[];
  /** 개발 리코더 말풍선의 최대 폭 — 이웃과 높이를 엇갈리게 두므로 작업대 간격 두 칸까지 쓸 수 있다. */
  bubbleMax: number }

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
  const desks = Array.from({ length: n }, (_, i) => ({ x: sx + i * (dw + gap), y: 222, w: dw, h: 58 }));
  const shelf = { x: 10, y: H - 66, w: W - 20, h: 58 };
  const sw = (shelf.w - 12) / 3;
  const segs = [0, 1, 2].map(i => ({ x: shelf.x + i * (sw + 6), y: shelf.y, w: sw, h: shelf.h }));
  const pitch = dw + gap;
  const bubbleMax = Math.floor(Math.max(60, Math.min(190, n === 1 ? 190 : 2 * pitch - 10)));
  return { W, H, plan, review, queue, qcols, desks, shelf, segs, bubbleMax };
}

/** 말풍선 폭 어림(한글 10px 글꼴 기준). */
export function bubbleWidth(text: string, max: number): number {
  return Math.min(max, Math.ceil([...text].reduce((w, ch) => w + (/[\u3131-\uD7A3]/.test(ch) ? 10 : 6), 0) + 18));
}

/** 가운데 x 에 폭 w 인 말풍선이 마을(폭 W) 밖으로 나가지 않게 옮길 거리. */
export function bubbleShift(x: number, w: number, W: number, pad = 4): number {
  if (x - w / 2 < pad) return Math.round(pad - (x - w / 2));
  if (x + w / 2 > W - pad) return Math.round(W - pad - (x + w / 2));
  return 0;
}

/** 선반 칸 안의 k 번째 완성 표시 위치(칸 기준). 칸이 넘치면 null(숫자만 올라간다). */
export function shelfSlot(seg: Rect, k: number): { x: number; y: number } | null {
  const per = Math.max(1, Math.floor((seg.w - 14) / 20));
  if (k < 0 || k >= per * 4) return null;
  return { x: 8 + (k % per) * 20, y: 20 + Math.floor(k / per) * 9 };
}

const css = `
.rc-vl{position:relative;height:${VILLAGE_HEIGHT}px;border-radius:10px;overflow:hidden;border:1px solid var(--vscode-panel-border,#2a2e35);
  background:radial-gradient(ellipse at 50% 120%,rgba(55,148,255,.10),transparent 60%),linear-gradient(rgba(127,127,127,.06) 1px,transparent 1px) 0 0/24px 24px,linear-gradient(90deg,rgba(127,127,127,.06) 1px,transparent 1px) 0 0/24px 24px,var(--vscode-editor-background,#191b1f)}
.rc-vl .zn{position:absolute;border:1px dashed var(--vscode-panel-border,#3a404a);border-radius:10px}
.rc-vl .zn>span{position:absolute;left:8px;right:8px;top:4px;font-size:10px;color:var(--vscode-descriptionForeground,#8b95a3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-vl .qc{position:absolute;border-radius:7px;background:rgba(0,0,0,.16)}
.rc-vl .qc>span{position:absolute;left:0;right:0;bottom:2px;text-align:center;font-size:9.5px;color:var(--vscode-descriptionForeground,#8b95a3)}
.rc-vl .lk{position:absolute;z-index:4;transform:translate(-50%,-50%);white-space:nowrap;padding:2px 8px;border-radius:99px;font-size:9.5px;color:var(--vscode-descriptionForeground,#aab3bf);background:rgba(12,14,18,.88);border:1px solid var(--vscode-panel-border,#3a404a)}
.rc-vl .cd.dim{opacity:.35}
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
.rc-vl .dk .lb{position:absolute;left:0;right:0;top:calc(100% + 3px);display:flex;flex-direction:column;align-items:center;gap:2px;font-size:9.5px;line-height:1.2;color:var(--vscode-descriptionForeground,#8b95a3)}
.rc-vl .dk .lb>span{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rc-vl .dk .lb b{font-weight:600;font-size:9px;padding:0 6px;border-radius:99px;white-space:nowrap;color:var(--vscode-charts-green,#73d39b);border:1px solid rgba(115,211,155,.4);background:rgba(115,211,155,.08)}
.rc-vl .sg{position:absolute;border-radius:8px;border:1px solid var(--vscode-panel-border,#2f353e);background:rgba(127,127,127,.04)}
.rc-vl .sg>span{position:absolute;left:8px;top:3px;font-size:9.5px;color:var(--vscode-descriptionForeground,#8b95a3)}
.rc-vl .sg>b{position:absolute;right:8px;top:3px;font-size:9.5px;font-weight:500;color:var(--vscode-charts-green,#73d39b)}
.rc-vl .sg>em{position:absolute;right:8px;bottom:3px;font-size:9px;font-style:normal;color:var(--vscode-editorWarning-foreground,#cca700)}
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
.rc-vl .bb{position:absolute;bottom:58px;left:50%;transform:translateX(-50%);max-width:190px;overflow:hidden;text-overflow:ellipsis;background:#f3f5f8;color:#1b1f24;font-size:10px;font-weight:600;padding:2px 8px;border-radius:9px;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.35);animation:rcVlPop .28s cubic-bezier(.3,1.5,.5,1) both}
.rc-vl .bb.up{bottom:${58 + BUBBLE_TIER}px}
.rc-vl .bb::after{content:"";position:absolute;left:50%;bottom:-4px;margin-left:-4px;border:4px solid transparent;border-bottom:0;border-top-color:#f3f5f8}
.rc-vl .bb.warn{background:#fff3c4}.rc-vl .bb.warn::after{border-top-color:#fff3c4}
.rc-vl .bb.ok{background:#d9f7e5}.rc-vl .bb.ok::after{border-top-color:#d9f7e5}
.rc-vl .fly{position:absolute;border-radius:3px;z-index:6;box-shadow:0 2px 6px rgba(0,0,0,.35);transition:left .7s cubic-bezier(.3,.7,.3,1),top .7s cubic-bezier(.3,.7,.3,1),width .7s ease-in,height .7s ease-in,border-radius .7s,box-shadow .7s}
.rc-vl .fly.land{box-shadow:none;border-radius:2px}
@keyframes rcVlWalk{from{transform:translateY(0) rotate(-4deg)}to{transform:translateY(-4px) rotate(4deg)}}
@keyframes rcVlType{from{transform:translateY(0)}to{transform:translateY(-2px)}}
@keyframes rcVlCheer{from{transform:translateY(0)}to{transform:translateY(-8px)}}
@keyframes rcVlScan{from{left:-35%}to{left:100%}}
@keyframes rcVlPop{from{opacity:0;transform:translateX(-50%) translateY(4px) scale(.85)}to{opacity:1;transform:translateX(-50%) translateY(0) scale(1)}}
@media (prefers-reduced-motion: reduce){.rc-vl .ag,.rc-vl .fly{transition:none!important}.rc-vl .ag .bw,.rc-vl .dk .bar i,.rc-vl .bb{animation:none!important}}
`;

const base = (p?: string) => (p ?? "").split("/").pop() || "";
const center = (r: Rect) => ({ x: r.x + r.w / 2, y: r.y + r.h / 2 });

export interface Fly { id: number; layer: number; from: { x: number; y: number }; to: { x: number; y: number }; small: boolean;
  /** 같은 작업대에서 한꺼번에 끝난 카드(한 번에 두 파일)는 조금씩 늦게 출발한다 — 겹쳐 보이지 않게. */
  delay?: number }
/** 날아가는 시간 — 끝나면 카드를 지우고, 같은 순간 선반에 그 칸이 나타난다(숫자 +1). */
const FLY_MS = 720;

function FlyingCard({ fly, onDone }: { fly: Fly; onDone: (id: number) => void }) {
  const [landed, setLanded] = useState(false);
  useEffect(() => {
    //: 두 프레임 뒤에 목적지로 — 첫 프레임에 시작 위치가 그려져야 transition 이 걸린다.
    let raf2 = 0;
    const raf = requestAnimationFrame(() => { raf2 = requestAnimationFrame(() => setLanded(true)); });
    const t = setTimeout(() => onDone(fly.id), FLY_MS + (fly.delay ?? 0) + 40);
    return () => { cancelAnimationFrame(raf); cancelAnimationFrame(raf2); clearTimeout(t); };
  }, [fly, onDone]);
  const at = landed ? fly.to : fly.from;
  const size = landed && fly.small ? { width: 16, height: 6 } : { width: 26, height: 17 };
  return <div className={`fly${landed ? " land" : ""}`} style={{ left: at.x, top: at.y, ...size, background: LAYER_COLOR[fly.layer] ?? LAYER_COLOR[1], transitionDelay: fly.delay ? `${fly.delay}ms` : undefined }} />;
}

//: 화면이 그려지기 전에 날아갈 카드를 정한다 — 그래야 선반에 칸이 먼저 번쩍 나타났다 사라지지 않는다(서버 렌더에서는 useEffect).
const useBeforePaint = typeof window !== "undefined" ? useLayoutEffect : useEffect;

/** 카드 집으러 가는 차례 간격(ms) — 동시에 시작한 리코더가 대기열 앞에 한꺼번에 몰리지 않게. */
const FETCH_GAP = 420;
const FETCH_MS = 950;
/** 걷는 시간(.ag 의 left·top transition 과 같게). */
const WALK_MS = 900;

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
  //: 파일별 직전 상태 — 새로 완성된 파일마다 그 파일을 맡은 작업대에서 카드를 날린다(한 번에 두 파일을 맡아도 정확히).
  const prevFiles = useRef<Map<string, string> | null>(null);
  //: 카드를 집으러 간 리코더 → { 단계, 차례(0·1 번갈아 — 대기열 앞의 왼쪽·오른쪽 자리) }
  const [fetching, setFetching] = useState<Record<string, { layer: number; slot: number }>>({});
  const fetchQueue = useRef<number>(0);
  //: 카드를 들고 작업대로 돌아오는 중 — 걷는 동안에는 말풍선을 띄우지 않는다(지나가며 다른 말풍선과 겹치지 않게).
  const [returning, setReturning] = useState<Record<string, true>>({});
  const timers = useRef<number[]>([]);
  const [flies, setFlies] = useState<Fly[]>([]);
  const flyId = useRef(0);
  useEffect(() => () => { timers.current.forEach(t => clearTimeout(t)); timers.current = []; }, []);

  const perLayer = [0, 1, 2].map(l => {
    const files = view.files.filter(f => f.layer === l);
    return {
      total: files.length,
      waiting: files.filter(f => f.state === "waiting").length,
      done: files.filter(f => f.state === "done" || f.state === "issue").length,
      polish: polishingCount(view, l),
    };
  });
  //: 날아가는 중인 카드는 아직 선반에 없다 — 내려앉는 순간 칸·숫자가 함께 올라간다.
  const inFlight = [0, 1, 2].map(l => flies.filter(f => f.layer === l).length);
  const shelfDone = perLayer.map((p, l) => Math.max(0, p.done - inFlight[l]));

  useBeforePaint(() => {
    const now: Record<string, { file?: string; done: number }> = {};
    const starts: Array<{ id: string; layer: number }> = [];
    devs.forEach(m => {
      const a = view.agents[m.id];
      const cur = { file: a?.file, done: a?.done ?? 0 };
      const was = prev.current[m.id];
      if (was && cur.file && cur.file !== was.file) starts.push({ id: m.id, layer: layerOf.get(cur.file) ?? 1 });
      now[m.id] = cur;
    });
    prev.current = now;
    const finishedNow = (s: string) => s === "done" || s === "issue";
    const landed: Array<{ desk: number; layer: number }> = [];
    if (prevFiles.current) {
      for (const f of view.files) {
        const before = prevFiles.current.get(f.file);
        if (finishedNow(f.state) && before !== undefined && !finishedNow(before)) {
          landed.push({ desk: devs.findIndex(d => d.id === f.by), layer: f.layer });
        }
      }
    }
    prevFiles.current = new Map(view.files.map(f => [f.file, f.state]));
    //: 새 카드가 내려앉을 선반 칸 — 이미 날아가는 카드 다음 차례(먼저 끝난 카드가 먼저 내려앉는다).
    const next = [0, 1, 2].map(l => perLayer[l].done - flies.filter(f => f.layer === l).length - landed.filter(x => x.layer === l).length);
    const fromDesk: Record<number, number> = {};
    const newFlies: Fly[] = landed.map(({ desk, layer }) => {
      const order = (fromDesk[desk] = (fromDesk[desk] ?? -1) + 1);
      //: 맡은 작업대를 모르면(이어 만들기로 불러온 파일 등) 대기열 앞에서 날린다.
      const d = L.desks[desk] ?? { x: L.queue.x + L.queue.w / 2 - 13, y: L.queue.y + L.queue.h - 20, w: 26, h: 0 };
      const seg = L.segs[layer] ?? L.segs[1];
      const slot = shelfSlot(seg, Math.max(0, next[layer]++));
      return { id: ++flyId.current, layer, from: { x: d.x + d.w / 2 - 13, y: d.y + 8 },
        to: slot ? { x: seg.x + slot.x, y: seg.y + slot.y } : { x: seg.x + seg.w - 30, y: seg.y + 4 }, small: !!slot,
        delay: order * 180 };
    });
    //: 같은 때 시작한 리코더는 차례로 카드를 집으러 간다 — 대기열 앞에는 많아야 두 명(왼쪽·오른쪽 자리).
    starts.forEach((s, k) => {
      const slot = (fetchQueue.current++) % 2;
      const go = window.setTimeout(() => {
        setFetching(f => ({ ...f, [s.id]: { layer: s.layer, slot } }));
        const back = window.setTimeout(() => {
          setFetching(f => { const n = { ...f }; delete n[s.id]; return n; });
          setReturning(r => ({ ...r, [s.id]: true }));
          const home = window.setTimeout(() => setReturning(r => { const n = { ...r }; delete n[s.id]; return n; }), WALK_MS);
          timers.current.push(home);
        }, FETCH_MS);
        timers.current.push(back);
      }, k * FETCH_GAP);
      timers.current.push(go);
    });
    if (timers.current.length > 64) timers.current = timers.current.slice(-64);
    if (newFlies.length) setFlies(f => [...f, ...newFlies].slice(-MAX_FLIES));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view.agents, view.files, devs, layerOf, L]);
  const dropFly = React.useCallback((id: number) => setFlies(f => f.filter(x => x.id !== id)), []);
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
      const fetch = fetching[m.id];
      if (fetch !== undefined) {
        const q = L.qcols[fetch.layer] ?? L.qcols[1];
        //: 차례로 집으러 오므로 대기열 앞에는 많아야 두 명 — 칸 바로 아래의 왼쪽·오른쪽 자리에 선다.
        const half = Math.max(28, q.w / 2);
        pos = { x: q.x + q.w / 2 + (fetch.slot ? half / 2 : -half / 2), y: L.queue.y + L.queue.h - 8 };
        cls += " walk"; carry = fetch.layer;
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
        cls += " wait"; bubble = { text: WAIT_TEXT[(i - 1) % WAIT_TEXT.length] };
      }
    }
    if (m.role === "dev" && returning[m.id]) bubble = null;
    //: 개발 리코더 말풍선 — 이웃과 높이를 엇갈리게, 폭은 작업대 간격에 맞춰, 뜨는 순간도 조금씩 다르게.
    const devIndex = m.role === "dev" ? devs.findIndex(d => d.id === m.id) : -1;
    const bubbleStyle: React.CSSProperties | undefined = devIndex >= 0
      ? { maxWidth: L.bubbleMax, animationDelay: `${(devIndex % 3) * 110}ms` } : undefined;
    const bubbleUp = devIndex >= 0 && devs.length > 1 && devIndex % 2 === 1 && fetching[m.id] === undefined && !checking && !finished;
    //: 가장자리 리코더의 말풍선이 마을 밖으로 잘리지 않게 안쪽으로 민다(글자 수로 폭을 어림).
    const shift = bubble ? bubbleShift(pos.x, bubbleWidth(bubble.text, devIndex >= 0 ? L.bubbleMax : 190), L.W) : 0;
    const style = shift ? { ...(bubbleStyle ?? {}), marginLeft: shift } : bubbleStyle;
    if (finished && !paused) cls += " cheer";
    if (paused) cls += " wait";
    return (
      <div key={m.id} className={cls} style={{ left: pos.x, top: pos.y }} data-agent={m.id}>
        {bubble && <div className={`bb ${bubble.tone ?? ""}${bubbleUp ? " up" : ""}`} style={style} title={bubble.text}>{bubble.text}</div>}
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
              <div key={k} className={`cd${unlocked(l) ? "" : " dim"}`} style={{ left: q.x + 4 + (k % per) * 30, top: q.y + q.h - 32 - Math.floor(k / per) * 20, background: LAYER_COLOR[l] }} />
            ))}
            {!unlocked(l) && <span className="lk" style={{ left: q.x + q.w / 2, top: q.y + q.h / 2 - 4 }}>{q.w >= 120 ? "🔒 앞 단계 끝나면 열림" : "🔒 대기"}</span>}
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
        const n = shelfDone[l];
        const slots = Array.from({ length: n }, (_, k) => shelfSlot(g, k)).filter((s): s is { x: number; y: number } => !!s);
        return (
          <div key={l} className="sg" style={{ left: g.x, top: g.y, width: g.w, height: g.h }}>
            <span>완성 · {LAYER[l]}</span>{n ? <b>{n}/{perLayer[l].total}</b> : null}
            {perLayer[l].polish > 0 && <em data-testid="shelf-polish">{perLayer[l].polish}개 다듬는 중</em>}
            {slots.map((s, k) => (
              <i key={k} className="st" style={{ left: s.x, top: s.y, background: LAYER_COLOR[l] }} />
            ))}
          </div>
        );
      })}
      {roster.map((m, k) => agentNode(m, k))}
      {flies.map(f => <FlyingCard key={f.id} fly={f} onDone={dropFly} />)}
    </div>
  );
}
