/**
 * Core 가 보내는 점검·위험 사유 문장을 화면용 한 줄로 줄인다.
 *
 * Core 문장은 "BLOCKER: 빌드·실행 실패 예상 — <무엇>. <왜>. 해결: <어떻게>(자동 수정 가능 — …)" 처럼
 * 원인·결과·해결을 한 문장에 모두 담는다. 같은 문장이 Dockerfile 초안 · 배포 준비 점검 · 승인 창 ·
 * 결과에 거듭 나와 화면이 글자로 덮였다. 화면에는 "무엇이 문제인가"만 한 줄로 보여 주고,
 * 원문은 접어 둔다(판정과 문장 자체는 Core 가 그대로 보낸다).
 */

const LEAD = /^(?:BLOCKER:\s*)?(?:빌드·실행 실패 예상|확인 필요|배포 전 자동 수정|배포 준비 점검)\s*[—–-]\s*/;

export type IssueKind = "blocker" | "check" | "autofix" | "other";

export function issueKind(text: string): IssueKind {
  const t = (text || "").trim();
  if (/^배포 전 자동 수정/.test(t)) return "autofix";
  if (/^BLOCKER:/.test(t)) return "blocker";
  if (/^확인 필요/.test(t)) return "check";
  return "other";
}

/** 첫 문장(문제 자체)만. 해결·자동 수정 안내와 둘째 문장은 뺀다. 너무 길면 줄임표. */
export function shortIssue(text: string, max = 96): string {
  let t = (text || "").replace(/^BLOCKER:\s*/, "").replace(LEAD, "").trim();
  t = t.split(/\s*해결:\s*/)[0];
  t = t.split(/\s*\(수정:/)[0];
  const sentence = /^([\s\S]+?[.。])(?:\s|$)/.exec(t);
  if (sentence) t = sentence[1];
  t = t.replace(/\s+/g, " ").trim();
  return t.length > max ? t.slice(0, max - 1).trimEnd() + "…" : t;
}
