import { useCallback, useRef, useState } from "react";
import { useVSCodeApi } from "./useVSCodeApi";

type Heal = { message: string; failed?: boolean; pending?: boolean };
export type HealState = Record<string, Heal>;

export function updateHealing(state: HealState, type: string, payload: unknown): HealState {
  const p = payload as { key?: string; message?: string; failed?: boolean; pending?: boolean } | null;
  if (!p?.key) return state;
  if (type === "selfHeal.finished") return { ...state, [p.key]: { ...state[p.key], message: state[p.key]?.pending ? "조치 요청 처리가 끝났습니다. 진단 결과를 확인하세요." : (state[p.key]?.message ?? ""), pending: false } };
  if (type !== "selfHeal") return state;
  return { ...state, [p.key]: { message: p.message ?? "", failed: p.failed, pending: !!p.pending } };
}

export function useSelfHeal() {
  const { postMessage, useMessage } = useVSCodeApi();
  const [heal, setHeal] = useState<HealState>({});
  const active = useRef<HealState>({});
  useMessage(useCallback(({ type, payload }) => {
    active.current = updateHealing(active.current, type, payload);
    setHeal(active.current);
  }, []));
  const start = (key: string) => {
    if (active.current[key]?.pending) return;
    active.current = updateHealing(active.current, "selfHeal", { key, pending: true, message: "조치 중…" });
    setHeal(active.current);
    postMessage("webview.diagnostics.fix", { key });
  };
  return { heal, start };
}
