/**
 * ReCoder — useVSCodeApi hook
 * Provides safe access to acquireVsCodeApi() and message passing utilities.
 */

import { useEffect, useCallback, useRef } from "react";

declare function acquireVsCodeApi(): {
  postMessage: (message: unknown) => void;
  getState: () => unknown;
  setState: (state: unknown) => void;
};

// Singleton VSCode API instance (acquireVsCodeApi can only be called once)
let vscodeApiInstance: ReturnType<typeof acquireVsCodeApi> | null = null;

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
    // eslint-disable-next-line react-hooks/rules-of-hooks
    useEffect(() => {
      const listener = (event: MessageEvent) => {
        const message = event.data;
        if (message && typeof message === "object" && "type" in message) {
          handler(message as { type: string; payload: unknown });
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
