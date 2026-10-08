/**
 * ReCoder — useVSCodeApi hook
 * Provides safe access to acquireVsCodeApi() and message passing utilities.
 */

import { useEffect, useCallback, useRef, useLayoutEffect } from "react";

declare function acquireVsCodeApi(): {
  postMessage: (message: unknown) => void;
  getState: () => unknown;
  setState: (state: unknown) => void;
};

// Singleton VSCode API instance (acquireVsCodeApi can only be called once)
let vscodeApiInstance: ReturnType<typeof acquireVsCodeApi> | null = null;
const useMessageEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;

function getVSCodeApi(): ReturnType<typeof acquireVsCodeApi> | null {
  if (typeof acquireVsCodeApi !== "undefined") {
    if (!vscodeApiInstance) {
      try {
        vscodeApiInstance = acquireVsCodeApi();
      } catch {
        // Already acquired — return cached instance
      }
    }
    return vscodeApiInstance;
  }
  return null;
}

/**
 * 화면 상태를 VS Code 웹뷰 상태에 남긴다 — 창을 다시 불러오거나 확장이 다시 시작돼 화면이
 * 새로 그려져도 보던 화면(예: 코드 생성)과 쓰던 요청으로 돌아온다. 값은 작은 것만 둔다.
 */
export function loadUiState(): Record<string, unknown> {
  try {
    const raw = getVSCodeApi()?.getState();
    return raw && typeof raw === "object" ? { ...(raw as Record<string, unknown>) } : {};
  } catch {
    return {};
  }
}

let uiStateTimer: ReturnType<typeof setTimeout> | undefined;
export function saveUiState(patch: Record<string, unknown>): void {
  try {
    const api = getVSCodeApi();
    if (!api) return;
    const next = { ...loadUiState(), ...patch };
    api.setState(next);
    //: 확장에도 알려 둔다 — 웹뷰 상태 저장은 창이 닫힐 때만 디스크에 남는 경우가 있어(웹 버전 실측),
    //: 확장이 재시작되거나 창을 다시 불러온 직후 화면을 되돌릴 때 확장 쪽 사본을 쓴다.
    clearTimeout(uiStateTimer);
    uiStateTimer = setTimeout(() => { try { api.postMessage({ type: "ui.state", payload: next }); } catch { /* ignore */ } }, 300);
  } catch {
    /* 상태 저장 실패는 화면 동작에 영향이 없다 */
  }
}

export function useVSCodeApi() {
  const apiRef = useRef(getVSCodeApi());

  const postMessage = useCallback((type: string, payload?: unknown) => {
    if (apiRef.current) {
      apiRef.current.postMessage({ type, payload });
    } else {
      // Dev fallback: log to console when running outside VSCode
      console.log("[useVSCodeApi] postMessage:", { type, payload });
    }
  }, []);

  /**
   * Subscribe to messages from the extension host.
   * Must be called at the top level of a component (it wraps useEffect internally).
   */
  const useMessage = (
    handler: (message: { type: string; payload: unknown }) => void
  ) => {
    const handlerRef = useRef(handler);
    useMessageEffect(() => { handlerRef.current = handler; });
    // eslint-disable-next-line react-hooks/rules-of-hooks
    useMessageEffect(() => {
      const listener = (event: MessageEvent) => {
        const message = event.data;
        if (message && typeof message === "object" && "type" in message) {
          handlerRef.current(message as { type: string; payload: unknown });
        }
      };
      window.addEventListener("message", listener);
      return () => window.removeEventListener("message", listener);
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
  };

  const getState = useCallback((): unknown => {
    return apiRef.current?.getState() ?? null;
  }, []);

  const setState = useCallback((state: unknown) => {
    apiRef.current?.setState(state);
  }, []);

  return { postMessage, useMessage, getState, setState };
}
