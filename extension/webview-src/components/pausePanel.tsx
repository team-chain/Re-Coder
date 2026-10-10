/**
 * 멈춘 생성 — 왜 멈췄는지와 다음에 할 수 있는 것을 보여 준다.
 *
 *  · 정해진 횟수를 넘겨 실패한 파일이 있으면: 파일과 이유를 보여 주고 [이 파일 다시 쓰기] / [이 파일 빼고 결과 받기].
 *    (다시 쓰기는 가장 작은 조각으로 처음부터 한 번 더, 빼고 받기는 AI 를 다시 부르지 않고 만든 것만 받는다.)
 *  · 그 밖(사용 한도·연결)에는 이유와 [이어서 만들기].
 */
import React from "react";

export interface FailedFile { file: string; kind?: string; reason?: string }
export interface PauseInfo { message: string; done: number; total: number; reason?: string; failed?: FailedFile[] }
export type ResumeMode = "retry" | "skip";

const btn = (primary: boolean): React.CSSProperties => ({
  border: primary ? "1px solid transparent" : "1px solid var(--vscode-button-border,var(--vscode-panel-border,#555))",
  borderRadius: 4, padding: "5px 11px", fontSize: 11.5, fontWeight: 600, cursor: "pointer", whiteSpace: "nowrap",
  background: primary ? "var(--vscode-button-background,#0e639c)" : "transparent",
  color: primary ? "var(--vscode-button-foreground,#fff)" : "var(--vscode-foreground,#ddd)",
});

//: 팀 보드 밖(한 번에 만들기)에서도 같은 모양 — 보드 CSS 에 기대지 않고 직접 칠한다.
const box = (standalone?: boolean): React.CSSProperties => ({
  alignItems: "center", gap: 8, padding: "8px 10px", fontSize: 11.5, lineHeight: 1.5, background: "rgba(204,167,0,.08)",
  ...(standalone ? { border: "1px solid rgba(204,167,0,.35)", borderRadius: 4 } : { borderTop: "1px solid var(--vscode-panel-border,#333)" }),
});

export const PausePanel: React.FC<{ paused: PauseInfo; onResume?: (mode: ResumeMode) => void; disabled?: boolean; standalone?: boolean }> = ({ paused, onResume, disabled, standalone }) => {
  const failed = paused.failed ?? [];
  const progress = paused.total ? ` (${paused.done}/${paused.total})` : "";
  if (failed.length) {
    return (
      <div className="rc-team-pause" role="status" data-testid="pause-failed" style={{ ...box(standalone), display: "block" }}>
        <div style={{ fontWeight: 600, marginBottom: 3 }}>
          ⏸ 만들지 못한 파일 {failed.length}개 — 나머지 {paused.done}/{paused.total}개는 저장했습니다
        </div>
        <ul style={{ margin: "0 0 6px", paddingLeft: 18 }}>
          {failed.slice(0, 6).map(f => (
            <li key={f.file}><code style={{ fontSize: 10.5 }}>{f.file}</code>{f.reason ? ` — 3번 시도해도 ${f.reason}` : ""}</li>
          ))}
          {failed.length > 6 && <li>외 {failed.length - 6}개</li>}
        </ul>
        {onResume && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button type="button" style={btn(true)} disabled={disabled} onClick={() => onResume("retry")}
              title="가장 작은 조각으로 이 파일만 처음부터 다시 씁니다">이 파일 다시 쓰기</button>
            <button type="button" style={btn(false)} disabled={disabled} onClick={() => onResume("skip")}
              title="AI 를 다시 부르지 않고 만든 파일만 받습니다. 빠진 파일은 결과의 남은 문제로 보여 줍니다">이 파일 빼고 결과 받기</button>
          </div>
        )}
      </div>
    );
  }
  return (
    <div className="rc-team-pause" role="status" data-testid="pause-resume" style={{ ...box(standalone), display: "flex" }}>
      <span style={{ flex: 1 }}>⏸ {paused.message}</span>
      {onResume && <button type="button" style={btn(true)} disabled={disabled} onClick={() => onResume("retry")}>이어서 만들기{progress}</button>}
    </div>
  );
};
