import React from "react";

export interface RemovalCheck {
  status: "checked" | "new_file" | "unavailable";
  removed: Array<{ kind: "function" | "route"; name: string; line: number }>;
  reason?: string;
}

const warningStyle: React.CSSProperties = {
  background: "var(--vscode-inputValidation-warningBackground, rgba(216,165,92,0.12))",
  border: "1px solid var(--vscode-inputValidation-warningBorder, #d6a55c)",
  borderRadius: 4, padding: "7px 9px", fontSize: 11, overflowWrap: "anywhere",
};

export function removalCounts(checks: Array<RemovalCheck | undefined>) {
  const items = checks.flatMap((check) => check?.removed ?? []);
  return {
    functions: items.filter((item) => item.kind === "function").length,
    routes: items.filter((item) => item.kind === "route").length,
    unchecked: checks.filter((check) => !check || check.status === "unavailable").length,
  };
}

export function CodeRemovalSummary({ checks }: { checks: Array<RemovalCheck | undefined> }) {
  const { functions, routes, unchecked } = removalCounts(checks);
  if (!functions && !routes && !unchecked) { return null; }
  return (
    <div role="status" style={{ ...warningStyle, marginBottom: 7 }}>
      {(functions > 0 || routes > 0) && <strong>기존 코드 삭제 후보: 함수 {functions}개 · 라우트 {routes}개</strong>}
      {unchecked > 0 && <div>삭제 검사 미완료: {unchecked}개 파일</div>}
      <div style={{ marginTop: 4 }}>적용 전에 아래 경고와 변경 보기를 확인하세요.</div>
    </div>
  );
}

export function CodeRemovalWarning({ check }: { check?: RemovalCheck }) {
  if (!check || check.status === "unavailable") {
    return (
      <div role="status" style={warningStyle}>
        <strong>삭제 검사 미완료</strong>
        <div>{check?.reason ?? "삭제 검사 정보가 없습니다. Core를 업데이트하고 변경 보기를 확인하세요."}</div>
      </div>
    );
  }
  if (!check.removed.length) { return null; }
  const { functions, routes } = removalCounts([check]);
  return (
    <div role="status" style={warningStyle}>
      <strong>삭제 후보: 함수 {functions}개 · 라우트 {routes}개</strong>
      <ul style={{ margin: "5px 0", paddingLeft: 18, maxHeight: 180, overflow: "auto" }}>
        {check.removed.map((item, i) => (
          <li key={i}>{item.kind === "route" ? "라우트" : "함수"} <code>{item.name}</code> · 기존 {item.line}행</li>
        ))}
      </ul>
      <div>생성 시점의 저장 파일 기준입니다. 이름 변경·이동도 포함될 수 있으니 변경 보기에서 확인하세요.</div>
    </div>
  );
}
