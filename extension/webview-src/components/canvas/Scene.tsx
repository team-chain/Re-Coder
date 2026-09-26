import React, { useEffect, useId, useRef, useState } from "react";
import * as THREE from "three";
import { colors, dropTargetAt, SceneEdge, SceneNode, Target } from "./model";
import { createArchitectureScene } from './threeScene';

// Inline paths: never load textures, fonts, or logos from the network in a webview.
const icons: Record<string, string> = {
  project: "M9 4H7a2 2 0 0 0-2 2v3l-2 3 2 3v3a2 2 0 0 0 2 2h2M15 4h2a2 2 0 0 1 2 2v3l2 3-2 3v3a2 2 0 0 1-2 2h-2",
  github: "M12 2a10 10 0 0 0-3.2 19.5v-2.7c-2.7.6-3.3-1.1-3.3-1.1-.5-1.3-1.2-1.6-1.2-1.6 1-.7 1.9 1 1.9 1 .9 1.5 2.5 1.1 3 .8.1-.7.4-1.2.7-1.5-2.2-.3-4.5-1.1-4.5-4.9 0-1.1.4-2 1-2.7-.1-.3-.4-1.3.1-2.7 0 0 .9-.3 2.8 1a9.8 9.8 0 0 1 5.1 0c1.9-1.3 2.8-1 2.8-1 .5 1.4.2 2.4.1 2.7.6.7 1 1.6 1 2.7 0 3.8-2.3 4.6-4.5 4.9.4.4.7 1 .7 2v3.1A10 10 0 0 0 12 2Z",
  docker: "M2 12h17c2 0 3-1 3-3-1 0-2 0-3 1-1-1-1-2-1-3-2 1-2 3-1 4H2v3c0 5 5 6 8 5 5-1 7-4 8-7M4 10V7h3v3M8 10V7h3v3M12 10V7h3v3M8 6V3h3v3",
  discord: "M7 4 3 6 1 18l5 3 2-3h8l2 3 5-3-2-12-4-2-1 2H8L7 4ZM7 11v3M17 11v3M7 17c3 2 7 2 10 0",
  folder: "M2 6h8l2 3h10v11H2V6Z", file: "M6 2h8l5 5v15H6V2Zm8 0v6h5M9 12h7M9 16h7",
  fn: "M16 3h-3c-3 0-3 3-3 6l-2 10c0 2-1 3-4 2M6 9h11",
};
export function Logo({ kind }: { kind: string }) {
  return <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true">{icons[kind] ? <path d={icons[kind]} fill={kind === "github" ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" /> : <text x="12" y="16" fontSize={kind === "ecs" ? 10 : 13} fontWeight="800" fill="currentColor" textAnchor="middle">{kind === "gate" ? "◇" : kind.toUpperCase()}</text>}</svg>;
}
export const slabPoints = (n: SceneNode, bottom = false): Array<[number, number]> => {
  const y = n.y + (bottom ? 29 : 0);
  return [[n.x, y - 25], [n.x + 64, y], [n.x, y + 28], [n.x - 64, y]];
};
const points = (values: Array<[number, number]>) => values.map(p => p.join(",")).join(" ");
function SvgSlab({ node }: { node: SceneNode }) {
  const c = node.locked ? colors.locked : node.color, p = slabPoints(node), b = slabPoints(node, true);
  return <g><ellipse cx={node.x} cy={node.y + 36} rx="79" ry="30" fill={c} opacity=".08" /><polygon points={points([p[3], p[2], b[2], b[3]])} fill={c} opacity=".36" /><polygon points={points([p[2], p[1], b[1], b[2]])} fill={c} opacity=".55" /><polygon points={points(p)} fill={c} fillOpacity=".26" stroke={c} strokeOpacity=".8" /></g>;
}

function curve(a: SceneNode, b: SceneNode) { return `M${a.x},${a.y} C${a.x},${a.y - 90} ${b.x},${b.y - 90} ${b.x},${b.y}`; }

/** Same screen coordinates for WebGL and SVG; loss of context never removes controls. */
function WebGLGround({ nodes, edges, height, onAvailable }: { nodes: SceneNode[]; edges: SceneEdge[]; height: number; onAvailable: (ok: boolean) => void }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    let renderer: THREE.WebGLRenderer | undefined;
    const architecture = createArchitectureScene(nodes, edges, height);
    let contextLost = false;
    const draw = () => {
      if (!renderer || contextLost) return;
      try {
        const width = canvas.clientWidth;
        if (!width) return;
        renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
        renderer.setSize(width, width * height / 1120, false);
        renderer.render(architecture.scene, architecture.camera);
        onAvailable(true);
      } catch { onAvailable(false); }
    };
    const lost = (event: Event) => { event.preventDefault(); contextLost = true; onAvailable(false); };
    const restored = () => { contextLost = false; draw(); };
    const resize = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(draw);
    try {
      renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      draw();
      canvas.addEventListener("webglcontextlost", lost);
      canvas.addEventListener("webglcontextrestored", restored);
      resize?.observe(canvas);
      window.addEventListener('resize', draw);
    } catch { onAvailable(false); }
    return () => {
      resize?.disconnect(); window.removeEventListener('resize', draw);
      canvas.removeEventListener("webglcontextlost",lost); canvas.removeEventListener("webglcontextrestored",restored);
      architecture.dispose(); renderer?.dispose(); onAvailable(false);
    };
  }, [nodes, edges, height, onAvailable]);
  return <canvas ref={ref} aria-hidden="true" className="rc-ground" />;
}

export function Scene({ nodes, edges, busy, onActivate, onDrop, force2D = false, showDetails = false, dragPrimary = false, fitHeight, idleHint }: { nodes: SceneNode[]; edges: SceneEdge[]; busy: boolean; onActivate: (node: SceneNode) => void; onDrop: (target: Target) => void; force2D?: boolean; showDetails?: boolean; dragPrimary?: boolean; fitHeight?: number; idleHint?: string }) {
  const [webgl, setWebgl] = useState(false), [zoom, setZoom] = useState(1);
  const render2D = force2D || nodes.length > 80;
  const [drag, setDrag] = useState<{ x: number; y: number; over: string } | null>(null);
  const gesture = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const suppress = useRef(false), svg = useRef<SVGSVGElement>(null);
  const id = useId().replace(/:/g, ""), height = Math.max(680, ...nodes.map(n => n.y + 100));
  // Navigation changes the graph, not its scale. Tall source graphs scroll at the
  // overview's scale instead of resizing every time a file or folder is opened.
  const referenceHeight = fitHeight ?? height;
  const source = nodes.find(n => n.id === 'project');
  const overNode = nodes.find(n => n.target && n.target === drag?.over);
  const point = (e: React.PointerEvent) => {
    const matrix = svg.current?.getScreenCTM();
    return matrix ? new DOMPoint(e.clientX,e.clientY).matrixTransform(matrix.inverse()) : null;
  };
  const cancelDrag = () => { if(gesture.current) suppress.current = gesture.current.moved; gesture.current = null; setDrag(null); };
  function move(e: React.PointerEvent) {
    if (!gesture.current || !svg.current) return;
    if (Math.hypot(e.clientX - gesture.current.x, e.clientY - gesture.current.y) > 6) gesture.current.moved = true;
    if (!gesture.current.moved) return;
    const p = point(e); if (!p) return;
    const over = dropTargetAt(nodes,p.x,p.y)?.target || '';
    setDrag({ x: p.x, y: p.y, over });
  }
  function release(e: React.PointerEvent) {
    if (!gesture.current) return;
    suppress.current = gesture.current.moved;
    const p = point(e);
    const target = p ? dropTargetAt(nodes,p.x,p.y) : undefined;
    if (gesture.current.moved && target?.target && !target.locked && !busy) onDrop(target.target);
    gesture.current = null; setDrag(null);
  }
  return <div data-renderer={webgl && !render2D ? 'three' : 'svg'} className={`rc-scene-shell ${showDetails ? 'rc-show-details' : ''} ${dragPrimary ? 'rc-drag-primary' : ''} ${drag ? 'rc-dragging' : ''}`} onKeyDown={e=>{if(e.key==='Escape') cancelDrag();}}>
    {(!dragPrimary || showDetails) && <div className="rc-zoom"><button onClick={() => setZoom(z => Math.max(.7,z-.15))} aria-label="축소">−</button><button onClick={() => setZoom(1)}>맞춤</button><button onClick={() => setZoom(z => Math.min(2,z+.15))} aria-label="확대">+</button><span>{render2D || !webgl ? "2D" : "3D"}</span></div>}
    <div className="rc-scene-scroll"><div className="rc-scene" style={{ width: `min(${zoom * 100}%, calc(var(--rc-scene-height) * ${1120/referenceHeight} * ${zoom}))`, aspectRatio: `1120 / ${height}` }}>
      {!render2D && <WebGLGround nodes={nodes} edges={edges} height={height} onAvailable={setWebgl} />}
      <svg ref={svg} viewBox={`0 0 1120 ${height}`} className="rc-svg" aria-label="프로젝트 배포 구조" onPointerMove={move} onPointerUp={release} onPointerCancel={cancelDrag} onLostPointerCapture={cancelDrag}>
        <defs><pattern id={`${id}-grid`} width="60" height="30" patternUnits="userSpaceOnUse"><path d="M0 0 60 30M60 0 0 30" stroke="#5b99bf" strokeOpacity=".05" strokeWidth=".6" /></pattern><marker id={`${id}-arrow`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="4" markerHeight="4" orient="auto"><path d="M0 0 10 5 0 10" fill="#7db9d4" /></marker></defs>
        <rect width="1120" height={height} fill={`url(#${id}-grid)`} />
        {(!webgl || render2D) && edges.map((e,i) => { const a=nodes.find(n=>n.id===e.from),b=nodes.find(n=>n.id===e.to); return a&&b ? <g key={`${e.from}-${e.to}-${i}`} style={{ pointerEvents:"none" }}><path d={curve(a,b)} fill="none" stroke={e.color} strokeOpacity=".10" strokeWidth="9" /><path d={curve(a,b)} fill="none" stroke={e.color} strokeOpacity=".65" strokeWidth="2" strokeDasharray={e.dashed ? "7 5" : undefined} markerEnd={`url(#${id}-arrow)`} /></g> : null; })}
        {drag && source && <path d={curve(source,{...source,x:drag.x,y:drag.y})} fill="none" stroke={overNode?.locked?colors.bad:colors.project} strokeWidth="3" strokeDasharray="7 5" opacity=".8" pointerEvents="none"/>}
        {nodes.map(n => <g key={n.id} data-node={n.id} data-dropping={drag?.over===n.target && Boolean(n.target) ? (n.locked?'locked':'ready') : undefined}>
          {(!webgl || render2D) && <SvgSlab node={n} />}
          {drag && n.target && !n.locked && !busy && <ellipse className="rc-drop-ring" cx={n.x} cy={n.y+20} rx={drag.over===n.target ? 87 : 76} ry="37" fill={drag.over===n.target ? "#81c5f52a" : "none"} stroke="#aad8ff" strokeWidth={drag.over===n.target ? 4 : 2} />}
          {n.kind === "gate" && (!webgl || render2D) ? <g style={{ pointerEvents:"none" }}><path d={`M${n.x} ${n.y-48} L${n.x+32} ${n.y-15} L${n.x} ${n.y} L${n.x-32} ${n.y-15}Z`} fill={n.color} opacity=".8" /><ellipse cx={n.x} cy={n.y-10} rx="57" ry="22" fill="none" stroke={n.color} /></g> : null}
          <foreignObject x={n.x-83} y={n.y-146} width="166" height="184" style={{ overflow:"visible" }}>
            <button className={`rc-node ${n.locked ? "rc-locked" : ""} ${n.id==='project'?'rc-project-node':''} ${n.kind==='gate'||n.flags?.length?'rc-node-attention':''}`} style={{ "--node-color": n.locked ? colors.locked : n.color } as React.CSSProperties}
              data-canvas-target={n.target} aria-label={`${n.name} · ${n.badge}`} title={`${n.name}\n${n.subtitle}\n${n.badge}`} aria-disabled={n.locked || undefined}
              onClick={() => { if (suppress.current) { suppress.current=false; return; } onActivate(n); }}
              onPointerDown={e => { suppress.current=false; if(n.id!=="project" || busy || e.button!==0) return; gesture.current={x:e.clientX,y:e.clientY,moved:false}; e.currentTarget.setPointerCapture(e.pointerId); }}>
              <span className="rc-node-label"><strong title={n.name}>{n.name}</strong><small title={n.subtitle}>{n.subtitle}</small><em>{n.locked ? "🔒 " : ""}{n.badge}</em></span>
              <span className={`rc-plate ${n.kind==="gate" ? "rc-gate-plate" : ""}`}><Logo kind={n.kind} /><small>{n.kind === "project" ? "PROJECT" : n.kind.toUpperCase()}</small></span>
            </button>
          </foreignObject>
        </g>)}
        {drag && <g transform={`translate(${drag.x},${drag.y})`} style={{ pointerEvents:"none" }}><rect x="-38" y="-38" width="76" height="62" rx="9" stroke="#a8d8ff" fill="#132a40" fillOpacity=".9" /><text fill="#a8d8ff" textAnchor="middle" y="-4" fontSize="22">{ "{ }" }</text><text fill="white" textAnchor="middle" y="15" fontSize="10">프로젝트</text></g>}
      </svg>
    </div></div>
    {dragPrimary && <div className="rc-drag-hint" role="status">{drag ? overNode?.locked ? 'AWS 연결이 필요한 대상입니다' : overNode ? `${overNode.name}에 놓기` : '배포할 대상 위에 놓으세요 · Esc 취소' : idleHint || '프로젝트를 끌어 배포할 대상에 놓으세요'}</div>}
    <div className="rc-mobile-nodes">{nodes.map(n=><button key={n.id} className={n.locked?'rc-locked':undefined} title={`${n.name} · ${n.badge}`} aria-label={`${n.name} · ${n.badge}`} onClick={()=>onActivate(n)}><Logo kind={n.kind}/><span><b>{n.name}</b><small>{n.locked ? "AWS 연결 필요" : n.badge}</small></span></button>)}</div>
  </div>;
}
