/**
 * 노드 위에 떠 있는 이름표(플레이트) — Canvas2D 로 그려 스프라이트 텍스처로 쓴다.
 *
 * 왜 3D 텍스트가 아니라 캔버스 텍스처인가
 *   three.js 로 글자를 그리려면 폰트를 따로 실어야 하는데(TextGeometry·
 *   troika 등), 한글까지 포함하면 폰트 파일만 수 MB 다. 번들에 못 넣는다.
 *   Canvas2D 는 **시스템 폰트를 그대로** 쓰므로 한글이 바로 나오고 용량이 0 이다.
 *
 * 왜 스프라이트인가
 *   `THREE.Sprite` 는 항상 카메라를 향한다. 아이소메트릭 시점에서 판을
 *   기울여 붙이면 각도에 따라 글자가 찌그러져 읽히지 않는다.
 *
 * 왜 HTML 오버레이가 아닌가
 *   오버레이는 선명하지만 캔버스 밖 DOM 이라 노드에 가려지지 않는다 —
 *   뒤에 있는 노드의 이름표가 앞 노드 위에 떠서 앞뒤가 뒤집혀 보인다.
 *   스프라이트는 깊이 정렬에 참여하므로 그 문제가 없다.
 */
import * as THREE from 'three';
import { LOGO_PATHS, LOGO_TEXT, LOGO_VIEWBOX } from './logos';

const PLATE_PX = 256;

function roundRect(
  c: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  c.beginPath();
  c.moveTo(x + r, y);
  c.lineTo(x + w - r, y);
  c.quadraticCurveTo(x + w, y, x + w, y + r);
  c.lineTo(x + w, y + h - r);
  c.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  c.lineTo(x + r, y + h);
  c.quadraticCurveTo(x, y + h, x, y + h - r);
  c.lineTo(x, y + r);
  c.quadraticCurveTo(x, y, x + r, y);
  c.closePath();
}

function spriteFrom(canvas: HTMLCanvasElement, scale: number): THREE.Sprite {
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  //: 스프라이트는 화면에서 작게 보일 때가 많다. 밉맵 없이 확대하면 계단이 생긴다.
  tex.minFilter = THREE.LinearMipmapLinearFilter;
  tex.generateMipmaps = true;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
  sprite.scale.set(scale, scale, 1);
  return sprite;
}

export interface PlateOptions {
  /** 노드 종류 — 로고가 있으면 패스를, 없으면 글자를 그린다. */
  kind: string;
  /** 아래쪽에 들어가는 작은 라벨. 보통 대상 이름. */
  sub: string;
  /** 테두리·글자 색. 잠긴 노드는 회색이 들어온다. */
  color: string;
  /** 잠금 자물쇠를 얹을지. */
  locked?: boolean;
  scale?: number;
}

/**
 * 로고(또는 글자) + 아래 라벨이 들어간 정사각 플레이트.
 * 잠긴 노드는 회색 + 자물쇠 — 색만으로 구분하면 색각 이상에서 구분이 안 된다.
 */
export function logoPlate(opts: PlateOptions, doc?: Document): THREE.Sprite {
  const d = doc ?? document;
  const canvas = d.createElement('canvas');
  canvas.width = PLATE_PX;
  canvas.height = PLATE_PX;
  const c = canvas.getContext('2d');
  if (!c) return spriteFrom(canvas, opts.scale ?? 2.3);

  const pad = 14;
  const g = c.createLinearGradient(pad, pad, PLATE_PX - pad, PLATE_PX - pad);
  g.addColorStop(0, 'rgba(28,34,44,0.96)');
  g.addColorStop(1, 'rgba(16,20,27,0.98)');
  roundRect(c, pad, pad, PLATE_PX - pad * 2, PLATE_PX - pad * 2, 34);
  c.fillStyle = g;
  c.fill();

  c.save();
  c.shadowColor = opts.color;
  c.shadowBlur = opts.locked ? 0 : 26;
  c.strokeStyle = opts.color;
  c.lineWidth = 3.2;
  c.stroke();
  c.restore();

  const path = LOGO_PATHS[opts.kind];
  if (path) {
    //: 24x24 패스를 플레이트 가운데 116px 크기로 확대해 그린다.
    const size = 116;
    const scale = size / LOGO_VIEWBOX;
    c.save();
    c.translate((PLATE_PX - size) / 2, 44 + (132 - size) / 2);
    c.scale(scale, scale);
    c.fillStyle = opts.color;
    c.shadowColor = opts.color;
    c.shadowBlur = opts.locked ? 0 : 16 / scale;
    c.fill(new Path2D(path));
    c.restore();
  } else {
    const text = LOGO_TEXT[opts.kind] ?? '';
    if (text) {
      c.save();
      c.shadowColor = opts.color;
      c.shadowBlur = opts.locked ? 0 : 18;
      c.fillStyle = opts.color;
      c.font = `800 ${text.length > 2 ? 62 : 76}px "Segoe UI", sans-serif`;
      c.textAlign = 'center';
      c.textBaseline = 'middle';
      c.fillText(text, PLATE_PX / 2, 110);
      c.restore();
    }
  }

  if (opts.locked) {
    //: 색만으로 잠금을 알리면 색각 이상에서 구분이 안 된다. 기호를 같이 쓴다.
    c.font = '700 40px "Segoe UI", sans-serif';
    c.textAlign = 'center';
    c.textBaseline = 'middle';
    c.fillStyle = opts.color;
    c.fillText('🔒', PLATE_PX - 52, 56);
  }

  c.fillStyle = opts.locked ? 'rgba(150,158,168,0.8)' : 'rgba(210,218,226,0.72)';
  c.font = '700 21px "Segoe UI", sans-serif';
  c.textAlign = 'center';
  c.textBaseline = 'middle';
  //: 긴 이름은 잘라서 넣는다. 플레이트 밖으로 나가면 다른 노드를 덮는다.
  c.fillText(fitText(c, opts.sub, PLATE_PX - 48), PLATE_PX / 2, PLATE_PX - 52);

  return spriteFrom(canvas, opts.scale ?? 2.3);
}

/** 주어진 픽셀 폭에 맞게 말줄임한다. */
export function fitText(c: CanvasRenderingContext2D, text: string, maxWidth: number): string {
  if (c.measureText(text).width <= maxWidth) return text;
  let cut = text;
  while (cut.length > 1 && c.measureText(`${cut}…`).width > maxWidth) {
    cut = cut.slice(0, -1);
  }
  return `${cut}…`;
}
