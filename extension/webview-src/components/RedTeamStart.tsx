/**
 * 공격 테스트 시작 카드(4.0.0) — 주소·모델·동의를 한 장에서.
 *
 * 예전에는 주소 → 모델 → 키 → 긴 확인창(Windows 기본 팝업)이 차례로 떴고, 주소·포트를 사람이 찾아 적어야 했다.
 * 대상은 지금 실행 중인 이 프로젝트 앱에서 고른다(Core 가 compose·컨테이너를 찾고 응답까지 확인). 직접 입력은 예외로만.
 */
import React, { useCallback, useEffect, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { DIALOG_CSS } from "./ui/Dialogs";
import { Checkbox } from "./Check";

export interface LocalApp { name: string; url: string; public?: boolean; reachable?: boolean }
export interface LocalApps { apps: LocalApp[]; others: LocalApp[]; reason?: string }
export type Provider = "openai" | "anthropic";
export const EULA_URL = "https://github.com/vxcontrol/pentagi/blob/main/EULA.md";

/** 고를 첫 대상 — 응답하는 공개 서비스 → 응답하는 서비스 → 이 프로젝트 첫 앱. 다른 프로젝트 앱은 자동으로 고르지 않는다. */
export function defaultTarget(list: LocalApps | null): string {
  const apps = list?.apps ?? [];
  return (apps.find(a => a.reachable && a.public) ?? apps.find(a => a.reachable) ?? apps[0])?.url ?? "";
}
export const hostPort = (url: string) => url.replace(/^https?:\/\//, "").replace(/\/$/, "");

const START_CSS = `
.rc-rt-opts{display:flex;flex-direction:column;gap:6px}
.rc-rt-opt{all:unset;cursor:pointer;display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:12px;background:var(--rc-tile,rgba(148,178,210,.035));border:1px solid transparent}
.rc-rt-opt[aria-checked=true]{border-color:var(--rc-teal,#35A9D1);background:var(--rc-tile2,rgba(148,178,210,.065))}
.rc-rt-opt .dot{width:14px;height:14px;border-radius:50%;border:2px solid var(--rc-fg3,#67768A);flex:none}
.rc-rt-opt[aria-checked=true] .dot{border-color:var(--rc-teal,#35A9D1);box-shadow:inset 0 0 0 3px var(--rc-solid,#0D131A);background:var(--rc-teal,#35A9D1)}
.rc-rt-opt b{font-weight:600}.rc-rt-opt code{font-family:var(--rc-mono);font-size:12px;color:var(--rc-fg2,#9AA8B8)}
.rc-rt-opt .st{margin-left:auto;font-size:11.5px;color:var(--rc-fg3,#67768A)}.rc-rt-opt .st.ok{color:var(--rc-ok,#3DD68C)}
.rc-rt-label{display:block;margin:16px 0 7px;font-size:12px;color:var(--rc-fg2,#9AA8B8)}
.rc-rt-seg{display:flex;gap:4px;padding:3px;border-radius:11px;background:var(--vscode-input-background,#0A1017);border:1px solid var(--rc-line2,rgba(160,190,220,.16))}
.rc-rt-seg button{all:unset;cursor:pointer;flex:1;text-align:center;font-size:12.5px;padding:7px;border-radius:8px;color:var(--rc-fg2,#9AA8B8)}
.rc-rt-seg button[aria-pressed=true]{background:var(--rc-tile2,rgba(148,178,210,.065));color:var(--rc-fg,#EEF3F8);box-shadow:inset 0 0 0 1px var(--rc-teal,#35A9D1)}
.rc-rt-note{font-size:11.5px;color:var(--rc-fg3,#67768A);margin:6px 2px 0}.rc-rt-note.ok{color:var(--rc-ok,#3DD68C)}
.rc-rt-link{all:unset;cursor:pointer;color:var(--rc-teal,#35A9D1);font-size:12px}
.rc-rt-agree{display:flex;gap:9px;align-items:center;margin-top:18px;font-size:12.5px;cursor:pointer}
.rc-rt-agree input{width:16px;height:16px;accent-color:var(--rc-teal,#35A9D1);margin:0}
.rc-rt-empty{padding:12px;border-radius:12px;background:var(--rc-tile,rgba(148,178,210,.035));font-size:12.5px;color:var(--rc-fg2,#9AA8B8)}
`;

export const RedTeamStart: React.FC<{
  keys?: Partial<Record<Provider, boolean>>; ready?: boolean;
  onStart: (target: string, provider: Provider) => void; onCancel: () => void; onGoLocal?: () => void;
  initialApps?: LocalApps | null;
}> = ({ keys = {}, ready, onStart, onCancel, onGoLocal, initialApps = null }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [apps, setApps] = useState<LocalApps | null>(initialApps);
  const [target, setTarget] = useState(defaultTarget(initialApps));
  const [manual, setManual] = useState(false);
  const [provider, setProvider] = useState<Provider>(keys.anthropic || !keys.openai ? "anthropic" : "openai");
  const [agreed, setAgreed] = useState(false);
  const [scope, setScope] = useState(false);
  useEffect(() => { if (!initialApps) postMessage("journey.localApps"); }, [postMessage, initialApps]);
  useMessage(useCallback(({ type, payload }) => {
    if (type !== "journey.localApps") return;
    const list = payload as LocalApps;
    setApps(list); setTarget(t => t || defaultTarget(list));
  }, []));
  const mine = apps?.apps ?? [];
  const list = mine.length ? mine : apps?.others ?? [];
  const valid = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\]):\d{2,5}(\/\S*)?$/.test(target.trim());
  return (
    <div className="rc-dlg-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
      <style>{DIALOG_CSS + START_CSS}</style>
      <div className="rc-dlg" role="dialog" aria-modal="true" aria-label="공격 테스트 시작" data-testid="redteam-start"
        onKeyDown={(e) => { if (e.key === "Escape") onCancel(); }}>
        <div className="rc-dlg-head"><span className="rc-dlg-mark" aria-hidden>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinejoin="round"><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z" /></svg>
        </span><div><h2>공격 테스트 시작</h2></div></div>

        <span className="rc-rt-label">대상</span>
        {!apps && <div className="rc-rt-empty">실행 중인 앱 찾는 중…</div>}
        {apps && !list.length && !manual && <div className="rc-rt-empty">
          실행 중인 앱이 없어요.{onGoLocal && <> <button type="button" className="rc-rt-link" onClick={onGoLocal}>로컬 실행하러 가기 ›</button></>}
        </div>}
        {!!list.length && !manual && <div className="rc-rt-opts" role="radiogroup" aria-label="대상 앱">
          {!mine.length && <p className="rc-rt-note" style={{ margin: "0 2px 2px" }}>이 프로젝트 앱이 없어 다른 실행 중 앱을 보여 줘요</p>}
          {list.map(app => <button key={app.url} type="button" role="radio" className="rc-rt-opt" aria-checked={target === app.url} onClick={() => setTarget(app.url)}>
            <span className="dot" /><b>{app.name}</b><code>{hostPort(app.url)}</code>
            <span className={`st${app.reachable ? " ok" : ""}`}>{app.reachable ? "응답 확인" : "응답 없음"}</span>
          </button>)}
        </div>}
        {manual
          ? <input className="rc-dlg-input" style={{ marginTop: 0 }} value={target} placeholder="http://localhost:3000" autoFocus spellCheck={false} onChange={(e) => setTarget(e.target.value)} />
          : null}
        <p className="rc-rt-note"><button type="button" className="rc-rt-link" onClick={() => { setManual(m => !m); if (manual) setTarget(defaultTarget(apps)); }}>{manual ? "목록에서 고르기" : "직접 입력"}</button></p>

        <span className="rc-rt-label">모델</span>
        <div className="rc-rt-seg">
          {(["openai", "anthropic"] as Provider[]).map(p => <button key={p} type="button" aria-pressed={provider === p} onClick={() => setProvider(p)}>{p === "openai" ? "OpenAI" : "Anthropic"}</button>)}
        </div>
        <p className={`rc-rt-note${keys[provider] ? " ok" : ""}`}>{keys[provider] ? "저장된 키 사용" : "시작할 때 키를 한 번 입력해요"}{ready ? " · 검사 도구 준비됨" : ""}</p>

        <Checkbox checked={agreed} onChange={setAgreed} style={{ marginTop: 18 }}
          label={<>내 앱이며 <a href={EULA_URL} className="rc-rt-link">PentAGI EULA</a>에 동의</>} />
        {scope && <div className="rc-dlg-detail">격리된 네트워크에서 고른 앱에만 요청 · 최대 300개 · 앱 응답이 고른 모델로 전송되고 사용료 발생 · 준비 최대 60분, 검사 30분 · 끝나면 환경 삭제</div>}
        <div className="rc-dlg-acts" style={{ alignItems: "center" }}>
          {!scope && <button type="button" className="rc-rt-link" style={{ marginRight: "auto" }} onClick={() => setScope(true)}>범위 보기</button>}
          <button type="button" className="rc-dlg-btn" onClick={onCancel}>취소</button>
          <button type="button" className="rc-dlg-btn p" disabled={!agreed || !valid} aria-disabled={!agreed || !valid}
            style={!agreed || !valid ? { opacity: .45, cursor: "default" } : undefined}
            onClick={() => { if (agreed && valid) onStart(target.trim(), provider); }}>시작</button>
        </div>
      </div>
    </div>
  );
};

export default RedTeamStart;
