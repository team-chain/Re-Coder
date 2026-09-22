/**
 * 배포 캔버스 공개 표면. 바깥에서는 여기서만 가져다 쓴다.
 *
 * 배포 화면에 붙일 때는 이 한 줄이면 된다:
 *   import { DeployCanvas, buildCanvasModel } from './canvas';
 */
export { DeployCanvas } from './DeployCanvas';
export { Canvas2DFallback } from './Canvas2DFallback';
export { buildCanvasModel, droppableTargets } from './model';
export { pickRenderMode, detectWebgl } from './capability';
export type {
  CanvasModel,
  CanvasNode,
  CanvasEdge,
  CanvasState,
  CanvasNodeKind,
  LockedAction,
} from './model';
export type { RenderMode, RenderCapability } from './capability';
