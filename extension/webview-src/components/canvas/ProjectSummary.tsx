/**
 * 프로젝트 요약 (4.0.1) — 캔버스의 프로젝트 노드를 누르면 열린다.
 * 예전에는 파일 그래프가 떴다(실사용 2026-11-19: "쓸데없다"). 지금 이 프로젝트가 어떤 상태인지 한눈에:
 * 서비스별 상태 · 기능 / 테스트 / 작업 수 · 사이트 열기 / 명세 / 다시 배포.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useVSCodeApi } from "../../hooks/useVSCodeApi";
import type { Spec } from "../SpecView";

export interface SpecWithDeploy extends Spec {
  deploy?: null | { id: string; at: string; status: string; failures: string[]; url: string; services: Record<string, "ok" | "fail" | "unknown"> };
}

export const SUMMARY_CSS = `
.rc-psum{display:flex;flex-direction:column;gap:18px}
.rc-psum .hd b{display:block;font-size:17px;font-weight:650}
.rc-psum .hd small{display:flex;align-items:center;gap:7px;margin-top:4px;color:var(--rc-fg3,#8a96a3);font-size:12px}
.rc-psum .dot{width:7px;height:7px;border-radius:50%;flex:none;background:var(--rc-fg3,#67768a);opacity:.55}
.rc-psum .dot.ok{background:var(--rc-ok,#3dd68c);opacity:1}.rc-psum .dot.fail{background:var(--rc-bad,#f27272);opacity:1}.rc-psum .dot.warn{background:var(--rc-warn,#f2b544);opacity:1}
.rc-psum .svcs{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px;font-size:12.5px}
.rc-psum .svcs span{display:flex;align-items:center;gap:8px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rc-psum .tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.rc-psum .tiles button{all:unset;box-sizing:border-box;cursor:pointer;padding:12px;border-radius:10px;border:1px solid var(--rc-line2,rgba(160,190,220,.14));text-align:left}
.rc-psum .tiles button:hover{background:var(--rc-tile2,rgba(148,178,210,.065))}
.rc-psum .tiles b{display:block;font-size:19px;font-weight:650;font-variant-numeric:tabular-nums}
.rc-psum .tiles small{color:var(--rc-fg3,#8a96a3);font-size:11.5px}
.rc-psum .fail-note{font-size:12px;color:var(--rc-bad,#f27272);margin:0}
.rc-psum .acts{display:flex;gap:8px;flex-wrap:wrap}
.rc-psum .acts button{flex:1}
`;

function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
const DEPLOY_LABEL: Record<string, string> = { success: "로컬 Docker 실행 중", degraded: "실행 중 · 일부 검사 실패", failed: "마지막 배포 실패", rolled_back: "이전 버전으로 되돌림" };

export const ProjectSummary: React.FC<{ active: boolean; branch?: string; onSpec: () => void; onDeploy: () => void; initial?: SpecWithDeploy | null }> = ({ active, branch, onSpec, onDeploy, initial = null }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [spec, setSpec] = useState<SpecWithDeploy | null>(initial);
  const [error, setError] = useState("");
  const req = useRef("");
  const load = useCallback(() => { req.current = `psum-${Date.now()}`; setError(""); postMessage("spec.load", { requestId: req.current }); }, [postMessage]);
  useEffect(() => { if (active && !initial) load(); }, [active, initial, load]);
  useMessage(useCallback(({ type, payload }) => {
    const p = (payload ?? {}) as { requestId?: string; spec?: SpecWithDeploy; message?: string };
    if (p.requestId !== req.current) return;
    if (type === "spec.result" && p.spec) setSpec(p.spec);
    if (type === "spec.error") setError(p.message || "불러오지 못했어요.");
  }, []));

  if (!spec) return <div className="rc-psum"><style>{SUMMARY_CSS}</style><p className="rc-muted">{error || "불러오는 중…"}</p></div>;
  const d = spec.deploy;
  const tone = !d ? "" : d.status === "success" ? "ok" : d.status === "failed" ? "fail" : "warn";
  const t = spec.tests ?? { passed: 0, total: 0, at: "", status: "" };
  const features = (spec.features ?? []).filter(f => !f.internal);
  const svcNames = [...(spec.services ?? []).map(s => s.name), ...(spec.databases ?? []).map(x => x.name)];
  return <div className="rc-psum" data-testid="project-summary">
    <style>{SUMMARY_CSS}</style>
    <div className="hd">
      <b>{spec.project}</b>
      <small title={d ? [DEPLOY_LABEL[d.status] || d.status, ...d.failures].join("\n") : "아직 로컬 실행 전"}>{branch && <>{branch} · </>}<span className={`dot ${tone}`} aria-label={d ? DEPLOY_LABEL[d.status] || d.status : "실행 전"} />{d ? when(d.at) : "—"}</small>
    </div>
    {spec.available === false ? <p className="rc-muted">{spec.reason}</p> : <>
      {svcNames.length > 0 && <div className="svcs" aria-label="서비스 상태">
        {svcNames.map(n => { const s = d?.services?.[n] ?? "unknown"; return <span key={n} title={s === "ok" ? "실행 중" : s === "fail" ? "시작 못 함" : "확인 전"}><i className={`dot ${s}`} />{n}</span>; })}
      </div>}
      <div className="tiles">
        <button type="button" onClick={onSpec}><b>{features.length}</b><small>기능</small></button>
        <button type="button" onClick={onSpec}><b style={{ color: t.total && t.passed < t.total ? "var(--rc-bad,#f27272)" : undefined }}>{t.total ? `${t.passed}/${t.total}` : "—"}</b><small>테스트</small></button>
        <button type="button" onClick={onSpec}><b style={{ color: spec.tasks?.length ? "var(--rc-warn,#f2b544)" : undefined }}>{spec.tasks?.length ?? 0}</b><small>작업</small></button>
      </div>
    </>}
    <div className="acts">
      {d?.url && <button type="button" className="rc-primary" title={d.url} onClick={() => postMessage("webview.open.external", { url: d.url })}>↗ 열기</button>}
      {spec.available !== false && <button type="button" onClick={onSpec}>명세</button>}
      <button type="button" onClick={onDeploy}>▶ {d ? "다시 배포" : "실행"}</button>
    </div>
  </div>;
};

export default ProjectSummary;
