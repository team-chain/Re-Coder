/**
 * 배포 캔버스 루트 — 3D 로 그릴 수 있으면 3D, 아니면 같은 모델을 2D 로.
 *
 * **이 컴포넌트는 아직 어디에도 붙어 있지 않다.** 2026-09-22 협의로 배포 화면
 * (`DeploymentCenter.tsx`)은 다른 사람이 고치는 중이라, 그 파일이 정리될 때까지
 * 캔버스는 독립 파일로만 만든다. 나중에 그 화면에서 이 컴포넌트를 마운트하는
 * 한 줄만 추가하면 된다 — 충돌 면적을 한 줄로 줄이는 것이 이 분리의 목적이다.
 *
 * three.js 는 **동적으로 불러온다.** 정적 import 로 두면 캔버스를 한 번도 열지
 * 않는 사용자도 번들 시작과 동시에 three 를 파싱해야 하고, WebGL 이 없는
 * 환경에서도 쓸모없이 로드된다. 렌더 테스트(node --test)가 three 를 건드리지
 * 않게 되는 것도 같은 이유에서 덤으로 따라온다.
 */
import React, { useEffect, useRef, useState } from 'react';
import type { CanvasModel, CanvasNode } from './model';
import { pickRenderMode, detectWebgl, type RenderMode } from './capability';
import { Canvas2DFallback } from './Canvas2DFallback';

export interface DeployCanvasProps {
  model: CanvasModel;
  /** 설정에서 2D 고정. 기본은 자동 판정. */
  forced2d?: boolean;
  onLockedClick?: (node: CanvasNode) => void;
  onDeploy?: (node: CanvasNode) => void;
}

type SceneHandle = {
  setModel: (m: CanvasModel) => void;
  resize: () => void;
  dispose: () => void;
};

export const DeployCanvas: React.FC<DeployCanvasProps> = ({
  model,
  forced2d,
  onLockedClick,
  onDeploy,
}) => {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const sceneRef = useRef<SceneHandle | null>(null);

  //: 첫 렌더에서는 '2d' 로 시작한다. WebGL 판정은 DOM 이 있어야 가능하고,
  //: 서버 렌더(테스트)에서는 document 가 없다. 잘못 3D 로 시작하면 빈 검은
  //: 박스가 한 프레임 보인다.
  const [mode, setMode] = useState<RenderMode>('2d');
  const [failReason, setFailReason] = useState<string | undefined>(undefined);

  useEffect(() => {
    setMode(pickRenderMode({ webgl: detectWebgl(), forced2d }));
  }, [forced2d]);

  useEffect(() => {
    if (mode !== '3d' || !canvasRef.current) return;
    let cancelled = false;
    const canvas = canvasRef.current;

    void (async () => {
      try {
        const mod = await import('./scene/SceneRenderer');
        if (cancelled) return;
        const scene = new mod.SceneRenderer(canvas);
        sceneRef.current = scene;
        scene.setModel(model);
      } catch (err) {
        //: 여기로 떨어지면 three 로드나 컨텍스트 생성이 실패한 것이다.
        //: 빈 캔버스를 보여 주는 대신 2D 로 내려가고, 사유를 화면에 남긴다.
        if (cancelled) return;
        setFailReason(err instanceof Error ? err.message : String(err));
        setMode('2d');
      }
    })();

    return () => {
      cancelled = true;
      sceneRef.current?.dispose();
      sceneRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  //: 모델이 바뀌면 씬에 반영. 씬을 다시 만들지 않는다.
  useEffect(() => {
    sceneRef.current?.setModel(model);
  }, [model]);

  useEffect(() => {
    if (mode !== '3d') return;
    const host = hostRef.current;
    if (!host || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => sceneRef.current?.resize());
    ro.observe(host);
    return () => ro.disconnect();
  }, [mode]);

  if (mode === '2d') {
    return (
      <Canvas2DFallback
        model={model}
        onLockedClick={onLockedClick}
        onDeploy={onDeploy}
        reason={failReason}
      />
    );
  }

  return (
    <div
      ref={hostRef}
      data-testid="deploy-canvas-3d"
      style={{ position: 'relative', width: '100%', height: '100%', minHeight: 320 }}
    >
      <canvas ref={canvasRef} style={{ display: 'block', width: '100%', height: '100%' }} />
    </div>
  );
};

export default DeployCanvas;
