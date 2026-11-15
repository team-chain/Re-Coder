/**
 * 화면 이동 + 그 안의 특정 영역 열기(4.0.0).
 * 진행 표시줄의 "공격 테스트" 를 누르면 보안 화면으로만 가고 레드팀은 접힌 채라 다시 두 번 더 눌러야 했다.
 * 이동을 요청한 쪽이 의도를 남기고, 도착한 화면이 처음 그릴 때 꺼내 쓴다(이벤트는 이미 열린 화면용).
 */
export type FocusTarget = "redteam";
let pending: FocusTarget | null = null;
export function requestFocus(target: FocusTarget): void {
  pending = target;
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent("recoder:focus", { detail: target }));
}
export function takeFocus(target: FocusTarget): boolean {
  if (pending !== target) return false;
  pending = null;
  return true;
}
