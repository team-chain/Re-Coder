interface DockerResult { plan_id?: string; deployment_id?: string; status?: string; health_ok?: boolean }
export interface DockerNotification { id: string; title: string; detail: string }

/** Only live local deployment results are eligible; history/other deployments are ignored. */
export class DockerNotifications {
  private plans = new Map<string, { deploymentId?: string; sent: Set<string> }>();
  consume(type: string, payload: any): DockerNotification | null {
    if (!payload) return null;
    if (type === 'deploy.verificationStatus') {
      const entry = [...this.plans].find(([, value]) => value.deploymentId === payload.deploymentId);
      if (!entry || !payload.deploymentId || payload.snapshot?.deployment_id !== payload.deploymentId) return null;
      const status = payload.snapshot.status;
      return status === 'stable' ? this.event(entry[0], 'done')
        : ['unstable', 'error'].includes(status) ? this.event(entry[0], 'failed') : null;
    }
    if (type !== 'deployResult' && type !== 'deploy.progress') return null;
    const result = payload as DockerResult;
    if (!result.plan_id) return null;
    if (!this.plans.has(result.plan_id)) {
      this.plans.set(result.plan_id, { sent: new Set() });
      if (this.plans.size > 60) this.plans.delete(this.plans.keys().next().value!);
    }
    const plan = this.plans.get(result.plan_id)!;
    if (type === 'deploy.progress') {
      // The terminal result, not the stream's "done" marker, confirms health.
      return payload.step === 'error' ? this.event(result.plan_id, 'failed') : null;
    }
    if (result.deployment_id) plan.deploymentId = result.deployment_id;
    if (['failed', 'error'].includes(result.status || '')) return this.event(result.plan_id, 'failed');
    if (!['success', 'pending'].includes(result.status || '')) return null;
    return this.event(result.plan_id, result.health_ok === true ? 'done' : 'pending');
  }
  private event(planId: string, outcome: 'done' | 'pending' | 'failed'): DockerNotification | null {
    const plan = this.plans.get(planId)!;
    if (plan.sent.has(outcome) || (outcome === 'pending' && (plan.sent.has('done') || plan.sent.has('failed')))) return null;
    plan.sent.add(outcome);
    const title = outcome === 'done' ? 'Docker 배포 완료' : outcome === 'failed' ? 'Docker 배포 실패' : 'Docker 헬스 확인 대기';
    const summary = outcome === 'done' ? '로컬 Docker 컨테이너 실행과 헬스 확인을 완료했습니다.'
      : outcome === 'failed' ? '로컬 Docker 배포 또는 헬스 확인에 실패했습니다. VS Code의 Docker 화면에서 원인을 확인하세요.'
      : '컨테이너 실행 요청이 끝났습니다. 앱 응답을 확인한 뒤 완료 알림을 보냅니다.';
    // Never forward raw build logs, environment variables, or exception strings.
    return { id: `docker:${planId}:${outcome}`, title, detail: `${summary}${plan.deploymentId ? `\n배포 ID: ${plan.deploymentId}` : ''}` };
  }
}
