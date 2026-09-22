/**
 * 3D 로 그릴지 2D 로 그릴지 — 그 판단만 한다.
 *
 * 왜 폴백이 필요한가
 *   VSCode 웹뷰는 Chromium 이라 보통 WebGL 이 되지만, 항상은 아니다.
 *   - Remote SSH / Codespaces / devcontainer 처럼 원격 화면일 때
 *   - GPU 드라이버가 막혀 소프트웨어 렌더링도 안 잡힐 때
 *   - `--disable-gpu` 로 띄운 VSCode
 *   이 경우 three.js 는 컨텍스트 생성에 실패하고, 그때 화면이 **빈 검은 박스**
 *   가 되면 사용자는 "배포 화면이 깨졌다" 고 읽는다. 배포 캔버스는 배포로
 *   가는 유일한 길이므로, 못 그리면 기능 자체가 막힌다. 그래서 같은 모델을
 *   2D 로 그리는 길을 항상 열어 둔다.
 */

export interface RenderCapability {
  /** WebGL 컨텍스트를 실제로 만들어 봤는가 (추측이 아니라 시도 결과). */
  webgl: boolean;
  /** 사용자가 설정에서 2D 를 고정했는가. */
  forced2d?: boolean;
  /** 이전에 3D 초기화가 실패했는가 — 한 번 실패하면 다시 시도하지 않는다. */
  failedBefore?: boolean;
}

export type RenderMode = '3d' | '2d';

/**
 * 순수 판정. 이 함수만 테스트하면 "언제 2D 로 내려가는가" 가 고정된다.
 */
export function pickRenderMode(cap: RenderCapability): RenderMode {
  if (cap.forced2d) return '2d';
  if (cap.failedBefore) return '2d';
  return cap.webgl ? '3d' : '2d';
}

/**
 * WebGL 지원 여부를 **실제로 컨텍스트를 만들어** 확인한다.
 *
 * `'WebGLRenderingContext' in window` 같은 검사는 생성자만 보고 판단하기
 * 때문에, 드라이버가 막혀 컨텍스트 생성이 실패하는 환경에서도 true 를
 * 돌려준다. 그 경우 three.js 는 뒤늦게 예외를 던지고 화면은 이미 비어 있다.
 * 만들어 보고, 만든 컨텍스트는 바로 잃어버리게 둔다.
 */
export function detectWebgl(doc?: Document): boolean {
  const d = doc ?? (typeof document !== 'undefined' ? document : undefined);
  if (!d) return false;
  try {
    const canvas = d.createElement('canvas');
    const gl =
      canvas.getContext('webgl2') ||
      canvas.getContext('webgl') ||
      canvas.getContext('experimental-webgl');
    return !!gl;
  } catch {
    //: 일부 환경은 getContext 자체가 던진다. 그것도 "안 된다" 로 읽는다.
    return false;
  }
}
