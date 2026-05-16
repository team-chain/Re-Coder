/**
 * ReCoder — usePolling hook
 * Periodically polls Core health and cost summary via postMessage to the extension host.
 */

import { useEffect, useCallback, useRef, useState } from "react";
import { useVSCodeApi } from "./useVSCodeApi";

export interface CoreHealth {
  status: "ok" | "degraded" | "down";
  version: string;
  uptime: number; // seconds
  port: number;
}

export interface CostSummary {
  daily_usd: number;
  monthly_usd: number;
  call_count: number;
  last_updated: string;
}

export interface PollingState {
  coreHealth: CoreHealth | null;
  costSummary: CostSummary | null;
  lastPolledAt: Date | null;
  isConnected: boolean;
}

export function usePolling(interval: number = 4000): PollingState {
  const { postMessage, useMessage } = useVSCodeApi();
  const [state, setState] = useState<PollingState>({
    coreHealth: null,
    costSummary: null,
    lastPolledAt: null,
    isConnected: false,
  });

  const poll = useCallback(() => {
    postMessage("webview.poll.health");
    postMessage("webview.poll.cost");
  }, [postMessage]);

  // Initial poll on mount
  useEffect(() => {
    poll();
  }, [poll]);

  // Interval polling
  useEffect(() => {
    const timer = setInterval(poll, interval);
    return () => clearInterval(timer);
  }, [poll, interval]);

  // Listen for responses from the extension host.
  // SidebarProvider sends "healthUpdate" and "costUpdate" (see SidebarProvider.ts).
  useMessage((message) => {
    const { type, payload } = message;

    if (type === "healthUpdate") {
      const health = payload as CoreHealth;
      setState((prev) => ({
        ...prev,
        coreHealth: health,
        lastPolledAt: new Date(),
        isConnected: health?.status === "ok",
      }));
    }

    if (type === "costUpdate") {
      setState((prev) => ({
        ...prev,
        costSummary: payload as CostSummary,
        lastPolledAt: new Date(),
      }));
    }

    // errorMessage from PollingService failure → mark as offline
    if (type === "errorMessage") {
      setState((prev) => ({
        ...prev,
        isConnected: false,
        coreHealth: prev.coreHealth?.status === "ok"
          ? { ...prev.coreHealth, status: "down" }
          : prev.coreHealth,
      }));
    }
  });

  return state;
}
