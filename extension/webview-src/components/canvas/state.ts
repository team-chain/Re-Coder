import type { EcsProgressStatus } from "../EcsDeploymentProgress";

/** Keep the response eligible even when AWS takes longer than the polling interval. */
export class SnapshotRequests {
  private pending = '';
  begin(id: string): boolean {
    if (this.pending) return false;
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
