/**
 * 배포 캔버스의 three.js 씬 — 생성 · 모델 반영 · 리사이즈 · 파기.
 *
 * 이 파일은 **React 를 모른다.** 캔버스 엘리먼트 하나를 받아서 그 위에 그리고,
 * `dispose()` 로 전부 되돌린다. 그래야 렌더러를 바꾸거나(2D 폴백) 테스트에서
 * 떼어 내도 React 쪽이 흔들리지 않는다.
 *
 * 시점은 **직교(Orthographic) 아이소메트릭**이다. 원근을 쓰면 같은 크기의
 * 노드가 위치에 따라 달라 보여서, 뒤쪽 타겟이 작아지고 "덜 중요해 보이는"
 * 착시가 생긴다. ArgoCD 계열 도구가 전부 직교를 쓰는 이유도 같다.
 *
 * 노드는 슬래브 + 테두리 + 로고 플레이트로, 연결은 위로 솟는 아크로 그린다.
 * 잠긴 노드는 회색 + 자물쇠이고, 잠긴 노드로 가는 아크는 아예 만들지 않는다
 * (선이 있으면 "연결돼 있다"로 읽힌다 — 판단은 `model.ts` 에서 이미 끝났다).
 */
import * as THREE from 'three';
import type { CanvasModel, CanvasNode } from '../model';
import { slab, rimEdges, floorGlow, disposeObject } from './primitives';
import { logoPlate } from './plates';
import { arcCurve, arcMeshes, EDGE_COLOR } from './arcs';

/** 노드 종류별 고정 자리. 배치가 매번 바뀌면 사용자가 위치를 못 외운다. */
const LAYOUT: Record<CanvasNode['kind'], { x: number; z: number }> = {
  project: { x: -13.5, z: 2 },
  gate: { x: -4.5, z: -0.5 },
  docker: { x: -1, z: 11.5 },
  github: { x: 0.5, z: -9.5 },
  s3: { x: 17.5, z: -6.5 },
  ecs: { x: 11, z: 6.5 },
};

const COLOR: Record<CanvasNode['kind'], number> = {
  project: 0x1e3f68,
  gate: 0x2ea043,
  docker: 0x2496ed,
  github: 0xb48ead,
  s3: 0xf0b35b,
  ecs: 0x2ea043,
};

/** 잠긴 노드는 종류와 무관하게 같은 회색. "지금은 못 쓴다" 하나만 말한다. */
const LOCKED_BODY = 0x272b33;
const LOCKED_EDGE = 0x565e69;

const CAMERA_ANGLE = Math.PI * 0.3;
const CAMERA_DISTANCE = 30;
/** 노드 한 개가 차지하는 반경(슬래브 + 이름표). 프레이밍 여백 계산에 쓴다. */
const NODE_RADIUS = 2.8;
/** 가장자리 여백 비율. 노드가 화면 끝에 붙으면 잘린 것처럼 보인다. */
const FRAME_MARGIN = 1.12;

export class SceneRenderer {
  private renderer: THREE.WebGLRenderer;
  private scene: THREE.Scene;
  private camera: THREE.OrthographicCamera;
  private nodeRoot: THREE.Group;
  private disposed = false;

  constructor(private canvas: HTMLCanvasElement, private doc?: Document) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.15;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0b0d11);
    this.scene.fog = new THREE.Fog(0x0b0d11, 44, 110);

    this.camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 300);

    this.scene.add(new THREE.AmbientLight(0x8fa4c4, 1.05));
    const key = new THREE.DirectionalLight(0xffffff, 1.9);
    key.position.set(14, 24, 10);
    this.scene.add(key);
    const rim = new THREE.DirectionalLight(0x4f86e0, 1.0);
    rim.position.set(-16, 9, -8);
    this.scene.add(rim);

    const grid = new THREE.GridHelper(140, 140, 0x1d2734, 0x141a22);
    grid.position.y = -0.03;
    this.scene.add(grid);

    this.nodeRoot = new THREE.Group();
    this.scene.add(this.nodeRoot);

    this.resize();
  }

  /**
   * 모델을 씬에 반영한다. 매번 노드를 **다 지우고 다시 만든다.**
   * 노드가 열 개 남짓이라 부분 갱신의 복잡도가 이득보다 크다.
   */
  setModel(model: CanvasModel): void {
    if (this.disposed) return;
    disposeObject(this.nodeRoot);
    this.nodeRoot.clear();

    //: 아크가 노드 표면에서 나가도록, 노드마다 윗면 높이를 기억해 둔다.
    const top = new Map<string, THREE.Vector3>();

    for (const node of model.nodes) {
      const at = LAYOUT[node.kind];
      if (!at) continue;
      const locked = node.availability === 'locked';
      const accent = locked ? LOCKED_EDGE : COLOR[node.kind];
      const isProject = node.kind === 'project';
      const height = isProject ? 1.9 : 1.0;

      const s = slab(
        isProject ? 4.0 : 3.2,
        isProject ? 3.3 : 2.7,
        height,
        locked ? LOCKED_BODY : COLOR[node.kind],
        locked ? 0.08 : 0.2,
      );
      s.mesh.position.set(at.x, 0, at.z);
      this.nodeRoot.add(s.mesh);

      const edge = rimEdges(s.geometry, accent, locked ? 0.7 : 0.92);
      edge.position.set(at.x, 0, at.z);
      this.nodeRoot.add(edge);

      //: 잠긴 노드에는 발광을 주지 않는다 — 빛나면 "쓸 수 있다" 로 읽힌다.
      if (!locked) {
        const glow = floorGlow(2.5, COLOR[node.kind], 0.07);
        glow.position.set(at.x, 0.012, at.z);
        this.nodeRoot.add(glow);
      }

      //: 게이트는 이름표를 따로 두지 않는다 — 다음 카드에서 관문 형태로 그린다.
      if (node.kind !== 'gate') {
        const plate = logoPlate(
          {
            kind: node.kind,
            sub: node.name,
            color: locked ? '#6e7781' : `#${COLOR[node.kind].toString(16).padStart(6, '0')}`,
            locked,
            scale: isProject ? 2.5 : 2.3,
          },
          this.doc,
        );
        plate.position.set(at.x, height + (isProject ? 1.4 : 1.25), at.z);
        this.nodeRoot.add(plate);
      }

      top.set(node.id, new THREE.Vector3(at.x, height, at.z));
    }

    for (const e of model.edges) {
      const from = top.get(e.from);
      const to = top.get(e.to);
      if (!from || !to) continue;
      const color = EDGE_COLOR[e.kind] ?? 0x5b8fd0;
      const { core, halo } = arcMeshes(arcCurve(from, to), color, e.kind === 'gate' ? 0.5 : 0.45);
      this.nodeRoot.add(core);
      this.nodeRoot.add(halo);
    }

    this.render();
  }

  /**
   * 캔버스 크기에 맞춰 카메라를 다시 잡는다.
   *
   * 절두체를 **고정값으로 두지 않는다.** 웹뷰 폭은 사이드바(300px)부터 전체
   * 창까지 크게 변하는데, 고정 배율이면 좁을 때는 노드가 잘리고 넓을 때는
   * 화면 대부분이 빈 격자가 된다. 노드 배치의 경계 상자를 카메라 시점으로
   * 투영해서 항상 꽉 차게 맞춘다.
   */
  resize(): void {
    if (this.disposed) return;
    const parent = this.canvas.parentElement;
    const w = Math.max(1, parent?.clientWidth ?? this.canvas.clientWidth);
    const h = Math.max(1, parent?.clientHeight ?? this.canvas.clientHeight);
    this.renderer.setSize(w, h, false);

    this.camera.position.set(
      Math.cos(CAMERA_ANGLE) * CAMERA_DISTANCE,
      18.5,
      Math.sin(CAMERA_ANGLE) * CAMERA_DISTANCE,
    );
    this.camera.lookAt(SceneRenderer.layoutCenter());
    this.camera.updateMatrixWorld();

    const box = SceneRenderer.frameBounds(this.camera);
    const aspect = w / h;
    const cx = (box.minX + box.maxX) / 2;
    const cy = (box.minY + box.maxY) / 2;
    let halfW = ((box.maxX - box.minX) / 2) * FRAME_MARGIN;
    let halfH = ((box.maxY - box.minY) / 2) * FRAME_MARGIN;
    //: 경계 상자를 화면 비율에 맞게 넓힌다. 한쪽만 맞추면 다른 쪽이 잘린다.
    if (halfW / halfH < aspect) halfW = halfH * aspect;
    else halfH = halfW / aspect;
    //: 절두체를 **경계 상자 중심에** 맞춘다. 원점 기준으로 대칭으로 잡으면
    //: 배치가 한쪽으로 치우친 만큼 반대편이 통째로 빈 격자가 된다.
    this.camera.left = cx - halfW;
    this.camera.right = cx + halfW;
    this.camera.top = cy + halfH;
    this.camera.bottom = cy - halfH;
    this.camera.updateProjectionMatrix();
    this.render();
  }

  /** 노드 배치의 한가운데. 카메라가 여기를 본다. */
  static layoutCenter(): THREE.Vector3 {
    const xs = Object.values(LAYOUT).map((p) => p.x);
    const zs = Object.values(LAYOUT).map((p) => p.z);
    return new THREE.Vector3(
      (Math.min(...xs) + Math.max(...xs)) / 2,
      1.0,
      (Math.min(...zs) + Math.max(...zs)) / 2,
    );
  }

  /**
   * 모든 노드를 담는 카메라 좌표계 경계 상자.
   * 노드 위치를 카메라 시점으로 옮긴 뒤 최소·최대를 찾는다.
   */
  static frameBounds(camera: THREE.Camera): {
    minX: number;
    maxX: number;
    minY: number;
    maxY: number;
  } {
    const inv = camera.matrixWorldInverse;
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const at of Object.values(LAYOUT)) {
      //: 이름표가 슬래브 위로 솟으므로 바닥과 윗면을 모두 본다.
      for (const y of [0, 3.6]) {
        const v = new THREE.Vector3(at.x, y, at.z).applyMatrix4(inv);
        minX = Math.min(minX, v.x - NODE_RADIUS);
        maxX = Math.max(maxX, v.x + NODE_RADIUS);
        minY = Math.min(minY, v.y - NODE_RADIUS);
        maxY = Math.max(maxY, v.y + NODE_RADIUS);
      }
    }
    return { minX, maxX, minY, maxY };
  }

  /**
   * 한 프레임 그린다. **상시 렌더 루프를 돌리지 않는다** — 캔버스는 대부분
   * 가만히 있고, 웹뷰가 배경 탭으로 내려가도 계속 돌면 노트북 배터리를 먹는다.
   * 애니메이션이 필요한 순간(드래그·게이트 진행)에만 루프를 켠다.
   */
  render(): void {
    if (this.disposed) return;
    this.renderer.render(this.scene, this.camera);
  }

  /** 화면에 그려진 노드 수 — 테스트와 진단에서 "정말 그려졌나" 확인용. */
  get nodeCount(): number {
    return this.nodeRoot.children.length;
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    disposeObject(this.scene);
    this.renderer.dispose();
  }
}

export default SceneRenderer;
