/**
 * 적용 전 dry run(2.0.34) — 생성 결과를 파일에 쓰기 전에, 그 내용으로 흐름을 다시 그려 바뀌는 함수와
 * 그 변경이 지나가는 API 경로·화면 동작을 보여 준다. 사용자는 무엇이 바뀌는지 보고 나서 적용한다.
 */
import React, { useCallback, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import FlowDryRun, { FlowFinding } from "./FlowDryRun";
import type { FlowPreview } from "./dryRunModel";

interface Op { file: string; content: string; action?: string; }
type State = { status: "idle" } | { status: "loading" } | { status: "error"; message: string }
  | { status: "ready"; preview: FlowPreview; prefix: string; projectRoot: string };

export const FlowPreviewPanel: React.FC<{
  turnId: number; ops: Op[]; targetFolder?: string; projectRoot?: string; findings?: FlowFinding[];
}> = ({ turnId, ops, targetFolder, projectRoot, findings }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<State>({ status: "idle" });
  const key = `flow:${turnId}`;

  useMessage(useCallback((msg) => {
    if (msg.type !== "code.flowPreviewResult") return;
    const p = (msg.payload ?? {}) as { key?: string; error?: string; preview?: FlowPreview; prefix?: string; projectRoot?: string };
    if (p.key !== key) return;
    if (p.error || !p.preview) setState({ status: "error", message: p.error ?? "흐름을 분석하지 못했습니다." });
    else setState({ status: "ready", preview: p.preview, prefix: p.prefix ?? "", projectRoot: p.projectRoot ?? "" });
  }, [key]));

  const request = useCallback(() => {
    setState({ status: "loading" });
    postMessage("code.flowPreview", {
      key, targetFolder, projectRoot,
      ops: ops.map((op) => ({ file: op.file, content: op.content, action: op.action })),
    });
  }, [key, ops, postMessage, projectRoot, targetFolder]);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && state.status !== "ready" && state.status !== "loading") request();
  };

  const ghost: React.CSSProperties = {
    background: "none", border: "1px solid #3fd6de66", color: "#3fd6de", fontSize: 11,
    borderRadius: 6, padding: "3px 9px", cursor: "pointer",
  };
  const changedCount = state.status === "ready" ? Object.keys(state.preview.changed).length : 0;
  const affectedCount = state.status === "ready" ? state.preview.affected.length : 0;

  return (
    <div data-testid="flow-preview" style={{ margin: "0 0 8px" }}>
      <button style={ghost} onClick={toggle} aria-expanded={open}
        title="적용하기 전에, 이 변경이 어떤 함수와 API 경로를 지나가는지 실행하지 않고 따라가 봅니다">
        {open ? "▾" : "▸"} 적용 전 흐름 미리 보기 (dry run)
      </button>
      {open && (
        <div style={{ marginTop: 8, border: "1px solid var(--vscode-panel-border, #333)", borderRadius: 6, padding: 10 }}>
          {state.status === "loading" && <div style={{ fontSize: 11.5, color: "var(--vscode-descriptionForeground)" }}>바뀔 내용으로 흐름을 분석하는 중…</div>}
          {state.status === "error" && (
            <div style={{ fontSize: 11.5, color: "#ff6573" }}>
              {state.message} <button style={{ ...ghost, marginLeft: 6 }} onClick={request}>다시 시도</button>
            </div>
          )}
          {state.status === "ready" && (
            <>
              <div data-testid="flow-preview-summary" style={{ fontSize: 11.5, marginBottom: 8, lineHeight: 1.5 }}>
                {changedCount
                  ? <>이번 변경으로 함수 <b>{changedCount}</b>개가 새로 생기거나 바뀌고, 진입점 <b>{affectedCount}</b>곳이 그 함수를 지나갑니다.</>
                  : <>호출 흐름이 바뀌는 함수가 없습니다(설정·문서·화면 파일만 바뀜).</>}
                <button style={{ ...ghost, marginLeft: 8, fontSize: 10.5 }} onClick={request}>다시 분석</button>
              </div>
              <FlowDryRun graph={state.preview.graph} changed={state.preview.changed} removed={state.preview.removed}
                affected={state.preview.affected} findings={findings} findingsPrefix={state.prefix}
                onOpen={(file, line) => postMessage("map.openFile", { id: file, line, projectRoot: state.projectRoot })} />
            </>
          )}
        </div>
      )}
    </div>
  );
};

export default FlowPreviewPanel;
