/**
 * 4.0 진행 흐름 — 모든 화면 맨 위에 "지금 어디까지 했고, 다음에 뭘 하면 되는지" 를 보여 준다.
 *
 * 만들기 → 함께 띄워 검증 → 보안 검사 → 로컬 실행 → 공격 테스트 → 운영 배포.
 * 개발·보안·배포·운영 화면이 각자 따로 있어 하나의 흐름으로 보이지 않았다(실사용 피드백 2026-11-15).
 * 상태는 Core 가 실제 결과로 남긴 기록(+ 폴더 내용)에서 온다. 화면이 임의로 "완료" 를 만들지 않는다.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { Glyph } from "./shell/parts";

export type JourneyStatus = "done" | "failed" | "stale" | "todo" | "locked" | "blocked" | "unverified" | "cancelled";
export interface JourneyStep { key: string; title: string; status: JourneyStatus; summary: string; at?: string }
export interface JourneyState { available: boolean; reason?: string; multi?: boolean; stack?: boolean; services?: number; databases?: number; steps?: JourneyStep[]; next?: string }

/** 단계 → 그 일을 하는 화면. 공격 테스트(레드팀)는 실행 중인 앱이 있어야 해서 로컬 실행 뒤에 둔다. */
export const STEP_VIEW: Record<string, string> = {
  build: "code", verify: "map", security: "hub:security", local: "ship", redteam: "hub:security", release: "hub:deploy",
};

const NEXT_TEXT: Record<string, string> = {
  build: "무엇을 만들지 요청하세요",
  verify: "생성 결과의 검증을 확인하고, 설계도와 코드가 맞는지 봅니다",
  security: "코드·이미지·비밀값 보안 검사를 실행합니다",
  local: "이 PC 에서 Docker 로 실행해 봅니다",
  redteam: "실행 중인 앱에 공격 테스트(레드팀)를 합니다",
  release: "AWS 에 운영 배포합니다",
};

//: 글자 기호 대신 아이콘(3.0.2 규칙) — 없으면 단계 번호.
const MARK: Partial<Record<JourneyStatus, string>> = { done: "check", failed: "alert", stale: "refresh", blocked: "minus", unverified: "alert", cancelled: "minus" };
const STATUS_TEXT: Record<JourneyStatus, string> = {
  done: "완료", failed: "다시 필요", stale: "코드가 바뀌어 다시 확인", todo: "할 차례", locked: "앞 단계 먼저",
  blocked: "이번 범위 밖", unverified: "확인 못 함", cancelled: "중단됨",
};

/** 다음 단계 안내 문구 — 다중 서비스는 로컬 실행 단계에서 compose 로 함께 띄운다는 것을 말한다. */
export function nextHint(state: JourneyState): string {
  const key = state.next ?? "";
  if (!key) return "모든 단계를 마쳤습니다";
  if (key === "local" && state.multi) return `서비스 ${state.services || "여러"}개 · DB ${state.databases || ""}개를 이 PC 에서 함께 실행합니다`;
  return NEXT_TEXT[key] ?? "";
}

export const JourneyBar: React.FC<{ view: string; onGo: (view: string) => void }> = ({ view, onGo }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [state, setState] = useState<JourneyState | null>(null);
  const recorded = useRef<string>("");
  const offered = useRef(false);
  const refresh = useCallback(() => postMessage("journey.get"), [postMessage]);
  useEffect(() => { refresh(); }, [refresh, view]);
  useEffect(() => {
    const id = window.setInterval(refresh, 20000);
    return () => window.clearInterval(id);
  }, [refresh]);
  useMessage(useCallback(({ type, payload }) => {
    if (type === "journey.state") { setState((payload ?? null) as JourneyState | null); return; }
    //: 단계가 끝나는 결과가 오면 다시 읽는다(Core 가 이미 기록했다).
    if (["code.result", "deployResult", "security.checkResult", "security.rebuildResult"].includes(type)) { refresh(); return; }
    if (type === "security.redteamResult") {
      //: 공격 테스트는 확장이 직접 보는 결과 — 끝난 실행만 한 번 남긴다.
      const local = ((payload ?? {}) as { local?: { session?: string; outcome?: string; phase?: string } }).local;
      const outcome = local?.outcome;
      if (local?.session && (outcome === "finished" || outcome === "failed" || outcome === "cancelled") && recorded.current !== local.session) {
        recorded.current = local.session;
        postMessage("journey.record", {
          step: "redteam", status: outcome === "finished" ? "done" : outcome,
          summary: outcome === "finished" ? "실행 중인 앱 공격 테스트 완료 — 결과는 보안 화면에서" : "공격 테스트가 끝나지 않았습니다",
        });
      }
    }
  }, [postMessage, refresh]));
  //: 로컬 실행을 마쳐 다음이 공격 테스트면, 검사 도구(약 17GB)를 미리 받을지 한 번 묻는다(확장이 알림으로 묻고 동의해야 받는다).
  const local = state?.steps?.find(s => s.key === "local");
  const offerKey = state?.next === "redteam" && local?.status === "done" ? "redteam" : "";
  useEffect(() => {
    if (!offerKey || offered.current) return;
    offered.current = true;
    postMessage("security.redteam", { action: "localPrewarmOffer", requestId: `journey-prewarm-${Date.now()}` });
  }, [offerKey, postMessage]);
  if (!state?.available || !state.steps?.length) return null;
  const nextKey = state.next ?? "";
  return (
    <nav className="rc-journey" aria-label="진행 흐름" data-testid="journey-bar">
      <style>{`
        .rc-journey{display:flex;align-items:center;gap:10px;margin:0 22px 10px;padding:8px 10px;border:1px solid var(--rc-line2,#2a2f36);border-radius:14px;background:var(--rc-solid,#0f1318);overflow-x:auto}
        .rc-journey ol{display:flex;align-items:center;gap:4px;margin:0;padding:0;list-style:none;flex:1;min-width:0}
        .rc-journey li{display:flex;align-items:center;gap:4px;flex:none}
        .rc-journey li+li::before{content:"";width:14px;height:1px;background:var(--rc-line2,#2a2f36)}
        .rc-journey-step{display:flex;align-items:center;gap:6px;padding:5px 9px;border-radius:999px;border:1px solid transparent;background:none;color:var(--rc-fg2,#9aa0a6);font-size:12px;cursor:pointer;white-space:nowrap}
        .rc-journey-step[data-current=true]{border-color:var(--rc-line2,#2a2f36);color:var(--rc-fg,#e6e6e6)}
        .rc-journey-step[data-next=true]{border-color:var(--rc-teal,#3fb9c7);color:var(--rc-fg,#e6e6e6)}
        .rc-journey-step:disabled{cursor:default;opacity:.55}
        .rc-journey-dot{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;font-size:10px;border:1px solid currentColor}
        .rc-journey-step[data-status=done] .rc-journey-dot{background:var(--rc-ok,#3fb950);border-color:var(--rc-ok,#3fb950);color:#05150a}
        .rc-journey-step[data-status=failed] .rc-journey-dot,.rc-journey-step[data-status=stale] .rc-journey-dot{border-color:var(--rc-warn,#d7a300);color:var(--rc-warn,#d7a300)}
        .rc-journey-next{flex:none;display:flex;align-items:center;gap:8px;font-size:12px;color:var(--rc-fg2,#9aa0a6)}
        .rc-journey-next button{padding:6px 12px;border-radius:999px;border:none;background:var(--rc-teal,#3fb9c7);color:#03191c;font-weight:600;font-size:12px;cursor:pointer;white-space:nowrap}
      `}</style>
      <ol>
        {state.steps.map((step, i) => {
          const target = STEP_VIEW[step.key];
          const disabled = step.status === "locked" || step.status === "blocked";
          return (
            <li key={step.key}>
              <button type="button" className="rc-journey-step" data-status={step.status} data-next={step.key === nextKey}
                data-current={target === view} disabled={disabled}
                title={`${step.title} — ${STATUS_TEXT[step.status] ?? step.status}${step.summary ? ` · ${step.summary}` : ""}`}
                aria-label={`${i + 1}단계 ${step.title}: ${STATUS_TEXT[step.status] ?? step.status}`}
                onClick={() => target && onGo(target)}>
                <span className="rc-journey-dot" aria-hidden>{(MARK[step.status] && <Glyph name={MARK[step.status] as string} size={10} sw={3} />) || i + 1}</span>{step.title}
              </button>
            </li>
          );
        })}
      </ol>
      <div className="rc-journey-next" data-testid="journey-next">
        <span>{nextHint(state)}</span>
        {nextKey && STEP_VIEW[nextKey] && STEP_VIEW[nextKey] !== view &&
          <button type="button" onClick={() => onGo(STEP_VIEW[nextKey])}>다음: {state.steps.find(s => s.key === nextKey)?.title} →</button>}
      </div>
    </nav>
  );
};

export default JourneyBar;
