/**
 * 팀 모드 UI — 구성(누구를 몇 명) · 진행(누가 무엇을) · 일시 정지/이어서 만들기.
 * 모든 숫자는 코어의 실제 진행 이벤트(teamState.reduceTeam)에서 나온다.
 */
import React from "react";
import { ANIMAL_NAMES, bodySvg, faceSvg } from "./teamAnimals";
import { MAX_DEV_AGENTS, TeamMember, TeamView, formatElapsed } from "./teamState";

const ROLE_LABEL: Record<TeamMember["role"], string> = { planner: "설계", dev: "개발", review: "검토" };
const ROLE_HINT: Record<TeamMember["role"], string> = {
  planner: "요청을 파일 사이의 약속과 작업 목록으로 나눕니다",
  dev: "맡은 파일을 만들고, 크면 이어서 씁니다",
  review: "파일마다 문법·하드코딩된 비밀값을 바로 검사하고 바뀔 부분만 고칩니다",
};
const LAYER = ["공통 기반", "기능", "화면"];

const css = `
.rc-team{border:1px solid var(--vscode-panel-border,#333);border-radius:8px;background:var(--vscode-editorWidget-background,#202020);margin:2px 0 8px;overflow:hidden}
.rc-team-h{display:flex;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid var(--vscode-panel-border,#333);font-size:12px}
.rc-team-h b{font-size:12.5px}
.rc-team-h .sp{margin-left:auto;color:var(--vscode-descriptionForeground,#999);font-size:11px;font-variant-numeric:tabular-nums}
.rc-team-bar{height:4px;background:rgba(55,148,255,.15)}
.rc-team-bar i{display:block;height:100%;background:var(--vscode-progressBar-background,#3794ff);transition:width .3s}
.rc-team-waves{display:flex;gap:4px;padding:7px 10px 2px;font-size:10.5px;color:var(--vscode-descriptionForeground,#999)}
.rc-team-waves span{padding:1px 7px;border-radius:99px;border:1px solid var(--vscode-panel-border,#3b3b3b)}
.rc-team-waves span.on{border-color:var(--vscode-focusBorder,#3794ff);color:var(--vscode-foreground,#ddd)}
.rc-team-waves span.ok{color:#6cc070;border-color:#6cc07055}
.rc-team-ag{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px;padding:8px 10px}
.rc-team-a{display:flex;align-items:center;gap:7px;border:1px solid var(--vscode-panel-border,#333);border-radius:7px;padding:5px 7px;min-width:0;background:var(--vscode-editor-background,#1e1e1e)}
.rc-team-a .av{flex:none;width:34px;height:34px;display:flex;align-items:flex-end;justify-content:center;filter:drop-shadow(0 0 .6px #fff8)}
.rc-team-a.busy .av{animation:rcTeamBob .55s ease-in-out infinite alternate}
.rc-team-a.wait .av{opacity:.65}
.rc-team-a .t{min-width:0;font-size:11px;line-height:1.35}
.rc-team-a .t b{font-size:11.5px}
.rc-team-a .t small{display:block;color:var(--vscode-descriptionForeground,#999);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:var(--vscode-editor-font-family,monospace);font-size:10.5px}
.rc-team-log{padding:4px 10px 8px;font-size:10.5px;color:var(--vscode-descriptionForeground,#999);line-height:1.6}
.rc-team-log div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-team-log div:last-child{color:var(--vscode-foreground,#ddd)}
.rc-team-pause{display:flex;align-items:center;gap:8px;padding:8px 10px;border-top:1px solid var(--vscode-panel-border,#333);font-size:11.5px;background:rgba(204,167,0,.08)}
.rc-team-comp{display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:6px 2px 0}
.rc-team-comp .m{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--vscode-panel-border,#3b3b3b);border-radius:99px;padding:1px 8px 1px 3px;font-size:11px;background:var(--vscode-editor-background,#1e1e1e)}
.rc-team-comp button{border:1px dashed var(--vscode-panel-border,#555);background:transparent;color:var(--vscode-foreground,#ddd);border-radius:99px;padding:2px 9px;font-size:11px;cursor:pointer}
.rc-team-comp button:disabled{opacity:.4;cursor:default}
@keyframes rcTeamBob{from{transform:translateY(0)}to{transform:translateY(-2.5px)}}
@media (prefers-reduced-motion: reduce){.rc-team-a.busy .av{animation:none}}
`;

function Svg({ html, className }: { html: string; className?: string }) {
  //: 고정된 자체 SVG 문자열만 넣는다(teamAnimals.ts) — 외부 입력 없음.
  return <span className={className} dangerouslySetInnerHTML={{ __html: html }} />;
}

/** 입력창 아래 — 팀 모드 켜기와 구성(개발 에이전트 추가·빼기). */
export const TeamComposer: React.FC<{
  enabled: boolean; roster: TeamMember[]; disabled?: boolean;
  onToggle: (on: boolean) => void; onAdd: () => void; onRemove: () => void;
}> = ({ enabled, roster, disabled, onToggle, onAdd, onRemove }) => {
  const devs = roster.filter(m => m.role === "dev").length;
  return (
    <div className="rc-team-comp" data-testid="team-composer">
      <style>{css}</style>
      <label style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11.5, cursor: disabled ? "default" : "pointer" }}
        title="큰 요청을 여러 에이전트가 나눠 동시에 만듭니다. 꺼 두어도 요청이 크면 자동으로 나눠 만듭니다.">
        <input type="checkbox" checked={enabled} disabled={disabled} onChange={e => onToggle(e.target.checked)} />
        <b>팀 모드</b>
      </label>
      {enabled && <>
        {roster.map(m => (
          <span key={m.id} className="m" title={`${ANIMAL_NAMES[m.animal]} · ${ROLE_LABEL[m.role]} — ${ROLE_HINT[m.role]}`}>
            <Svg html={faceSvg(m.animal, 18)} />{ROLE_LABEL[m.role]}
          </span>
        ))}
        <button type="button" onClick={onAdd} disabled={disabled || devs >= MAX_DEV_AGENTS} aria-label="개발 에이전트 추가">＋ 개발 에이전트</button>
        <button type="button" onClick={onRemove} disabled={disabled || devs <= 1} aria-label="개발 에이전트 빼기">−</button>
        <span style={{ fontSize: 10.5, color: "var(--vscode-descriptionForeground,#999)" }}>
          동시 개발 {devs}명{devs > 3 ? " · 학생용 AI 는 분당 호출 한도가 있어 3명 넘게는 효과가 작을 수 있어요" : ""}
        </span>
      </>}
    </div>
  );
};

const AGENT_STATE_TEXT: Record<string, string> = {
  idle: "대기", writing: "작성 중", parts: "이어 쓰는 중", fixing: "고치는 중", waiting: "잠시 대기", done: "완료",
};

/** 생성 중인 턴 — 누가 무엇을 하고 있는지, 전체 진행, 멈췄으면 이어서 만들기. */
export const TeamBoard: React.FC<{
  roster: TeamMember[]; view: TeamView;
  paused?: { message: string; done: number; total: number } | null;
  onResume?: () => void;
}> = ({ roster, view, paused, onResume }) => {
  const pct = view.total ? Math.round((view.done / view.total) * 100) : 0;
  const status = paused ? "일시 정지" : view.phase === "planning" ? "설계 중" : view.phase === "checking" ? "전체 점검 중" : view.phase === "done" ? "완료" : "만드는 중";
  const layersDone = [0, 1, 2].map(l => view.files.length > 0 && view.files.filter(f => f.layer === l).every(f => f.state === "done" || f.state === "issue"));
  const members = roster.length ? roster : [];
  return (
    <div className="rc-team" data-testid="team-board">
      <style>{css}</style>
      <div className="rc-team-h">
        <b>팀 작업</b><span>{status}</span>
        <span className="sp">파일 {view.done}/{view.total || "?"} · {formatElapsed(view.elapsed)}</span>
      </div>
      <div className="rc-team-bar"><i style={{ width: `${pct}%` }} /></div>
      {view.files.length > 0 && (
        <div className="rc-team-waves">
          {LAYER.map((name, l) => {
            const n = view.files.filter(f => f.layer === l).length;
            return n ? <span key={l} className={layersDone[l] ? "ok" : view.layer === l ? "on" : ""}>{layersDone[l] ? "✓ " : ""}{name} {n}</span> : null;
          })}
        </div>
      )}
      <div className="rc-team-ag">
        {members.map(m => {
          let state: string, detail: string;
          if (m.role === "planner") {
            state = view.phase === "planning" ? "작성 중" : "완료";
            detail = view.phase === "planning" ? "약속·작업 목록 정리" : `작업 ${view.total}개로 나눔`;
          } else if (m.role === "review") {
            state = view.phase === "checking" ? "전체 점검" : view.fixes ? "검사 중" : "대기";
            detail = view.fixes || view.issues
              ? `고친 문제 ${view.fixes}${view.secretFixes ? ` (비밀값 ${view.secretFixes})` : ""}${view.issues ? ` · 확인 필요 ${view.issues}` : ""}`
              : "파일마다 문법·비밀값 검사";
          } else {
            const a = view.agents[m.id];
            state = AGENT_STATE_TEXT[a?.state ?? "idle"];
            detail = a?.file ? `${a.file}${a.state === "parts" && a.part ? ` · ${a.part}조각` : ""}` : a?.done ? `${a.done}개 완료` : "다음 작업 대기";
          }
          const busy = !paused && view.phase !== "done" && /중|전체/.test(state);
          return (
            <div key={m.id} className={`rc-team-a${busy ? " busy" : ""}${state === "잠시 대기" ? " wait" : ""}`} title={ROLE_HINT[m.role]}>
              <Svg className="av" html={bodySvg(m.animal, 34)} />
              <div className="t"><b>{ANIMAL_NAMES[m.animal]}</b> · {ROLE_LABEL[m.role]} <span style={{ color: "var(--vscode-descriptionForeground,#999)" }}>{state}</span>
                <small>{detail}</small></div>
            </div>
          );
        })}
      </div>
      {view.log.length > 0 && <div className="rc-team-log" aria-live="polite">{view.log.slice(-3).map((l, i) => <div key={i}>{l}</div>)}</div>}
      {paused && (
        <div className="rc-team-pause" role="status">
          <span style={{ flex: 1 }}>⏸ {paused.message}</span>
          {onResume && <button type="button" onClick={onResume} style={{ border: "1px solid transparent", borderRadius: 4, padding: "5px 11px", background: "var(--vscode-button-background,#0e639c)", color: "var(--vscode-button-foreground,#fff)", fontSize: 11.5, fontWeight: 600, cursor: "pointer" }}>
            이어서 만들기{paused.total ? ` (${paused.done}/${paused.total})` : ""}
          </button>}
        </div>
      )}
    </div>
  );
};
