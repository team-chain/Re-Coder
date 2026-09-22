/**
 * 배포 캔버스의 데이터 모델 — "무엇이 있고, 지금 무엇을 할 수 있나".
 *
 * 왜 화면과 분리했나
 *   캔버스는 3D 로도 그리고(WebGL), 2D 로도 그린다(폴백). 두 렌더러가 같은
 *   판단을 각자 하면 반드시 갈라진다 — 3D 에서는 드롭이 되는데 2D 에서는
 *   안 되는 식으로. 그래서 "이 타겟에 놓을 수 있는가" 는 여기서 **한 번만**
 *   정하고, 렌더러는 그 결과를 그리기만 한다.
 *
 * 잠금 규칙 (2026-09-22 확정 5번)
 *   쓸 수 없는 타겟을 화면에서 **지우지 않는다.** 회색으로 남겨 두고 드롭만
 *   막는다. 지우면 "여기에 AWS 로 배포할 수 있다" 는 사실 자체를 처음 쓰는
 *   사람이 알 방법이 없어지고, ReCoder 에서 AWS 연결은 부가 설정이 아니라
 *   핵심 온보딩이라 그 진입점이 사라지면 안 된다.
 */

export type CanvasNodeKind =
  | 'project'
  | 'gate'
  | 'docker'
  | 'github'
  | 's3'
  | 'ecs';

/** 잠긴 노드를 눌렀을 때 어디로 보낼지. 화면이 이 값으로 분기한다. */
export type LockedAction = 'aws-connect' | 'docker-start' | 'github-connect';

export interface CanvasNode {
  id: string;
  kind: CanvasNodeKind;
  /**
   * 화면에 뜨는 이름. 연결돼 있으면 **내 계정의 실제 리소스 이름**을 쓴다
   * (`recoder-cluster`, `recoder-site-ldk511`). 일반명(`ECS`, `S3`)은 아직
   * 연결되지 않아 이름을 모를 때만.
   */
  name: string;
  sub?: string;
  /** ready = 색이 들어오고 드롭 가능 / locked = 회색, 드롭 불가 */
  availability: 'ready' | 'locked';
  lockedReason?: string;
  lockedAction?: LockedAction;
  /**
   * 드롭 대상이 될 수 있는가. `availability === 'ready'` 와 항상 같지는 않다 —
   * 게이트·프로젝트는 준비돼 있어도 드롭 대상이 아니다.
   */
  droppable: boolean;
}

export interface CanvasEdge {
  from: string;
  to: string;
  kind: 'gate' | 'deploy' | 'push';
}

export interface CanvasModel {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  /** 상단 칩에 쓰는 연결 정보. 미연결이면 undefined. */
  account?: string;
  region?: string;
}

/** 캔버스가 모델을 만들 때 참고하는 현재 상태. 전부 기존 상태 API 에서 온다. */
export interface CanvasState {
  projectName: string;
  imageTag?: string;
  docker: { ready: boolean };
  github: { connected: boolean; repo?: string; branch?: string };
  aws: {
    connected: boolean;
    account?: string;
    region?: string;
    bucket?: string;
    cluster?: string;
    service?: string;
  };
}

const AWS_LOCK = 'AWS 연결 필요';

/**
 * 상태 → 캔버스 모델. **순수 함수다.** three.js 도 DOM 도 모른다.
 *
 * 엣지는 `ready` 노드로만 그린다. 잠긴 타겟까지 선을 그으면 "연결돼 있다"는
 * 인상을 주는데, 실제로는 드롭이 막혀 있어 화면이 거짓말을 하게 된다.
 */
export function buildCanvasModel(state: CanvasState): CanvasModel {
  const { aws, docker, github } = state;

  const project: CanvasNode = {
    id: 'project',
    kind: 'project',
    name: state.projectName,
    sub: state.imageTag ? `프로젝트 · ${state.imageTag}` : '프로젝트',
    availability: 'ready',
    droppable: false,
  };

  const gate: CanvasNode = {
    id: 'gate',
    kind: 'gate',
    name: '보안 게이트',
    sub: 'trivy · hadolint · gitleaks · OPA',
    availability: 'ready',
    droppable: false,
  };

  const dockerNode: CanvasNode = docker.ready
    ? {
        id: 'docker',
        kind: 'docker',
        name: 'Docker',
        sub: state.imageTag ? `${state.imageTag} · 로컬` : '로컬 컨테이너',
        availability: 'ready',
        droppable: true,
      }
    : {
        id: 'docker',
        kind: 'docker',
        name: 'Docker',
        sub: '로컬 컨테이너',
        availability: 'locked',
        lockedReason: 'Docker 데몬 꺼짐',
        lockedAction: 'docker-start',
        droppable: false,
      };

  const githubNode: CanvasNode = github.connected
    ? {
        id: 'github',
        kind: 'github',
        name: github.repo || 'GitHub',
        sub: github.branch ? `GitHub · ${github.branch}` : 'GitHub',
        availability: 'ready',
        droppable: true,
      }
    : {
        id: 'github',
        kind: 'github',
        name: 'GitHub',
        sub: '저장소 미연결',
        availability: 'locked',
        lockedReason: 'GitHub 연결 필요',
        lockedAction: 'github-connect',
        droppable: false,
      };

  const s3: CanvasNode = aws.connected
    ? {
        id: 's3',
        kind: 's3',
        name: aws.bucket || 'S3',
        sub: aws.region ? `S3 · ${aws.region}` : 'S3',
        availability: 'ready',
        droppable: true,
      }
    : {
        id: 's3',
        kind: 's3',
        name: 'S3',
        sub: '정적 사이트 호스팅',
        availability: 'locked',
        lockedReason: AWS_LOCK,
        lockedAction: 'aws-connect',
        droppable: false,
      };

  const ecs: CanvasNode = aws.connected
    ? {
        id: 'ecs',
        kind: 'ecs',
        name: aws.cluster || 'ECS',
        sub: [aws.service, aws.region ? `ECS Fargate · ${aws.region}` : 'ECS Fargate']
          .filter(Boolean)
          .join(' · '),
        availability: 'ready',
        droppable: true,
      }
    : {
        id: 'ecs',
        kind: 'ecs',
        name: 'ECS',
        sub: 'ECS Fargate',
        availability: 'locked',
        lockedReason: AWS_LOCK,
        lockedAction: 'aws-connect',
        droppable: false,
      };

  const nodes = [project, gate, dockerNode, githubNode, s3, ecs];

  const edges: CanvasEdge[] = [{ from: 'project', to: 'gate', kind: 'gate' }];
  for (const target of [dockerNode, githubNode, s3, ecs]) {
    if (target.availability !== 'ready') continue;
    edges.push({
      from: 'gate',
      to: target.id,
      kind: target.kind === 'github' ? 'push' : 'deploy',
    });
  }

  return {
    nodes,
    edges,
    account: aws.connected ? aws.account : undefined,
    region: aws.connected ? aws.region : undefined,
  };
}

/** 드롭 대상 후보. 렌더러가 링을 띄울 노드를 고를 때 쓴다. */
export function droppableTargets(model: CanvasModel): CanvasNode[] {
  return model.nodes.filter((n) => n.droppable);
}
