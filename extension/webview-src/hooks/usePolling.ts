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

  // Listen for responses from the extension host
  useMessage((message) => {
    const { type, payload } = message;

    if (type === "core.health.update") {
      setState((prev) => ({
        ...prev,
        coreHealth: payload as CoreHealth,
        lastPolledAt: new Date(),
        isConnected: (payload as CoreHealth)?.status === "ok",
      }));
    }

    if (type === "core.cost.update") {
      setState((prev) => ({
        ...prev,
        costSummary: payload as CostSummary,
        lastPolledAt: new Date(),
      }));
    }

    if (type === "core.offline") {
      setState((prev) => ({
        ...prev,
        isConnected: false,
        coreHealth: { status: "down", version: "-", uptime: 0, port: 0 },
      }));
    }
  });

  return state;
}
