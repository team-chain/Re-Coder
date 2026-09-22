/**
 * 노드를 잇는 흐름 아크.
 *
 * 왜 직선이 아니라 위로 솟는 곡선인가
 *   아이소메트릭 시점에서 직선은 바닥 격자와 각도가 겹쳐 선이 격자에 묻힌다.
 *   위로 한 번 솟았다 내려오면 바닥에서 떨어져 나와 어느 노드에서 어느
 *   노드로 가는지가 읽힌다. ArgoCD 의 리소스 트리가 곡선을 쓰는 이유도 같다.
 *
 * 왜 Line 이 아니라 Tube 인가
 *   `THREE.Line` 의 굵기(`linewidth`)는 대부분의 플랫폼에서 무시돼 항상 1px 로
 *   나온다. 고해상도 화면에서는 실처럼 얇아 안 보인다. 가는 원통을 쓰면
 *   굵기가 실제로 먹고, 조명도 받는다.
 */
import * as THREE from 'three';

/** 엣지 종류별 색. 배포는 초록, 소스 푸시는 보라, 게이트 진입은 파랑. */
export const EDGE_COLOR: Record<string, number> = {
  deploy: 0x2ea043,
  push: 0xb48ead,
  gate: 0x4a9eff,
};

/**
 * 두 점을 잇는, 가운데가 솟은 이차 베지에.
 * `lift` 는 거리에 비례시킨다 — 고정값이면 가까운 노드끼리는 과장돼 보이고
 * 먼 노드끼리는 거의 직선이 된다.
 */
export function arcCurve(a: THREE.Vector3, b: THREE.Vector3): THREE.QuadraticBezierCurve3 {
  const mid = a.clone().add(b).multiplyScalar(0.5);
  mid.y += Math.max(1.4, a.distanceTo(b) * 0.16);
  return new THREE.QuadraticBezierCurve3(a.clone(), mid, b.clone());
}

export interface ArcMeshes {
  core: THREE.Mesh;
  halo: THREE.Mesh;
}

/**
 * 아크 하나 = 가는 심지 + 그 둘레의 옅은 후광.
 * 후광이 없으면 어두운 배경에서 선이 배경에 먹힌다. 블룸 없이도 빛나 보이게
 * 하는 가장 싼 방법이다.
 */
export function arcMeshes(
  curve: THREE.Curve<THREE.Vector3>,
  color: number,
  opacity = 0.45,
  width = 0.05,
): ArcMeshes {
  const core = new THREE.Mesh(
    new THREE.TubeGeometry(curve, 48, width, 8, false),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity }),
  );
  const halo = new THREE.Mesh(
    new THREE.TubeGeometry(curve, 48, width * 3.4, 8, false),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: opacity * 0.14 }),
  );
  return { core, halo };
}
