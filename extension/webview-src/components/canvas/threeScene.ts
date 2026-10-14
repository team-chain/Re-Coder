import * as THREE from 'three';
import { colors, SceneEdge, SceneNode } from './model';

/** Screen-space projection keeps the accessible DOM controls and drop targets aligned. */
export function createArchitectureScene(nodes: SceneNode[], edges: SceneEdge[], height: number) {
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(0, 1120, 0, -height, .1, 2000);
  camera.position.z = 1000;
  scene.add(new THREE.HemisphereLight(0xddeeff, 0x18202c, 2));
  const key = new THREE.DirectionalLight(0xffffff, 3);
  key.position.set(-300, 400, 800);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x85bdff, 1.4);
  rim.position.set(900, -300, 500);
  scene.add(rim);

  const shape = new THREE.Shape();
  const half = 43, radius = 7;
  shape.moveTo(-half + radius, -half);
  shape.lineTo(half - radius, -half);
  shape.quadraticCurveTo(half, -half, half, -half + radius);
  shape.lineTo(half, half - radius);
  shape.quadraticCurveTo(half, half, half - radius, half);
  shape.lineTo(-half + radius, half);
  shape.quadraticCurveTo(-half, half, -half, half - radius);
  shape.lineTo(-half, -half + radius);
  shape.quadraticCurveTo(-half, -half, -half + radius, -half);
  const platformGeometry = new THREE.ExtrudeGeometry(shape, {
    depth: 27, bevelEnabled: true, bevelSegments: 2, steps: 1, bevelSize: 1.5, bevelThickness: 1.5, curveSegments: 6,
  });
  platformGeometry.translate(0, 0, -27);
  // A real extruded platform, tilted and rotated to match the reference's isometric view.
  platformGeometry.rotateZ(Math.PI / 4);
  platformGeometry.rotateX(-Math.acos(.43));
  const outlineGeometry = new THREE.EdgesGeometry(platformGeometry, 30);
  const haloGeometry = new THREE.RingGeometry(64, 82, 64);

  for (const node of nodes) {
    const color = new THREE.Color(node.locked ? colors.locked : node.color);
    const group = new THREE.Group();
    group.name = node.id;
    group.position.set(node.x, -node.y, 0);
    scene.add(group);
    const halo = new THREE.Mesh(haloGeometry, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: node.locked ? .03 : .09, depthWrite: false }));
    halo.scale.y = .4;
    halo.position.set(0, -26, -90);
    group.add(halo);
    if (node.kind === 'gate') {
      const gate = new THREE.Mesh(new THREE.OctahedronGeometry(33), new THREE.MeshStandardMaterial({ color, roughness: .38, metalness: .2, emissive: color, emissiveIntensity: .18 }));
      gate.position.set(0, 25, 30);
      gate.rotation.set(.2, Math.PI / 4, 0);
      group.add(gate);
      for (const radius of [47, 63]) {
        const ring = new THREE.Mesh(new THREE.TorusGeometry(radius, 1, 8, 64), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: .7 }));
        ring.scale.y = .38;
        ring.position.set(0, 14, 45);
        group.add(ring);
      }
    } else {
      const platform = new THREE.Mesh(platformGeometry, new THREE.MeshStandardMaterial({
        color: color.clone().multiplyScalar(node.locked ? .12 : .43), roughness: .42, metalness: .25,
        emissive: color, emissiveIntensity: node.locked ? .01 : .06,
      }));
      group.add(platform);
      group.add(new THREE.LineSegments(outlineGeometry, new THREE.LineBasicMaterial({ color, transparent: true, opacity: node.locked ? .2 : .65 })));
    }
  }

  for (const [index, edge] of edges.entries()) {
    const a = nodes.find(n => n.id === edge.from), b = nodes.find(n => n.id === edge.to);
    if (!a || !b) continue;
    const curve = new THREE.CubicBezierCurve3(
      new THREE.Vector3(a.x, -a.y, -55), new THREE.Vector3(a.x, -a.y + 100, -55),
      new THREE.Vector3(b.x, -b.y + 100, -55), new THREE.Vector3(b.x, -b.y, -55),
    );
    const locked = Boolean(a.locked || b.locked);
    const connection = new THREE.Group();
    connection.name = `edge:${edge.from}:${edge.to}:${index}`;
    if (edge.dashed || locked) {
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(64)), new THREE.LineDashedMaterial({ color: edge.color, transparent: true, opacity: locked ? .15 : .65, dashSize: 6, gapSize: 5 }));
      line.computeLineDistances();
      connection.add(line);
    } else {
      connection.add(new THREE.Mesh(new THREE.TubeGeometry(curve, 64, 1.5, 6, false), new THREE.MeshBasicMaterial({ color: edge.color, transparent: true, opacity: .8 })));
    }
    if (!locked) connection.add(new THREE.Mesh(new THREE.TubeGeometry(curve, 48, 4, 6, false), new THREE.MeshBasicMaterial({ color: edge.color, transparent: true, opacity: .07, depthWrite: false })));
    const arrow = new THREE.Mesh(new THREE.ConeGeometry(3.5, 11, 6), new THREE.MeshBasicMaterial({ color:edge.color, transparent:true, opacity:locked ? .15 : .8 }));
    arrow.position.copy(curve.getPoint(.84));
    arrow.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), curve.getTangent(.84).normalize());
    connection.add(arrow);
    scene.add(connection);
  }

  let disposed = false;
  return { scene, camera, dispose: () => {
    if (disposed) return;
    disposed = true;
    const geometries = new Set<THREE.BufferGeometry>([platformGeometry, outlineGeometry, haloGeometry]);
    const materials = new Set<THREE.Material>();
    scene.traverse(object => {
      const mesh = object as THREE.Mesh;
      if (mesh.geometry) geometries.add(mesh.geometry);
      if (mesh.material) for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) materials.add(material);
    });
    geometries.forEach(geometry => geometry.dispose());
    materials.forEach(material => material.dispose());
    scene.clear();
  } };
}
