/**
 * 캔버스 3D 기본 도형 — 둥근 모서리 슬래브와 그 테두리.
 *
 * ArgoCD 의 노드처럼 "박스 하나 = 대상 하나" 로 읽히게 하려고 평평한 판이
 * 아니라 두께가 있는 슬래브를 쓴다. 바닥 그림자와 테두리 선이 있어야 아이소
 * 메트릭 시점에서 겹쳐 있어도 앞뒤가 구분된다.
 *
 * three.js 타입만 참조하고 씬에 직접 붙이지 않는다 — 붙이는 건 SceneRenderer.
 */
import * as THREE from 'three';

/** 둥근 사각형 단면. 슬래브를 뽑아낼 때 쓴다. */
export function roundShape(w: number, d: number, rad: number): THREE.Shape {
  const sh = new THREE.Shape();
  const x = -w / 2;
  const y = -d / 2;
  sh.moveTo(x + rad, y);
  sh.lineTo(x + w - rad, y);
  sh.quadraticCurveTo(x + w, y, x + w, y + rad);
  sh.lineTo(x + w, y + d - rad);
  sh.quadraticCurveTo(x + w, y + d, x + w - rad, y + d);
  sh.lineTo(x + rad, y + d);
  sh.quadraticCurveTo(x, y + d, x, y + d - rad);
  sh.lineTo(x, y + rad);
  sh.quadraticCurveTo(x, y, x + rad, y);
  return sh;
}

export interface Slab {
  mesh: THREE.Mesh;
  geometry: THREE.ExtrudeGeometry;
  material: THREE.MeshStandardMaterial;
}

/** 두께가 있는 둥근 판 하나. */
export function slab(
  w: number,
  d: number,
  h: number,
  color: number,
  emissiveIntensity = 0.2,
): Slab {
  const geometry = new THREE.ExtrudeGeometry(roundShape(w, d, Math.min(0.4, w / 6)), {
    depth: h,
    bevelEnabled: true,
    bevelSize: 0.06,
    bevelThickness: 0.06,
    bevelSegments: 3,
    curveSegments: 8,
  });
  //: ExtrudeGeometry 는 XY 평면에 만들어진다. 바닥에 눕힌다.
  geometry.rotateX(-Math.PI / 2);
  const material = new THREE.MeshStandardMaterial({
    color,
    metalness: 0.45,
    roughness: 0.35,
    emissive: color,
    emissiveIntensity,
    transparent: true,
    opacity: 0.94,
  });
  return { mesh: new THREE.Mesh(geometry, material), geometry, material };
}

/** 슬래브 위에 얹는 테두리 선. 어두운 배경에서 형태를 잡아 준다. */
export function rimEdges(
  geometry: THREE.BufferGeometry,
  color: number,
  opacity = 0.9,
): THREE.LineSegments {
  return new THREE.LineSegments(
    new THREE.EdgesGeometry(geometry, 25),
    new THREE.LineBasicMaterial({ color, transparent: true, opacity }),
  );
}

/** 노드 바닥에 까는 발광 원반. 잠긴 노드에는 쓰지 않는다. */
export function floorGlow(radius: number, color: number, opacity: number): THREE.Mesh {
  const mesh = new THREE.Mesh(
    new THREE.CircleGeometry(radius, 40),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity }),
  );
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = 0.012;
  return mesh;
}

/**
 * 붙였던 것을 전부 되돌린다.
 *
 * 웹뷰는 탭을 옮겨 다닐 때마다 컴포넌트가 붙었다 떨어진다. geometry 와
 * material 은 GPU 자원이라 JS 가비지 컬렉션으로 사라지지 않는다 — 놔두면
 * 탭을 몇 번 오가는 것만으로 메모리가 계속 는다.
 */
export function disposeObject(root: THREE.Object3D): void {
  root.traverse((obj) => {
    const mesh = obj as THREE.Mesh & { material?: THREE.Material | THREE.Material[] };
    if (mesh.geometry) mesh.geometry.dispose();
    const mat = mesh.material;
    if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
    else if (mat) mat.dispose();
  });
}
