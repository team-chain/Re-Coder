/**
 * 팀 구성 버튼과 창(2.0.45) — 입력창 도구줄의 「팀 6명 · 균형」 버튼을 누르면 열린다.
 *
 *  - 팀 모드 켜기/끄기 · 프리셋(품질 우선 / 균형 / 비용 절약 / 직접 설정)
 *  - 직접 설정에서만 역할(설계·개발·검토·테스트)마다 복잡/간단 모델을 고른다.
 *  - 개발 인원(−/+) · 테스트 켜기/끄기
 *  - 예상 시간·비용은 **지난 실행 기록**에서만. 기록이 없으면 "첫 실행 뒤 표시".
 *
 * 순수 계산은 teamConfig.ts 에 있다(여기서는 그리기와 열고 닫기만).
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Switch } from "./Check";
import {
  MAX_DEVS, MIN_DEVS, ModelOption, PRESET_TEXT, Preset, ROLE_KEYS, ROLE_TEXT, RunStat, TeamConfig, TeamRoleKey, PRESETS,
  applyPreset, effectiveConfig, estimate, formatCost, lockedRoles, recommendPreset, restoreRecommendation, sensitiveOf, setDevs, setModel,
} from "./teamConfig";

const css = `
.rc-ts-btn{display:inline-flex;align-items:center;gap:7px;height:36px;padding:0 12px;border-radius:8px;border:1px solid var(--rc-line2);background:transparent;color:var(--rc-fg);font:inherit;font-size:14px;cursor:pointer;white-space:nowrap}
.rc-ts-btn.on{border-color:var(--rc-teal);background:color-mix(in srgb,var(--rc-teal) 12%,transparent)}
.rc-ts-btn:disabled{opacity:.55;cursor:default}
.rc-ts-btn:focus-visible,.rc-ts-pop button:focus-visible,.rc-ts-pop select:focus-visible{outline:2px solid var(--rc-teal);outline-offset:2px}
.rc-ts-badge{padding:1px 6px;border-radius:99px;background:color-mix(in srgb,var(--rc-teal) 16%,transparent);color:var(--vscode-textLink-foreground);font-size:11px;font-weight:600}
.rc-ts-pop{position:fixed;z-index:1200;box-sizing:border-box;width:440px;max-width:calc(100vw - 24px);max-height:calc(100vh - 24px);overflow:auto;padding:16px;border-radius:12px;border:1px solid var(--rc-line2);background:var(--rc-solid);color:var(--rc-fg);box-shadow:0 18px 48px rgba(0,0,0,.5);font-size:13px}
.rc-ts-pop:focus{outline:none}
.rc-ts-hd{display:flex;align-items:center;gap:8px}.rc-ts-hd b{font-size:15px}.rc-ts-hd .label{margin-left:auto;font-size:12.5px;color:var(--rc-fg2)}
.rc-ts-link{border:0;background:transparent;padding:0;color:var(--vscode-textLink-foreground);font:inherit;font-size:12.5px;cursor:pointer;white-space:nowrap}
.rc-ts-seg{display:flex;gap:4px;margin-top:12px;padding:4px;border-radius:9px;border:1px solid var(--rc-line2);background:var(--vscode-input-background)}
.rc-ts-seg button{flex:1;height:32px;padding:0 4px;border:0;border-radius:6px;background:transparent;color:var(--rc-fg2);font:inherit;font-size:13px;white-space:nowrap;cursor:pointer}
.rc-ts-seg button[aria-checked=true]{background:color-mix(in srgb,var(--rc-teal) 16%,transparent);color:var(--rc-fg);font-weight:600}
.rc-ts-seg small{font-size:10.5px;font-weight:600;color:var(--vscode-textLink-foreground);margin-left:4px}
.rc-ts-note{display:flex;align-items:flex-start;gap:8px;margin-top:10px;font-size:12.5px;line-height:1.5}.rc-ts-note svg{flex:none;margin-top:2px;color:var(--rc-teal)}.rc-ts-note span{flex:1;min-width:0}.rc-ts-note.changed{color:var(--rc-fg2)}.rc-ts-note.lock,.rc-ts-note.lock svg{color:var(--rc-warn)}
.rc-ts-roles{margin-top:12px;border-top:1px solid var(--rc-line)}
.rc-ts-row{display:flex;align-items:center;gap:10px;min-height:48px;border-bottom:1px solid var(--rc-line)}.rc-ts-row.off{opacity:.45}
.rc-ts-name{display:inline-flex;align-items:center;gap:6px;min-width:64px;font-size:13.5px;font-weight:600;white-space:nowrap}
.rc-ts-dot{width:6px;height:6px;border-radius:50%;background:transparent}.rc-ts-dot.on{background:var(--vscode-textLink-foreground)}
.rc-ts-step{display:inline-flex;align-items:center;height:28px;border-radius:7px;border:1px solid var(--rc-line2)}.rc-ts-step button{width:28px;height:100%;border:0;background:transparent;color:var(--rc-fg);font:inherit;font-size:15px;cursor:pointer}.rc-ts-step button:disabled{opacity:.35;cursor:default}.rc-ts-step b{min-width:18px;text-align:center;font-size:13.5px}
.rc-ts-model{margin-left:auto;display:inline-flex;align-items:center;gap:8px;min-width:0}.rc-ts-model>svg{color:var(--rc-warn);flex:none}.rc-ts-ro{display:inline-flex;align-items:baseline;gap:6px;justify-content:flex-end;min-width:0}.rc-ts-ro .model{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.rc-ts-tier{font-size:11.5px;color:var(--rc-fg2);white-space:nowrap}
.rc-ts-sel{height:32px;width:178px;max-width:100%;padding:0 8px;border-radius:8px;border:1px solid var(--rc-teal);background:var(--vscode-input-background);color:var(--rc-fg);font:inherit;font-size:12.5px;cursor:pointer}.rc-ts-sel:disabled{opacity:.6;cursor:default}
.rc-ts-ft{display:flex;align-items:center;gap:10px;margin-top:12px;font-size:12.5px;color:var(--rc-fg2)}.rc-ts-ft b{color:var(--rc-fg);font-weight:600}.rc-ts-ft .rc-ts-link{margin-left:auto}
@media(max-width:420px){.rc-ts-pop{padding:12px}.rc-ts-row{gap:6px;flex-wrap:wrap;padding:8px 0}.rc-ts-model{max-width:100%}.rc-ts-seg{display:grid;grid-template-columns:1fr 1fr}.rc-ts-ft{flex-wrap:wrap}}
`;

export const RecommendationIcon: React.FC<{ locked?: boolean }> = ({ locked }) => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true">
    {locked ? <><rect x="3.5" y="7" width="9" height="6.5" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/></>
      : <><path d="M7 2c.4 2.8 1.6 4 4.4 4.4C8.6 6.8 7.4 8 7 10.8 6.6 8 5.4 6.8 2.6 6.4 5.4 6 6.6 4.8 7 2Z"/><path d="M12 9.5c.2 1.2.7 1.7 1.9 1.9-1.2.2-1.7.7-1.9 1.9-.2-1.2-.7-1.7-1.9-1.9 1.2-.2 1.7-.7 1.9-1.9Z"/></>}
  </svg>
);

/** 버튼 글자 — 팀이면 "팀 6명 · 균형", 혼자면 "혼자 · 균형". */
export function teamButtonText(cfg: TeamConfig, teamMode: boolean): string {
  const people = 2 + cfg.devs + (cfg.test ? 1 : 0);
  const who = teamMode ? `팀 ${people}명` : "혼자";
  return `${who} · ${PRESET_TEXT[cfg.preset]}`;
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
  lockSensitive?: boolean;
  autoRecommend?: boolean;
  onOpenModels?: () => void;
}

export const TeamSetup: React.FC<TeamSetupProps> = ({ teamMode, config, options, history, instruction, disabled, onTeamMode, onChange, onOpen, defaultOpen, lockSensitive = true, autoRecommend = true, onOpenModels }) => {
  const [open, setOpen] = useState(!!defaultOpen);
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);

  const place = () => {
    const b = btnRef.current?.getBoundingClientRect();
    if (!b || typeof window === "undefined") return;
    const width = Math.min(440, window.innerWidth - 24);
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

  const rec = recommendPreset(instruction);
  const cfg = effectiveConfig(config, instruction, lockSensitive, autoRecommend);
  const locks = lockedRoles(instruction, lockSensitive);
  const sens = sensitiveOf(instruction);
  const custom = cfg.preset === "custom";
  const differs = ROLE_KEYS.some((r) => cfg.models[r] !== PRESETS[rec][r]);
  const following = !custom && !differs && cfg.preset === rec;
  const est = estimateText(history, cfg, teamMode);
  const recEst = estimateText(history, { ...cfg, preset: rec }, teamMode);
  const label = teamButtonText(cfg, teamMode);
  const name = (spec: string) => options.find((o) => o.spec === spec)?.label || (spec === "quality" ? "복잡한 작업 모델" : "간단한 작업 모델");
  const subject = { 결제: "결제가", 인증: "인증이", 개인정보: "개인정보가" };
  const changed = custom || !following;
  let note = rec === "economy" ? `작은 요청이라 모든 역할에 간단한 작업 모델(${name("fast")})을 추천합니다.`
    : rec === "balanced" ? `설계·개발은 복잡한 작업 모델(${name("quality")}), 검토·테스트는 간단한 작업 모델(${name("fast")})을 추천합니다.`
    : `큰 요청이라 모든 역할에 복잡한 작업 모델(${name("quality")})을 추천합니다.`;
  if (sens.length) note = locks.length
    ? `${subject[sens[0]]} 들어간 요청이라 설계·개발은 복잡한 작업 모델(${name("quality")})로 고정합니다.`
    : `${subject[sens[0]]} 들어간 요청이라 모든 역할에 복잡한 작업 모델(${name("quality")})을 추천합니다.`;
  if (custom && !differs) note = "역할마다 모델을 고르세요. 지금은 추천과 같습니다.";
  else if (!following) note = `추천(${PRESET_TEXT[rec]})과 다르게 골랐습니다.${recEst?.cost ? ` 추천대로면 ${recEst.cost}입니다.` : ""}`;

  return (
    <>
      <style>{css}</style>
      <button ref={btnRef} type="button" className={`rc-ts-btn${teamMode ? " on" : ""}`} data-testid="team-setup-button" disabled={disabled}
        aria-haspopup="dialog" aria-expanded={open} title="팀 구성 · 역할별 모델"
        onClick={() => { if (!open) onOpen?.(); setOpen(!open); }}>
        <span className="t">{label}</span>
        {following && <span className="rc-ts-badge">추천</span>}
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" style={{ opacity: .7 }}><path d="m4.5 6 3.5 3.5L11.5 6" /></svg>
      </button>
      {open && (
        <div ref={popRef} className="rc-ts-pop" role="dialog" aria-label="팀 구성" tabIndex={-1} data-testid="team-setup"
          style={pos ? { left: pos.left, top: pos.top, bottom: pos.bottom } : { left: 12, top: 12 }}>
          <div className="rc-ts-hd"><b>팀 구성</b><span className="label">팀 모드</span>
            <Switch checked={teamMode} onChange={onTeamMode} ariaLabel="팀 모드" />
          </div>
          <div className="rc-ts-seg" role="radiogroup" aria-label="프리셋">
            {(["quality", "balanced", "economy", "custom"] as Preset[]).map((p) => (
              <button key={p} type="button" role="radio" aria-checked={cfg.preset === p} data-preset={p}
                onClick={() => onChange(applyPreset(config, p, instruction, lockSensitive, autoRecommend))}>
                {PRESET_TEXT[p]}{p === rec && <small>추천</small>}
              </button>
            ))}
          </div>
          <div className={`rc-ts-note${changed ? " changed" : locks.length ? " lock" : ""}`} data-testid="team-recommend" aria-live="polite">
            <RecommendationIcon locked={!changed && locks.length > 0} /><span>{note}</span>
            {changed && <button type="button" className="rc-ts-link" onClick={() => onChange(restoreRecommendation(config, instruction))}>되돌리기</button>}
          </div>
          <div className="rc-ts-roles">
          {ROLE_KEYS.map((role) => {
            const off = !(teamMode || ROLE_IN_SOLO[role]) || (role === "test" && !cfg.test);
            const locked = locks.includes(role);
            const spec = cfg.models[role];
            return (
              <div key={role} className={`rc-ts-row${off ? " off" : ""}`} data-role={role}>
                <span className="rc-ts-name"><span className={`rc-ts-dot${custom && spec !== PRESETS[rec][role] ? " on" : ""}`} aria-hidden="true"/>
                  <b>{role === "dev" && !teamMode ? "만들기" : ROLE_TEXT[role].name}</b>
                </span>
                {role === "dev" && teamMode && <span className="rc-ts-step" aria-label="개발 인원">
                  <button type="button" aria-label="개발 한 명 빼기" disabled={cfg.devs <= MIN_DEVS} onClick={() => onChange(setDevs(config, cfg.devs - 1))}>−</button>
                  <b data-testid="team-devs">{cfg.devs}</b>
                  <button type="button" aria-label="개발 한 명 더하기" disabled={cfg.devs >= MAX_DEVS} onClick={() => onChange(setDevs(config, cfg.devs + 1))}>+</button>
                </span>}
                {role === "test" && <Switch checked={cfg.test} onChange={(on) => onChange({ ...config, test: on })} ariaLabel="테스트 켜기" />}
                <span className="rc-ts-model" title={locked ? "복잡한 작업 모델로 고정됨" : undefined}>
                  {locked && <RecommendationIcon locked />}
                  {custom ? <select className="rc-ts-sel" aria-label={`${ROLE_TEXT[role].name}에 쓸 모델`} disabled={off || locked} value={spec}
                    onChange={(e) => onChange(setModel(config, role, e.target.value as TeamConfig["models"][TeamRoleKey]))}>
                    {options.map((o) => <option key={o.spec} value={o.spec} disabled={!o.ready}>{o.label} · {o.spec === "quality" ? "복잡" : "간단"}</option>)}
                  </select> : <span className="rc-ts-ro"><span className="model">{name(spec)}</span><span className="rc-ts-tier">{spec === "quality" ? "복잡" : "간단"}</span></span>}
                </span>
              </div>
            );
          })}
          </div>
          <div className="rc-ts-ft" data-testid="team-estimate">
            {est ? <span title={est.basis}>예상 <b>{est.cost ? `${est.cost} · ` : ""}{est.time}</b></span> : <span>예상 시간·비용은 첫 실행 뒤 표시</span>}
            <button type="button" className="rc-ts-link" onClick={() => { setOpen(false); onOpenModels?.(); }}>모델 설정</button>
          </div>
        </div>
      )}
    </>
  );
};

export default TeamSetup;
