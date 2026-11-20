import type { DiscordOpsResult } from './DiscordController';

/** 배포 화면과 같은 Core 기록(GET /api/deploy/canvas)을 Discord 에 보낼 짧은 상태로 바꾼다. */
interface CanvasSnapshotLike {
    deployment?: { running?: boolean; stage?: string; stage_text?: string; deployment_id?: string; service_url?: string; finished_at?: string; started_at?: string; error?: string };
    resource?: { cluster?: string; service?: string; region?: string } | null;
    s3?: { bucket?: string; region?: string } | null;
    scan?: { blocked?: boolean } | null;
}

export function ago(iso: string | undefined, now = Date.now()): string {
    const t = iso ? Date.parse(iso) : NaN;
    if (!Number.isFinite(t)) return '';
    const min = Math.max(0, Math.round((now - t) / 60000));
    if (min < 1) return '방금';
    if (min < 60) return `${min}분 전`;
    const hours = Math.round(min / 60);
    if (hours < 48) return `${hours}시간 전`;
    return `${Math.round(hours / 24)}일 전`;
}

export function statusSummary(projectName: string, snapshot: CanvasSnapshotLike | null | undefined, now = Date.now()): DiscordOpsResult {
    const d = snapshot?.deployment ?? {};
    const r = snapshot?.resource;
    const where = r?.cluster || r?.service ? `ECS ${[r?.cluster, r?.service].filter(Boolean).join(' / ')}` : '';
    const blocked = snapshot?.scan?.blocked ? '보안 게이트가 마지막 배포를 막았어요.' : '';
    const lines = (...parts: string[]) => parts.filter(Boolean).join('\n');
    if (d.running) {
        return { ok: true, state: 'status', title: `${projectName} · 배포 진행 중`, detail: lines(where, d.stage_text || '배포를 진행하고 있어요.') };
    }
    if (d.deployment_id && d.stage === 'done') {
        return { ok: true, state: 'status', title: `${projectName} · 마지막 배포 완료`, detail: lines([where, ago(d.finished_at, now)].filter(Boolean).join(' · '), blocked), url: d.service_url || '' };
    }
    if (d.deployment_id && d.stage === 'failed') {
        return { ok: false, state: 'status', title: `${projectName} · 마지막 배포 실패`, detail: lines([where, ago(d.finished_at || d.started_at, now)].filter(Boolean).join(' · '), d.error || d.stage_text || '', blocked) };
    }
    if (snapshot?.s3?.bucket) {
        return { ok: true, state: 'status', title: `${projectName} · S3 정적 사이트`, detail: `버킷 ${snapshot.s3.bucket}${snapshot.s3.region ? ` (${snapshot.s3.region})` : ''}` };
    }
    return { ok: true, state: 'status', title: `${projectName} · 아직 배포 기록이 없어요`, detail: '"배포해줘"라고 보내면 VS Code에서 승인한 뒤 배포해요.' };
}
