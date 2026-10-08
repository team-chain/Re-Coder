import type { EcsProgressStatus } from "../EcsDeploymentProgress";

/** Keep the response eligible even when AWS takes longer than the polling interval. */
export class SnapshotRequests {
  private pending = '';
  /**
   * force: 프로젝트가 바뀌었을 때 — 진행 중인 요청(옛 프로젝트)의 응답은 버리고 새로 받는다.
   * 예전에는 AWS 확인으로 늦어진 옛 스냅샷이 끝날 때까지 새 요청이 막혀, 프로젝트를 바꿔도
   * 배포 화면이 옛 프로젝트를 보여 줬다(실기기 재현: 위치를 바꾼 뒤 Deploy 가 이전 폴더).
   */
  begin(id: string, force = false): boolean {
    if (this.pending && !force) return false;
    this.pending = id;
    return true;
  }
  finish(id: string): boolean {
    if (!this.pending || id !== this.pending) return false;
    this.pending = '';
    return true;
  }
}

/** Snapshot and fast status polling may complete in the opposite order. */
export function mergeDeployment(current: EcsProgressStatus | null, next: EcsProgressStatus): EcsProgressStatus {
  if (!current?.deployment_id) return next;
  if (!next.deployment_id) return current;
  if (current.deployment_id === next.deployment_id) {
    if (current.observed_at && next.observed_at && Date.parse(next.observed_at) < Date.parse(current.observed_at)) return current;
    if ((current.finished_at || ['done', 'failed', 'cancelled'].includes(current.stage)) && next.running) return current;
  } else if (current.started_at && next.started_at && Date.parse(next.started_at) < Date.parse(current.started_at)) {
    return current;
  }
  return next;
}
