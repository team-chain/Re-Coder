import type { EcsProgressStatus } from "../EcsDeploymentProgress";

export type Target = "docker" | "github" | "ecs" | "s3";
export interface Finding { node: string; kind: string; severity: string; title: string; detail: string; fix: string }
export interface Scan { blocked: boolean; gaps: string[]; tools: Array<{ tool: string; state: string; detail: string }>; findings: Array<{ tool: string; severity: string; rule: string; title: string; location: string; fix: string }> }
export interface Snapshot {
  workspace: string; projectName: string;
  container_port?: number | null;
  git: { repository: string; branch: string; dirty: boolean; connected: boolean; head?: string; initialized?: boolean; hasOrigin?: boolean; error?: string };
  aws: { ready: boolean; region: string; account: string };
  deployment: EcsProgressStatus;
  resource: null | { cluster: string; service: string; region: string; image: string; image_digest: string; previous_task_definition: string };
  topology: null | { cluster: string; service: string; region: string; desired: number | null; running: number | null; task_definition: string; observed_at: string; truncated: boolean; exposure?: Array<{name:string;dns:string;scheme:string;type:string}>; exposure_warning?:string; tasks: Array<{ id: string; status: string; health: string; launch_type: string; images: Array<{ image: string; digest: string }> }> };
  scan: Scan | null; s3: null | { bucket: string; region: string }; warnings: string[];
  projects?: Array<{ path: string; name: string; active: boolean }>;
}
export interface SceneNode { id: string; name: string; subtitle: string; badge: string; kind: string; color: string; x: number; y: number; locked?: boolean; target?: Target; flags?: string[] }
export interface SceneEdge { from: string; to: string; color: string; dashed?: boolean }
export const colors = { project: "#3b9ff5", gate: "#36c77a", docker: "#14b6ec", github: "#bda3c6", ecs: "#f28b2c", s3: "#8cbf2f", discord: "#8182ff", bad: "#ff6a73", warn: "#f4cd65", file: "#79b8ef", fn: "#4bcaba", style: "#b48ee0", locked: "#75808f" };
export function available(target: Target, snapshot: Snapshot | null): boolean {
  if (!snapshot?.workspace) return false;
  return (target !== "ecs" && target !== "s3") || snapshot.aws.ready;
}
export function gateLabel(snapshot: Snapshot | null): string {
  if (snapshot?.scan?.blocked) return "차단됨";
  if (snapshot?.deployment.running) return snapshot.deployment.stage_text || "검사·배포 중";
  if (!snapshot?.scan) return "검사 대기";
  if (snapshot.scan.tools.some(t => ["unverified", "not_run", "pending"].includes(t.state))) return "검사 미확인 항목 있음";
  return "검사 결과 확인";
}
export function overview(snapshot: Snapshot | null, discord: string, security: boolean): { nodes: SceneNode[]; edges: SceneEdge[] } {
  const locked = !snapshot?.aws.ready;
  const node = (id: string, name: string, subtitle: string, badge: string, x: number, y: number, target?: Target): SceneNode => ({ id, name, subtitle, badge, x, y, kind: id, color: colors[id as keyof typeof colors] || colors.project, target, locked: target === "ecs" || target === "s3" ? locked : false });
  const nodes = [
    node("project", snapshot?.projectName || "프로젝트", "프로젝트 컨테이너", "클릭하여 요약 보기", 180, 245),
    node("gate", "보안 게이트", "소스 · 이미지 · 정책", gateLabel(snapshot), 460, 285),
    node("github", snapshot?.git.repository || "GitHub", snapshot?.git.branch || "저장소 연결", snapshot?.git.repository ? snapshot.git.head==='' ? "첫 커밋 필요" : "커밋된 변경 푸시" : "연결 필요", 750, 240, "github"),
    node("docker", "Docker", "로컬 컨테이너", "빌드 · 실행 · 롤백", 330, 510, "docker"),
    node("ecs", snapshot?.resource?.cluster || "ECS", snapshot?.resource?.service || "Fargate 컨테이너", locked ? "AWS 연결 필요" : snapshot?.topology ? `실행 ${snapshot.topology.running ?? "?"} / 희망 ${snapshot.topology.desired ?? "?"}` : "배포 설정", 710, 545, "ecs"),
    node("s3", snapshot?.s3?.bucket || "S3", snapshot?.s3?.region || "정적 웹사이트", locked ? "AWS 연결 필요" : snapshot?.s3 ? "확인된 버킷" : "첫 배포 시 생성", 1010, 440, "s3"),
    node("discord", "Discord", discord || "채널 연결", discord ? "배포 이벤트 알림" : "클릭하여 연결", 575, 150),
  ];
  const gate = nodes.find(n=>n.id==='gate')!;
  gate.color = snapshot?.scan?.blocked ? colors.bad : snapshot?.deployment.running || snapshot?.scan?.tools.some(t=>['unverified','not_run','pending'].includes(t.state)) ? colors.warn : snapshot?.scan ? colors.gate : colors.locked;
  const edges: SceneEdge[] = [{ from: "project", to: "gate", color: colors.project }, ...(["github", "docker", "ecs", "s3"] as const).map(to => ({ from: "gate", to, color: (to === "ecs" || to === "s3") && locked ? colors.locked : colors[to], dashed: true })), { from: "gate", to: "discord", color: colors.discord, dashed: true }];
  if (snapshot?.topology && !locked) {
    const ecs = nodes.find(n => n.id === "ecs")!;
    ecs.y = 595; ecs.x = 640; ecs.kind = "cluster";
    nodes.push({id:"ecs-service",name:snapshot.topology.service,subtitle:"Service · Fargate",badge:`희망 ${snapshot.topology.desired ?? "?"} · 실행 ${snapshot.topology.running ?? "?"}`,kind:"service",color:colors.ecs,x:820,y:625});
    edges.push({from:"ecs",to:"ecs-service",color:colors.ecs});
    snapshot.topology.tasks.slice(0,4).forEach((task,i)=>{
      nodes.push({id:`task-${task.id}`,name:task.id.slice(-12),subtitle:`Task · ${task.health}`,badge:task.status,kind:"task",color:colors.ecs,x:660+(i%2)*190,y:825+Math.floor(i/2)*185});
      edges.push({from:"ecs-service",to:`task-${task.id}`,color:colors.ecs});
    });
  }
  if (security && snapshot?.scan?.findings.some(f => f.tool === "gitleaks")) {
    nodes[0] = { ...nodes[0], color: colors.bad, badge: "시크릿 탐지 · 값 숨김" };
    // The scan proves a source finding, not that it was committed or deployed.
    edges.push({ from: "project", to: "github", color: colors.bad, dashed: true });
  }
  if(security) snapshot?.topology?.exposure?.filter(lb=>lb.scheme==='internet-facing').forEach((lb,i)=>{
    nodes.push({id:`public-${i}`,name:lb.name,subtitle:`${lb.type.toUpperCase()} · internet-facing`,badge:"공개 진입점 · 연결 확인",kind:"public",color:colors.bad,x:1020,y:720+i*185});
    edges.push({from:"ecs",to:`public-${i}`,color:colors.bad});
  });
  return { nodes, edges };
}

/** Keep the reference's isometric architecture; detailed inventory is opt-in. */
export function deploymentScene(snapshot: Snapshot | null, discord: string, security: boolean, detailed = false) {
  const scene = overview(snapshot, discord, security);
  if (detailed || security) return scene;
  const nodes = scene.nodes.filter(n => n.id === 'project' || n.target || n.id === 'gate' || n.id === 'discord')
    .map(n => n.id === 'ecs' ? { ...n, kind:'ecs', x:710, y:545 } : n);
  const ids = new Set(nodes.map(n => n.id));
  return { nodes, edges: scene.edges.filter(e => ids.has(e.from) && ids.has(e.to)) };
}


