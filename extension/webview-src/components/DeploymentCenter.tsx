/** ReCoder Workspace 안에서 사용하는 배포 센터와 AI-DLC 배포 결정 카드. */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { DecisionOptionCards } from "./DecisionOptionCards";
import { AwsConnection } from "./AwsConnection";

//: 예전에 폼에 박혀 있던 값. **이제 기본값으로 쓰지 않는다.**
//: 여기 남겨 둔 이유는 회귀 테스트가 "이 값이 다시 기본값이 되지 않았는지"
//: 를 검사하기 때문이다.
export const FALLBACK_REGION = "ap-northeast-2";

export type AwsStatusMessage = { ready?: boolean; region?: string };

/**
 * `aws.status` 에서 **믿을 수 있는** 리전만 꺼낸다. 없으면 빈 문자열.
 *
 * 왜 ready 를 보는가
 *   `GET /api/aws/status` 는 자격증명이 없어도 리전을 돌려준다 — 그것도
 *   `AWS_REGION` 이 없으면 서버 상수 "ap-northeast-2" 다. 그걸 그대로
 *   받아 두면, 연결된 계정이 하나도 없는데도 "현재 리전은 ap-northeast-2"
 *   라고 단언하고 그걸 근거로 사용자를 막게 된다.
 */
export function coreRegionFromStatus(status: AwsStatusMessage | null | undefined): string {
  if (!status || !status.ready) { return ""; }
  return (status.region ?? "").trim();
}

/**
 * 배포 폼의 리전 기본값.
 *
 * 사고 경위
 *   폼이 리전을 "ap-northeast-2" 로 하드코딩하고 있었다. 그런데 실제
 *   자격증명(AWS Academy 랩)은 us-east-1 이었다. 그대로 「ECS 배포 실행」을
 *   누르면 자격증명이 유효하지 않은 리전으로 요청이 나가 실패한다.
 *
 * 규칙
 *   · 사용자가 한 번이라도 입력란을 건드렸으면 **그 값을 유지한다.**
 *   · 아직 안 건드렸으면 코어가 쓰는 리전으로 갈아끼운다.
 *
 * `touched` 를 따로 받는 이유
 *   예전에는 "값이 기본값과 같으면 안 건드린 것" 으로 추측했다. 그러면
 *   사용자가 **일부러** 그 값을 고른 경우와 구분이 안 돼서, 다음 aws.status
 *   가 오면 선택이 조용히 덮어써졌다.
 */
export function resolveRegionDefault(
  coreRegion: string | undefined | null,
  currentValue: string,
  touched = false,
): string {
  const core = (coreRegion ?? "").trim();
  const current = (currentValue ?? "").trim();
  if (!core) { return current; }
  if (touched) { return current; }
  return core;
}

/**
 * 폼 리전이 코어가 쓰는 리전과 다르면 실행 **전에** 낼 경고. 같으면 null.
 *
 * **막지 않는다.** 다른 리전에 일부러 배포할 수 있다(팀의 ECR/클러스터가
 * 다른 리전에 있는 경우가 흔하다). 예전 구현은 여기서 곧바로 return 해
 * 버려서 교차 리전 배포가 **아예 불가능**했고, EC2 는 원래 없던 차단까지
 * 새로 생겼다. 지금은 한 번 보여 주고, 확인하면 진행한다.
 *
 * 문구도 고쳤다 — 이 값은 자격증명에서 유도한 게 아니라 코어의
 * `AWS_REGION` 이다. "자격증명이 유효한 리전" 이라고 말하면 사실이 아니고,
 * 그 거짓말 때문에 진짜 불일치를 못 알아채게 된다.
 */
export function regionMismatchWarning(
  coreRegion: string | undefined | null,
  formRegion: string | undefined | null,
): string | null {
  const core = (coreRegion ?? "").trim().toLowerCase();
  const form = (formRegion ?? "").trim().toLowerCase();
  if (!core || !form || core === form) { return null; }
  return (
    `배포 리전(${form})이 코어가 사용 중인 리전(${core})과 다릅니다. `
    + `자격증명이 ${form} 에서 유효하지 않으면 인증에 실패합니다. `
    + `그대로 진행하려면 한 번 더 누르세요.`
  );
}

/**
 * 리전이 비어 있으면 배포를 **막는다** — 이건 취향이 아니라 필수 입력이다.
 *
 * 예전에는 빈 값이면 하드코딩된 기본값이 대신 나갔다. 사용자가 고른 적 없는
 * 리전으로 배포가 시작되고, 화면에는 나중에 다른 값이 표시됐다.
 */
export function regionBlockingError(formRegion: string | undefined | null): string | null {
  if ((formRegion ?? "").trim()) { return null; }
  return "AWS 리전을 입력하세요. (예: us-east-1)";
}

export type RegionGate = {
  //: 배포를 진행해도 되는가.
  ok: boolean;
  //: 사용자에게 보여 줄 문구(없으면 null).
  message: string | null;
  //: 다음 클릭에서 "이미 확인했다" 로 인정할 조합. 진행/차단이면 null.
  ack: string | null;
};

/**
 * 배포 직전 리전 게이트. **컴포넌트 밖에 두는 이유가 있다.**
 *
 * 이 판단이 컴포넌트 안에 있었을 때는, 「차단이 아니라 확인인가」를 검사할
 * 방법이 컴파일 결과에서 문자열을 grep 하는 것뿐이었다. 그 검사는 로직을
 * 예전의 즉시 차단으로 되돌려도 **그대로 통과했다**(변이 시험에서 확인).
 * 순수 함수로 빼면 동작 자체를 검사할 수 있다.
 *
 * 규칙
 *   · 리전이 비면 막는다 — 필수 입력이다.
 *   · 코어 리전과 다르면 **한 번** 경고하고 막는다.
 *   · 같은 조합으로 다시 누르면 진행한다 — 다른 리전에 일부러 배포하는 건
 *     정상적인 사용이다(팀의 ECR/클러스터가 다른 리전에 있는 경우).
 */
export function regionGate(
  coreRegion: string | undefined | null,
  formRegion: string | undefined | null,
  acknowledged: string,
): RegionGate {
  const blocking = regionBlockingError(formRegion);
  if (blocking) { return { ok: false, message: blocking, ack: null }; }

  const warning = regionMismatchWarning(coreRegion, formRegion);
  if (!warning) { return { ok: true, message: null, ack: null }; }

  const key = `${(coreRegion ?? "").trim()}|${(formRegion ?? "").trim()}`;
  if (acknowledged === key) { return { ok: true, message: null, ack: null }; }
  return { ok: false, message: warning, ack: key };
}

//: preflight 가 없을 때 자동 재검사 주기/횟수. 코어가 돌아오면 대개 첫
//: 한두 번에 잡힌다. 무한 재시도는 죽은 코어를 계속 두드릴 뿐이다.
export const PREFLIGHT_RETRY_INTERVAL_MS = 5000;
export const PREFLIGHT_RETRY_LIMIT = 12;   // 최대 1분

/**
 * 감지 결과 한 줄. **값이 없을 때 undefined 를 그대로 노출하지 않는다.**
 *
 * 예전에는 `감지됨: ${preflight?.summary}` 였다. 코어 연결이 끊겨 preflight 가
 * 아직(또는 영영) 없으면 화면에 그대로 `감지됨: undefined` 가 떴다. 사용자에게
 * 아무 의미 없는 문자열이고, 제품이 고장 난 것처럼 보인다.
 *
 * 문구가 약속한 재검사는 `PREFLIGHT_RETRY_*` 타이머가 실제로 수행한다.
 */
export function describeDetection(summary: string | undefined | null): string {
  const text = (summary ?? "").trim();
  if (!text) {
    return "확인할 수 없음 — 코어에 연결되면 다시 검사합니다.";
  }
  return `감지됨: ${text}`;
}

type Target = "decision" | "docker" | "actions" | "ec2" | "ecs" | "s3" | "aws";
type Proposal = { proposal_id: string; target_path: string; content: string; approval_level: number };
type DeployTarget = "ecs" | "s3" | "local";
type PreflightIssue = {
  code: string;
  message: string;
  fix: string;
  severity: string;
  remediation_available: boolean;
  proposal_id: string | null;
};
type Preflight = {
  app_kind: "server" | "static" | "unknown";
  summary: string;
  evidence: string[];
  recommended_target: DeployTarget;
  blocked?: boolean;
  score?: number;
  reasons?: PreflightIssue[];
  warnings?: PreflightIssue[];
};
type S3Deployed = {
  url: string;
  bucket: string;
  region: string;
  uploaded: string[];
  bucket_created: boolean;
  index_copied_from?: string | null;
  message: string;
};
type EcsRollbackProposal = {
  proposal_id: string;
  deployment_id: string;
  cluster: string;
  service: string;
  region: string;
  reason: string;
  previous_task_definition: string;
  current_task_definition: string;
  approval_level: number;
  status: "pending" | "approving" | "completed" | "ignored" | "failed" | "superseded";
};

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

const issueNames: Record<string, string> = {
  MISSING_REQUIRED_ENV: "필수 설정값이 빠져 있어요",
  ENV_FILE_NOT_GITIGNORED: "비밀 설정 파일이 Git에 올라갈 수 있어요",
  INVALID_ENV_FORMAT: "환경설정 파일 형식이 올바르지 않아요",
  MISSING_HEALTH_ENDPOINT: "서비스 상태 확인 주소가 없어요",
  APP_ENTRYPOINT_NOT_FOUND: "앱을 시작할 파일을 찾지 못했어요",
  MISSING_DOCKERFILE: "컨테이너 실행 설정(Dockerfile)이 없어요",
  DOCKERFILE_BUILD_RISK: "컨테이너 설정에 배포 위험이 있어요",
  HOST_PORT_CONFLICT: "사용하려는 포트가 이미 사용 중이에요",
  APP_PORT_MISMATCH: "앱 포트와 배포 포트가 서로 달라요",
  UNPINNED_DEPENDENCIES: "라이브러리 버전이 고정되지 않았어요",
  CRITICAL_VULNERABILITY: "의존성에서 심각한 보안 취약점이 발견됐어요",
  SECRET_LEAK_RISK: "코드에 비밀키가 직접 들어 있을 수 있어요",
};

const DeploymentBlockers: React.FC<{
  reasons: PreflightIssue[];
  checking: boolean;
  applyingProposalId: string | null;
  onApply: (proposalId: string) => void;
  onRerun: () => void;
}> = ({ reasons, checking, applyingProposalId, onApply, onRerun }) => (
  <div style={{ border: "1px solid rgba(241, 76, 76, .62)", borderRadius: 7, padding: 13, background: "rgba(241, 76, 76, .08)" }}>
    <div style={{ fontSize: 13, fontWeight: 700, color: "var(--vscode-editorError-foreground, #f14c4c)" }}>배포 전에 고쳐야 할 문제가 있어요</div>
    <div style={{ marginTop: 5, color: "var(--vscode-descriptionForeground, #bbb)", fontSize: 11, lineHeight: 1.5 }}>문제를 해결한 뒤 다시 검사하면 배포 대상을 선택할 수 있습니다.</div>
    <div style={{ display: "grid", gap: 9, marginTop: 12 }}>
      {reasons.map((issue) => (
        <div key={issue.code} style={{ padding: "10px 11px", borderRadius: 6, border: "1px solid var(--vscode-panel-border, #3f3f3f)", background: "var(--vscode-editorWidget-background, #252526)" }}>
          <div style={{ fontSize: 12, fontWeight: 650 }}>문제 · {issueNames[issue.code] ?? issue.message}</div>
          {issueNames[issue.code] && <div style={{ marginTop: 4, fontSize: 11, color: "var(--vscode-descriptionForeground, #aaa)", lineHeight: 1.45 }}>{issue.message}</div>}
          <div style={{ marginTop: 8, paddingTop: 8, borderTop: "1px solid var(--vscode-panel-border, #3f3f3f)", fontSize: 11, lineHeight: 1.45 }}><b style={{ color: "var(--vscode-textLink-foreground, #75beff)" }}>수정 방법 · </b>{issue.fix}</div>
          {issue.remediation_available && issue.proposal_id && (
            <button onClick={() => onApply(issue.proposal_id!)} disabled={Boolean(applyingProposalId)} style={{ ...button, marginTop: 9, padding: "6px 9px", opacity: applyingProposalId && applyingProposalId !== issue.proposal_id ? .55 : 1 }}>
              {applyingProposalId === issue.proposal_id ? "자동 수정 적용 중…" : "자동 수정"}
            </button>
          )}
        </div>
      ))}
    </div>
    <button onClick={onRerun} disabled={checking || Boolean(applyingProposalId)} style={{ ...button, marginTop: 13 }}>
      {checking ? "다시 검사 중…" : "다시 검사"}
    </button>
  </div>
);

/** 기존 결정 카드 UI를 그대로 써서, 롤백도 제안 → 선택 → 실행 순서를 지킨다. */
const EcsRollbackApprovalCard: React.FC<{
  proposal: EcsRollbackProposal;
  submitting: boolean;
  onResolve: (approved: boolean) => void;
}> = ({ proposal, submitting, onResolve }) => {
  const [selected, setSelected] = useState<"rollback" | "ignore">();
  useEffect(() => setSelected(undefined), [proposal.proposal_id]);
  const target = proposal.previous_task_definition.split(":").pop() || proposal.previous_task_definition;
  return (
    <div style={{ marginBottom: 16, border: "1px solid rgba(241, 196, 15, .7)", borderRadius: 9, overflow: "hidden", background: "rgba(241, 196, 15, .07)" }}>
      <div style={{ padding: "13px 15px", borderBottom: "1px solid rgba(241, 196, 15, .35)" }}>
        <div style={{ fontSize: 14, fontWeight: 750, color: "var(--vscode-editorWarning-foreground, #f1c40f)" }}>⚠️ 이상 감지 · 이전 버전으로 롤백할까요?</div>
        <div style={{ marginTop: 7, fontSize: 12, lineHeight: 1.5 }}>{proposal.reason}</div>
        {proposal.status === "failed" && <div style={{ marginTop: 7, fontSize: 11, color: "var(--vscode-editorWarning-foreground, #f1c40f)" }}>이전 롤백 요청이 실패했습니다. AWS 자격증명 또는 ECS 상태를 고친 뒤 다시 승인할 수 있습니다.</div>}
        <div style={{ marginTop: 8, fontSize: 11, color: "var(--vscode-descriptionForeground, #aaa)", lineHeight: 1.55 }}>
          대상: <b>{proposal.cluster}/{proposal.service}</b> · {proposal.region}<br />
          복귀 버전: <code>{target}</code> · 승인 레벨 {proposal.approval_level}
        </div>
      </div>
      <div style={{ padding: 12 }}>
        <DecisionOptionCards
          radioName={`ecs-rollback-${proposal.proposal_id}`}
          selectedKey={selected}
          disabled={submitting}
          onSelect={(key) => setSelected(key as "rollback" | "ignore")}
          options={[
            { key: "rollback", label: "↩ 이전 버전으로 롤백", summary: "이전 Task Definition으로 ECS 서비스를 갱신합니다.", detail: "승인 전에는 AWS 리소스를 변경하지 않습니다.", recommended: true },
            { key: "ignore", label: "현재 버전 유지 · 무시", summary: "이번 제안을 닫고 ECS 서비스를 변경하지 않습니다.", detail: "나중에 원인을 고친 뒤 다시 배포할 수 있습니다." },
          ]}
        />
        <button
          disabled={!selected || submitting}
          onClick={() => onResolve(selected === "rollback")}
          style={{ ...button, marginTop: 12, opacity: !selected || submitting ? .55 : 1, background: selected === "rollback" ? "var(--vscode-editorWarning-foreground, #c58b00)" : button.background }}
        >
          {submitting ? "결정 처리 중…" : selected === "rollback" ? (proposal.status === "failed" ? "롤백 다시 승인 →" : "롤백 승인 →") : "무시하고 현재 버전 유지"}
        </button>
      </div>
    </div>
  );
};

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
  const [applyingProposalId, setApplyingProposalId] = useState<string | null>(null);
  const [awsReady, setAwsReady] = useState(false);
  //: 현재 자격증명이 유효한 리전. 폼 기본값과 불일치 경고의 기준.
  //: 코어가 사용 중인 리전(AWS_REGION). **자격증명에서 유도한 값이 아니다.**
  const [coreRegion, setCoreRegion] = useState("");
  //: 사용자가 리전 입력란을 건드렸는가. 값으로 추측하면, 일부러 같은 값을
  //: 고른 경우와 구분이 안 돼 선택이 조용히 덮어써진다.
  const regionTouchedRef = useRef(false);
  const s3DirTouchedRef = useRef(false);
  //: 불일치 경고를 이미 보여 준 조합("코어리전|폼리전"). 같은 조합으로 한 번
  //: 더 누르면 진행한다 — 경고이지 차단이 아니다.
  const [regionWarningAck, setRegionWarningAck] = useState("");
  //: S3 정적 배포 — 올릴 폴더와 결과.
  const [s3Dir, setS3Dir] = useState("");
  const [s3DirTouched, setS3DirTouched] = useState(false);
  const [s3Busy, setS3Busy] = useState(false);
  const [s3Result, setS3Result] = useState<S3Deployed | null>(null);
  const [rollbackProposal, setRollbackProposal] = useState<EcsRollbackProposal | null>(null);
  const [resolvingRollback, setResolvingRollback] = useState(false);
  const [ec2, setEc2] = useState({ image_name: "recoder-app", tag: "latest", host_port: "8000", container_port: "8000", aws_region: "", ecr_registry: "", ec2_host: "", ec2_ssh_key: "", ec2_user: "ec2-user" });
  const [ecs, setEcs] = useState({ image_name: "recoder-app", tag: "latest", aws_region: "", ecr_registry: "", ecs_cluster: "", ecs_service: "", task_family: "recoder-task", container_port: "8000", cpu: "256", memory: "512" });

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
      const result = payload as { ok?: boolean; status?: { permission_check?: { inspected?: boolean; advisory_only?: boolean; missing_actions?: string[]; warnings?: string[] } }; message?: string };
      const deploymentRequest = pendingEcsDeploymentRef.current;
      pendingEcsDeploymentRef.current = null;
      setCheckingEcsPermissions(false);
      const permission = result.status?.permission_check;
      const canProceed = result.ok && (permission?.missing_actions?.length ?? 0) === 0 && (permission?.inspected || permission?.advisory_only);
      if (canProceed) {
        setMessage(permission?.advisory_only
          ? "IAM 역할 경로 때문에 권한 시뮬레이션을 완료하지 못했습니다. 명시적인 부족 권한은 없어 배포를 시작합니다…"
          : "ECS 배포 대상 리전의 권한을 확인했습니다. 배포를 시작합니다…");
        postMessage("workspace.deploy.ecs", deploymentRequest);
      } else {
        const detail = permission?.missing_actions?.length
          ? `부족 권한: ${permission.missing_actions.join(", ")}`
          : (permission?.warnings?.[0] ?? result.message ?? "ECS 배포 대상 권한을 완료 확인하지 못했습니다.");
        setMessage(`ECS 배포를 시작하지 않았습니다. ${detail}`);
      }
    }
    if (type === "workspace.deploy.remediationResult") {
      const result = payload as { message?: string; applied_files?: string[] };
      setApplyingProposalId(null);
      setMessage(`${result.message ?? "자동 수정을 적용했습니다."} ${result.applied_files?.length ? `변경 파일: ${result.applied_files.join(", ")}. ` : ""}이제 다시 검사해 주세요.`);
    }
    if (type === "workspace.deploy.remediationError") { setApplyingProposalId(null); setMessage((payload as { message?: string })?.message ?? "자동 수정 적용에 실패했습니다."); }
    if (type === "workspace.deploy.ecs.statusResult") {
      const status = payload as { rollback_proposal?: EcsRollbackProposal | null };
      const next = status.rollback_proposal;
      if (next?.status === "pending" || next?.status === "failed") setRollbackProposal(next);
      else setRollbackProposal(null);
    }
    if (type === "workspace.deploy.ecs.rollbackResult") {
      const result = payload as { message?: string; adr_path?: string };
      setResolvingRollback(false);
      setRollbackProposal(null);
      setMessage(`${result.message ?? "롤백 결정을 기록했습니다."}${result.adr_path ? ` 기록: ${result.adr_path}` : ""}`);
    }
    if (type === "workspace.deploy.ecs.rollbackError") {
      setResolvingRollback(false);
      setMessage((payload as { message?: string })?.message ?? "롤백 처리에 실패했습니다.");
    }
    if (type === "workspace.deploy.result") setMessage((payload as { message?: string })?.message ?? "배포 요청을 보냈습니다.");
    if (type === "workspace.deploy.s3.dirs") {
      //: 사용자가 직접 고른 폴더는 덮어쓰지 않는다.
      const suggested = (payload as { suggested?: string })?.suggested ?? "";
      setS3Dir(cur => (s3DirTouchedRef.current ? cur : suggested));
    }
    if (type === "workspace.deploy.s3.result") {
      setS3Busy(false);
      const r = payload as { ok?: boolean; result?: S3Deployed; message?: string };
      if (r?.ok && r.result) {
        setS3Result(r.result);
        setMessage(r.result.message);
      } else {
        setS3Result(null);
        setMessage(r?.message ?? "S3 배포에 실패했습니다.");
      }
    }
    if (type === "aws.status") {
      const status = payload as AwsStatusMessage;
      setAwsReady(Boolean(status?.ready));
      // 코어가 쓰는 리전을 알게 되면 **아직 손대지 않은** 폼 값을 맞춘다.
      // 예전에는 폼이 ap-northeast-2 로 고정이라, us-east-1 자격증명으로
      // 배포를 누르면 인증이 유효하지 않은 리전으로 요청이 나갔다.
      const region = coreRegionFromStatus(status);
      if (region) {
        setCoreRegion(region);
        const touched = regionTouchedRef.current;
        setEcs(cur => ({ ...cur, aws_region: resolveRegionDefault(region, cur.aws_region, touched) }));
        setEc2(cur => ({ ...cur, aws_region: resolveRegionDefault(region, cur.aws_region, touched) }));
      }
    }
    if (type === "errorMessage") setMessage((payload as { message?: string })?.message ?? "요청 처리에 실패했습니다.");
  }, []));

  useEffect(() => { runPreflight(); postMessage("aws.status"); }, [runPreflight, postMessage]);
  useEffect(() => {
    //: S3 탭을 열 때 어떤 폴더가 있는지 물어본다. 웹뷰는 파일시스템을 못 본다.
    if (target === "s3") { postMessage("workspace.deploy.s3.dirs"); }
  }, [target, postMessage]);
  useEffect(() => {
    // **"코어에 연결되면 다시 검사합니다" 라고 써 놓고 아무것도 안 하면 거짓말이다.**
    //
    // 코어가 끊긴 동안 preflight 는 null 로 남는다. 예전에는 그 상태에서
    // 문구만 바뀌고 재검사는 영영 일어나지 않았다 — 코어가 5초 뒤 돌아와도
    // 화면은 그대로였다. 여기서 실제로 다시 물어본다.
    //
    // 무한히 두드리지 않는다. 코어가 정말 죽어 있으면 조용히 기다리는 게
    // 맞고, 사용자에게는 「다시 검사」 버튼이 있다.
    if (preflight || checking) { return; }
    let attempts = 0;
    const timer = window.setInterval(() => {
      attempts += 1;
      if (attempts > PREFLIGHT_RETRY_LIMIT) { window.clearInterval(timer); return; }
      runPreflight();
    }, PREFLIGHT_RETRY_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [preflight, checking, runPreflight]);
  useEffect(() => {
    // ECS 이상 감지는 사용자가 다른 배포 탭을 보고 있어도 카드로 보여야 한다.
    // 실제 롤백은 이 폴링이 아니라 아래 승인 버튼으로만 호출된다.
    const poll = () => postMessage("workspace.deploy.ecs.status", { reportProgress: target === "ecs" });
    poll();
    const timer = window.setInterval(poll, 4000);
    return () => window.clearInterval(timer);
  }, [target, postMessage]);
  useEffect(() => {
    // ECS 감시 폴링과 별개로 EC2 탭의 기존 상태 폴링을 유지한다.
    if (target !== "ec2") return;
    postMessage("workspace.deploy.ec2.status");
    const timer = window.setInterval(() => postMessage("workspace.deploy.ec2.status"), 4000);
    return () => window.clearInterval(timer);
  }, [target, postMessage]);

  const chooseTarget = (choice: DeployTarget) => {
    if (!preflight || preflight.blocked || savingDecision) return;
    if (choice !== "local" && !awsReady) {
      setTarget("aws");
      setMessage("원격 배포 전에 AWS 계정을 연결하세요.");
      return;
    }
    setSavingDecision(true);
    postMessage("workspace.deploy.chooseTarget", { target: choice, evidence: preflight.evidence });
  };
  const applyRemediation = (proposalId: string) => {
    if (applyingProposalId) return;
    setApplyingProposalId(proposalId);
    setMessage("");
    postMessage("workspace.deploy.remediation.apply", { proposalId });
  };
  const generateActions = () => { setMessage("GitHub Actions 워크플로우 생성 중…"); postMessage("generateGithubActions", { workspacePath: "" }); };
  const deployEc2 = () => {
    if (!passesRegionCheck(ec2.aws_region)) { return; }
    setMessage("EC2 배포 요청 전송 중…");
    postMessage("workspace.deploy.ec2", { ...ec2, host_port: Number(ec2.host_port), container_port: Number(ec2.container_port) });
  };
  const deployEcs = () => {
    if (!awsReady) { setTarget("aws"); setMessage("ECS 배포를 시작하려면 AWS 계정을 연결하세요."); return; }
    // 리전이 어긋나면 **실행 전에** 멈춘다. 그대로 보내면 인증 실패로 끝나는데,
    // 원인이 리전이라는 걸 알아채는 데 오래 걸린다(데모에서 실제로 그랬다).
    if (!passesRegionCheck(ecs.aws_region)) { return; }
    setCheckingEcsPermissions(true);
    setMessage("입력한 ECS 리전과 대상 리소스의 권한을 확인 중…");
    pendingEcsDeploymentRef.current = { ...ecs, container_port: Number(ecs.container_port) };
    postMessage("aws.permissions.check", {
      deploymentContext: {
        // repo는 서버가 ECSAgent.ecr_repo_name()과 같은 규칙(빈 값이면
        // service 이름)으로 결정한다. 별도 registry는 현재 ECS 어댑터가
        // 사용하지 않으므로 권한 검사에도 전달하지 않는다.
        ecsCluster: ecs.ecs_cluster,
        ecsService: ecs.ecs_service,
        taskFamily: ecs.task_family,
        awsRegion: ecs.aws_region,
      },
    });
  };
  const resolveRollback = (approved: boolean) => {
    if (!rollbackProposal || resolvingRollback) return;
    setResolvingRollback(true);
    setMessage(approved ? "이전 버전 롤백 승인을 전송했습니다…" : "롤백 제안을 무시하는 중…");
    postMessage("workspace.deploy.ecs.rollback", { proposalId: rollbackProposal.proposal_id, approved });
  };
  /**
   * 배포 직전 리전 검사. 진행해도 되면 true.
   *
   * · 비어 있으면 **막는다** — 필수 입력이다. 예전에는 하드코딩된 기본값이
   *   대신 나가서, 사용자가 고른 적 없는 리전으로 배포가 시작됐다.
   * · 코어 리전과 다르면 경고를 한 번 보여 주고 막는다. 같은 조합으로 다시
   *   누르면 진행한다 — 다른 리전에 일부러 배포하는 건 정상적인 사용이다.
   */
  const passesRegionCheck = (formRegion: string): boolean => {
    const gate = regionGate(coreRegion, formRegion, regionWarningAck);
    if (gate.message) { setMessage(gate.message); }
    if (gate.ack) { setRegionWarningAck(gate.ack); }
    return gate.ok;
  };

  const update = <T extends Record<string, string>>(set: React.Dispatch<React.SetStateAction<T>>, key: keyof T, value: string) => {
    if (key === "aws_region") {
      regionTouchedRef.current = true;
      //: 값을 바꾸면 이전 확인은 무효다 — 새 조합을 다시 보여 줘야 한다.
      setRegionWarningAck("");
    }
    set(cur => ({ ...cur, [key]: value }));
  };

  return (
    <div style={{ fontFamily: "var(--vscode-font-family)", color: "var(--vscode-foreground, #ddd)" }}>
      <div style={{ fontSize: 18, fontWeight: 650, marginBottom: 5 }}>배포 센터</div>
      <div style={{ fontSize: 12, color: "var(--vscode-descriptionForeground, #999)", marginBottom: 16 }}>감지 결과를 확인하고, 배포 대상은 직접 선택하세요.</div>
      {rollbackProposal && <EcsRollbackApprovalCard proposal={rollbackProposal} submitting={resolvingRollback} onResolve={resolveRollback} />}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 7, marginBottom: 18 }}>
        {([ ["decision", "배포 결정"], ["aws", awsReady ? "AWS 연결됨" : "AWS 연결"], ["docker", "Local"], ["actions", "Actions"], ["ec2", "EC2"], ["ecs", "ECS"] ] as [Target, string][]).map(([id, label]) => <button key={id} onClick={() => { setTarget(id); setMessage(""); }} style={{ padding: "9px 6px", borderRadius: 6, border: `1px solid ${target === id ? "var(--vscode-focusBorder, #3794ff)" : "var(--vscode-panel-border, #3f3f3f)"}`, background: target === id ? "var(--vscode-list-activeSelectionBackground, #094771)" : "var(--vscode-editorWidget-background, #252526)", color: id === "aws" && awsReady ? "var(--vscode-charts-green, #4ec9b0)" : "var(--vscode-foreground, #ddd)", cursor: "pointer", fontSize: 11, fontWeight: target === id ? 600 : 400 }}>{label}</button>)}
      </div>

      {target === "aws" && <AwsConnection />}

      {target === "decision" && <div style={{ border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 9, overflow: "hidden", background: "var(--vscode-editorWidget-background, #252526)" }}>
        <div style={{ padding: "14px 16px", borderBottom: "1px solid var(--vscode-panel-border, #3f3f3f)", background: "linear-gradient(120deg, rgba(55,148,255,.16), transparent)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 14, fontWeight: 700 }}><span>⌕</span> {checking ? "프로젝트 구성 확인 중…" : describeDetection(preflight?.summary)}</div>
          {!checking && <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10 }}>{preflight?.evidence.map(item => <span key={item} style={{ padding: "3px 7px", borderRadius: 99, fontSize: 11, color: "var(--vscode-textLink-foreground, #75beff)", background: "rgba(55,148,255,.13)", border: "1px solid rgba(55,148,255,.28)" }}>{item}</span>)}</div>}
          <button onClick={runPreflight} disabled={checking || Boolean(applyingProposalId)} style={{ marginTop: 11, border: "none", background: "transparent", padding: 0, color: "var(--vscode-textLink-foreground, #75beff)", cursor: "pointer", fontSize: 11 }}>다시 검사</button>
        </div>
        <div style={{ padding: 12 }}>
          {preflight?.blocked ? (
            <DeploymentBlockers reasons={preflight.reasons ?? []} checking={checking} applyingProposalId={applyingProposalId} onApply={applyRemediation} onRerun={runPreflight} />
          ) : <>
            <div style={{ fontSize: 12, fontWeight: 650, margin: "0 0 9px 2px" }}>이 앱을 어디에 배포할까요?</div>
            {/*
              preflight 가 없으면 카드를 **비활성화한다.** 예전에는 활성처럼
              보이는데 chooseTarget 이 `if (!preflight) return` 으로 즉시
              돌아가서, 눌러도 아무 일도 안 나고 아무 메시지도 없었다.
              고장을 숨긴 화면이 고장난 화면보다 나쁘다.
            */}
            <DecisionOptionCards options={choices.map(choice => ({ ...choice, label: `${choice.icon}  ${choice.label}`, recommended: preflight?.recommended_target === choice.key }))} onSelect={(key) => chooseTarget(key as DeployTarget)} disabled={checking || savingDecision || !preflight} />
            {!preflight && <div style={{ margin: "9px 2px 0", fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>프로젝트 감지 결과가 없어 배포 대상을 고를 수 없습니다. 코어에 연결되면 자동으로 다시 검사합니다.</div>}
            <div style={{ margin: "11px 2px 1px", fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>선택 근거는 <code>docs/adr</code>에 기록됩니다. 실제 원격 배포는 필요한 설정을 확인한 다음 시작됩니다.</div>
          </>}
        </div>
      </div>}

      {target === "docker" && <div style={{ border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 7, padding: 16 }}><b>로컬 Docker 배포</b><p style={{ color: "var(--vscode-descriptionForeground, #999)", lineHeight: 1.55 }}>Dockerfile 생성, 보안 스캔, build/run 및 헬스체크를 진행합니다.</p><button onClick={onOpenDocker} style={button}>로컬에서 검증 시작</button></div>}
      {target === "s3" && <div style={{ border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 7, padding: 16 }}>
        <b>S3 정적 호스팅</b>
        <p style={{ color: "var(--vscode-descriptionForeground, #999)", lineHeight: 1.55, fontSize: 12 }}>
          본인 AWS 계정의 버킷에 직접 올립니다. 버킷이 없으면 만들고, 정적 웹사이트 호스팅까지 설정한 뒤 공개 URL을 돌려줍니다.
        </p>
        <label style={{ display: "block", fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>
          올릴 폴더 (비우면 워크스페이스 루트)
          <input
            value={s3Dir}
            onChange={e => { s3DirTouchedRef.current = true; setS3DirTouched(true); setS3Dir(e.target.value); }}
            placeholder="dist"
            style={{ ...input, marginTop: 4 }}
          />
        </label>
        {/*
          빌드 산출물이 아니라 소스 폴더를 올리면 브라우저가 .tsx 를 실행할 수
          없어 흰 화면이 나온다. 그 실패는 배포가 아니라 앱 문제처럼 보인다.
        */}
        <div style={{ marginTop: 6, fontSize: 11, color: "var(--vscode-descriptionForeground, #999)", lineHeight: 1.5 }}>
          빌드 산출물 폴더를 지정하세요. 파일은 최대 30개까지 올라갑니다.
          {s3DirTouched ? "" : " (감지된 폴더를 자동으로 채웠습니다)"}
        </div>
        <button
          disabled={s3Busy}
          onClick={() => { setS3Busy(true); setS3Result(null); setMessage("S3에 올리는 중…"); postMessage("workspace.deploy.s3", { dir: s3Dir.trim(), region: ecs.aws_region.trim() }); }}
          style={{ ...button, marginTop: 13, opacity: s3Busy ? .7 : 1 }}
        >{s3Busy ? "배포 중…" : "S3에 배포"}</button>

        {s3Result && (
          <div style={{ marginTop: 13, padding: "10px 12px", borderRadius: 6, background: "rgba(78, 201, 176, .10)", border: "1px solid rgba(78, 201, 176, .32)" }}>
            <div style={{ fontSize: 12, fontWeight: 650, color: "var(--vscode-charts-green, #4ec9b0)" }}>배포 완료</div>
            {/*
              **URL 을 안 보여 주면 사용자는 배포하고도 어디로 가야 할지 모른다.**
              이 링크가 이 기능의 결과물 그 자체다.
            */}
            <div style={{ marginTop: 7, fontSize: 12, wordBreak: "break-all" }}>
              <a href={s3Result.url} target="_blank" rel="noreferrer" style={{ color: "var(--vscode-textLink-foreground, #75beff)" }}>{s3Result.url}</a>
            </div>
            <div style={{ marginTop: 7, fontSize: 11, color: "var(--vscode-descriptionForeground, #999)", lineHeight: 1.55 }}>
              버킷 {s3Result.bucket} · {s3Result.region}{s3Result.bucket_created ? " (새로 만듦)" : ""} · 파일 {s3Result.uploaded.length}개
              {s3Result.index_copied_from ? ` · index.html 이 없어 ${s3Result.index_copied_from} 를 진입 문서로 함께 올렸습니다` : ""}
            </div>
          </div>
        )}

        <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--vscode-panel-border, #3f3f3f)" }}>
          <div style={{ fontSize: 11, color: "var(--vscode-descriptionForeground, #999)", lineHeight: 1.5 }}>매번 손으로 올리는 대신 CI에서 배포하려면:</div>
          <button onClick={generateActions} style={{ ...button, marginTop: 8, background: "var(--vscode-button-secondaryBackground, #3a3d41)", color: "var(--vscode-button-secondaryForeground, #fff)" }}>배포 워크플로우 생성</button>
        </div>
      </div>}
      {target === "actions" && <div style={{ border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 7, padding: 16 }}><b>GitHub Actions</b><p style={{ color: "var(--vscode-descriptionForeground, #999)", lineHeight: 1.55 }}>프로젝트에 맞는 CI/CD 워크플로우를 생성하고, 승인 후 <code>.github/workflows/deploy.yml</code>에 저장합니다.</p><button onClick={generateActions} style={button}>워크플로우 생성</button>{proposal && <><pre style={{ marginTop: 12, maxHeight: 280, overflow: "auto", background: "var(--vscode-textCodeBlock-background, #1e1e1e)", borderRadius: 5, padding: 10, fontSize: 11 }}>{proposal.content}</pre><button onClick={() => postMessage("approveGithubActions", { proposalId: proposal.proposal_id, approved: true })} style={{ ...button, marginTop: 10 }}>승인하고 저장</button></>}</div>}
      {target === "ec2" && <div style={{ border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 7, padding: 16 }}><b>EC2 배포</b><div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 12 }}>{([ ["image_name", "이미지"], ["tag", "태그"], ["host_port", "호스트 포트"], ["container_port", "컨테이너 포트"], ["aws_region", "AWS 리전"], ["ecr_registry", "ECR Registry"], ["ec2_host", "EC2 Host"], ["ec2_ssh_key", "SSH 키 경로"], ["ec2_user", "EC2 사용자"] ] as [keyof typeof ec2, string][]).map(([key, label]) => <label key={key} style={{ fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>{label}<input value={ec2[key]} onChange={e => update(setEc2, key, e.target.value)} style={{ ...input, marginTop: 4 }} /></label>)}</div><button onClick={deployEc2} style={{ ...button, marginTop: 14 }}>EC2 배포 실행</button></div>}
      {target === "ecs" && <div style={{ border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 7, padding: 16 }}><b>ECS Fargate 배포</b><p style={{ color: "var(--vscode-descriptionForeground, #999)", fontSize: 11, lineHeight: 1.45 }}>선택 근거가 ADR에 기록되었습니다. 입력한 리전과 ECS 대상의 권한을 확인한 뒤 배포를 시작합니다.</p><EcsCostNotice /><div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 12 }}>{([ ["image_name", "이미지"], ["tag", "태그"], ["aws_region", "AWS 리전"], ["ecr_registry", "ECR Registry"], ["ecs_cluster", "ECS Cluster"], ["ecs_service", "ECS Service"], ["task_family", "Task Family"], ["container_port", "컨테이너 포트"], ["cpu", "CPU"], ["memory", "Memory"] ] as [keyof typeof ecs, string][]).map(([key, label]) => <label key={key} style={{ fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>{label}<input value={ecs[key]} onChange={e => update(setEcs, key, e.target.value)} style={{ ...input, marginTop: 4 }} /></label>)}</div><button disabled={checkingEcsPermissions} onClick={deployEcs} style={{ ...button, marginTop: 14, opacity: checkingEcsPermissions ? .7 : 1 }}>{checkingEcsPermissions ? "배포 권한 확인 중…" : "ECS 배포 실행"}</button></div>}
      {message && <div style={{ marginTop: 12, padding: "8px 10px", borderRadius: 5, background: "var(--vscode-editorInfo-background, rgba(55,148,255,.12))", color: "var(--vscode-editorInfo-foreground, #75beff)", fontSize: 12 }}>{message}</div>}
    </div>
  );
};

export default DeploymentCenter;
