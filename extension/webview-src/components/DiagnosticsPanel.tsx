/**
 * ReCoder — DiagnosticsPanel component
 * Displays First Run diagnostic checklist with activation guides.
 */

import React, { useState } from "react";
import { Button, Kit } from "./ui/kit";
import { Icon } from "./ui/icons";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { useSelfHeal } from "../hooks/useSelfHeal";

export type ReadyState = string;

export interface DiagnosticsResult {
  core_ready: string;
  ai_ready: string;
  docker_ready: string;
  aws_deploy_ready: string;
  ops_ready: string;
  resolved_model_id?: string;
  resolved_region?: string;
  is_cross_region_profile?: boolean;
  provider_type?: string;
  validation_time?: string;
  details?: Record<string, unknown>;
}

interface DiagnosticsPanelProps {
  diagnostics: DiagnosticsResult | null;
  onRetry?: () => void;
}

interface CheckItem {
  key: keyof DiagnosticsResult;
  label: string;
  description: string;
  activationGuide: string;
  docLink?: string;
  enabledModes: string[];
}

const CHECK_ITEMS: CheckItem[] = [
  {
    key: "core_ready",
    label: "ReCoder 코어",
    description: "이 컴퓨터에서 도는 ReCoder 코어 서버에 연결됩니다.",
    activationGuide: "명령 팔레트에서 「ReCoder: Start Core」를 실행하세요. 자동 조치가 먼저 다시 켜 봅니다.",
    docLink: "https://github.com/recoder/docs/core-setup",
    enabledModes: ["개발", "배포", "운영"],
  },
  {
    key: "ai_ready",
    label: "AI 연결",
    description: "AI 연결 — 본인 AWS(Bedrock), 게이트웨이, 또는 Claude·ChatGPT API 키.",
    activationGuide: "AWS를 쓰지 않으면 아래에서 Claude 또는 ChatGPT API 키로 연결하세요. AWS를 쓰면 AWS 연결 화면에서 자격 증명을 등록합니다.",
    docLink: "https://github.com/recoder/docs/ai-setup",
    enabledModes: ["개발", "배포", "운영"],
  },
  {
    key: "docker_ready",
    label: "Docker",
    description: "Docker 가 실행 중이라 이미지를 빌드하고 로컬에서 돌릴 수 있습니다.",
    activationGuide: "Docker Desktop 을 설치하거나 켜 주세요. 터미널에서 docker info 로 확인할 수 있습니다.",
    docLink: "https://docs.docker.com/get-docker/",
    enabledModes: ["배포"],
  },
  {
    key: "aws_deploy_ready",
    label: "AWS 배포",
    description: "배포 전용 AWS 권한과 ECR 접근이 준비됐습니다.",
    activationGuide: "상단의 「AWS 연결」에서 배포 전용 권한을 연결하세요. ECR · ECS 권한이 필요합니다.",
    docLink: "https://github.com/recoder/docs/aws-deploy",
    enabledModes: ["배포"],
  },
  {
    key: "ops_ready",
    label: "운영 대응",
    description: "장애 대응에 쓸 서버 주소와 접속 정보가 준비됐습니다.",
    activationGuide: "운영 화면 설정에서 서버 주소와 SSH 키 경로를 입력하세요.",
    docLink: "https://github.com/recoder/docs/ops-setup",
    enabledModes: ["운영"],
  },
];

const AI_PROVIDER_NAMES: Record<string, string> = { anthropic: "Claude API 키", openai: "ChatGPT(OpenAI) API 키", bedrock: "본인 AWS (Bedrock)", gateway: "게이트웨이", gemini: "Gemini" };

//: 자동 실행 등급(가역·로컬·무비용) — 코어/확장이 직접 고쳐 본다. 나머지는 설정 화면.
const AUTO_FIXABLE = new Set(["aws_deploy_ready", "ai_ready", "docker_ready", "core_ready"]);
function autoFixable(key: string): boolean { return AUTO_FIXABLE.has(key); }

const STATE_TEXT: Record<string, string> = { ready: "정상", partial: "일부", not_ready: "필요", error: "오류" };
const ITEM_ICON: Record<string, string> = { core_ready: "chip", ai_ready: "code", docker_ready: "box", aws_deploy_ready: "upload", ops_ready: "pulse" };
function tone(state: ReadyState): "ok" | "warn" | "bad" | "" {
  return state === "ready" ? "ok" : state === "partial" ? "warn" : state === "error" ? "bad" : "";
}

const DIAG_CSS = `
.rc-diag{font-size:12.5px;color:var(--vscode-foreground);padding:12px 14px}
.rc-diag .hd{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.rc-diag .hd b{font-size:13px;font-weight:600}.rc-diag .hd small{color:var(--rk-fg3)}
.rc-diag .hd .sp{margin-left:auto}
.rc-diag .list{border-radius:12px;background:var(--rk-surface);overflow:hidden}
.rc-diag details+details{border-top:1px solid var(--rk-hair)}
.rc-diag summary{list-style:none;display:flex;align-items:center;gap:10px;padding:9px 12px;cursor:pointer}
.rc-diag summary::-webkit-details-marker{display:none}
.rc-diag summary .nm{flex:1;min-width:0}
.rc-diag summary .md{color:var(--rk-fg3);font-size:11px}
.rc-diag .st{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;color:var(--rk-fg2);min-width:38px}
.rc-diag .st::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--rk-fg3)}
.rc-diag .st[data-tone=ok]::before{background:var(--rk-ok)}.rc-diag .st[data-tone=warn]::before{background:var(--rk-warn)}.rc-diag .st[data-tone=bad]::before{background:var(--rk-bad)}
.rc-diag .body{padding:0 12px 12px 36px;display:flex;flex-direction:column;gap:8px;color:var(--rk-fg2);line-height:1.5}
.rc-diag .guide{background:var(--rk-surface2);border-radius:8px;padding:8px 10px;color:var(--rk-fg)}
.rc-diag .acts{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.rc-diag .model{font-size:11.5px;color:var(--rk-fg3);margin:0 0 8px}.rc-diag .model code{color:var(--rk-fg)}
`;

export const DiagnosticsPanel: React.FC<DiagnosticsPanelProps> = ({
  diagnostics,
  onRetry,
}) => {
  const { postMessage } = useVSCodeApi();
  const [retrying, setRetrying] = useState(false);
  const { heal, start } = useSelfHeal();

  const handleRetry = () => {
    setRetrying(true);
    postMessage("webview.diagnostics.rerun");
    onRetry?.();
    setTimeout(() => setRetrying(false), 3000);
  };

  if (!diagnostics) {
    return (
      <Kit className="rc-diag"><style>{DIAG_CSS}</style>
        <div className="hd"><b>시스템 점검</b><small>점검 중…</small></div>
      </Kit>
    );
  }

  const readyCount = CHECK_ITEMS.filter((item) => diagnostics[item.key] === "ready").length;

  return (
    <Kit className="rc-diag"><style>{DIAG_CSS}</style>
      <div className="hd">
        <b>시스템 점검</b>
        <small>{readyCount}/{CHECK_ITEMS.length} 정상{diagnostics.validation_time && ` · ${new Date(diagnostics.validation_time).toLocaleTimeString()}`}</small>
        <span className="sp" />
        <Button small variant="ghost" onClick={handleRetry} disabled={retrying}>{retrying ? "점검 중…" : "다시 점검"}</Button>
      </div>
      {diagnostics.resolved_model_id && (
        <p className="model" title="진단 시 호출에 성공한 모델입니다. 작업 종류와 폴백에 따라 실제 사용 모델은 달라질 수 있습니다.">
          연결 검증 모델 <code>{diagnostics.resolved_model_id}</code>{diagnostics.resolved_region && <> · {diagnostics.resolved_region}</>}
          {diagnostics.is_cross_region_profile && " · 교차 리전"} · 작업 종류와 폴백에 따라 실제 모델은 달라질 수 있습니다.
        </p>
      )}
      <div className="list">
        {CHECK_ITEMS.map((item) => {
          const state: ReadyState = (diagnostics[item.key] as ReadyState) ?? "not_ready";
          const isReady = state === "ready";
          return (
            <details key={item.key} data-testid={`diag-${item.key}`}>
              <summary>
                <Icon name={ITEM_ICON[item.key] || "file"} size={15} style={{ color: "var(--rk-fg2)" }} />
                <span className="nm">{item.label}</span>
                <span className="md">{item.enabledModes.join(" · ")}</span>
                <span className="st" data-tone={tone(state)}>{STATE_TEXT[state] ?? "확인 안 됨"}</span>
              </summary>
              <div className="body">
                <span>{item.description}</span>
                {!isReady && <div className="guide">{item.activationGuide}</div>}
                <div className="acts">
                  {!isReady && (
                    <Button small
                      disabled={!!heal[item.key]?.pending}
                      onClick={() => start(item.key)}
                      title={autoFixable(item.key) ? "코어가 직접 고쳐 봅니다 — 안 되면 다음 행동을 안내합니다." : "설정 화면을 엽니다."}>
                      {!!heal[item.key]?.pending ? "조치 중…" : (autoFixable(item.key) ? "자동 조치" : "설정 열기")}
                    </Button>
                  )}
                  {item.docLink && (
                    <a className="rk-link" href={item.docLink} target="_blank" rel="noreferrer"
                      onClick={(e) => { e.preventDefault(); postMessage("webview.open.external", { url: item.docLink }); }}>
                      문서 보기
                    </a>
                  )}
                </div>
                {item.key === "ai_ready" && (
                  <div className="acts">
                    {isReady && diagnostics.provider_type && <span>현재: {AI_PROVIDER_NAMES[diagnostics.provider_type] ?? diagnostics.provider_type}</span>}
                    <Button small variant="ghost" onClick={() => postMessage("ai.connect", { provider: "anthropic" })}>Claude API 키로 연결</Button>
                    <Button small variant="ghost" onClick={() => postMessage("ai.connect", { provider: "openai" })}>ChatGPT API 키로 연결</Button>
                    <Button small variant="ghost" onClick={() => postMessage("ai.connect")}>연결 방법 바꾸기</Button>
                  </div>
                )}
                {heal[item.key] && (
                  <div style={{ color: heal[item.key].failed ? "var(--rk-warn)" : "var(--rk-ok)" }}>
                    {heal[item.key].message}
                  </div>
                )}
              </div>
            </details>
          );
        })}
      </div>
    </Kit>
  );
};

export default DiagnosticsPanel;
