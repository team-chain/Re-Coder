/**
 * ReCoder — 코드 작성 및 수정 패널 (Build 탭 하위)
 *  - 대상 폴더 지정 · 참고 파일 첨부 · 이어서 수정(멀티턴)
 *  - 파일별 적용 / 변경 보기(diff) / 시크릿 경고
 */
import React, { useCallback, useEffect, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { isHostLinkLost } from "../hooks/useHostLink";
import { loadUiState, saveUiState } from "../hooks/uiState";
import { DecisionOptionCards } from "./DecisionOptionCards";
import { CodeRemovalSummary, CodeRemovalWarning, RemovalCheck } from "./CodeRemovalWarning";

interface SecretWarning { rule: string; line: number; masked: string; }
interface CodeOp {
  action: "create" | "edit";
  file: string; language: string; content: string; rationale: string;
  secret_warnings?: SecretWarning[];
  removal_check?: RemovalCheck;
}
interface CodeResult { summary: string; ops: CodeOp[]; model: string; requestId?: number; projectRoot?: string; }
interface DecisionOption { key: string; label: string; summary: string; pros: string[]; cons: string[]; recommended: boolean; }
interface Decision { id: string; question: string; options: DecisionOption[]; impact: string; }
//: 확정된 결정 하나. **`impact` 를 반드시 함께 보낸다.**
//:
//: 예전에는 여기서 impact 를 떨어뜨렸다. 화면(결정 모달)에는 영향 설명이
//: 보이는데 서버로는 안 갔고, 코어의 `adr.normalize_decisions` 가
//: `d.get("impact")` 로 읽으므로 항상 빈 문자열이 됐다. 그 결과 생성된 모든
//: ADR 의 「## 영향」이 `(영향 미기재)` 로 남았다.
export interface DecisionChoice {
  id: string;
  question: string;
  chosen_key: string;
  options: DecisionOption[];
  impact: string;
}

//: 결정 목록 + 사용자의 선택 → 서버로 보낼 확정 결정 목록.
//:
//: 컴포넌트 밖의 순수 함수로 둔 이유: 이 변환이 ADR 내용을 결정하는데,
//: 모달을 클릭해야만 도달하는 코드였어서 필드가 하나 빠져도 아무 테스트가
//: 깨지지 않았다. 밖으로 꺼내 직접 검사한다.
export function buildDecisionChoices(
  decisions: Decision[],
  selections: Record<string, string>,
): DecisionChoice[] {
  return decisions.map((decision) => ({
    id: decision.id,
    question: decision.question,
    chosen_key: selections[decision.id],
    options: decision.options,
    //: 미기재를 빈 문자열로 정규화 — 코어가 `_clean` 으로 다시 다듬는다.
    impact: decision.impact ?? "",
  }));
}
//: 턴은 **요청 시점의 대상 폴더를 함께 기억**한다.
//:
//: 사용자가 결과를 받은 뒤 폴더 선택을 바꾸고 나서 "적용"을 누르면, 현재
//: 선택된 폴더가 아니라 **그 결과를 만들 때 쓴 폴더**에 써야 한다. 코드는
//: 폴더 A 의 맥락으로 생성됐고 ADR 번호도 A 기준으로 예약됐는데 B 에 쓰면
//: 같은 이름의 파일·ADR 이 덮어써진다. 적용·모두 적용·diff·경로 표시가
//: 전부 이 고정값을 쓴다.
interface Turn { id: number; prompt: string; targetFolder: string; contextNames?: string[]; status: "planning" | "generating" | "done" | "error"; result?: CodeResult; error?: string; progress?: string; }
export interface CtxFile { path: string; content: string; }
interface PendingRequest { instruction: string; targetFolder: string; contextFiles: CtxFile[]; }
interface DecisionModal { requestId: number; decisions: Decision[]; selections: Record<string, string>; step: number; dropped: string[]; }

//: 파일 하나의 적용 상태.
//:
//: "적용됨"은 **확장 호스트가 실제로 썼다고 확인해 준 뒤에만** 붙는다.
//: 클릭 즉시 적용됨으로 바꾸면, 대상이 읽기 전용이라 쓰기가 실패해도
//: 버튼이 비활성화된 채 "적용됨"으로 남아 재시도가 불가능해진다.
type ApplyStatus = "pending" | "applied" | "failed";

let _turnSeq = 1;

//: 확장이 요청을 받았다는 첫 답(code.status)을 기다리는 시간.
export const FIRST_ACK_SECONDS = 15;
//: 확장이 요청을 받지 못했을 때. 거의 늘 "확장이 다시 시작돼 이 창이 옛 확장에 붙어 있는" 경우다.
export const NOT_RECEIVED = "ReCoder 확장이 이 요청을 받지 못했습니다. 확장이 다시 시작되면서 이 창과 연결이 끊긴 것 같습니다. Ctrl+Shift+P → Developer: Reload Window 로 창을 다시 불러온 뒤 다시 보내 주세요. 보낸 내용은 입력창에 다시 넣어 두었습니다.";

//: 위치 칩 글자 — 절대 경로(다른 프로젝트 폴더)는 폴더 이름만, 전체 경로는 마우스를 올리면 보인다.
export function folderLabel(folder: string): string {
  if (!folder) return "루트";
  const parts = folder.replace(/[\\/]+$/, "").split(/[\\/]/);
  return /^([A-Za-z]:|\/|\\\\|~)/.test(folder) ? (parts[parts.length - 1] || folder) : folder;
}

//: 첫 화면 예시 — 누르면 입력창에 채워지기만 한다(보내지는 않는다).
const EXAMPLES: { label: string; text: string }[] = [
  { label: "게시판 API", text: "게시판 REST API를 만들어줘 — 글 작성·목록·수정·삭제" },
  { label: "로그인 추가", text: "회원가입과 로그인 기능을 추가해줘" },
  { label: "에러 고치기", text: "이 에러를 고쳐줘: " },
];

//: 채팅 승인 카드에서 넘어온 요청. App 이 chat.actionAccepted 를 받아 내려준다.
//: requestId 는 확장 호스트가 정한 값(Date.now())이라 이 컴포넌트의 턴 번호와 겹치지 않는다.
export interface ExternalTurn { requestId: number; instruction: string; targetFolder: string; contextFiles?: CtxFile[]; }

//: 탐색기 "여기에 코드 생성" 으로 들어온 대상 폴더 — 코드 화면이 아직 없을 때 App 이 받아 두고,
//: 코드 화면이 처음 그려질 때 꺼내 쓴다(메시지가 화면보다 먼저 와서 사라지던 문제).
let pendingTargetFolder: string | null = null;
export function setPendingTargetFolder(folder: string): void { pendingTargetFolder = folder; }

export const CodeAgent: React.FC<{ isActive: boolean; externalTurn?: ExternalTurn | null; onReviewRequired?: () => void; connectionPending?: boolean; connectionError?: string }> = ({ isActive, externalTurn, onReviewRequired, connectionPending, connectionError }) => {
  const { postMessage, useMessage } = useVSCodeApi();

  //: 쓰던 요청은 화면이 다시 그려져도(창 다시 불러오기·확장 재시작) 남는다.
  const [input, setInput] = useState(() => { const d = loadUiState().codeDraft; return typeof d === "string" ? d : ""; });
  useEffect(() => { saveUiState({ codeDraft: input }); }, [input]);
  const [targetFolder, setTargetFolder] = useState(() => { const f = pendingTargetFolder ?? ""; pendingTargetFolder = null; return f; });
  const [contextFiles, setContextFiles] = useState<CtxFile[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [applyState, setApplyState] = useState<Record<string, ApplyStatus>>({});
  const [applyErrors, setApplyErrors] = useState<Record<string, string>>({});
  //: 생성 결과는 파일 목록만 먼저 보여 준다(파일이 수십 개면 내용이 화면을 덮었다). 이름을 누르면 내용을 펼친다.
  const [openPreview, setOpenPreview] = useState<Record<string, boolean>>({});
  const [decisionModal, setDecisionModal] = useState<DecisionModal | null>(null);
  const inputRef = React.useRef<HTMLTextAreaElement | null>(null);
  const pendingRequestsRef = React.useRef<Record<number, PendingRequest>>({});
  const handledExternalRef = React.useRef<number | null>(null);
  const responseTimers = React.useRef(new Map<number, ReturnType<typeof setTimeout>>());
  const expiredRequests = React.useRef(new Set<number>());
  // A ref also guards rapid clicks/shortcuts before React commits the busy state.
  const activeRequest = React.useRef<number | null>(null);
  const finishWaiting = (id?: number) => {
    if (id === undefined) return;
    clearTimeout(responseTimers.current.get(id));
    responseTimers.current.delete(id);
  };
  const waitForResponse = (id: number, seconds: number) => {
    finishWaiting(id);
    responseTimers.current.set(id, setTimeout(() => {
      responseTimers.current.delete(id);
      expiredRequests.current.add(id);
      if (activeRequest.current === id) activeRequest.current = null;
      const unsent = pendingRequestsRef.current[id]?.instruction ?? "";
      delete pendingRequestsRef.current[id];
      const acknowledged = acknowledgedRequests.current.has(id);
      //: 확장에 닿지 않은 요청은 다시 쓰지 않게 입력창에 되돌려 둔다(이미 새로 쓰고 있으면 건드리지 않는다).
      if (!acknowledged && unsent) { setInput((cur) => cur.trim() ? cur : unsent); }
      setTurns(ts => ts.map(t => t.id === id ? {...t,status:"error",error: acknowledged
        ? `${t.progress ? `"${t.progress.replace(/…$/, "")}" 단계에서 ` : ""}응답이 너무 오래 없습니다. 명령 팔레트에서 "ReCoder: Restart Core" 를 실행하거나 창을 다시 불러온 뒤 다시 요청해 주세요.`
        : NOT_RECEIVED} : t));
    }, seconds * 1000));
  };
  //: 확장 호스트가 한 번이라도 "받았다(code.status)"고 알려 온 요청.
  const acknowledgedRequests = React.useRef(new Set<number>());
  useEffect(() => () => { responseTimers.current.forEach(clearTimeout); }, []);

  //: 채팅에서 승인된 요청을 이 패널의 턴으로 등록하고 곧장 code.plan 을 보낸다.
  //: 이후 결정 모달 → 생성 → diff → 적용은 직접 입력한 턴과 완전히 같은 경로다.
  useEffect(() => {
    if (!externalTurn) { return; }
    if (handledExternalRef.current === externalTurn.requestId) { return; }
    handledExternalRef.current = externalTurn.requestId;
    const { requestId, instruction, targetFolder: folder } = externalTurn;
    activeRequest.current = requestId;
    const files = externalTurn.contextFiles ?? [];
    pendingRequestsRef.current[requestId] = { instruction, targetFolder: folder, contextFiles: files };
    setTargetFolder(folder);
    setTurns((ts) => [...ts, { id: requestId, prompt: instruction, targetFolder: folder, contextNames: files.map((f) => f.path), status: "planning" }]);
    waitForResponse(requestId, 30);
    postMessage("code.plan", { requestId, instruction, targetFolder: folder, contextFiles: files });
  }, [externalTurn, postMessage]);

  useMessage(useCallback((msg) => {
    const { type, payload } = msg;
    const responseId = (payload as {requestId?:number})?.requestId;
    if (["code.result", "code.planResult", "code.error"].includes(type) && !(payload as {ackKey?:string})?.ackKey) {
      if (responseId !== undefined && expiredRequests.current.has(responseId)) return;
      finishWaiting(responseId);
    }
    if (type === "code.status" || type === "code.generating") {
      //: 확장이 요청을 받아 다음 단계로 넘어갔다 — 그 단계에 맞는 시간만큼 다시 기다린다.
      const st = payload as { requestId?: number; message?: string; waitSeconds?: number };
      if (st.requestId === undefined || expiredRequests.current.has(st.requestId)) return;
      acknowledgedRequests.current.add(st.requestId);
      if (type === "code.status") {
        waitForResponse(st.requestId, Math.max(30, Number(st.waitSeconds) || 180));
        if (st.message) setTurns(ts => ts.map(t => t.id === st.requestId ? { ...t, progress: st.message } : t));
      }
      return;
    }
    if (type === "code.result") {
      const res = payload as CodeResult;
      const requestId = res.requestId;
      if (requestId === undefined || activeRequest.current === requestId) activeRequest.current = null;
      if (requestId !== undefined) delete pendingRequestsRef.current[requestId];
      setTurns((ts) => {
        const copy = [...ts];
        for (let i = copy.length - 1; i >= 0; i--) {
          if ((requestId === undefined || copy[i].id === requestId) && copy[i].status === "generating") {
            copy[i] = { ...copy[i], status: "done", result: res };
            break;
          }
        }
        return copy;
      });
    } else if (type === "code.applied") {
      // 확장 호스트의 **쓰기 확인**. ackKey 가 있어야 어느 파일의 응답인지
      // 알 수 있다 — 없는 메시지(모두 적용의 총계 등)는 상태를 바꾸지 않는다.
      const ack = payload as { ackKey?: string; ok?: boolean };
      if (ack.ackKey) {
        const key = ack.ackKey;
        setApplyState((s) => ({ ...s, [key]: ack.ok === false ? "failed" : "applied" }));
        if (ack.ok !== false) {
          setApplyErrors((e) => { const copy = { ...e }; delete copy[key]; return copy; });
        }
      }
    } else if (type === "code.error") {
      const m = (payload as { message?: string })?.message ?? String(payload);
      // 적용 실패(ackKey 있음)는 **그 파일의 상태**만 바꾼다. 턴을 건드리면
      // 진행 중인 다른 생성 턴이 엉뚱하게 에러로 뒤집힌다.
      const ackKey = (payload as { ackKey?: string })?.ackKey;
      if (ackKey) {
        setApplyState((s) => ({ ...s, [ackKey]: "failed" }));
        setApplyErrors((e) => ({ ...e, [ackKey]: m }));
        return;
      }
      if (responseId === undefined || activeRequest.current === responseId) activeRequest.current = null;
      if (responseId !== undefined) delete pendingRequestsRef.current[responseId];
      setTurns((ts) => {
        const copy = [...ts];
        const requestId = (payload as { requestId?: number })?.requestId;
        for (let i = copy.length - 1; i >= 0; i--) {
          if ((requestId === undefined || copy[i].id === requestId) && (copy[i].status === "planning" || copy[i].status === "generating")) {
            copy[i] = { ...copy[i], status: "error", error: m };
            break;
          }
        }
        return copy;
      });
    } else if (type === "code.planResult") {
      const plan = payload as { requestId?: number; decisions?: Decision[]; dropped?: string[] };
      const requestId = plan.requestId;
      if (requestId === undefined) { return; }
      const request = pendingRequestsRef.current[requestId];
      if (!request) { return; }
      const dropped = Array.isArray(plan.dropped) ? plan.dropped : [];
      let decisions = plan.decisions ?? [];
      if (decisions.length === 0) {
        //: [안전장치 이중화] 코어는 결정이 없어도 항상 확인 카드 1장을
        //: 보장한다(FR-02-05). 그래도 빈 목록이 오면(구버전 코어 등) 예전에는
        //: 여기서 **사람 승인 없이** 곧장 생성으로 직행했다 — AI-DLC 의 전제
        //: (항상 사람 승인)가 웹뷰 한 곳의 분기로 깨질 수 있었다. 같은 모양의
        //: 확인 카드를 만들어 모달을 띄운다. id 가 예약 접두사(__)라 코어
        //: 확인 카드와 동일하게 ADR 로는 기록되지 않는다.
        decisions = [{
          id: "__confirm__",
          question: `"${request.instruction.replace(/\s+/g, " ").slice(0, 60)}" — 이대로 진행할까요?`,
          impact: "",
          options: [{ key: "proceed", label: "진행", summary: "설계상 갈림길이 없어 요청대로 바로 반영합니다.", pros: ["추가 선택 불필요"], cons: [], recommended: true }],
        }];
      }
      const selections: Record<string, string> = {};
      for (const decision of decisions) {
        selections[decision.id] = decision.options.find((option) => option.recommended)?.key ?? decision.options[0]?.key ?? "";
      }
      setDecisionModal({ requestId, decisions, selections, step: 0, dropped });
      onReviewRequired?.();
    } else if (type === "code.folderPicked" || type === "code.setTargetFolder") {
      pendingTargetFolder = null;
      setTargetFolder((payload as { folder?: string })?.folder ?? "");
    } else if (type === "code.contextAdded") {
      const files = (payload as { files?: CtxFile[] })?.files ?? [];
      setContextFiles((cur) => {
        const seen = new Set(cur.map((c) => c.path));
        return [...cur, ...files.filter((f) => !seen.has(f.path))];
      });
    }
  }, [onReviewRequired]));

  const send = useCallback(() => {
    const text = input.trim();
    if (!text || activeRequest.current !== null) { return; }
    const id = _turnSeq++;
    activeRequest.current = id;
    pendingRequestsRef.current[id] = { instruction: text, targetFolder, contextFiles };
    // 요청 시점의 폴더를 턴에 **고정**한다 — 이후 폴더 선택을 바꿔도
    // 이 턴의 적용·diff·경로 표시는 전부 이 값을 쓴다.
    setTurns((ts) => [...ts, { id, prompt: text, targetFolder, contextNames: contextFiles.map((f) => f.path), status: "planning" }]);
    //: 확장은 받자마자 code.status 로 답한다 — 15초 안에 답이 없으면 끊긴 것이다(예전 30초).
    waitForResponse(id, isHostLinkLost() ? 5 : FIRST_ACK_SECONDS);
    postMessage("code.plan", { requestId: id, instruction: text, targetFolder, contextFiles });
    setInput("");
  }, [input, targetFolder, contextFiles, postMessage]);

  const chooseDecision = useCallback((key: string) => {
    setDecisionModal((current) => current ? {
      ...current,
      selections: { ...current.selections, [current.decisions[current.step].id]: key },
    } : current);
  }, []);

  const cancelDecision = useCallback(() => {
    if (!decisionModal) { return; }
    setTurns((ts) => ts.map((turn) => turn.id === decisionModal.requestId
      ? { ...turn, status: "error", error: "설계 결정을 취소해서 생성을 중단했습니다." }
      : turn));
    delete pendingRequestsRef.current[decisionModal.requestId];
    activeRequest.current = null;
    postMessage("code.cancelPlan", { requestId: decisionModal.requestId });
    setDecisionModal(null);
  }, [decisionModal, postMessage]);

  const confirmDecisions = useCallback(() => {
    if (!decisionModal) { return; }
    const request = pendingRequestsRef.current[decisionModal.requestId];
    if (!request) { setDecisionModal(null); return; }
    const choices = buildDecisionChoices(decisionModal.decisions, decisionModal.selections);
    setTurns((ts) => ts.map((turn) => turn.id === decisionModal.requestId ? { ...turn, status: "generating" } : turn));
    setDecisionModal(null);
    waitForResponse(decisionModal.requestId, 30);
    postMessage("code.generate", { requestId: decisionModal.requestId, instruction: request.instruction, targetFolder: request.targetFolder, contextFiles: request.contextFiles, decisions: choices });
  }, [decisionModal, postMessage]);

  const applyOp = useCallback((turn: Turn, op: CodeOp) => {
    const key = `${turn.id}:${op.file}`;
    // "적용 중"까지만 낙관한다. "적용됨"은 호스트의 code.applied 확인이
    // 와야 붙는다 — 쓰기 실패가 성공으로 굳는 것을 막는다.
    setApplyState((s) => ({ ...s, [key]: "pending" }));
    setApplyErrors((e) => { const copy = { ...e }; delete copy[key]; return copy; });
    postMessage("code.apply", { file: op.file, content: op.content, targetFolder: turn.targetFolder, projectRoot: turn.result?.projectRoot, ackKey: key });
  }, [postMessage]);

  const applyAll = useCallback((turn: Turn) => {
    if (!turn.result) { return; }
    const ops = turn.result.ops.map((op) => ({
      file: op.file, content: op.content, ackKey: `${turn.id}:${op.file}`,
    }));
    setApplyState((s) => {
      const copy = { ...s };
      for (const op of turn.result!.ops) { copy[`${turn.id}:${op.file}`] = "pending"; }
      return copy;
    });
    setApplyErrors((e) => {
      const copy = { ...e };
      for (const op of turn.result!.ops) { delete copy[`${turn.id}:${op.file}`]; }
      return copy;
    });
    postMessage("code.applyAll", { ops, targetFolder: turn.targetFolder, projectRoot: turn.result?.projectRoot });
  }, [postMessage]);

  const showDiff = useCallback((turn: Turn, op: CodeOp) => {
    postMessage("code.diff", { file: op.file, content: op.content, targetFolder: turn.targetFolder, projectRoot: turn.result?.projectRoot });
  }, [postMessage]);
  const linkBtn: React.CSSProperties = {
    fontSize: 11, border: "none", background: "transparent",
    color: "var(--vscode-textLink-foreground, #3794ff)", padding: 0, cursor: "pointer",
  };
  const primaryBtn: React.CSSProperties = {
    background: "var(--vscode-button-background, #2563eb)", color: "var(--vscode-button-foreground, #fff)",
    border: "none", borderRadius: 4, padding: "5px 12px", fontSize: 12, fontWeight: 500, cursor: "pointer",
  };
  const ghostBtn: React.CSSProperties = {
    background: "transparent", color: "var(--vscode-foreground, #ccc)",
    border: "1px solid var(--vscode-input-border, #3f3f3f)", borderRadius: 4, padding: "3px 9px",
    fontSize: 11, cursor: "pointer",
  };
  const isBusy = turns.some((turn) => turn.status === "planning" || turn.status === "generating");
  const hasTurns = turns.length > 0;

  return (
    <section aria-label="AI와 대화하기" className="rc-cg" data-has-turns={hasTurns ? "true" : "false"}>
      <style>{`
        .rc-cg { max-width: 860px; margin: 8px auto 0; display: flex; flex-direction: column; }
        .rc-cg-hero { margin: clamp(12px, 13vh, 132px) 0 18px; text-align: center; font-size: 22px; font-weight: 600; letter-spacing: -.2px; color: var(--vscode-foreground, #eee); }
        .rc-cg-notice { margin: 0 0 10px; border: 1px solid var(--vscode-widget-border, var(--vscode-panel-border, #333)); border-radius: 8px; padding: 8px 12px; color: var(--vscode-descriptionForeground, #aaa); font-size: 12px; line-height: 1.5; }
        .rc-cg-box { background: var(--vscode-input-background, #252526); border: 1px solid var(--vscode-input-border, var(--vscode-widget-border, #3f3f3f)); border-radius: 12px; padding: 10px 10px 8px 12px; transition: border-color .12s ease; }
        .rc-cg-box:focus-within { border-color: var(--vscode-focusBorder, #3794ff); }
        .rc-cg-input { display: block; width: 100%; box-sizing: border-box; border: 0; outline: none; background: transparent; color: var(--vscode-input-foreground, #ccc); font-family: var(--vscode-font-family, sans-serif); font-size: 13.5px; line-height: 1.6; padding: 2px 2px 0; resize: none; field-sizing: content; max-height: 260px; overflow-y: auto; }
        .rc-cg-input::placeholder { color: var(--vscode-input-placeholderForeground, #6b6b6b); }
        .rc-cg-input:disabled { opacity: .6; }
        .rc-cg-tools { display: flex; align-items: flex-end; gap: 8px; margin-top: 6px; }
        .rc-cg-ctx { flex: 1; min-width: 0; display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
        .rc-cg-actions { flex: none; display: flex; align-items: center; gap: 8px; }
        @media (max-width: 520px) { .rc-cg-kbd { display: none; } }
        .rc-cg-chip { display: inline-flex; align-items: center; gap: 5px; height: 24px; max-width: 240px; padding: 0 8px; border-radius: 6px; border: 1px solid var(--vscode-widget-border, var(--vscode-panel-border, #3f3f3f)); background: transparent; color: var(--vscode-foreground, #ccc); font: inherit; font-size: 12px; cursor: pointer; white-space: nowrap; }
        .rc-cg-chip:hover { background: var(--vscode-toolbar-hoverBackground, rgba(90,93,94,.31)); }
        .rc-cg-chip.quiet { border-color: transparent; color: var(--vscode-descriptionForeground, #999); }
        .rc-cg-chip > span { overflow: hidden; text-overflow: ellipsis; }
        .rc-cg-file { display: inline-flex; align-items: center; height: 24px; max-width: 220px; padding: 0 2px 0 8px; border-radius: 6px; background: var(--vscode-badge-background, #2b2b2c); color: var(--vscode-badge-foreground, #ccc); font-family: var(--vscode-editor-font-family, monospace); font-size: 11.5px; }
        .rc-cg-file > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .rc-cg-x { border: 0; background: transparent; color: inherit; opacity: .7; cursor: pointer; padding: 0 5px; font-size: 13px; line-height: 1; }
        .rc-cg-x:hover { opacity: 1; }
        .rc-cg-kbd { font-size: 11px; color: var(--vscode-descriptionForeground, #777); opacity: .8; }
        .rc-cg-send { width: 30px; height: 30px; flex: none; display: grid; place-items: center; border: 0; border-radius: 8px; padding: 0; background: var(--vscode-button-background, #0e639c); color: var(--vscode-button-foreground, #fff); cursor: pointer; transition: filter .12s ease, transform .05s ease; }
        .rc-cg-send:hover:not(:disabled) { filter: brightness(1.12); }
        .rc-cg-send:active:not(:disabled) { transform: translateY(1px); }
        .rc-cg-send:disabled { background: var(--vscode-button-secondaryBackground, #3a3a3a); color: var(--vscode-disabledForeground, #8b8b8b); cursor: not-allowed; }
        .rc-cg-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
        .rc-cg-examples { display: flex; flex-wrap: wrap; justify-content: center; gap: 6px; margin-top: 14px; }
        .rc-cg-examples button { border: 1px solid var(--vscode-widget-border, var(--vscode-panel-border, #3f3f3f)); border-radius: 999px; padding: 3px 11px; background: transparent; color: var(--vscode-descriptionForeground, #999); font: inherit; font-size: 12px; cursor: pointer; }
        .rc-cg-examples button:hover { color: var(--vscode-foreground, #ddd); border-color: var(--vscode-focusBorder, #3794ff); }
        .rc-cg-turn { margin-bottom: 16px; }
        .rc-cg-me { width: fit-content; max-width: 82%; margin-left: auto; padding: 8px 13px; border-radius: 14px; background: var(--vscode-list-inactiveSelectionBackground, #2b2b2c); color: var(--vscode-foreground, #eee); font-size: 13.5px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
        .rc-cg-meta { margin: 4px 2px 8px; text-align: right; font-size: 11px; color: var(--vscode-descriptionForeground, #888); }
        .rc-cg[data-has-turns="true"] { min-height: calc(100vh - 200px); }
        .rc-cg-dock { position: sticky; bottom: 0; z-index: 2; margin-top: auto; padding: 14px 0 6px; background: linear-gradient(to bottom, transparent, var(--rc-cg-bg, var(--vscode-editor-background, #1e1e1e)) 18px); box-shadow: 0 32px 0 0 var(--rc-cg-bg, var(--vscode-editor-background, #1e1e1e)); }
        body:not(:has(.rc-workspace)) .rc-cg-dock { --rc-cg-bg: var(--vscode-sideBar-background, #181818); }
        .rc-cg[data-has-turns="false"] .rc-cg-input { min-height: 64px; }
        .rc-decision-option:hover { border-color: var(--vscode-focusBorder, #3794ff) !important; }
      `}</style>
      {decisionModal && (() => {
        const decision = decisionModal.decisions[decisionModal.step];
        const isLast = decisionModal.step === decisionModal.decisions.length - 1;
        return (
          <div role="dialog" aria-modal="true" aria-label="설계 결정" style={{ position: "fixed", inset: 0, zIndex: 1000, display: "grid", placeItems: "center", padding: 18, background: "rgba(0,0,0,.58)", backdropFilter: "blur(2px)" }}>
            <div style={{ width: "min(560px, 100%)", maxHeight: "calc(100vh - 36px)", overflowY: "auto", border: "1px solid var(--vscode-widget-border, #454545)", borderRadius: 10, background: "var(--vscode-editorWidget-background, #252526)", boxShadow: "0 18px 48px rgba(0,0,0,.45)" }}>
              <div style={{ padding: "15px 18px 12px", borderBottom: "1px solid var(--vscode-panel-border, #3b3b3b)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ width: 4, height: 20, borderRadius: 2, background: "var(--vscode-textLink-foreground, #3794ff)" }} />
                  <strong style={{ fontSize: 15 }}>설계 결정을 골라주세요</strong>
                  <span style={{ marginLeft: "auto", borderRadius: 99, padding: "3px 8px", background: "var(--vscode-badge-background, #4d4d4d)", color: "var(--vscode-badge-foreground, #fff)", fontSize: 11, fontWeight: 600 }}>설계 결정 {decisionModal.step + 1}/{decisionModal.decisions.length}</span>
                </div>
                {/* 코어가 형식 문제로 걸러낸 결정 — 안 보여주면 사용자에게는
                    "AI 가 설계를 안 해준다"로 보인다(보드 이슈). */}
                {decisionModal.dropped.length > 0 && (
                  <div style={{ marginTop: 8, padding: "7px 9px", borderRadius: 5, background: "rgba(204,167,0,.10)", border: "1px solid rgba(204,167,0,.35)", color: "var(--vscode-editorWarning-foreground, #cca700)", fontSize: 10.5, lineHeight: 1.5 }}>
                    <div style={{ fontWeight: 650 }}>제시됐지만 제외된 결정 {decisionModal.dropped.length}건</div>
                    {decisionModal.dropped.map((reason, i) => <div key={i}>· {reason}</div>)}
                  </div>
                )}
              </div>
              <div style={{ padding: "18px" }}>
                <h3 style={{ margin: 0, color: "var(--vscode-foreground, #eee)", fontSize: 18, lineHeight: 1.4 }}>{decision.question}</h3>
                {decision.impact && <p style={{ margin: "7px 0 16px", color: "var(--vscode-descriptionForeground, #aaa)", fontSize: 12, lineHeight: 1.5 }}>{decision.impact}</p>}
                <DecisionOptionCards options={decision.options} selectedKey={decisionModal.selections[decision.id]} onSelect={chooseDecision} radioName={`decision-${decision.id}`} />
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 18px 16px", borderTop: "1px solid var(--vscode-panel-border, #3b3b3b)" }}>
                <button onClick={cancelDecision} style={{ ...ghostBtn, padding: "7px 11px" }}>취소</button>
                {decisionModal.step > 0 && <button onClick={() => setDecisionModal((current) => current ? { ...current, step: current.step - 1 } : current)} style={{ ...ghostBtn, padding: "7px 11px" }}>이전</button>}
                <button onClick={() => isLast ? confirmDecisions() : setDecisionModal((current) => current ? { ...current, step: current.step + 1 } : current)} disabled={!decisionModal.selections[decision.id]} style={{ ...primaryBtn, marginLeft: "auto", padding: "8px 13px", opacity: decisionModal.selections[decision.id] ? 1 : .5 }}>
                  {isLast ? "이 선택으로 생성 →" : "다음 결정 →"}
                </button>
              </div>
            </div>
          </div>
        );
      })()}
      {!hasTurns && <h2 className="rc-cg-hero">무엇을 만들까요?</h2>}

      {/* 히스토리 */}
      {turns.map((turn) => (
        <div key={turn.id} className="rc-cg-turn">
          <div className="rc-cg-me">{turn.prompt}</div>
          {(turn.targetFolder || (turn.contextNames && turn.contextNames.length > 0)) ? (
            <div className="rc-cg-meta">{[turn.targetFolder || "", ...(turn.contextNames ?? [])].filter(Boolean).join(" · ")}</div>
          ) : <div style={{ height: 8 }} />}

          {(turn.status === "planning" || turn.status === "generating") && (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--vscode-descriptionForeground, #888)", fontSize: 11, padding: "2px 0 6px" }}>
                <div style={{ width: 11, height: 11, border: "2px solid #3f3f3f", borderTopColor: "var(--vscode-progressBar-background, #3794ff)", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} />
                <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
                {turn.progress || (turn.status === "planning" ? "설계 결정을 준비하는 중…" : "코드 생성 중…")}
              </div>
            </>
          )}
          {turn.status === "error" && (
            <div style={{ background: "var(--vscode-inputValidation-errorBackground, rgba(239,68,68,0.1))", border: "1px solid var(--vscode-inputValidation-errorBorder, #ef4444)", borderRadius: 4, padding: "7px 10px", color: "var(--vscode-errorForeground, #f48771)", fontSize: 11 }}>{turn.error}</div>
          )}
          {turn.status === "done" && turn.result && (
            <div>
              <CodeRemovalSummary checks={turn.result.ops.map((op) => op.removal_check)} />
              {(() => {
                const ops = turn.result!.ops;
                const keys = ops.map((op) => `${turn.id}:${op.file}`);
                const anyPending = keys.some((k) => applyState[k] === "pending");
                const allApplied = keys.every((k) => applyState[k] === "applied");
                const created = ops.filter((op) => op.action === "create").length;
                return (
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, marginBottom: 6 }}>
                    <span data-testid="code-result-summary" style={{ fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>
                      파일 {ops.length}개{created ? ` · 새 파일 ${created}` : ""}{ops.length - created ? ` · 수정 ${ops.length - created}` : ""}
                    </span>
                    {ops.length > 1 && (
                      <button onClick={() => applyAll(turn)} disabled={anyPending || allApplied}
                        style={{ ...primaryBtn, ...(anyPending || allApplied ? { opacity: 0.55, cursor: "default" } : {}) }}>
                        {allApplied ? "모두 적용됨" : anyPending ? "적용 중…" : "모두 적용"}
                      </button>
                    )}
                  </div>
                );
              })()}
              {turn.result.ops.map((op, i) => {
                const key = `${turn.id}:${op.file}`;
                const warned = !!(op.secret_warnings && op.secret_warnings.length);
                const previewOpen = turn.result!.ops.length === 1 || !!openPreview[key];
                return (
                  <div key={i} style={{ marginBottom: 7, border: "1px solid var(--vscode-panel-border, #333)", borderRadius: 4, overflow: "hidden" }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 6, background: "var(--vscode-editorGroupHeader-tabsBackground, #2d2d2d)", padding: "5px 8px" }}>
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 6, minWidth: 0 }}>
                        <span style={{ fontSize: 9, fontWeight: 600, color: op.action === "create" ? "#6cc070" : "#d6a55c" }}>
                          {op.action === "create" ? "새 파일" : "수정"}
                        </span>
                        <button type="button" aria-expanded={previewOpen} title={previewOpen ? "내용 접기" : "내용 보기"}
                          onClick={() => setOpenPreview((cur) => ({ ...cur, [key]: !previewOpen }))}
                          style={{ border: "none", background: "transparent", color: "inherit", padding: 0, cursor: "pointer", fontSize: 11.5, fontFamily: "var(--vscode-editor-font-family, monospace)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", textAlign: "left", minWidth: 0 }}>
                          <span aria-hidden="true" style={{ display: "inline-block", width: 10, color: "var(--vscode-descriptionForeground, #888)" }}>{previewOpen ? "▾" : "▸"}</span>{turn.targetFolder ? `${turn.targetFolder}/${op.file}` : op.file}
                        </button>
                      </span>
                      <span style={{ display: "inline-flex", gap: 6, flexShrink: 0 }}>
                        <button onClick={() => showDiff(turn, op)} style={ghostBtn}>변경 보기</button>
                        <button onClick={() => applyOp(turn, op)} disabled={applyState[key] === "pending" || applyState[key] === "applied"}
                          style={{ ...primaryBtn, padding: "3px 11px", fontSize: 11, ...(applyState[key] === "applied" ? { background: "transparent", color: "#6cc070", cursor: "default" } : applyState[key] === "pending" ? { opacity: 0.6, cursor: "default" } : {}) }}>
                          {applyState[key] === "applied" ? "적용됨" : applyState[key] === "pending" ? "적용 중…" : applyState[key] === "failed" ? "다시 적용" : "적용"}
                        </button>
                      </span>
                    </div>
                    <CodeRemovalWarning check={op.removal_check} />
                    {applyState[key] === "failed" && applyErrors[key] && (
                      <div style={{ background: "var(--vscode-inputValidation-errorBackground, rgba(239,68,68,0.1))", borderTop: "1px solid var(--vscode-inputValidation-errorBorder, #ef4444)", padding: "5px 8px", fontSize: 10.5, color: "var(--vscode-errorForeground, #f48771)" }}>
                        {applyErrors[key]}
                      </div>
                    )}
                    {previewOpen && <pre style={{ margin: 0, background: "var(--vscode-textCodeBlock-background, #1e1e1e)", color: "var(--vscode-editor-foreground, #ddd)", padding: "6px 8px", fontFamily: "var(--vscode-editor-font-family, monospace)", fontSize: 10.5, maxHeight: 150, overflow: "auto", whiteSpace: "pre", lineHeight: 1.5 }}>
                      {op.content.length > 1000 ? op.content.slice(0, 1000) + "\n…" : op.content}
                    </pre>}
                    {warned && (
                      <div style={{ background: "rgba(216,165,92,0.12)", borderTop: "1px solid rgba(216,165,92,0.3)", padding: "5px 8px", fontSize: 10.5, color: "#d6a55c" }}>
                        키가 코드에 포함된 것 같습니다 ({op.secret_warnings!.length}건). .env로 옮기세요.
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ))}

      {/* 입력 — 위치 · 참고 파일 · 보내기를 입력창 한 덩어리 안에 둔다 */}
      <div className={hasTurns ? "rc-cg-dock" : undefined}>
        {(connectionPending || connectionError || !isActive) && (
          <div role="status" className="rc-cg-notice">
            {connectionPending ? "AI 연결을 확인하고 있습니다. 요청을 미리 입력할 수 있습니다." : connectionError || "AI 연결 설정을 확인해 주세요. 상단 연결 상태에서 자세한 내용을 볼 수 있습니다."}
            {!connectionPending && <button onClick={() => postMessage("runDiagnostics")} style={{ ...linkBtn, marginLeft: 10 }}>연결 다시 확인</button>}
            {!connectionPending && !isActive && <button onClick={() => postMessage("ai.connect")} style={{ ...linkBtn, marginLeft: 10 }}>AWS 없이 API 키로 연결</button>}
          </div>
        )}
        <div className="rc-cg-box">
          <textarea
            ref={inputRef}
            className="rc-cg-input"
            aria-label="AI 개발 요청"
            value={input}
            rows={hasTurns ? 1 : 3}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (!e.nativeEvent.isComposing && (e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); send(); } }}
            placeholder={hasTurns ? "이어서 수정할 점 (예: 버튼 색을 파랑으로)" : "만들거나 고칠 내용 (예: SQLite 게시판 REST API를 FastAPI로)"}
            disabled={isBusy}
          />
          <div className="rc-cg-tools">
            <div className="rc-cg-ctx">
            <button type="button" className="rc-cg-chip" onClick={() => postMessage("code.pickFolder")} title={targetFolder ? `코드를 만들 위치: ${targetFolder}` : "코드를 만들 위치: 현재 프로젝트 루트"}>
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" aria-hidden="true"><path d="M1.5 4.5h4l1.5 1.5h7.5v7h-13z" /></svg>
              <span>{folderLabel(targetFolder)}</span>
            </button>
            {targetFolder && <button type="button" className="rc-cg-x" aria-label="위치 지우기" title="루트로" onClick={() => setTargetFolder("")} style={{ marginLeft: -4 }}>×</button>}
            {contextFiles.map((c) => (
              <span key={c.path} className="rc-cg-file" title={c.path}>
                <span>{c.path}</span>
                <button type="button" className="rc-cg-x" aria-label={`${c.path} 참고 파일 제거`} onClick={() => setContextFiles((cur) => cur.filter((x) => x.path !== c.path))}>×</button>
              </span>
            ))}
            <button type="button" className="rc-cg-chip quiet" aria-label="참고 파일 추가" onClick={() => postMessage("code.pickContext")}>
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" aria-hidden="true"><path d="M10.5 4.5 5 10a1.8 1.8 0 0 0 2.5 2.5L13 7a3.2 3.2 0 0 0-4.5-4.5L3 8" /></svg>
              <span>참고 파일</span>
            </button>
            </div>
            <div className="rc-cg-actions">
            <span className="rc-cg-kbd">Ctrl+Enter</span>
            <button type="button" onClick={send} disabled={!input.trim() || isBusy} className="rc-cg-send" title="보내기 (Ctrl+Enter)">
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" /></svg>
              <span className="rc-cg-sr">보내기</span>
            </button>
            </div>
          </div>
        </div>
        {!hasTurns && (
          <div className="rc-cg-examples">
            {EXAMPLES.map((ex) => (
              <button type="button" key={ex.label} onClick={() => { setInput(ex.text); inputRef.current?.focus(); }}>{ex.label}</button>
            ))}
          </div>
        )}
      </div>
    </section>
  );
};

export default CodeAgent;
