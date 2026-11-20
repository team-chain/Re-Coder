/**
 * 배포 보드 (4.0.1) — Railway 프로젝트 캔버스처럼 평평한 카드. 프로젝트 카드를 대상 카드로 끌어다 놓으면 배포 확인 창이 열린다.
 *
 * 실사용 2026-11-19: 3D 입체 노드가 "프로덕션 프론트가 아니다". 끌어다 놓기는 유지.
 * 노드는 기존 deploymentScene 그대로 받는다(project · gate · docker · github · ecs · s3 · discord) — 동작(onActivate · onDrop)은 바뀌지 않는다.
 * 프로젝트 카드의 서비스 목록과 로컬 실행 상태는 명세(spec.load)에서 읽는다.
 */
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useVSCodeApi } from "../../hooks/useVSCodeApi";
import { BRAND_GLYPHS } from "./brandIcons";
import type { SceneNode, Target } from "./model";
import type { Spec } from "../SpecView";

export const BOARD_CSS = `
.fb{position:relative;min-height:520px;border:1px solid var(--fb-line);border-radius:12px;overflow:hidden;
  --fb-line:#262626;--fb-line2:#333;--fb-card:#111;--fb-fg:#ededed;--fb-fg2:#a1a1a1;--fb-fg3:#707070;--fb-ok:#0cce6b;--fb-bad:#ee4444;--fb-warn:#f5a623;
  --fb-bg:#0b0b0b;--fb-dot:#232323;--fb-hover:#444;--fb-idle:rgba(10,10,10,.6);
  background-color:var(--fb-bg);background-image:radial-gradient(var(--fb-dot) 1px,transparent 1px);background-size:16px 16px;color:var(--fb-fg);font-size:13.5px}
.fb *{box-sizing:border-box}
.fb button{font:inherit;color:inherit}
.fb-lines{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.fb-grid{position:relative;display:grid;grid-template-columns:minmax(260px,330px) 1fr minmax(260px,320px);align-items:center;gap:0;padding:40px 40px 64px;min-height:520px}
.fb-proj{border:1px solid var(--fb-line2);border-radius:10px;background:var(--fb-card);cursor:grab;user-select:none;transition:opacity .15s,transform .15s}
.fb-proj:active{cursor:grabbing}.fb-proj[aria-grabbed=true]{opacity:.35}
.fb-proj:focus-visible{outline:2px solid #3291ff;outline-offset:2px}
.fb-proj-h{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--fb-line)}
.fb-proj-h b{font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fb-proj-h small{color:var(--fb-fg3);font-size:12.5px;white-space:nowrap}
.fb-grip{color:var(--fb-fg3);flex:none}
.fb-svcs{display:grid;grid-template-columns:1fr 1fr;gap:6px;padding:8px}
.fb-svc{border:1px solid var(--fb-line);border-radius:6px;padding:7px 10px;min-width:0}
.fb-svc b{display:flex;align-items:center;gap:6px;font-weight:400;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fb-svc small{display:block;color:var(--fb-fg3);font:11.5px var(--rc-mono,ui-monospace,monospace);margin-top:1px}
.fb-svc.db b{color:var(--fb-fg2)}
.fb-d{width:7px;height:7px;border-radius:50%;flex:none;background:#555;display:inline-block}
.fb-d.ok{background:var(--fb-ok)}.fb-d.bad{background:var(--fb-bad)}.fb-d.warn{background:var(--fb-warn)}
.fb-mid{display:flex;justify-content:center;flex-direction:column;align-items:center;gap:10px}
.fb .fb-gate{all:unset;cursor:pointer;display:inline-flex;align-items:center;gap:7px;padding:5px 12px;border-radius:999px;background:var(--fb-card);border:1px solid var(--fb-line2);font-size:12.5px;color:var(--fb-fg2);position:relative;z-index:1}
.fb .fb-gate:hover{color:var(--fb-fg);border-color:var(--fb-hover)}
.fb .fb-chip{all:unset;cursor:pointer;display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--fb-fg3);position:relative;z-index:1}
.fb .fb-chip:hover{color:var(--fb-fg2)}
.fb-chip svg{width:14px;height:14px}
.fb-tgts{display:grid;gap:12px}
.fb .fb-tgt{all:unset;box-sizing:border-box;cursor:pointer;display:flex;align-items:center;gap:14px;padding:14px 16px;border:1px solid var(--fb-line2);border-radius:10px;background:var(--fb-card);transition:border-color .12s,background .12s;position:relative}
.fb .fb-tgt:hover{border-color:var(--fb-hover)}
.fb-tgt:focus-visible{outline:2px solid #3291ff;outline-offset:2px}
.fb-tgt.locked .fb-logo,.fb-tgt.locked .fb-t{opacity:.5}
.fb .fb-tgt.drop{border:1.5px dashed var(--c);background:color-mix(in srgb,var(--c) 8%,var(--fb-card))}
.fb .fb-tgt.over{border-style:solid;background:color-mix(in srgb,var(--c) 14%,var(--fb-card))}
.fb-logo{width:40px;height:40px;border-radius:8px;border:1px solid var(--fb-line);background:var(--fb-logo,#0b0b0b);display:grid;place-items:center;padding:8px;flex:none}
.fb-logo svg{width:100%;height:100%}
.fb-t{flex:1;min-width:0}
.fb-t b{display:block;font-weight:600;font-size:14px}
.fb-t small{display:flex;align-items:center;gap:6px;color:var(--fb-fg2);font-size:12.5px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fb-act{font-size:12.5px;padding:4px 10px;border:1px solid var(--fb-line2);border-radius:6px;color:var(--fb-fg)}
.fb-hint{position:absolute;right:16px;top:50%;transform:translateY(-50%);font-size:12px;font-weight:600;color:var(--c)}
.fb-foot{position:absolute;left:20px;right:20px;bottom:14px;display:flex;align-items:center;gap:16px;font-size:12.5px;color:var(--fb-fg3)}
.fb-idle{position:absolute;inset:0;display:grid;place-items:center;color:var(--fb-fg2);background:var(--fb-idle);z-index:3}
@media(max-width:860px){.fb-grid{grid-template-columns:1fr;gap:28px;padding:24px 20px 56px}.fb-lines{display:none}.fb-mid{flex-direction:row}}
body.vscode-light .fb{--fb-line:#e1e6ec;--fb-line2:#cfd7e0;--fb-card:#fff;--fb-fg:#0e1a26;--fb-fg2:#4e5d6e;--fb-fg3:#66768a;--fb-ok:#14945c;--fb-bad:#cf4343;--fb-warn:#b97f0c;
  --fb-bg:#f7f9fb;--fb-dot:#dde3ea;--fb-hover:#9fb0c2;--fb-logo:#f3f6f9;--fb-idle:rgba(247,249,251,.7)}
`;

const TARGETS: Array<{ id: Target; kind: string; label: string; color: string }> = [
  { id: "docker", kind: "docker", label: "로컬 Docker", color: "#0db7ed" },
  { id: "github", kind: "github", label: "GitHub", color: "var(--fb-fg)" },
  { id: "ecs", kind: "ecs", label: "AWS ECS", color: "#ff9900" },
  { id: "s3", kind: "s3", label: "AWS S3", color: "#7aa116" },
];

const Glyph: React.FC<{ kind: string }> = ({ kind }) => {
  const g = BRAND_GLYPHS[kind];
  if (!g) return null;
  const aws = g.viewBox === "0 0 64 64";
  return <svg viewBox={g.viewBox} aria-hidden>{g.paths.map((p, i) => <path key={i} d={p.d} fill={p.fill || "currentColor"} fillRule={aws ? "evenodd" : undefined} />)}</svg>;
};

const DEPLOY_LABEL: Record<string, string> = { success: "실행 중", degraded: "실행 중 · 검사 일부 실패", failed: "마지막 배포 실패", rolled_back: "이전 버전 실행 중" };

export const FlatBoard: React.FC<{
  nodes: SceneNode[]; busy: boolean; idleHint?: string;
  onActivate: (node: SceneNode) => void; onDrop: (target: Target) => void;
  initialSpec?: Spec | null;
}> = ({ nodes, busy, idleHint, onActivate, onDrop, initialSpec = null }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [spec, setSpec] = useState<Spec | null>(initialSpec);
  const [dragging, setDragging] = useState(false);
  const [over, setOver] = useState<Target | "">("");
  const req = useRef("");
  const project = nodes.find(n => n.id === "project");
  const gate = nodes.find(n => n.id === "gate");
  const discord = nodes.find(n => n.id === "discord");
  const workspaceKey = project?.name ?? "";
  useEffect(() => { if (initialSpec) return; req.current = `board-${Date.now()}`; postMessage("spec.load", { requestId: req.current }); }, [postMessage, workspaceKey, initialSpec]);
  useMessage(useCallback(({ type, payload }) => {
    const p = (payload ?? {}) as { requestId?: string; spec?: Spec };
    if (type === "spec.result" && p.requestId === req.current && p.spec) setSpec(p.spec);
  }, []));

  // 선 — 프로젝트 → 보안 검사 → 대상. 실제 카드 위치에서 그린다(창 크기가 바뀌어도 맞게).
  const root = useRef<HTMLDivElement>(null);
  const [paths, setPaths] = useState<Array<{ d: string; on: boolean; color: string }>>([]);
  useLayoutEffect(() => {
    const el = root.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const draw = () => {
      const box = el.getBoundingClientRect();
      const pr = el.querySelector(".fb-proj")?.getBoundingClientRect();
      const gr = el.querySelector(".fb-gate")?.getBoundingClientRect();
      if (!pr || !gr) return;
      const y = gr.top - box.top + gr.height / 2;
      const out: Array<{ d: string; on: boolean; color: string }> = [{ d: `M${pr.right - box.left},${pr.top - box.top + pr.height / 2} C${gr.left - box.left - 40},${pr.top - box.top + pr.height / 2} ${gr.left - box.left - 40},${y} ${gr.left - box.left},${y}`, on: false, color: "" }];
      el.querySelectorAll<HTMLElement>(".fb-tgt").forEach(t => {
        const r = t.getBoundingClientRect();
        const x2 = r.left - box.left, y2 = r.top - box.top + r.height / 2, x1 = gr.right - box.left;
        out.push({ d: `M${x1},${y} C${x1 + 60},${y} ${x2 - 60},${y2} ${x2},${y2}`, on: t.dataset.live === "1", color: t.dataset.color || "" });
      });
      setPaths(out);
    };
    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(el);
    return () => ro.disconnect();
  }, [nodes, spec]);

  const d = spec?.deploy;
  const live = d && ["success", "degraded", "rolled_back"].includes(d.status);
  const services = spec?.services ?? [];
  const dbs = spec?.databases ?? [];
  const status = (svc: string) => d?.services?.[svc] === "ok" ? "ok" : d?.services?.[svc] === "fail" ? "bad" : "";
  const gateTone = !gate ? "" : /차단/.test(gate.badge) ? "bad" : /확인|미확인|대기|중/.test(gate.badge) ? "warn" : "ok";

  const drag = {
    draggable: !busy,
    onDragStart: (e: React.DragEvent) => { e.dataTransfer.setData("text/plain", "recoder-project"); e.dataTransfer.effectAllowed = "move"; setDragging(true); },
    onDragEnd: () => { setDragging(false); setOver(""); },
  };

  return <div className="fb" ref={root} role="group" aria-label="프로젝트 배포 구조" data-testid="deploy-board">
    <style>{BOARD_CSS}</style>
    <svg className="fb-lines" aria-hidden>
      {paths.map((p, i) => <path key={i} d={p.d} fill="none" strokeWidth="1.5" style={{ stroke: p.on ? p.color : "var(--fb-line2)" }} strokeDasharray={i === 0 || p.on ? undefined : "5 5"} />)}
    </svg>
    <div className="fb-grid">
      <div>
        <div className="fb-proj" role="button" tabIndex={0} aria-grabbed={dragging} aria-label={`${project?.name ?? "프로젝트"} — 대상 카드로 끌어다 놓아 배포, 누르면 요약`} {...drag}
          onClick={() => project && onActivate(project)} onKeyDown={(e) => { if ((e.key === "Enter" || e.key === " ") && project) { e.preventDefault(); onActivate(project); } }}>
          <div className="fb-proj-h">
            <svg className="fb-grip" width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden><path d="M6 3.5a1 1 0 1 1-2 0 1 1 0 0 1 2 0zm6 0a1 1 0 1 1-2 0 1 1 0 0 1 2 0zM6 8a1 1 0 1 1-2 0 1 1 0 0 1 2 0zm6 0a1 1 0 1 1-2 0 1 1 0 0 1 2 0zm-6 4.5a1 1 0 1 1-2 0 1 1 0 0 1 2 0zm6 0a1 1 0 1 1-2 0 1 1 0 0 1 2 0z" /></svg>
            <b>{project?.name ?? "프로젝트"}</b><small>{project && project.subtitle !== "프로젝트 컨테이너" ? project.subtitle : ""}</small>
          </div>
          {services.length > 0 ? <div className="fb-svcs">
            {services.slice(0, 6).map(s => <div className="fb-svc" key={s.name}><b><i className={`fb-d ${status(s.name)}`} />{s.name}</b><small>{s.port ? `:${s.port}` : s.kind === "worker" ? "worker" : " "}</small></div>)}
            {dbs.slice(0, 2).map(x => <div className="fb-svc db" key={x.name}><b><i className={`fb-d ${status(x.name)}`} />{x.name}</b><small>{x.engine}</small></div>)}
            {services.length + dbs.length > 8 && <div className="fb-svc db"><b>+{services.length + dbs.length - 8}</b><small>&nbsp;</small></div>}
          </div> : <div style={{ padding: "14px", color: "var(--fb-fg3)", fontSize: 12.5 }}>단일 서비스</div>}
        </div>
      </div>
      <div className="fb-mid">
        {gate && <button type="button" className="fb-gate" onClick={() => onActivate(gate)} title={gate.subtitle}><i className={`fb-d ${gateTone}`} />{gate.badge === "검사 대기" ? "보안 검사" : gate.badge}</button>}
        {discord && <button type="button" className="fb-chip" onClick={() => onActivate(discord)}><Glyph kind="discord" />{discord.subtitle === "채널 연결" ? "Discord 알림" : discord.subtitle}</button>}
      </div>
      <div className="fb-tgts">
        {TARGETS.map(t => {
          const n = nodes.find(x => x.id === t.id);
          if (!n) return null;
          const locked = Boolean(n.locked);
          const isLive = t.id === "docker" && live;
          const sub = t.id === "docker"
            ? (d ? <><i className={`fb-d ${isLive ? "ok" : d.status === "failed" ? "bad" : ""}`} />{DEPLOY_LABEL[d.status] ?? d.status}</> : "내 PC 에서 실행")
            : locked ? "AWS 연결 필요" : n.badge;
          const canDrop = dragging && !locked;
          return <button key={t.id} type="button" className={`fb-tgt${locked ? " locked" : ""}${canDrop ? " drop" : ""}${over === t.id ? " over" : ""}`}
            style={{ "--c": t.color } as React.CSSProperties} data-live={isLive ? "1" : "0"} data-color={t.color}
            onClick={() => onActivate(n)}
            onDragOver={(e) => { if (!canDrop) return; e.preventDefault(); e.dataTransfer.dropEffect = "move"; setOver(t.id); }}
            onDragLeave={() => setOver(o => o === t.id ? "" : o)}
            onDrop={(e) => { e.preventDefault(); setDragging(false); setOver(""); if (!locked) onDrop(t.id); }}
            aria-label={`${t.label} — ${locked ? "AWS 연결 필요" : "여기에 프로젝트를 놓아 배포"}`}>
            <span className="fb-logo" style={{ color: t.color }}><Glyph kind={t.kind} /></span>
            <span className="fb-t"><b>{t.id === "github" && n.name !== "GitHub" ? n.name : t.label}</b><small>{sub}</small></span>
            {canDrop ? <span className="fb-hint">여기에 놓기</span> : locked ? <span className="fb-act">연결</span> : null}
          </button>;
        })}
      </div>
    </div>
    <div className="fb-foot"><span>프로젝트를 대상으로 끌어다 놓거나 대상을 누르면 배포 확인 창이 열려요</span></div>
    {busy && idleHint && <div className="fb-idle" role="status">{idleHint}</div>}
  </div>;
};

export default FlatBoard;
