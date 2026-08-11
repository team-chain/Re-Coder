/** ReCoder Workspace 안에서 사용하는 배포 센터와 AI-DLC 배포 결정 카드. */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { DecisionOptionCards } from "./DecisionOptionCards";
import { AwsConnection } from "./AwsConnection";

type Target = "decision" | "docker" | "actions" | "ec2" | "ecs" | "s3" | "aws";
type Proposal = { proposal_id: string; target_path: string; content: string; approval_level: number };
type DeployTarget = "ecs" | "s3" | "local";
type Preflight = { app_kind: "server" | "static" | "unknown"; summary: string; evidence: string[]; recommended_target: DeployTarget };

const input: React.CSSProperties = {
  width: "100%", background: "var(--vscode-input-background, #252526)", color: "var(--vscode-input-foreground, #ddd)",
  border: "1px solid var(--vscode-input-border, #3f3f3f)", borderRadius: 5, padding: "7px 9px", fontSize: 12,
};
const button: React.CSSProperties = {
  background: "var(--vscode-button-background, #0e639c)", color: "var(--vscode-button-foreground, #fff)",
  border: "none", borderRadius: 5, padding: "8px 12px", fontSize: 12, fontWeight: 600, cursor: "pointer",
};

const choices: Array<{ key: DeployTarget; icon: string; label: string; summary: string; detail: string }> = [
  { key: "ecs", icon: "▣", label: "ECS 컨테이너", summary: "서버형 앱을 안정적으로 운영", detail: "API·백그라운드 작업·데이터 저장이 필요한 앱에 적합" },
  { key: "s3", icon: "◇", label: "S3 정적 호스팅", summary: "빌드된 정적 파일을 빠르게 제공", detail: "HTML·CSS·JS 중심의 프론트엔드에 적합" },
  { key: "local", icon: "○", label: "나중에 · 로컬 먼저", summary: "원격 배포 전 내 컴퓨터에서 검증", detail: "AWS 설정 없이 Docker로 먼저 확인" },
];

const ECS_BUDGETS_GUIDE = "https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html";

const EcsCostNotice: React.FC = () => (
  <div style={{ margin: "12px 0", padding: "10px 11px", borderRadius: 6, border: "1px solid rgba(55, 148, 255, .35)", background: "rgba(55, 148, 255, .10)", fontSize: 11, lineHeight: 1.6 }}>
    <div style={{ fontWeight: 700, color: "var(--vscode-textLink-foreground, #75beff)" }}>예상 비용 · 최소 ECS Fargate 사양</div>
    <div style={{ marginTop: 3 }}>0.25 vCPU · 0.5 GB를 서울 리전에서 24시간 가동하면 월 <b>약 US$12</b>부터 예상됩니다.</div>
    <div style={{ marginTop: 3, color: "var(--vscode-descriptionForeground, #999)" }}>로드 밸런서, 공인 IP, 데이터 전송, ECR 저장소 비용은 포함하지 않은 대략적인 Fargate 실행 비용입니다. 데모가 끝나면 서비스를 중지하세요.</div>
    <div style={{ marginTop: 5, color: "var(--vscode-descriptionForeground, #999)" }}>예산 알람: AWS 콘솔 → <b>Billing and Cost Management</b> → <b>Budgets</b> → <b>Create budget</b>에서 월 예산과 이메일 알림을 설정하세요.</div>
    <a href={ECS_BUDGETS_GUIDE} target="_blank" rel="noreferrer" style={{ display: "inline-block", marginTop: 7, color: "var(--vscode-textLink-foreground, #75beff)" }}>AWS 예산 알람 설정하기 →</a>
  </div>
);

export const DeploymentCenter: React.FC<{ onOpenDocker: () => void }> = ({ onOpenDocker }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [target, setTarget] = useState<Target>("decision");
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [message, setMessage] = useState("");
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [checking, setChecking] = useState(true);
  const [savingDecision, setSavingDecision] = useState(false);
  const [checkingEcsPermissions, setCheckingEcsPermissions] = useState(false);
  const pendingEcsDeploymentRef = useRef<Record<string, unknown> | null>(null);
  const [awsReady, setAwsReady] = useState(false);
  const [ec2, setEc2] = useState({ image_name: "recoder-app", tag: "latest", host_port: "8000", container_port: "8000", aws_region: "ap-northeast-2", ecr_registry: "", ec2_host: "", ec2_ssh_key: "", ec2_user: "ec2-user" });
  const [ecs, setEcs] = useState({ image_name: "recoder-app", tag: "latest", aws_region: "ap-northeast-2", ecr_registry: "", ecs_cluster: "", ecs_service: "", task_family: "recoder-task", container_port: "8000", cpu: "256", memory: "512" });

  const runPreflight = useCallback(() => {
    setChecking(true);
    setMessage("");
    postMessage("workspace.deploy.preflight");
  }, [postMessage]);

  useMessage(useCallback((event) => {
    const { type, payload } = event;
    if (type === "proposalReady") {
      const p = payload as Proposal & { file_type?: string };
      if (p.file_type === "github-actions") { setProposal(p); setMessage("GitHub Actions 워크플로우가 생성되었습니다. 내용을 확인하고 저장하세요."); }
    }
    if (type === "workspace.deploy.preflightResult") { setPreflight(payload as Preflight); setChecking(false); }
    if (type === "workspace.deploy.preflightError") { setChecking(false); setMessage((payload as { message?: string })?.message ?? "프로젝트 감지에 실패했습니다."); }
    if (type === "workspace.deploy.decisionResult") {
      const result = payload as { next_view: Target; adr_path: string };
      setSavingDecision(false);
      setTarget(result.next_view);
      setMessage(`선택 근거를 ${result.adr_path}에 기록했습니다.`);
    }
    if (type === "workspace.deploy.decisionError") { setSavingDecision(false); setMessage((payload as { message?: string })?.message ?? "배포 대상 기록에 실패했습니다."); }
    if (type === "aws.permissions.result" && pendingEcsDeploymentRef.current) {
      const result = payload as { ok?: boolean; status?: { permission_check?: { inspected?: boolean; missing_actions?: string[]; warnings?: string[] } }; message?: string };
      const deploymentRequest = pendingEcsDeploymentRef.current;
      pendingEcsDeploymentRef.current = null;
      setCheckingEcsPermissions(false);
      const permission = result.status?.permission_check;
      if (result.ok && permission?.inspected && (permission.missing_actions?.length ?? 0) === 0) {
        setMessage("ECS 배포 대상 리전의 권한을 확인했습니다. 배포를 시작합니다…");
        postMessage("workspace.deploy.ecs", deploymentRequest);
      } else {
        const detail = permission?.missing_actions?.length
          ? `부족 권한: ${permission.missing_actions.join(", ")}`
          : (permission?.warnings?.[0] ?? result.message ?? "ECS 배포 대상 권한을 완료 확인하지 못했습니다.");
        setMessage(`ECS 배포를 시작하지 않았습니다. ${detail}`);
      }
    }
    if (type === "workspace.deploy.result") setMessage((payload as { message?: string })?.message ?? "배포 요청을 보냈습니다.");
    if (type === "aws.status") setAwsReady(Boolean((payload as { ready?: boolean })?.ready));
    if (type === "errorMessage") setMessage((payload as { message?: string })?.message ?? "요청 처리에 실패했습니다.");
  }, []));

  useEffect(() => { runPreflight(); postMessage("aws.status"); }, [runPreflight, postMessage]);
  useEffect(() => {
    if (target !== "ec2" && target !== "ecs") return;
    const timer = window.setInterval(() => postMessage(target === "ec2" ? "workspace.deploy.ec2.status" : "workspace.deploy.ecs.status"), 4000);
    return () => window.clearInterval(timer);
  }, [target, postMessage]);

  const chooseTarget = (choice: DeployTarget) => {
    if (!preflight || savingDecision) return;
    if (choice !== "local" && !awsReady) {
      setTarget("aws");
      setMessage("원격 배포 전에 AWS 계정을 연결하세요.");
      return;
    }
    setSavingDecision(true);
    postMessage("workspace.deploy.chooseTarget", { target: choice, evidence: preflight.evidence });
  };
  const generateActions = () => { setMessage("GitHub Actions 워크플로우 생성 중…"); postMessage("generateGithubActions", { workspacePath: "" }); };
  const deployEc2 = () => { setMessage("EC2 배포 요청 전송 중…"); postMessage("workspace.deploy.ec2", { ...ec2, host_port: Number(ec2.host_port), container_port: Number(ec2.container_port) }); };
  const deployEcs = () => {
    if (!awsReady) { setTarget("aws"); setMessage("ECS 배포를 시작하려면 AWS 계정을 연결하세요."); return; }
    setCheckingEcsPermissions(true);
    setMessage("입력한 ECS 리전과 대상 리소스의 권한을 확인 중…");
    pendingEcsDeploymentRef.current = { ...ecs, container_port: Number(ecs.container_port) };
    postMessage("aws.permissions.check", {
      deploymentContext: {
        // ECSDeployRequest의 repo_name 기본값과 반드시 같아야 한다.
        ecrRepo: "recoder-app",
        ecsCluster: ecs.ecs_cluster,
        ecsService: ecs.ecs_service,
        awsRegion: ecs.aws_region,
      },
    });
  };
  const update = <T extends Record<string, string>>(set: React.Dispatch<React.SetStateAction<T>>, key: keyof T, value: string) => set(cur => ({ ...cur, [key]: value }));

  return (
    <div style={{ fontFamily: "var(--vscode-font-family)", color: "var(--vscode-foreground, #ddd)" }}>
      <div style={{ fontSize: 18, fontWeight: 650, marginBottom: 5 }}>배포 센터</div>
      <div style={{ fontSize: 12, color: "var(--vscode-descriptionForeground, #999)", marginBottom: 16 }}>감지 결과를 확인하고, 배포 대상은 직접 선택하세요.</div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 7, marginBottom: 18 }}>
        {([ ["decision", "배포 결정"], ["aws", awsReady ? "AWS 연결됨" : "AWS 연결"], ["docker", "Local"], ["actions", "Actions"], ["ec2", "EC2"], ["ecs", "ECS"] ] as [Target, string][]).map(([id, label]) => <button key={id} onClick={() => { setTarget(id); setMessage(""); }} style={{ padding: "9px 6px", borderRadius: 6, border: `1px solid ${target === id ? "var(--vscode-focusBorder, #3794ff)" : "var(--vscode-panel-border, #3f3f3f)"}`, background: target === id ? "var(--vscode-list-activeSelectionBackground, #094771)" : "var(--vscode-editorWidget-background, #252526)", color: id === "aws" && awsReady ? "var(--vscode-charts-green, #4ec9b0)" : "var(--vscode-foreground, #ddd)", cursor: "pointer", fontSize: 11, fontWeight: target === id ? 600 : 400 }}>{label}</button>)}
      </div>

      {target === "aws" && <AwsConnection />}

      {target === "decision" && <div style={{ border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 9, overflow: "hidden", background: "var(--vscode-editorWidget-background, #252526)" }}>
        <div style={{ padding: "14px 16px", borderBottom: "1px solid var(--vscode-panel-border, #3f3f3f)", background: "linear-gradient(120deg, rgba(55,148,255,.16), transparent)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 14, fontWeight: 700 }}><span>⌕</span> {checking ? "프로젝트 구성 확인 중…" : `감지됨: ${preflight?.summary}`}</div>
          {!checking && <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10 }}>{preflight?.evidence.map(item => <span key={item} style={{ padding: "3px 7px", borderRadius: 99, fontSize: 11, color: "var(--vscode-textLink-foreground, #75beff)", background: "rgba(55,148,255,.13)", border: "1px solid rgba(55,148,255,.28)" }}>{item}</span>)}</div>}
          <button onClick={runPreflight} disabled={checking} style={{ marginTop: 11, border: "none", background: "transparent", padding: 0, color: "var(--vscode-textLink-foreground, #75beff)", cursor: "pointer", fontSize: 11 }}>다시 감지</button>
        </div>
        <div style={{ padding: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 650, margin: "0 0 9px 2px" }}>이 앱을 어디에 배포할까요?</div>
          <DecisionOptionCards options={choices.map(choice => ({ ...choice, label: `${choice.icon}  ${choice.label}`, recommended: preflight?.recommended_target === choice.key }))} onSelect={(key) => chooseTarget(key as DeployTarget)} disabled={checking || savingDecision} />
          <div style={{ margin: "11px 2px 1px", fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>선택 근거는 <code>docs/adr</code>에 기록됩니다. 실제 원격 배포는 필요한 설정을 확인한 다음 시작됩니다.</div>
        </div>
      </div>}

      {target === "docker" && <div style={{ border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 7, padding: 16 }}><b>로컬 Docker 배포</b><p style={{ color: "var(--vscode-descriptionForeground, #999)", lineHeight: 1.55 }}>Dockerfile 생성, 보안 스캔, build/run 및 헬스체크를 진행합니다.</p><button onClick={onOpenDocker} style={button}>로컬에서 검증 시작</button></div>}
      {target === "s3" && <div style={{ border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 7, padding: 16 }}><b>S3 정적 호스팅</b><p style={{ color: "var(--vscode-descriptionForeground, #999)", lineHeight: 1.55 }}>정적 배포 대상을 선택했고 ADR에 기록했습니다. 다음으로 CI/CD 워크플로우를 생성해 검토·승인할 수 있습니다.</p><button onClick={generateActions} style={button}>배포 워크플로우 생성</button></div>}
      {target === "actions" && <div style={{ border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 7, padding: 16 }}><b>GitHub Actions</b><p style={{ color: "var(--vscode-descriptionForeground, #999)", lineHeight: 1.55 }}>프로젝트에 맞는 CI/CD 워크플로우를 생성하고, 승인 후 <code>.github/workflows/deploy.yml</code>에 저장합니다.</p><button onClick={generateActions} style={button}>워크플로우 생성</button>{proposal && <><pre style={{ marginTop: 12, maxHeight: 280, overflow: "auto", background: "var(--vscode-textCodeBlock-background, #1e1e1e)", borderRadius: 5, padding: 10, fontSize: 11 }}>{proposal.content}</pre><button onClick={() => postMessage("approveGithubActions", { proposalId: proposal.proposal_id, approved: true })} style={{ ...button, marginTop: 10 }}>승인하고 저장</button></>}</div>}
      {target === "ec2" && <div style={{ border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 7, padding: 16 }}><b>EC2 배포</b><div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 12 }}>{([ ["image_name", "이미지"], ["tag", "태그"], ["host_port", "호스트 포트"], ["container_port", "컨테이너 포트"], ["aws_region", "AWS 리전"], ["ecr_registry", "ECR Registry"], ["ec2_host", "EC2 Host"], ["ec2_ssh_key", "SSH 키 경로"], ["ec2_user", "EC2 사용자"] ] as [keyof typeof ec2, string][]).map(([key, label]) => <label key={key} style={{ fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>{label}<input value={ec2[key]} onChange={e => update(setEc2, key, e.target.value)} style={{ ...input, marginTop: 4 }} /></label>)}</div><button onClick={deployEc2} style={{ ...button, marginTop: 14 }}>EC2 배포 실행</button></div>}
      {target === "ecs" && <div style={{ border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 7, padding: 16 }}><b>ECS Fargate 배포</b><p style={{ color: "var(--vscode-descriptionForeground, #999)", fontSize: 11, lineHeight: 1.45 }}>선택 근거가 ADR에 기록되었습니다. 입력한 리전과 ECS 대상의 권한을 확인한 뒤 배포를 시작합니다.</p><EcsCostNotice /><div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 12 }}>{([ ["image_name", "이미지"], ["tag", "태그"], ["aws_region", "AWS 리전"], ["ecr_registry", "ECR Registry"], ["ecs_cluster", "ECS Cluster"], ["ecs_service", "ECS Service"], ["task_family", "Task Family"], ["container_port", "컨테이너 포트"], ["cpu", "CPU"], ["memory", "Memory"] ] as [keyof typeof ecs, string][]).map(([key, label]) => <label key={key} style={{ fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>{label}<input value={ecs[key]} onChange={e => update(setEcs, key, e.target.value)} style={{ ...input, marginTop: 4 }} /></label>)}</div><button disabled={checkingEcsPermissions} onClick={deployEcs} style={{ ...button, marginTop: 14, opacity: checkingEcsPermissions ? .7 : 1 }}>{checkingEcsPermissions ? "배포 권한 확인 중…" : "ECS 배포 실행"}</button></div>}
      {message && <div style={{ marginTop: 12, padding: "8px 10px", borderRadius: 5, background: "var(--vscode-editorInfo-background, rgba(55,148,255,.12))", color: "var(--vscode-editorInfo-foreground, #75beff)", fontSize: 12 }}>{message}</div>}
    </div>
  );
};

export default DeploymentCenter;
