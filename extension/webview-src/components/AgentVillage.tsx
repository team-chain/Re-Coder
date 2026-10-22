/**
 * AI Village (2.0.45) — 팀 생성 진행을 리코더 카드와 오른쪽 패널로 보여 준다.
 *
 *  - 단계: 공통 기반 → 기능 → 화면 → 전체 점검 → 테스트. 앞 단계가 끝나야 다음이 열린다(엔진의 순서 그대로).
 *  - 카드: 리코더마다 지금 파일·상태·일한 시간·AI 호출·비용. 누르면 오른쪽 패널에 코드·검사·기록.
 *  - 다시 보기: 끝난 뒤(또는 진행 중에도) 막대를 끌면 그때까지의 이벤트로 다시 접은 화면을 보여 준다.
 *
 * 관통 원칙(2.0.24 마을과 같다): 보이는 모든 값은 코어의 실제 진행 이벤트(teamState)에서만 나온다.
 * 지어낸 대화·가짜 진행률은 넣지 않는다. 비용은 단가를 아는 호출만 더하고, 모르면 "단가 미설정"으로 따로 센다.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { CHARACTER_SRC } from "./recoderCharacter";
import { prettyModel, formatCost } from "./teamConfig";
import { AgentUsage, TeamMember, TeamRecord, TeamView, agentName, agentSeconds, formatElapsed, totalUsage, viewAt } from "./teamState";

const LAYER = ["공통 기반", "기능", "화면"];

const css = `
.rc-av{display:grid;grid-template-columns:minmax(0,1fr);border-top:1px solid var(--vscode-panel-border,#333)}
.rc-av.split{grid-template-columns:minmax(0,1fr) 320px}
.rc-av-main{padding:12px;display:flex;flex-direction:column;gap:12px;min-width:0}
.rc-av-stages{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:12px}
.rc-av-stage{display:inline-flex;align-items:center;gap:6px;height:26px;padding:0 11px;border-radius:99px;border:1px solid var(--vscode-widget-border,#3c3c3c);color:var(--vscode-descriptionForeground,#999)}
.rc-av-stage.done{color:var(--vscode-charts-green,#3fb950);border-color:rgba(63,185,80,.4)}
.rc-av-stage.now{color:var(--vscode-foreground,#e6e6e6);border-color:var(--vscode-focusBorder,#3794ff);background:rgba(55,148,255,.12)}
.rc-av-arrow{color:var(--vscode-disabledForeground,#6f6f6f)}
.rc-av-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.rc-av-card{all:unset;box-sizing:border-box;display:flex;flex-direction:column;gap:9px;padding:12px;border-radius:9px;cursor:pointer;
  border:1px solid var(--vscode-widget-border,#3c3c3c);background:var(--vscode-editor-background,#1e1e1e);min-width:0}
.rc-av-card:hover{border-color:var(--vscode-focusBorder,#3794ff)}
.rc-av-card:focus-visible{outline:2px solid var(--vscode-focusBorder,#3794ff);outline-offset:2px}
.rc-av-card.sel{border-color:var(--vscode-focusBorder,#3794ff);box-shadow:0 0 0 3px rgba(55,148,255,.16)}
.rc-av-top{display:flex;align-items:center;gap:10px;min-width:0}
.rc-av-top img{width:34px;height:34px;flex:none}
.rc-av-name{font-weight:600;font-size:13.5px;line-height:1.25}
.rc-av-role{font-size:11.5px;color:var(--vscode-descriptionForeground,#999)}
.rc-av-st{margin-left:auto;display:inline-flex;align-items:center;gap:5px;font-size:11.5px;white-space:nowrap;color:var(--vscode-descriptionForeground,#999)}
.rc-av-st::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.rc-av-st.w{color:var(--vscode-focusBorder,#3794ff)}.rc-av-st.f{color:var(--vscode-editorWarning-foreground,#cca700)}
.rc-av-st.d{color:var(--vscode-charts-green,#3fb950)}.rc-av-st.x{color:var(--vscode-errorForeground,#f48771)}
.rc-av-file{font-family:var(--vscode-editor-font-family,monospace);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--vscode-foreground,#ddd)}
.rc-av-bar{height:3px;border-radius:2px;background:rgba(128,128,128,.25);overflow:hidden}
.rc-av-bar i{display:block;height:100%;background:var(--vscode-focusBorder,#3794ff)}
.rc-av-bar.run i{width:35%;animation:rcAvRun 1.3s ease-in-out infinite}
.rc-av-meta{display:flex;justify-content:space-between;gap:8px;font-size:11.5px;color:var(--vscode-descriptionForeground,#999);font-variant-numeric:tabular-nums}
@keyframes rcAvRun{from{transform:translateX(-100%)}to{transform:translateX(290%)}}
@media (prefers-reduced-motion: reduce){.rc-av-bar.run i{animation:none;width:100%;opacity:.5}}
.rc-av-board{border:1px solid var(--vscode-widget-border,#3c3c3c);border-radius:9px;padding:10px 12px}
.rc-av-board h4{margin:0 0 8px;font-size:12px;font-weight:600;color:var(--vscode-descriptionForeground,#aaa);display:flex;justify-content:space-between;gap:8px}
.rc-av-routes{display:flex;flex-wrap:wrap;gap:6px}
.rc-av-routes span{font-family:var(--vscode-editor-font-family,monospace);font-size:11.5px;border:1px solid var(--vscode-widget-border,#3c3c3c);border-radius:5px;padding:2px 7px}
.rc-av-routes em{font-style:normal;color:var(--vscode-focusBorder,#3794ff);margin-right:6px}
.rc-av-side{border-left:1px solid var(--vscode-panel-border,#333);display:flex;flex-direction:column;min-width:0}
.rc-av.stack .rc-av-side{border-left:0;border-top:1px solid var(--vscode-panel-border,#333)}
.rc-av-ih{display:flex;align-items:center;gap:10px;padding:14px 14px 10px}
.rc-av-ih img{width:36px;height:36px}
.rc-av-tabs{display:flex;gap:16px;padding:0 14px;border-bottom:1px solid var(--vscode-panel-border,#333);font-size:12.5px}
.rc-av-tabs button{all:unset;cursor:pointer;padding:8px 0;color:var(--vscode-descriptionForeground,#999)}
.rc-av-tabs button[aria-selected=true]{color:var(--vscode-foreground,#e6e6e6);box-shadow:0 -2px 0 var(--vscode-focusBorder,#3794ff) inset}
.rc-av-tabs button:focus-visible{outline:2px solid var(--vscode-focusBorder,#3794ff)}
.rc-av-tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding:12px 14px 4px}
.rc-av-tile{border:1px solid var(--vscode-widget-border,#3c3c3c);border-radius:7px;padding:7px 9px}
.rc-av-tile div{font-size:11px;color:var(--vscode-descriptionForeground,#999)}
.rc-av-tile b{font-size:14.5px;font-weight:600;font-variant-numeric:tabular-nums}
.rc-av-body{padding:10px 14px 14px;font-size:12.5px;display:flex;flex-direction:column;gap:8px;min-height:0}
.rc-av-code{margin:0;border:1px solid var(--vscode-widget-border,#3c3c3c);border-radius:7px;background:var(--vscode-textCodeBlock-background,rgba(0,0,0,.25));
  padding:9px 11px;font:11.5px/1.6 var(--vscode-editor-font-family,monospace);white-space:pre;overflow:auto;max-height:260px}
.rc-av-chips{display:flex;flex-wrap:wrap;gap:6px}
.rc-av-chips button{all:unset;cursor:pointer;font-family:var(--vscode-editor-font-family,monospace);font-size:11.5px;padding:2px 8px;border-radius:5px;border:1px solid var(--vscode-widget-border,#3c3c3c)}
.rc-av-chips button[aria-pressed=true]{border-color:var(--vscode-focusBorder,#3794ff);background:rgba(55,148,255,.12)}
.rc-av-list{display:flex;flex-direction:column;gap:6px;margin:0;padding:0;list-style:none}
.rc-av-list li{display:flex;gap:8px;align-items:flex-start;line-height:1.45}
.rc-av-dot{width:7px;height:7px;border-radius:50%;margin-top:6px;flex:none;background:var(--vscode-descriptionForeground,#999)}
.rc-av-dot.ok{background:var(--vscode-charts-green,#3fb950)}.rc-av-dot.warn{background:var(--vscode-editorWarning-foreground,#cca700)}.rc-av-dot.bad{background:var(--vscode-errorForeground,#f48771)}
.rc-av-scrub{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;padding:10px 12px;border-top:1px solid var(--vscode-panel-border,#333);font-size:12px;color:var(--vscode-descriptionForeground,#999);font-variant-numeric:tabular-nums}
.rc-av-scrub input{flex:1 1 140px;min-width:120px;accent-color:var(--vscode-focusBorder,#3794ff)}
.rc-av-scrub > span{white-space:nowrap}
.rc-av-scrub button{all:unset;cursor:pointer;display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 10px;white-space:nowrap;border-radius:6px;border:1px solid var(--vscode-widget-border,#3c3c3c);color:var(--vscode-foreground,#ddd)}
.rc-av-scrub button:focus-visible{outline:2px solid var(--vscode-focusBorder,#3794ff)}
.rc-av-empty{color:var(--vscode-descriptionForeground,#999)}
`;

type Tone = "w" | "f" | "d" | "x" | "";
interface Card { id: string; name: string; role: string; state: string; tone: Tone; file?: string; busy: boolean; meta: [string, string]; }

const STATE_TEXT: Record<string, [string, Tone]> = {
  idle: ["대기", ""], writing: ["작성 중", "w"], parts: ["이어 쓰는 중", "w"], fixing: ["고치는 중", "f"], waiting: ["잠시 대기", "f"], done: ["완료", "d"],
};

/** 화면 이름 — 개발은 번호로(개발 1, 개발 2 …). */
export function displayName(roster: TeamMember[], m: TeamMember): string {
  if (m.role !== "dev") return agentName(m.id).who;
  const n = roster.filter((x) => x.role === "dev").findIndex((x) => x.id === m.id) + 1;
  return `개발 ${n || ""}`.trim();
}

function usageText(u: AgentUsage | undefined): string {
  if (!u || !u.calls) return "—";
  return u.costUsd > 0 ? `${u.calls}회 · ${formatCost(u.costUsd)}` : `${u.calls}회`;
}

/** 리코더 한 명의 카드 내용(순수). */
export function cardOf(roster: TeamMember[], m: TeamMember, view: TeamView, paused: boolean): Card {
  const name = displayName(roster, m);
  const u = view.usage?.[m.id];
  const live = !paused && view.phase !== "done";
  if (m.role === "planner") {
    const planning = view.phase === "planning";
    return { id: m.id, name, role: "설계", state: planning ? "설계 중" : "완료", tone: planning ? "w" : "d", busy: planning && live,
      meta: [planning ? "작업 목록 만드는 중" : `작업 ${view.total}개 배정`, usageText(u)] };
  }
  if (m.role === "review") {
    const checking = view.phase === "checking" && !view.test;
    const g = view.gates ?? { passed: 0, fixed: 0, returned: 0 };
    return { id: m.id, name, role: "문법 · 비밀값", state: checking ? "전체 점검" : view.phase === "done" ? "완료" : g.passed ? "관문 확인" : "대기",
      tone: checking || (g.passed && view.phase !== "done") ? "w" : view.phase === "done" ? "d" : "", busy: checking && live,
      meta: [`통과 ${g.passed} · 수정 ${g.fixed + view.fixes}${view.issues ? ` · 확인 필요 ${view.issues}` : ""}${view.securityBlocks ? ` · 차단 ${view.securityBlocks}` : ""}`, usageText(u)] };
  }
  if (m.role === "test") {
    const t = view.test;
    if (!t) return { id: m.id, name, role: "완성 기준 시험", state: "대기", tone: "", busy: false, meta: ["점검 뒤 시작", usageText(u)] };
    const done = t.phase === "result";
    const tone: Tone = done ? (t.failed ? "x" : "d") : "w";
    const state = t.phase === "writing" ? "시험 작성 중" : t.phase === "running" || t.phase === "written" ? "실행 중" : t.failed ? `실패 ${t.failed}` : "통과";
    return { id: m.id, name, role: "완성 기준 시험", state, tone, busy: !done && live,
      meta: [done ? `${t.passed}/${t.total} 통과${t.unverifiable ? ` · 확인 불가 ${t.unverifiable}` : ""}` : t.checks ? `시험 ${t.checks}개` : "시험 만드는 중", usageText(u)] };
  }
  const a = view.agents[m.id];
  const [text, tone] = STATE_TEXT[a?.state ?? "idle"] ?? ["대기", ""];
  const file = a?.file;
  const layer = file ? view.files.find((f) => f.file === file)?.layer : undefined;
  const sec = agentSeconds(a);
  return { id: m.id, name, role: layer !== undefined ? LAYER[layer] ?? "개발" : "개발", state: a?.done && !file ? `완료 ${a.done}` : text,
    tone: a?.done && !file ? "d" : tone, file, busy: !!file && live,
    meta: [a?.state === "parts" && a.part ? `조각 ${a.part}` : a?.done ? `파일 ${a.done}` : "다음 작업 대기", `${sec ? formatElapsed(sec) : "—"} · ${usageText(u)}`] };
}

/** 단계 칩(순수) — 각 칸이 끝났는지·지금인지. */
export function stagesOf(view: TeamView): Array<{ label: string; state: "done" | "now" | "" }> {
  const layers = [0, 1, 2].map((l) => view.files.filter((f) => f.layer === l));
  const doneLayer = (l: number) => layers[l].length > 0 && layers[l].every((f) => f.state === "done" || f.state === "issue");
  const out: Array<{ label: string; state: "done" | "now" | "" }> = [];
  [0, 1, 2].forEach((l) => {
    if (!layers[l].length) return;
    const n = layers[l].filter((f) => f.state === "done" || f.state === "issue").length;
    const failed = layers[l].filter((f) => f.state === "failed").length;
    out.push({ label: `${LAYER[l]} ${n}/${layers[l].length}${failed ? ` · 못 만듦 ${failed}` : ""}`, state: doneLayer(l) ? "done" : view.phase === "building" && view.layer !== null && view.layer >= l ? "now" : "" });
  });
  const checkDone = view.phase === "done" || !!view.test;
  out.push({ label: "전체 점검", state: checkDone ? "done" : view.phase === "checking" ? "now" : "" });
  if (view.test) {
    out.push({ label: view.test.phase === "result" ? `테스트 ${view.test.passed}/${view.test.total}` : "테스트",
      state: view.test.phase === "result" ? (view.test.failed ? "now" : "done") : "now" });
  }
  return out;
}

function recordsOf(view: TeamView, roster: TeamMember[], m: TeamMember): TeamRecord[] {
  const name = displayName(roster, m);
  const who = agentName(m.id).who;
  return (view.records ?? []).filter((r) => r.who === name || r.who === who).slice(-12);
}

export interface AgentVillageProps {
  roster: TeamMember[];
  view: TeamView;
  paused?: boolean;
  /** 결과가 오면 완성 파일 전체 내용 — 코드 탭이 미리보기 대신 보여 준다. */
  files?: Record<string, string>;
  /** 폭(px). 0 이면(서버 렌더) 한 줄로 쌓는다. */
  width?: number;
}

export const AgentVillage: React.FC<AgentVillageProps> = ({ roster, view, paused = false, files, width = 0 }) => {
  const [replay, setReplay] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const shown = useMemo(() => (replay === null ? view : viewAt(view, replay)), [view, replay]);
  const members = roster;
  const cards = members.map((m) => ({ m, c: cardOf(members, m, shown, paused) }));
  const firstBusy = cards.find(({ c }) => c.busy)?.m.id ?? members.find((m) => m.role === "dev")?.id ?? members[0]?.id;
  const [selected, setSelected] = useState<string | null>(null);
  const sel = members.find((m) => m.id === (selected ?? firstBusy)) ?? members[0];
  const [tab, setTab] = useState<"code" | "checks" | "log">("code");
  const [pickedFile, setPickedFile] = useState<string | null>(null);
  const split = width >= 760;
  const total = Math.max(1, Math.round(view.elapsed));

  //: 다시 보기 재생 — 8배속으로 끝까지. 손으로 막대를 움직이면 멈춘다.
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    if (!playing) return undefined;
    timer.current = setInterval(() => {
      setReplay((t) => {
        const next = (t ?? 0) + 2;
        if (next >= total) { setPlaying(false); return null; }
        return next;
      });
    }, 250);
    return () => { if (timer.current) clearInterval(timer.current); };
  }, [playing, total]);

  if (!sel) return null;
  const selCard = cards.find(({ m }) => m.id === sel.id)!.c;
  const a = shown.agents[sel.id];
  const u = shown.usage?.[sel.id];
  const myFiles = sel.role === "dev" ? [...(a?.files ?? []), ...(a?.file && !(a.files ?? []).includes(a.file) ? [a.file] : [])]
    : sel.role === "planner" ? shown.files.filter((f) => !f.by && (f.state === "done")).map((f) => f.file).slice(0, 8) : [];
  const fileToShow = pickedFile && myFiles.includes(pickedFile) ? pickedFile : (a?.file ?? myFiles[myFiles.length - 1]);
  const code = fileToShow ? (files?.[fileToShow] ?? shown.previews?.[fileToShow] ?? "") : "";
  const writingNow = !!a?.file && fileToShow === a.file && !files?.[a.file];
  const seconds = sel.role === "dev" || sel.role === "test" ? agentSeconds(shown.agents[sel.id]) : 0;
  const testCriteria = shown.test?.criteria ?? [];
  const sum = totalUsage(shown);

  return (
    <div className={`rc-av${split ? " split" : " stack"}`} data-testid="agent-village">
      <style>{css}</style>
      <div className="rc-av-main">
        <div className="rc-av-stages" aria-label="진행 단계">
          {stagesOf(shown).map((s, i, all) => (
            <React.Fragment key={s.label}>
              <span className={`rc-av-stage ${s.state}`}>{s.state === "done" ? "✓ " : ""}{s.label}</span>
              {i < all.length - 1 && <span className="rc-av-arrow" aria-hidden="true">›</span>}
            </React.Fragment>
          ))}
        </div>
        <div className="rc-av-grid" role="listbox" aria-label="리코더">
          {cards.map(({ m, c }) => (
            <button key={m.id} type="button" role="option" aria-selected={m.id === sel.id} data-agent={m.id}
              className={`rc-av-card${m.id === sel.id ? " sel" : ""}`} onClick={() => { setSelected(m.id); setPickedFile(null); }}>
              <span className="rc-av-top">
                <img src={CHARACTER_SRC} alt="" />
                <span style={{ minWidth: 0 }}><div className="rc-av-name">{c.name}</div><div className="rc-av-role">{c.role}</div></span>
                <span className={`rc-av-st ${c.tone}`}>{c.state}</span>
              </span>
              {c.file && <span className="rc-av-file" title={c.file}>{c.file}</span>}
              {(c.busy || c.file) && <span className={`rc-av-bar${c.busy ? " run" : ""}`}><i style={c.busy ? undefined : { width: "100%" }} /></span>}
              <span className="rc-av-meta"><span>{c.meta[0]}</span><span>{c.meta[1]}</span></span>
            </button>
          ))}
        </div>
        {(shown.board ?? []).length > 0 && (
          <div className="rc-av-board" data-testid="village-board">
            <h4><span>공용 게시판</span><span style={{ fontWeight: 400 }}>서버 경로 — 화면 담당이 읽고 시작</span></h4>
            <div className="rc-av-routes">
              {(shown.board ?? []).slice(-12).map((b) => <span key={`${b.method} ${b.path}`}><em>{b.method}</em>{b.path}</span>)}
            </div>
          </div>
        )}
      </div>

      <aside className="rc-av-side" aria-label={`${selCard.name} 자세히`} data-testid="village-inspector">
        <div className="rc-av-ih">
          <img src={CHARACTER_SRC} alt="" />
          <div style={{ minWidth: 0 }}>
            <b style={{ fontSize: 14 }}>{selCard.name}</b>
            <div className="rc-av-role">{selCard.role}{fileToShow && sel.role === "dev" ? ` · ${fileToShow}` : ""}</div>
          </div>
        </div>
        <div className="rc-av-tabs" role="tablist">
          {(["code", "checks", "log"] as const).map((t) => (
            <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>{t === "code" ? "코드" : t === "checks" ? "검사" : "기록"}</button>
          ))}
        </div>
        <div className="rc-av-tiles">
          <div className="rc-av-tile"><div>일한 시간</div><b>{seconds ? formatElapsed(seconds) : "—"}</b></div>
          <div className="rc-av-tile"><div>AI 호출</div><b>{u?.calls ?? 0}</b></div>
          <div className="rc-av-tile"><div>비용</div><b>{u && u.costUsd > 0 ? formatCost(u.costUsd) : u && u.unknownCost ? "단가 미설정" : "—"}</b></div>
        </div>
        <div className="rc-av-body" role="tabpanel">
          {u && u.models.length > 0 && <div className="rc-av-empty">모델 · {u.models.map(prettyModel).join(", ")}</div>}
          {tab === "code" && (
            myFiles.length || code ? <>
              {myFiles.length > 1 && (
                <div className="rc-av-chips">
                  {myFiles.slice(-8).map((f) => <button key={f} type="button" aria-pressed={f === fileToShow} onClick={() => setPickedFile(f)}>{f.split("/").pop()}</button>)}
                </div>
              )}
              {code ? <pre className="rc-av-code" data-testid="village-code">{code}</pre>
                : <div className="rc-av-empty">{writingNow ? `${fileToShow} 작성 중 — 파일이 완성되면 내용이 보입니다.` : "아직 완성한 파일이 없습니다."}</div>}
              {!files?.[fileToShow ?? ""] && code && <div className="rc-av-empty">앞 40줄 · 비밀값 모양은 가림</div>}
            </> : <div className="rc-av-empty">{sel.role === "test" ? "테스트는 코드를 쓰지 않고 실행 중인 앱에 요청을 보내 확인합니다." : sel.role === "review" ? "검토는 다른 리코더의 파일을 읽고 고칩니다." : "아직 완성한 파일이 없습니다."}</div>
          )}
          {tab === "checks" && (
            sel.role === "test" ? (testCriteria.length ? (
              <ul className="rc-av-list">
                {testCriteria.map((c) => (
                  <li key={c.id}><span className={`rc-av-dot ${c.verdict === "passed" ? "ok" : c.verdict === "failed" ? "bad" : ""}`} />
                    <span><b>{c.id}</b> {c.verdict === "passed" ? "통과" : c.verdict === "failed" ? "실패" : "확인 불가"}</span></li>
                ))}
              </ul>) : <div className="rc-av-empty">{shown.test ? "시험을 실행하는 중입니다." : "전체 점검이 끝나면 시험을 시작합니다."}</div>)
            : sel.role === "review" ? (
              <ul className="rc-av-list">
                <li><span className="rc-av-dot ok" /><span>관문 통과 {shown.gates?.passed ?? 0}</span></li>
                <li><span className="rc-av-dot warn" /><span>자동 수정 {(shown.gates?.fixed ?? 0) + shown.fixes}{shown.secretFixes ? ` (비밀값 ${shown.secretFixes})` : ""}</span></li>
                <li><span className="rc-av-dot" /><span>작성자에게 돌려보냄 {shown.gates?.returned ?? 0}</span></li>
                {shown.issues > 0 && <li><span className="rc-av-dot bad" /><span>확인 필요 {shown.issues}</span></li>}
                {(shown.securityBlocks ?? 0) > 0 && <li data-testid="security-blocks"><span className="rc-av-dot bad" /><span>수상한 패키지 차단 {shown.securityBlocks}</span></li>}
              </ul>
            ) : (
              (() => {
                const mine = shown.files.filter((f) => f.by === sel.id);
                return mine.length ? (
                  <ul className="rc-av-list">
                    {mine.map((f) => (
                      <li key={f.file}><span className={`rc-av-dot ${f.state === "done" ? "ok" : f.state === "fixing" || f.polish ? "warn" : f.state === "failed" || f.state === "issue" ? "bad" : ""}`} />
                        <span className="rc-av-file">{f.file}</span>
                        <span className="rc-av-empty" style={{ marginLeft: "auto", whiteSpace: "nowrap" }}>{f.state === "done" ? (f.polish ? "다듬는 중" : "완료") : f.state === "fixing" ? "고치는 중" : f.state === "failed" ? "실패" : f.state === "issue" ? "확인 필요" : "작성 중"}</span></li>
                    ))}
                  </ul>
                ) : <div className="rc-av-empty">맡은 파일이 아직 없습니다.</div>;
              })()
            )
          )}
          {tab === "log" && (() => {
            const recs = recordsOf(shown, members, sel);
            return recs.length ? (
              <ul className="rc-av-list" aria-live="polite">
                {recs.map((r, i) => <li key={i}><span className={`rc-av-dot ${r.tone === "ok" ? "ok" : r.tone === "warn" ? "warn" : ""}`} /><span>{r.text}</span></li>)}
              </ul>
            ) : <div className="rc-av-empty">기록이 아직 없습니다.</div>;
          })()}
        </div>
      </aside>

      {view.elapsed > 0 && (view.events ?? []).length > 1 && (
        <div className="rc-av-scrub" style={{ gridColumn: "1 / -1" }}>
          <button type="button" onClick={() => { if (playing) { setPlaying(false); return; } setReplay(replay === null || replay >= total ? 0 : replay); setPlaying(true); }}
            aria-label={playing ? "다시 보기 멈춤" : "처음부터 다시 보기"}>{playing ? "❚❚" : "▶"} 다시 보기</button>
          <input type="range" min={0} max={total} step={1} value={replay ?? total} aria-label="다시 볼 시점"
            onChange={(e) => { setPlaying(false); const t = Number(e.target.value); setReplay(t >= total ? null : t); }} />
          <span>{formatElapsed(replay ?? view.elapsed)} / {formatElapsed(view.elapsed)}</span>
          {replay !== null && <button type="button" onClick={() => { setPlaying(false); setReplay(null); }}>지금으로</button>}
          <span style={{ marginLeft: 4 }}>{sum.calls ? `AI ${sum.calls}회${sum.costUsd > 0 ? ` · ${formatCost(sum.costUsd)}` : ""}` : ""}</span>
        </div>
      )}
    </div>
  );
};

export default AgentVillage;
