/**
 * WebGL 이 없을 때의 배포 캔버스 — 같은 모델을 2D 로 그린다.
 *
 * "예쁘지 않은 대신 기능은 같다" 가 목표다. 3D 에서 할 수 있는 판단(어디에
 * 놓을 수 있나, 무엇이 잠겼나)이 여기서도 똑같이 보이고 똑같이 막혀야 한다.
 * 그래서 노드 목록·잠금 사유·드롭 가능 여부를 전부 `model.ts` 에서 받는다.
 */
import React from 'react';
import type { CanvasModel, CanvasNode } from './model';

const KIND_LABEL: Record<CanvasNode['kind'], string> = {
  project: '프로젝트',
  gate: '게이트',
  docker: 'Docker',
  github: 'GitHub',
  s3: 'S3',
  ecs: 'ECS',
};

const card: React.CSSProperties = {
  border: '1px solid var(--vscode-panel-border, #3a3d41)',
  borderRadius: 8,
  padding: '10px 12px',
  background: 'var(--vscode-editorWidget-background, #252526)',
  display: 'flex',
  alignItems: 'center',
  gap: 10,
};

export interface Canvas2DFallbackProps {
  model: CanvasModel;
  /** 잠긴 노드를 눌렀을 때. 3D 와 같은 콜백을 받는다. */
  onLockedClick?: (node: CanvasNode) => void;
  /** 드롭 대신 쓰는 경로 — 2D 에서는 드래그가 없으므로 버튼으로 같은 일을 한다. */
  onDeploy?: (node: CanvasNode) => void;
  reason?: string;
}

export const Canvas2DFallback: React.FC<Canvas2DFallbackProps> = ({
  model,
  onLockedClick,
  onDeploy,
  reason,
}) => {
  const targets = model.nodes.filter((n) => n.kind !== 'project' && n.kind !== 'gate');
  const project = model.nodes.find((n) => n.kind === 'project');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: 12 }}>
      <div style={{ fontSize: 11, color: 'var(--vscode-descriptionForeground, #9aa4ae)' }}>
        이 환경에서는 3D 보기를 쓸 수 없어 목록으로 표시합니다
        {reason ? ` — ${reason}` : ''}. 할 수 있는 일은 같습니다.
      </div>

      {project && (
        <div style={{ ...card, borderColor: 'var(--vscode-focusBorder, #3794ff)' }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 600, fontSize: 13 }}>{project.name}</div>
            <div style={{ fontSize: 11, color: 'var(--vscode-descriptionForeground, #9aa4ae)' }}>
              {project.sub}
            </div>
          </div>
        </div>
      )}

      <div style={{ fontSize: 11, color: 'var(--vscode-descriptionForeground, #9aa4ae)' }}>
        배포 대상
      </div>

      {targets.map((node) => {
        const locked = node.availability === 'locked';
        return (
          <div
            key={node.id}
            style={{ ...card, opacity: locked ? 0.6 : 1 }}
            data-node-id={node.id}
            data-availability={node.availability}
          >
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 600, fontSize: 13 }}>
                {locked ? '🔒 ' : ''}
                {node.name}
              </div>
              <div style={{ fontSize: 11, color: 'var(--vscode-descriptionForeground, #9aa4ae)' }}>
                {node.sub || KIND_LABEL[node.kind]}
              </div>
            </div>
            {locked ? (
              <button
                onClick={() => onLockedClick?.(node)}
                style={{
                  border: '1px solid var(--vscode-button-border, transparent)',
                  borderRadius: 4,
                  padding: '4px 10px',
                  fontSize: 11,
                  cursor: 'pointer',
                  background: 'var(--vscode-button-secondaryBackground, #3a3d41)',
                  color: 'var(--vscode-button-secondaryForeground, #fff)',
                }}
              >
                {node.lockedReason}
              </button>
            ) : (
              <button
                onClick={() => onDeploy?.(node)}
                disabled={!node.droppable}
                style={{
                  border: 'none',
                  borderRadius: 4,
                  padding: '4px 10px',
                  fontSize: 11,
                  cursor: node.droppable ? 'pointer' : 'default',
                  background: 'var(--vscode-button-background, #0e639c)',
                  color: 'var(--vscode-button-foreground, #fff)',
                }}
              >
                여기로 보내기
              </button>
            )}
          </div>
        );
      })}

      {model.account && (
        <div style={{ fontSize: 10.5, color: 'var(--vscode-descriptionForeground, #9aa4ae)' }}>
          AWS 연결됨 · {model.account}
          {model.region ? ` · ${model.region}` : ''}
        </div>
      )}
    </div>
  );
};

export default Canvas2DFallback;
