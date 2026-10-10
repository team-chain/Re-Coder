/**
 * 생성 결과에 끝까지 남은 문제 — 일관성 점검·빌드 검증이 고치지 못한 오류.
 *
 *  · 남은 오류가 있으면 결과 맨 위에 빨간 상자로 "빌드 실패 예상 N건" 과 각 원인·해결 방법을 보여 준다.
 *  · 확인 체크 전에는 적용 버튼을 잠근다 — 모르고 깨진 코드를 적용하지 않게(알고 적용하는 것은 막지 않는다).
 */
import React from "react";
import { Checkbox } from "./Check";

export interface ConsistencyIssue { code: string; severity: string; file?: string; message: string; fix?: string }

/** 적용을 잠글 오류(같은 내용은 한 번만). 경고는 넣지 않는다. */
export function blockingIssues(issues?: ConsistencyIssue[] | null): ConsistencyIssue[] {
  const seen = new Set<string>();
  const out: ConsistencyIssue[] = [];
  for (const i of issues ?? []) {
    if (!i || i.severity !== "error" || !i.message) continue;
    const key = `${i.code}|${i.file ?? ""}|${i.message}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(i);
  }
  return out;
}

/** 적용 버튼을 잠글지 — 남은 오류가 있고 아직 확인하지 않았으면 잠근다. */
export function applyLocked(issues: ConsistencyIssue[] | null | undefined, acknowledged: boolean): boolean {
  return blockingIssues(issues).length > 0 && !acknowledged;
}

const ISSUE_TITLE: Record<string, string> = {
  GENERATED_BUILD_FAILED: "컨테이너 빌드 실패",
  NODE_IMPORT_NAME_MISSING: "불러오는 이름이 없음",
  NODE_LOCAL_IMPORT_MISSING: "불러오는 파일이 없음",
  PAYMENT_MOCK_CONTRACT_MISSING: "모의 결제 모드 빠짐",
  PAYMENT_WEBHOOK_MISSING: "결제 완료 웹훅 빠짐",
  PAYMENT_DEMO_NO_LISTEN: "로컬 데모에서 서버가 안 뜸",
  GENERATED_SECRET_IN_FILE: "파일에 키가 들어감",
  UNUSED_GENERATED_FILE: "아무도 쓰지 않는 파일",
  SERVER_ROUTE_NOT_MOUNTED: "서버에 등록 안 된 API",
  GENERATED_FILE_SKIPPED: "만들지 못해 뺀 파일",
};

export const RemainingIssues: React.FC<{
  issues?: ConsistencyIssue[] | null;
  acknowledged: boolean;
  onAcknowledge: (v: boolean) => void;
}> = ({ issues, acknowledged, onAcknowledge }) => {
  const list = blockingIssues(issues);
  if (!list.length) return null;
  return (
    <section role="alert" data-testid="code-remaining-issues" aria-label="남은 문제"
      style={{ border: "1px solid var(--vscode-inputValidation-errorBorder,#be1100)", background: "var(--vscode-inputValidation-errorBackground,rgba(190,17,0,.12))",
        borderRadius: 6, padding: "8px 10px", margin: "0 0 8px", fontSize: 11, lineHeight: 1.5 }}>
      <div style={{ fontWeight: 700, color: "var(--vscode-errorForeground,#f48771)", marginBottom: 4 }}>
        빌드·실행 실패 예상 {list.length}건 — 자동 교정으로 고치지 못했습니다
      </div>
      <ol style={{ margin: "0 0 6px", paddingLeft: 18 }}>
        {list.slice(0, 6).map((i, n) => (
          <li key={n} style={{ marginBottom: 3 }}>
            <b>{ISSUE_TITLE[i.code] ?? "확인 필요"}</b>
            {i.file && <code style={{ marginLeft: 6, fontSize: 10.5 }}>{i.file}</code>}
            <div style={{ color: "var(--vscode-foreground,#ddd)", wordBreak: "break-word" }}>{i.message.length > 300 ? i.message.slice(0, 300) + "…" : i.message}</div>
            {i.fix && <div style={{ color: "var(--vscode-descriptionForeground,#999)" }}>해결: {i.fix}</div>}
          </li>
        ))}
      </ol>
      {list.length > 6 && <div style={{ color: "var(--vscode-descriptionForeground,#999)", marginBottom: 4 }}>외 {list.length - 6}건</div>}
      <Checkbox compact checked={acknowledged} onChange={onAcknowledge} tone="warn"
        label="이 문제를 확인했고, 적용한 뒤 직접(또는 배포 준비 점검의 자동 수정으로) 고치겠습니다" />
    </section>
  );
};

export interface UnusedFile { file: string; consumers?: string[] }

/** "모두 적용" 에 넣을 파일 — 아무도 쓰지 않는 파일은 사용자가 포함을 고르기 전까지 뺀다. */
export function filesToApply<T extends { file: string }>(ops: T[], unused: UnusedFile[] | null | undefined, includeUnused: boolean): T[] {
  if (includeUnused || !unused?.length) return ops;
  const skip = new Set(unused.map(u => u.file));
  return ops.filter(op => !skip.has(op.file));
}

export const UnusedFilesNote: React.FC<{
  unused?: UnusedFile[] | null;
  include: boolean;
  onInclude: (v: boolean) => void;
}> = ({ unused, include, onInclude }) => {
  if (!unused?.length) return null;
  return (
    <section data-testid="code-unused-files" aria-label="아무도 쓰지 않는 파일"
      style={{ border: "1px solid var(--vscode-panel-border,#3b3b3b)", borderRadius: 6, padding: "7px 10px", margin: "0 0 8px", fontSize: 11, lineHeight: 1.5 }}>
      <div style={{ fontWeight: 600 }}>아무도 쓰지 않는 파일 {unused.length}개 — {include ? "함께 적용합니다" : "'모두 적용' 에서 뺐습니다"}</div>
      <div style={{ color: "var(--vscode-descriptionForeground,#999)", margin: "2px 0 4px" }}>
        AI 가 만들었지만 어느 파일도 불러오지 않습니다. 빼도 앱 동작은 같습니다.
      </div>
      <ul style={{ margin: "0 0 4px", paddingLeft: 18 }}>
        {unused.slice(0, 8).map(u => <li key={u.file}><code style={{ fontSize: 10.5 }}>{u.file}</code></li>)}
      </ul>
      <Checkbox compact checked={include} onChange={onInclude} label="이 파일도 함께 적용" />
    </section>
  );
};
