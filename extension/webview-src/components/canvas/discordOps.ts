/** Discord 에서 온 배포·롤백 요청을 배포 화면의 결과와 잇는 규칙. 화면과 따로 시험한다. */
export interface DiscordOpsRequest {
  requestId: string;
  action: "deploy" | "rollback";
  requester: string;
  projectName: string;
  workspace: string;
  receivedAt: number;
  accepted: boolean;
}

export interface OpsOutcome { ok: boolean; state: "accepted" | "done" | "failed"; title: string; detail: string }

const norm = (p?: string) => (p || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();

/** 지금 배포 화면의 프로젝트에 온 요청(없으면 undefined). */
export function opsFor(requests: DiscordOpsRequest[], workspace?: string): DiscordOpsRequest | undefined {
  if (!workspace) return undefined;
  return requests.find(r => norm(r.workspace) === norm(workspace));
}

const DEPLOY_DONE = new Set(["배포 완료", "S3 배포 완료", "GitHub 푸시 완료"]);
const DEPLOY_FAILED = new Set(["배포 실패", "보안 게이트 차단", "배포 요청 차단"]);
const ROLLBACK_FAILED = /실패|fail|error|거부|denied|차단|취소/i;

/**
 * 배포 화면이 남긴 기록(제목·내용) 하나가 이 요청의 결과인가.
 * 배포는 VS Code 에서 승인한 뒤의 결과만 답한다 — 승인 전 다른 배포 결과를 Discord 요청의 결과로 착각하지 않게.
 */
export function opsOutcome(op: DiscordOpsRequest, title: string, detail: string): OpsOutcome | null {
  if (op.action === "deploy") {
    if (title === "배포 시작" && !op.accepted) return { ok: true, state: "accepted", title: "VS Code에서 배포를 시작했어요", detail: "끝나면 결과를 알려 드릴게요." };
    if (!op.accepted) return null;
    if (DEPLOY_DONE.has(title)) return { ok: true, state: "done", title, detail };
    if (DEPLOY_FAILED.has(title)) return { ok: false, state: "failed", title, detail };
    return null;
  }
  if (title === "롤백 결과") {
    const failed = ROLLBACK_FAILED.test(detail);
    return { ok: !failed, state: failed ? "failed" : "done", title: failed ? "롤백 실패" : "롤백 완료", detail };
  }
  return null;
}
