/**
 * 프로젝트 요약 (4.0.1) — 캔버스의 프로젝트 노드를 누르면 열린다.
 * 예전에는 파일 그래프가 떴다(실사용 2026-11-19: "쓸데없다"). Vercel 배포 상세처럼
 * 상태 · 시간 · 주소를 키-값 표로, 서비스와 명세 요약을 목록으로 보여 준다. 이모티콘 · 큰 색 숫자는 쓰지 않는다.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useVSCodeApi } from "../../hooks/useVSCodeApi";
import type { Spec } from "../SpecView";

export type SpecWithDeploy = Spec;

export const SUMMARY_CSS = `
.rc-psum{--ps-line:var(--rc-line2,rgba(160,190,220,.12));--ps-mute:var(--rc-fg3,#6e7b8b);--ps-fg2:var(--rc-fg2,#9aa8b8);display:flex;flex-direction:column;gap:20px;font-size:13px;line-height:1.45}
.rc-psum h3{margin:0;font-size:15px;font-weight:600}
.rc-psum dl{margin:0;display:grid;grid-template-columns:72px minmax(0,1fr);gap:8px 12px}
.rc-psum dt{color:var(--ps-mute);font-size:12px}
.rc-psum dd{margin:0;display:flex;align-items:center;gap:8px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rc-psum .dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--ps-mute)}
.rc-psum .dot.ok{background:#3fb950}.rc-psum .dot.fail{background:#f85149}.rc-psum .dot.warn{background:#d29922}
.rc-psum .sec{font-size:12px;font-weight:600;color:var(--ps-fg2);margin:0 0 8px}
.rc-psum .list{border:1px solid var(--ps-line);border-radius:6px}
.rc-psum .row{all:unset;box-sizing:border-box;display:flex;align-items:center;gap:10px;width:100%;padding:7px 12px;border-top:1px solid var(--ps-line)}
.rc-psum .row:first-child{border-top:0}
.rc-psum button.row{cursor:pointer}.rc-psum button.row:hover{background:rgba(148,178,210,.045)}
.rc-psum .row .k{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rc-psum .row .v{color:var(--ps-fg2);font-variant-numeric:tabular-nums}
.rc-psum .row .v.bad{color:#f85149}
.rc-psum .cols{display:grid;grid-template-columns:1fr 1fr}
.rc-psum .cols .row:nth-child(2){border-top:0}
.rc-psum .cols .row:nth-child(odd){border-right:1px solid var(--ps-line)}
.rc-psum .acts{display:flex;gap:8px}
.rc-psum .b{all:unset;box-sizing:border-box;cursor:pointer;height:32px;padding:0 14px;display:inline-flex;align-items:center;justify-content:center;border-radius:6px;border:1px solid var(--ps-line);background:rgba(148,178,210,.05);font-size:13px;font-weight:500;color:var(--rc-fg,#e6edf3)}
.rc-psum .b:hover{background:rgba(148,178,210,.1)}
.rc-psum .b.pri{background:var(--rc-fg,#e6edf3);color:var(--rc-solid,#0d1117);border-color:transparent}
.rc-psum .b.pri:hover{opacity:.9}
.rc-psum a{color:var(--rc-teal,#2f81f7);text-decoration:none;font-family:var(--vscode-editor-font-family,ui-monospace,monospace);font-size:12.5px}
.rc-psum a:hover{text-decoration:underline}
`;

function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
const DEPLOY_LABEL: Record<string, string> = { success: "실행 중", degraded: "실행 중 (일부 검사 실패)", failed: "실패", rolled_back: "이전 버전으로 되돌림" };

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
    if (type === "spec.error") setError(p.message || "불러오지 못했습니다.");
  }, []));

  if (!spec) return <div className="rc-psum"><style>{SUMMARY_CSS}</style><p className="rc-muted">{error || "불러오는 중"}</p></div>;
  const d = spec.deploy;
  const tone = !d ? "" : d.status === "success" ? "ok" : d.status === "failed" ? "fail" : "warn";
  const t = spec.tests ?? { passed: 0, total: 0, at: "", status: "" };
  const features = (spec.features ?? []).filter(f => !f.internal);
  const svcNames = [...(spec.services ?? []).map(s => s.name), ...(spec.databases ?? []).map(x => x.name)];
  return <div className="rc-psum" data-testid="project-summary">
    <style>{SUMMARY_CSS}</style>
    <h3>{spec.project}</h3>
    <dl>
      <dt>상태</dt><dd title={d?.failures.join("\n") || undefined}><span className={`dot ${tone}`} />{d ? DEPLOY_LABEL[d.status] || d.status : "실행 전"}</dd>
      {d && <><dt>실행 시각</dt><dd>{when(d.at)}</dd></>}
      {d?.url && <><dt>주소</dt><dd><a href={d.url} onClick={e => { e.preventDefault(); postMessage("webview.open.external", { url: d.url }); }}>{d.url.replace(/^https?:\/\//, "")}</a></dd></>}
      {branch && <><dt>브랜치</dt><dd>{branch}</dd></>}
    </dl>
    {spec.available === false ? <p className="rc-muted">{spec.reason}</p> : <>
      {svcNames.length > 0 && <div>
        <p className="sec">서비스 {svcNames.length}</p>
        <div className="list cols" aria-label="서비스 상태">
          {svcNames.map(n => { const s = d?.services?.[n] ?? "unknown"; return <div className="row" key={n} title={s === "ok" ? "실행 중" : s === "fail" ? "시작 못 함" : "확인 전"}><i className={`dot ${s}`} /><span className="k">{n}</span></div>; })}
        </div>
      </div>}
      <div>
        <p className="sec">명세</p>
        <div className="list">
          <button type="button" className="row" onClick={onSpec}><span className="k">기능</span><span className="v">{features.length}</span></button>
          <button type="button" className="row" onClick={onSpec}><span className="k">테스트</span><span className={`v${t.total && t.passed < t.total ? " bad" : ""}`}>{t.total ? `${t.passed}/${t.total}` : "실행 전"}</span></button>
          <button type="button" className="row" onClick={onSpec}><span className="k">남은 작업</span><span className="v">{spec.tasks?.length ?? 0}</span></button>
        </div>
      </div>
    </>}
    <div className="acts">
      {d?.url && <button type="button" className="b pri" onClick={() => postMessage("webview.open.external", { url: d.url })}>사이트 열기</button>}
      {spec.available !== false && <button type="button" className="b" onClick={onSpec}>명세</button>}
      <button type="button" className="b" onClick={onDeploy}>{d ? "다시 배포" : "로컬 실행"}</button>
    </div>
  </div>;
};

export default ProjectSummary;
