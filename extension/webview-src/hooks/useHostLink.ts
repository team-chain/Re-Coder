/**
 * ReCoder — 이 화면이 확장과 아직 이어져 있는지 확인한다.
 *
 * 확장이 다시 시작되면(VSIX 설치 뒤 "확장 다시 시작", 빈 창에 폴더 추가 등) 예전 화면이
 * 탭에 그대로 남지만 그 화면의 메시지를 받을 확장이 없을 수 있다. 그 상태에서 요청을
 * 보내면 30초를 기다린 뒤에야 "확장이 요청을 받지 못했습니다"가 떴다.
 *
 * 화면이 열릴 때·다시 보일 때·주기적으로 host.ping 을 보내고, 확장이 정해진 시간 안에
 * 아무 메시지도 보내지 않으면 끊긴 것으로 본다. 확장에서 메시지가 하나라도 오면 다시
 * 이어진 것으로 본다.
 */
import { useCallback, useEffect, useRef, useState } from "react";

export const HOST_PING_TIMEOUT_MS = 5000;
export const HOST_PING_INTERVAL_MS = 20000;

let lost = false;
/** 다른 화면(코드 생성 등)이 보내기 직전에 확인한다. */
export function isHostLinkLost(): boolean { return lost; }

export function useHostLink(
  postMessage: (type: string, payload?: unknown) => void,
  useMessage: (handler: (message: { type: string; payload: unknown }) => void) => void,
): boolean {
  const [isLost, setIsLost] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const mark = useCallback((value: boolean) => { lost = value; setIsLost(value); }, []);

  useMessage(useCallback(() => {
    //: 어떤 메시지든 확장이 살아 있다는 증거다.
    if (timer.current) { clearTimeout(timer.current); timer.current = null; }
    if (lost) { mark(false); }
  }, [mark]));

  const check = useCallback(() => {
    if (timer.current) { return; }
    postMessage("host.ping", { nonce: Date.now() });
    timer.current = setTimeout(() => { timer.current = null; mark(true); }, HOST_PING_TIMEOUT_MS);
  }, [postMessage, mark]);

  useEffect(() => {
    if (typeof document === "undefined") { return; }
    const onVisible = () => { if (document.visibilityState === "visible") { check(); } };
    const interval = setInterval(onVisible, HOST_PING_INTERVAL_MS);
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    onVisible();
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
      if (timer.current) { clearTimeout(timer.current); timer.current = null; }
    };
  }, [check]);

  return isLost;
}
