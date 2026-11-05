import { useEffect, useReducer, useRef } from "react";
import { useVSCodeApi } from "./useVSCodeApi";
import type { AiId } from "../components/AiPicker";
import type { ModelsState } from "../components/modelRoutingModel";

export interface AiActivity { generating: boolean; switching: boolean; target: string | null }
export interface AiSwitchState {
  target: AiId | null; pending: boolean; locked: boolean; generating: boolean; done: boolean; notice: string;
}
export const initialAiSwitch: AiSwitchState = { target: null, pending: false, locked: false, generating: false, done: false, notice: "" };
type Event = { type: "begin"; provider: AiId } | { type: "activity"; activity: AiActivity }
  | { type: "settled"; provider: string; activity?: AiActivity } | { type: "error"; message: string }
  | { type: "timeout" | "clearNotice" | "clearDone" };
const isId = (id: unknown): id is AiId => id === "anthropic" || id === "openai";

export function reduceAiSwitch(state: AiSwitchState, event: Event): AiSwitchState {
  switch (event.type) {
    case "begin": return { ...state, target: event.provider, pending: true, locked: true, done: false, notice: "" };
    case "activity": {
      const a = event.activity;
      if (!a.switching) return { ...state, pending: false, target: null, locked: false, generating: a.generating };
      return { ...state, locked: true, generating: a.generating, pending: !state.notice && isId(a.target),
        target: isId(a.target) ? a.target : null, done: false };
    }
    case "settled": return { ...state, target: null, pending: false, locked: !!event.activity?.switching,
      generating: !!event.activity?.generating, done: state.target === event.provider,
      notice: state.target === event.provider ? "" : state.notice };
    case "error": return { ...state, target: null, pending: false, locked: false, done: false, notice: event.message };
    // A slow host can still finish. Hide the spinner but keep mutation controls locked until its result.
    case "timeout": return { ...state, pending: false, done: false, notice: "AI 연결이 지연되고 있습니다. 연결 결과를 기다리는 중입니다." };
    case "clearNotice": return { ...state, notice: "" };
    case "clearDone": return { ...state, done: false };
  }
}

/** Shared state contract for the composer, settings, and the second webview. */
export function useAiSwitch() {
  const { postMessage, useMessage } = useVSCodeApi();
  const [state, dispatch] = useReducer(reduceAiSwitch, initialAiSwitch);
  const stateRef = useRef(state);
  stateRef.current = state;
  useMessage(({ type, payload }) => {
    if (type === "ai.activity") dispatch({ type: "activity", activity: payload as AiActivity });
    if (type === "models.state") {
      const p = payload as ModelsState;
      if (p.activity?.switching) dispatch({ type: "activity", activity: p.activity });
      else dispatch({ type: "settled", provider: p.routing.quality.provider, activity: p.activity });
    }
    if (type === "ai.switchError") dispatch({ type: "error", message: String((payload as { message?: string })?.message || "AI를 바꾸지 못했습니다.") });
  });
  useEffect(() => {
    if (!state.pending) return undefined;
    const timer = setTimeout(() => { dispatch({ type: "timeout" }); postMessage("models.load"); }, 45000);
    return () => clearTimeout(timer);
  }, [state.pending, postMessage]);
  useEffect(() => {
    if (!state.done) return undefined;
    const timer = setTimeout(() => dispatch({ type: "clearDone" }), 2500);
    return () => clearTimeout(timer);
  }, [state.done]);
  return { ...state, switching: state.pending ? state.target : null,
    clearNotice: () => dispatch({ type: "clearNotice" }),
    begin: (provider: AiId, connect = false) => {
      if (stateRef.current.locked || stateRef.current.generating) return;
      stateRef.current = reduceAiSwitch(stateRef.current, { type: "begin", provider });
      dispatch({ type: "begin", provider });
      postMessage(connect ? "ai.connect" : "ai.switch", { provider });
    } };
}
