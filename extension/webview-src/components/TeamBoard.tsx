/**
 * 팀 작업 상자 — 머리(상태·파일 수·시간·AI 호출·비용) + AI Village(카드·자세히·다시 보기) + 일시 정지.
 * 모든 숫자는 코어의 실제 진행 이벤트(teamState.reduceTeam)에서 나온다.
 *
 * 생성 중에는 펼쳐 두고, 끝난 뒤에는 접힌 한 줄(「팀 작업 다시 보기」)로 결과 위에 남는다.
 */
import React, { useEffect, useRef, useState } from "react";
import { AgentVillage } from "./AgentVillage";
import { formatCost } from "./teamConfig";
import { TeamMember, TeamRole, TeamView, formatElapsed, polishingCount, totalUsage } from "./teamState";
import { PauseInfo, PausePanel, ResumeMode } from "./pausePanel";

const ROLE_LABEL: Record<TeamRole, string> = { planner: "설계", dev: "개발", review: "검토", test: "테스트" };

/** 화면에 보이는 팀원 이름 — 모두 리코더라서 역할과 번호로 구분한다(개발 1, 개발 2 …). */
export function memberLabel(roster: TeamMember[], m: TeamMember): string {
  if (m.role !== "dev") return ROLE_LABEL[m.role];
  const n = roster.filter(x => x.role === "dev").findIndex(x => x.id === m.id) + 1;
  return `${ROLE_LABEL.dev} ${n || ""}`.trim();
}

/** 머리 상태 글자(순수). */
export function boardStatus(view: TeamView, paused: boolean): string {
  if (paused) return "일시 정지";
  if (view.phase === "planning") return "설계 중";
  if (view.test && view.test.phase !== "result") return "테스트 중";
  if (view.phase === "checking") return "전체 점검 중";
  if (view.phase === "done") return "완료";
  return "만드는 중";
}

/** 머리 오른쪽 숫자(순수) — 파일 · 고친 문제 · AI 호출 · 비용 · 시간. 없는 값은 빼고 쓴다. */
export function boardMetrics(view: TeamView): string[] {
  const out = [`파일 ${view.done}/${view.total || "?"}`];
  const polishing = polishingCount(view);
  if (polishing) out.push(`${polishing}개 다듬는 중`);
  if (view.fixes) out.push(`자동으로 고친 문제 ${view.fixes}`);
  const u = totalUsage(view);
  if (u.calls) out.push(`AI ${u.calls}회`);
  if (u.costUsd > 0) out.push(`${formatCost(u.costUsd)}${u.unknownCost ? "+" : ""}`);
  out.push(formatElapsed(view.elapsed));
  return out;
}

const css = `
.rc-team{border:1px solid var(--vscode-widget-border,var(--vscode-panel-border,#333));border-radius:10px;background:var(--vscode-editorWidget-background,#202020);margin:2px 0 10px;overflow:hidden}
.rc-team-h{display:flex;align-items:center;gap:10px;padding:10px 12px;font-size:13px;min-width:0}
.rc-team-h b{font-size:13.5px;white-space:nowrap}
@media (max-width: 560px){.rc-team-h{flex-wrap:wrap;row-gap:4px}.rc-team-h .sp{flex-basis:100%;margin-left:0;white-space:normal}.rc-team-tg{flex-wrap:wrap;row-gap:4px}}
.rc-team-h .st{display:inline-flex;align-items:center;gap:6px;color:var(--vscode-descriptionForeground,#aaa);white-space:nowrap}
.rc-team-h .st::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--vscode-focusBorder,#3794ff)}
.rc-team-h .st.done::before{background:var(--vscode-charts-green,#3fb950)}
.rc-team-h .st.pause::before{background:var(--vscode-editorWarning-foreground,#cca700)}
.rc-team-h .sp{margin-left:auto;color:var(--vscode-descriptionForeground,#999);font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-team-tg{all:unset;box-sizing:border-box;display:flex;align-items:center;gap:10px;width:100%;cursor:pointer}
.rc-team-tg:focus-visible{outline:2px solid var(--vscode-focusBorder,#3794ff);outline-offset:2px}
.rc-team-bar{height:3px;background:rgba(55,148,255,.15)}
.rc-team-bar i{display:block;height:100%;background:var(--vscode-progressBar-background,#3794ff);transition:width .3s}
.rc-team-last{padding:0 12px 9px;font-size:12px;color:var(--vscode-descriptionForeground,#999);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
`;

export const TeamBoard: React.FC<{
  roster: TeamMember[]; view: TeamView;
  paused?: PauseInfo | null;
  onResume?: (mode: ResumeMode) => void;
  /** 결과의 완성 파일(경로 → 내용) — 코드 탭이 미리보기 대신 전체를 보여 준다. */
  files?: Record<string, string>;
  /** 끝난 뒤 접어 두는 보기 — 머리를 누르면 펼친다. */
  collapsible?: boolean;
}> = ({ roster, view, paused, onResume, files, collapsible }) => {
  const [open, setOpen] = useState(!collapsible);
  const pct = view.total ? Math.round((view.done / view.total) * 100) : 0;
  const status = boardStatus(view, !!paused);
  const metrics = boardMetrics(view);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = boxRef.current;
    if (!el || typeof ResizeObserver === "undefined") { return; }
    const ro = new ResizeObserver(entries => setWidth(Math.round(entries[0]?.contentRect.width ?? 0)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const last = (view.records ?? []).slice(-1)[0];
  const head = (
    <>
      <b>{collapsible ? "팀 작업 다시 보기" : "팀 작업"}</b>
      <span className={`st${status === "완료" ? " done" : paused ? " pause" : ""}`}>{status}</span>
      <span className="sp" data-testid="team-metrics">{metrics.join(" · ")}</span>
    </>
  );
  return (
    <div className="rc-team" data-testid="team-board" ref={boxRef}>
      <style>{css}</style>
      <div className="rc-team-h">
        {collapsible
          ? <button type="button" className="rc-team-tg" aria-expanded={open} onClick={() => setOpen(!open)}>{head}<span aria-hidden="true">{open ? "▾" : "▸"}</span></button>
          : head}
      </div>
      {!collapsible && <div className="rc-team-bar"><i style={{ width: `${pct}%` }} /></div>}
      {!collapsible && last && (
        <div className="rc-team-last" aria-live="polite" data-testid="team-records">{last.who ? `${last.who} · ` : ""}{last.text}</div>
      )}
      {open && <AgentVillage roster={roster} view={view} paused={!!paused} files={files} width={width} />}
      {paused && <PausePanel paused={paused} onResume={onResume} />}
    </div>
  );
};
