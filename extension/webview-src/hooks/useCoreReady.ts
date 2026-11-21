import { useEffect, useRef } from "react";

/** Core 가 (다시) 연결되면 한 번 부른다 — 창을 열 때 Core 가 아직 켜지는 중이라 실패한 읽기를 다시 하게.
 *  실사용(2026-11-21): AI 모델·배포 캔버스·홈이 「Core 에 연결하지 못했습니다」 로 남아 있었다. */
export const CORE_READY_EVENT = "recoder:core-ready";
export function useCoreReady(fn: () => void): void {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const on = () => ref.current();
    window.addEventListener(CORE_READY_EVENT, on);
    return () => window.removeEventListener(CORE_READY_EVENT, on);
  }, []);
}
