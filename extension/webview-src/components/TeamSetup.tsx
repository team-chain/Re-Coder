/**
 * 팀 구성 버튼과 창(2.0.45) — 입력창 도구줄의 「팀 6명 · 균형」 버튼을 누르면 열린다.
 *
 *  - 팀 모드 켜기/끄기 · 프리셋(품질 우선 / 균형 / 비용 절약 / 직접 설정)
 *  - 역할(설계·개발·검토·테스트)마다 쓸 모델. 키가 없는 공급자는 고를 수 없게 흐리게("AI 연결에서 추가").
 *  - 개발 인원(−/+) · 테스트 켜기/끄기
 *  - 예상 시간·비용은 **지난 실행 기록**에서만. 기록이 없으면 "첫 실행 뒤 표시".
 *
 * 순수 계산은 teamConfig.ts 에 있다(여기서는 그리기와 열고 닫기만).
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { CHARACTER_SRC } from "./recoderCharacter";
import { Switch } from "./Check";
import {
  MAX_DEVS, MIN_DEVS, ModelOption, PRESET_TEXT, Preset, ROLE_KEYS, ROLE_TEXT, RunStat, SIZE_TEXT, TeamConfig, TeamRoleKey,
  applyPreset, defaultConfig, estimate, formatCost, recommend, setDevs, setModel,
} from "./teamConfig";

const css = `
.rc-ts-btn{display:inline-flex;align-items:center;gap:8px;height:36px;max-width:280px;padding:0 12px;border-radius:8px;
  border:1px solid var(--vscode-widget-border,var(--vscode-panel-border,#3f3f3f));background:transparent;color:var(--vscode-foreground,#ccc);
  font:inherit;font-size:14px;cursor:pointer;white-space:nowrap}
.rc-ts-btn:hover:not(:disabled){background:var(--vscode-toolbar-hoverBackground,rgba(90,93,94,.31))}
.rc-ts-btn:focus-visible{outline:2px solid var(--vscode-focusBorder,#3794ff);outline-offset:1px}
.rc-ts-btn.on{border-color:var(--vscode-focusBorder,#3794ff);background:rgba(55,148,255,.12)}
.rc-ts-btn:disabled{opacity:.55;cursor:default}
.rc-ts-btn > span.t{overflow:hidden;text-overflow:ellipsis}
.rc-ts-avs{display:inline-flex}
.rc-ts-avs img{width:22px;height:22px;border-radius:50%;background:var(--vscode-badge-background,#2d3340);border:2px solid var(--vscode-input-background,#252526);margin-left:-8px;object-fit:contain}
.rc-ts-avs img:first-child{margin-left:0}
.rc-ts-pop{position:fixed;z-index:1200;width:500px;max-width:calc(100vw - 24px);max-height:calc(100vh - 24px);overflow:auto;border-radius:12px;
  border:1px solid var(--vscode-widget-border,#454545);background:var(--vscode-editorWidget-background,#252526);color:var(--vscode-foreground,#ddd);
  box-shadow:0 18px 48px rgba(0,0,0,.5);font-size:13px}
.rc-ts-pop:focus{outline:none}
.rc-ts-hd{display:flex;align-items:center;gap:10px;padding:14px 16px 10px}
.rc-ts-hd b{font-size:15px}
.rc-ts-hd .rec{margin-left:auto;font-size:12.5px;color:var(--vscode-descriptionForeground,#999)}
.rc-ts-link{all:unset;cursor:pointer;color:var(--vscode-textLink-foreground,#3794ff);font-size:12.5px;margin-left:6px}
.rc-ts-link:focus-visible{outline:2px solid var(--vscode-focusBorder,#3794ff)}
.rc-ts-mode{display:flex;align-items:center;gap:12px;margin:0 16px 12px;padding:10px 12px;border-radius:9px;border:1px solid var(--vscode-widget-border,#3c3c3c)}
.rc-ts-mode .d{font-size:12px;color:var(--vscode-descriptionForeground,#999)}
.rc-ts-seg{display:flex;gap:4px;margin:0 16px 8px;padding:4px;border-radius:9px;border:1px solid var(--vscode-widget-border,#3c3c3c);background:var(--vscode-input-background,#1e1e1e)}
.rc-ts-seg button{all:unset;box-sizing:border-box;flex:1;text-align:center;height:34px;line-height:34px;border-radius:6px;font-size:13.5px;cursor:pointer;color:var(--vscode-descriptionForeground,#aaa);white-space:nowrap}
.rc-ts-seg button[aria-checked=true]{background:var(--vscode-list-activeSelectionBackground,#34343b);color:var(--vscode-list-activeSelectionForeground,var(--vscode-foreground,#fff));font-weight:600}
.rc-ts-seg button:focus-visible{outline:2px solid var(--vscode-focusBorder,#3794ff)}
.rc-ts-seg small{font-size:11.5px;font-weight:400;opacity:.75;margin-left:4px}
.rc-ts-row{display:flex;align-items:center;gap:12px;padding:10px 16px}
.rc-ts-row + .rc-ts-row{border-top:1px solid var(--vscode-panel-border,#333)}
.rc-ts-row img{width:32px;height:32px;flex:none;object-fit:contain}
.rc-ts-row .t{flex:1;min-width:0}
.rc-ts-row .t b{font-size:14px;font-weight:600}
.rc-ts-row .t div{font-size:12px;color:var(--vscode-descriptionForeground,#999)}
.rc-ts-row.off{opacity:.5}
.rc-ts-sel{height:34px;min-width:168px;max-width:200px;padding:0 8px;border-radius:7px;border:1px solid var(--vscode-dropdown-border,var(--vscode-widget-border,#3f3f3f));
  background:var(--vscode-dropdown-background,#1f1f22);color:var(--vscode-dropdown-foreground,#ddd);font:inherit;font-size:13px;cursor:pointer}
.rc-ts-sel:focus-visible{outline:2px solid var(--vscode-focusBorder,#3794ff)}
.rc-ts-sel:disabled{opacity:.5;cursor:default}
.rc-ts-step{display:inline-flex;align-items:center;height:34px;border-radius:7px;border:1px solid var(--vscode-widget-border,#3f3f3f)}
.rc-ts-step button{all:unset;width:32px;height:100%;display:grid;place-items:center;cursor:pointer;font-size:16px;color:var(--vscode-foreground,#ddd)}
.rc-ts-step button:disabled{opacity:.35;cursor:default}
.rc-ts-step button:focus-visible{outline:2px solid var(--vscode-focusBorder,#3794ff)}
.rc-ts-step b{min-width:22px;text-align:center;font-size:14.5px;font-variant-numeric:tabular-nums}
.rc-ts-note{padding:0 16px 8px;font-size:12px;color:var(--vscode-descriptionForeground,#999)}
.rc-ts-ft{display:flex;align-items:center;gap:14px;padding:12px 16px;border-top:1px solid var(--vscode-panel-border,#333);font-size:12.5px;color:var(--vscode-descriptionForeground,#aaa)}
.rc-ts-ft b{color:var(--vscode-foreground,#ddd);font-weight:600;font-variant-numeric:tabular-nums}
.rc-ts-ft .reset{all:unset;margin-left:auto;cursor:pointer;color:var(--vscode-descriptionForeground,#aaa)}
.rc-ts-ft .reset:hover{color:var(--vscode-foreground,#ddd)}
.rc-ts-ft .reset:focus-visible{outline:2px solid var(--vscode-focusBorder,#3794ff)}
.rc-ts-row .t b{white-space:nowrap}
@media (max-width: 520px){.rc-ts-row img{display:none}.rc-ts-sel{min-width:0;width:150px}.rc-ts-row .t div{display:none}.rc-ts-row{gap:8px}
  .rc-ts-hd{flex-wrap:wrap}.rc-ts-hd .rec{margin-left:0;flex-basis:100%}}
`;

/** 버튼 글자 — 팀이면 "팀 6명 · 균형", 혼자면 "혼자 · 균형". 테스트를 끄면 붙여 알린다. */
export function teamButtonText(cfg: TeamConfig, teamMode: boolean): string {
  const people = 2 + cfg.devs + (cfg.test ? 1 : 0);
  const who = teamMode ? `팀 ${people}명` : "혼자";
  return `${who} · ${PRESET_TEXT[cfg.preset]}${cfg.test ? "" : " · 테스트 끔"}`;
}

/** 예상 문구(순수) — 기록이 없으면 지어내지 않는다. */
export function estimateText(history: RunStat[], cfg: TeamConfig, teamMode: boolean): { time: string; cost: string; basis: string } | null {
  const e = estimate(history, cfg, teamMode);
  if (!e) return null;
  const [lo, hi] = e.minutes;
  const time = lo === hi ? `${lo}분` : `${lo}–${hi}분`;
  const cost = e.cost ? (e.cost[0] === e.cost[1] ? formatCost(e.cost[0]) : `${formatCost(e.cost[0])}–${formatCost(e.cost[1])}`) : "";
  return { time, cost, basis: `지난 ${e.runs}회 기준` };
}

const ROLE_IN_SOLO: Record<TeamRoleKey, boolean> = { planner: false, dev: true, review: false, test: true };

export interface TeamSetupProps {
  teamMode: boolean;
  config: TeamConfig;
  options: ModelOption[];
  history: RunStat[];
  instruction: string;
  disabled?: boolean;
  onTeamMode: (on: boolean) => void;
  onChange: (cfg: TeamConfig) => void;
  /** 창을 열 때 — 모델 목록을 새로 받는다. */
  onOpen?: () => void;
  /** 처음부터 열어 둔다(화면 점검용). */
  defaultOpen?: boolean;
}

export const TeamSetup: React.FC<TeamSetupProps> = ({ teamMode, config, options, history, instruction, disabled, onTeamMode, onChange, onOpen, defaultOpen }) => {
  const [open, setOpen] = useState(!!defaultOpen);
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);

  const place = () => {
    const b = btnRef.current?.getBoundingClientRect();
    if (!b || typeof window === "undefined") return;
    const width = Math.min(500, window.innerWidth - 24);
    const left = Math.max(12, Math.min(b.left, window.innerWidth - width - 12));
    //: 아래 공간이 모자라면(입력창이 화면 아래에 붙어 있을 때) 위로 연다.
    const below = window.innerHeight - b.bottom;
    setPos(below >= 470 || b.top < 300 ? { left, top: b.bottom + 8 } : { left, bottom: window.innerHeight - b.top + 8 });
  };
  useLayoutEffect(() => { if (open) place(); }, [open]);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (popRef.current?.contains(t) || btnRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); btnRef.current?.focus(); } };
    const onResize = () => place();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", onResize);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey, true); window.removeEventListener("resize", onResize); };
  }, [open]);
  useEffect(() => { if (open) popRef.current?.focus({ preventScroll: true }); }, [open]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);

  const rec = recommend(instruction);
  const est = estimateText(history, config, teamMode);
  const label = teamButtonText(config, teamMode);
  const roles = ROLE_KEYS;

  return (
    <>
      <style>{css}</style>
      <button ref={btnRef} type="button" className={`rc-ts-btn${teamMode ? " on" : ""}`} data-testid="team-setup-button" disabled={disabled}
        aria-haspopup="dialog" aria-expanded={open} title="팀 구성 · 역할별 모델"
        onClick={() => { if (!open) onOpen?.(); setOpen(!open); }}>
        <span className="rc-ts-avs" aria-hidden="true">
          {Array.from({ length: teamMode ? 3 : 1 }, (_, i) => <img key={i} src={CHARACTER_SRC} alt="" />)}
        </span>
        <span className="t">{label}</span>
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" style={{ opacity: .7 }}><path d="m4.5 6 3.5 3.5L11.5 6" /></svg>
      </button>
      {open && (
        <div ref={popRef} className="rc-ts-pop" role="dialog" aria-label="팀 구성" tabIndex={-1} data-testid="team-setup"
          style={pos ? { left: pos.left, top: pos.top, bottom: pos.bottom } : { left: 12, top: 12 }}>
          <div className="rc-ts-hd">
            <b>팀 구성</b>
            {teamMode && instruction.trim() && (
              <span className="rec" data-testid="team-recommend" title="요청 글의 길이·기능 수로 본 추천입니다. 정확한 작업 수는 설계가 끝나야 압니다.">
                요청 크기 {SIZE_TEXT[rec.size]} · 추천 개발 {rec.devs}명
                {rec.devs !== config.devs && <button type="button" className="rc-ts-link" onClick={() => onChange(setDevs(config, rec.devs))}>적용</button>}
              </span>
            )}
          </div>

          <div className="rc-ts-mode">
            <div style={{ flex: 1, minWidth: 0 }}>
              <b style={{ fontSize: 13.5 }}>팀 모드</b>
              <div className="d">{teamMode ? "설계 → 여러 개발이 동시에 → 검토 → 테스트" : "리코더 한 명이 만들고, 요청이 크면 자동으로 나눕니다"}</div>
            </div>
            <Switch checked={teamMode} onChange={onTeamMode} ariaLabel="팀 모드" />
          </div>

          <div className="rc-ts-seg" role="radiogroup" aria-label="프리셋">
            {(["quality", "balanced", "economy", "custom"] as Preset[]).map((p) => (
              <button key={p} type="button" role="radio" aria-checked={config.preset === p} data-preset={p}
                onClick={() => onChange(applyPreset(config, p))}>
                {PRESET_TEXT[p]}{p === "balanced" && <small>추천</small>}
              </button>
            ))}
          </div>

          {roles.map((role) => {
            const active = teamMode || ROLE_IN_SOLO[role];
            const off = !active || (role === "test" && !config.test);
            return (
              <div key={role} className={`rc-ts-row${off ? " off" : ""}`} data-role={role}>
                <img src={CHARACTER_SRC} alt="" />
                <div className="t">
                  <b>{role === "dev" && !teamMode ? "만들기" : ROLE_TEXT[role].name}</b>
                  <div>{!active ? "팀 모드에서만" : role === "dev" && !teamMode ? "요청한 코드를 작성" : ROLE_TEXT[role].detail}</div>
                </div>
                {role === "dev" && teamMode && (
                  <span className="rc-ts-step" aria-label="개발 인원">
                    <button type="button" aria-label="개발 한 명 빼기" disabled={config.devs <= MIN_DEVS} onClick={() => onChange(setDevs(config, config.devs - 1))}>−</button>
                    <b data-testid="team-devs">{config.devs}</b>
                    <button type="button" aria-label="개발 한 명 더하기" disabled={config.devs >= MAX_DEVS} onClick={() => onChange(setDevs(config, config.devs + 1))}>+</button>
                  </span>
                )}
                {role === "test" && (
                  <Switch checked={config.test} onChange={(on) => onChange({ ...config, test: on })} ariaLabel="테스트 켜기" />
                )}
                <select className="rc-ts-sel" aria-label={`${ROLE_TEXT[role].name}에 쓸 모델`} disabled={off} value={config.models[role]}
                  onChange={(e) => onChange(setModel(config, role, e.target.value as TeamConfig["models"][TeamRoleKey]))}>
                  {options.map((o) => (
                    <option key={o.spec} value={o.spec} disabled={!o.ready} title={o.detail}>{o.label}{o.ready ? "" : " — 키 없음"}</option>
                  ))}
                </select>
              </div>
            );
          })}
          {options.some((o) => !o.ready) && <div className="rc-ts-note">흐린 모델은 API 키가 없어 고를 수 없습니다 — 설정의 AI 연결에서 추가하세요.</div>}
          {config.devs > 3 && teamMode && <div className="rc-ts-note">학생용 AI 는 분당 호출 한도가 있어 개발 3명을 넘기면 효과가 작을 수 있어요.</div>}

          <div className="rc-ts-ft" data-testid="team-estimate">
            {est ? <>
              <span>예상 <b>{est.time}</b></span>
              {est.cost && <span>비용 <b>{est.cost}</b></span>}
              <span>{est.basis}</span>
            </> : <span>예상 시간·비용은 첫 실행 뒤 표시</span>}
            <button type="button" className="reset" onClick={() => { onChange({ ...defaultConfig() }); }}>기본값으로</button>
          </div>
        </div>
      )}
    </>
  );
};

export default TeamSetup;
