/** AWS BYO 계정 연결 — 키는 검증 뒤 VS Code SecretStorage에만 저장된다. */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { AwsPolicyGuide, EcsPolicyContext } from "./AwsPolicyGuide";

type AwsStatus = {
  ready: boolean;
  identity?: { account?: string; arn?: string } | null;
  region?: string;
  access_key_last4?: string;
  profile?: string;
  storage?: string;
  message?: string;
  permission_check?: {
    inspected: boolean;
    required_actions: string[];
    missing_actions: string[];
    excessive_policies: string[];
    warnings: string[];
  } | null;
};

const input: React.CSSProperties = {
  width: "100%", boxSizing: "border-box", marginTop: 5, padding: "8px 9px", borderRadius: 5,
  color: "var(--vscode-input-foreground, #ddd)", background: "var(--vscode-input-background, #252526)",
  border: "1px solid var(--vscode-input-border, #3f3f3f)", fontSize: 12,
};

export const AwsConnection: React.FC<{ ecsPolicyContext?: EcsPolicyContext }> = ({ ecsPolicyContext }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [accessKeyId, setAccessKeyId] = useState("");
  const [secretAccessKey, setSecretAccessKey] = useState("");
  //: **여기에 리전을 박아 두면 안 된다.** 예전에는 "ap-northeast-2" 가
  //: 기본값이었고, 사용자가 그대로 두면 connect_aws 가 코어의 AWS_REGION 을
  //: 그 값으로 덮어썼다. us-east-1 자격증명을 넣어도 코어는 ap-northeast-2 로
  //: 바뀌고, 배포 센터는 그 값을 "현재 리전" 이라고 믿는다 — 리전 불일치
  //: 경고까지 조용해져서, 아무도 어긋난 걸 모른 채 배포가 실패한다.
  //: 코어가 이미 쓰고 있는 리전을 그대로 보여 주고, 바꾸려면 사용자가 친다.
  const [region, setRegion] = useState("");
  const [status, setStatus] = useState<AwsStatus | null>(null);
  //: useMessage 핸들러는 [] 의존으로 고정돼 있어 state 를 직접 읽으면 낡는다.
  const regionTouchedRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  //: AWS CLI 를 이미 쓰는 사용자에게 키 재입력을 강요하지 않기 위한 목록.
  //: 비어 있으면 이 섹션 자체가 렌더되지 않으므로, CLI 를 안 쓰는 사용자는
  //: 지금과 완전히 같은 화면을 본다.
  const [profiles, setProfiles] = useState<string[]>([]);
  //: 원클릭 IAM 셋업 결과 — 브라우저를 연 뒤 화면에 남길 다음 단계 안내.
  const [onboarding, setOnboarding] = useState<{ hosted?: boolean; steps?: string[]; error?: string } | null>(null);

  useEffect(() => {
    postMessage("aws.status");
    postMessage("aws.listProfiles");
  }, [postMessage]);
  useMessage(useCallback(({ type, payload }) => {
    if (type === "aws.status") {
      const next = payload as AwsStatus;
      setStatus(next);
      //: 사용자가 손대지 않았으면 코어의 현재 리전을 그대로 보여 준다.
      const current = (next?.region ?? "").trim();
      if (current) { setRegion(cur => (regionTouchedRef.current ? cur : current)); }
    }
    if (type === "aws.configure.result") {
      const result = payload as { ok: boolean; status?: AwsStatus; message?: string };
      setBusy(false);
      if (result.ok) {
        setStatus(result.status ?? null);
        // 성공 후에는 렌더링 메모리에도 비밀키를 더 보관하지 않는다.
        setAccessKeyId("");
        setSecretAccessKey("");
        setError("");
      } else {
        setError(result.message ?? "AWS 자격증명을 확인하지 못했습니다.");
      }
    }
    if (type === "aws.clear.result") {
      const result = payload as { ok: boolean; message?: string };
      setBusy(false);
      if (result.ok) { setStatus({ ready: false, message: "AWS 연결이 해제되었습니다." }); setError(""); }
      else { setError(result.message ?? "AWS 연결 해제에 실패했습니다."); }
    }
    if (type === "aws.permissions.result") {
      const result = payload as { ok: boolean; status?: AwsStatus; message?: string };
      setBusy(false);
      if (result.ok) { setStatus(result.status ?? null); setError(""); }
      else { setError(result.message ?? "AWS 권한을 점검하지 못했습니다."); }
    }
    if (type === "aws.onboarding.result") {
      setBusy(false);
      setOnboarding(payload as { hosted?: boolean; steps?: string[]; error?: string });
    }
    if (type === "aws.profiles") {
      const result = payload as { profiles?: string[] };
      setProfiles(Array.isArray(result?.profiles) ? result.profiles : []);
    }
  }, []));

  const connectProfile = (profile: string) => {
    setBusy(true);
    setError("");
    //: 리전 칸을 사용자가 만졌으면 그 값을 존중하고, 아니면 비워 보낸다 —
    //: 비어 있으면 코어가 프로필 자신의 설정에서 리전을 가져온다.
    postMessage("aws.connect.profile", {
      profile,
      region: regionTouchedRef.current ? region.trim() : "",
    });
  };

  const connect = () => {
    if (!accessKeyId.trim() || !secretAccessKey.trim()) {
      setError("Access Key ID와 Secret Access Key를 모두 입력하세요.");
      return;
    }
    setBusy(true);
    setError("");
    postMessage("aws.configure", {
      accessKeyId: accessKeyId.trim(), secretAccessKey: secretAccessKey.trim(),
      //: 비어 있으면 **코어가 쓰던 리전을 유지**한다. 하드코딩한 값으로
      //: 덮어쓰면 사용자가 고른 적 없는 리전이 시스템 전체의 "현재 리전" 이 된다.
      region: region.trim() || (status?.region ?? "").trim(),
    });
  };

  const card: React.CSSProperties = {
    border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 8, padding: 15,
    background: "var(--vscode-editorWidget-background, #252526)",
  };
  const button: React.CSSProperties = {
    border: "none", borderRadius: 5, padding: "8px 12px", fontSize: 12, fontWeight: 650, cursor: busy ? "wait" : "pointer",
    background: "var(--vscode-button-background, #0e639c)", color: "var(--vscode-button-foreground, #fff)", opacity: busy ? .7 : 1,
  };

  if (status?.ready) {
    const permission = status.permission_check;
    return <div style={card}>
      <div style={{ color: "var(--vscode-charts-green, #4ec9b0)", fontSize: 15, fontWeight: 700 }}>✓ AWS 연결됨</div>
      <div style={{ marginTop: 9, fontSize: 12, lineHeight: 1.6 }}>
        <div>계정: <b>{status.identity?.account ?? "확인됨"}</b></div>
        <div>리전: <b>{status.region || "ap-northeast-2"}</b>{status.access_key_last4 ? ` · 키 끝 ${status.access_key_last4}` : ""}{status.storage === "aws_profile" && status.profile ? ` · 프로필 ${status.profile}` : ""}</div>
      </div>
      {permission && (permission.missing_actions.length > 0 || permission.excessive_policies.length > 0 || permission.warnings.length > 0) && (
        <div style={{ marginTop: 12, padding: "9px 10px", borderRadius: 5, background: "rgba(245, 180, 0, .12)", border: "1px solid rgba(245, 180, 0, .35)", color: "var(--vscode-editorWarning-foreground, #cca700)", fontSize: 11, lineHeight: 1.55 }}>
          {permission.excessive_policies.length > 0 && <div><b>⚠️ 이 키는 너무 강력합니다:</b> {permission.excessive_policies.join(", ")}. 배포 전용 최소권한 키를 권장합니다.</div>}
          {permission.missing_actions.length > 0 && <div style={{ marginTop: permission.excessive_policies.length ? 5 : 0 }}><b>배포 전 확인할 권한:</b> {permission.missing_actions.join(", ")}</div>}
          {permission.warnings.map((warning) => <div key={warning} style={{ marginTop: (permission.excessive_policies.length || permission.missing_actions.length) ? 5 : 0 }}>{warning}</div>)}
        </div>
      )}
      {permission?.inspected && permission.missing_actions.length === 0 && permission.excessive_policies.length === 0 && (
        <div style={{ marginTop: 12, padding: "9px 10px", borderRadius: 5, background: "rgba(78, 201, 176, .10)", border: "1px solid rgba(78, 201, 176, .32)", color: "var(--vscode-charts-green, #4ec9b0)", fontSize: 11 }}>✓ 권한 점검 완료: 기본 ECS 배포 권한이 확인되었습니다.</div>
      )}
      <div style={{ marginTop: 11, color: "var(--vscode-descriptionForeground, #999)", fontSize: 11, lineHeight: 1.5 }}>키는 VS Code의 OS 보안 금고에 암호화되어 저장되며 프로젝트 파일에는 기록되지 않습니다.</div>
      <div style={{ display: "flex", gap: 8, marginTop: 13 }}>
        <button disabled={busy} onClick={() => { setBusy(true); setError(""); postMessage("aws.permissions.check"); }} style={{ ...button, background: "var(--vscode-button-secondaryBackground, #3a3d41)", color: "var(--vscode-button-secondaryForeground, #fff)" }}>{busy ? "권한 점검 중…" : "권한 다시 점검"}</button>
        <button disabled={busy} onClick={() => { setBusy(true); setError(""); postMessage("aws.clear"); }} style={{ ...button, background: "var(--vscode-button-secondaryBackground, #3a3d41)", color: "var(--vscode-button-secondaryForeground, #fff)" }}>연결 해제</button>
      </div>
      {error && <div style={{ marginTop: 10, color: "var(--vscode-errorForeground, #f48771)", fontSize: 12 }}>{error}</div>}
      {/*
        연결된 뒤에도 필요하다. 권한 점검에서 빠진 액션이 나왔을 때, 무엇을
        허용해야 하는지 같은 화면에서 바로 볼 수 있어야 한다.
      */}
      <AwsPolicyGuide region={status.region} ecsContext={ecsPolicyContext} />
    </div>;
  }

  return <div style={card}>
    <div style={{ fontSize: 15, fontWeight: 700 }}>AWS 계정 연결</div>
    <p style={{ margin: "7px 0 14px", color: "var(--vscode-descriptionForeground, #999)", fontSize: 12, lineHeight: 1.5 }}>배포는 본인의 AWS 계정에서 실행됩니다. 입력한 키는 검증 후 VS Code 보안 금고에만 저장합니다.</p>
    <div style={{ marginBottom: 14, padding: "10px 11px", borderRadius: 6, background: "rgba(78,201,176,.07)", border: "1px solid rgba(78,201,176,.28)" }}>
      {/*
        원클릭 IAM 셋업 — 보드 카드 「AWS 온보딩 마찰 제거」.
        키가 아직 없는 사용자용: 버튼을 누르면 최소권한 사용자·정책·키를
        만드는 CloudFormation 화면이 브라우저에 열린다. 스택 Outputs 의
        키 두 개를 아래 입력란에 붙여넣으면 온보딩 끝.
      */}
      <div style={{ fontSize: 12, fontWeight: 650 }}>아직 액세스 키가 없다면</div>
      <div style={{ marginTop: 4, fontSize: 11, color: "var(--vscode-descriptionForeground, #999)", lineHeight: 1.5 }}>버튼 한 번으로 배포 전용 최소권한 사용자와 키를 만드는 AWS 화면을 엽니다.</div>
      <button disabled={busy} onClick={() => { setBusy(true); setOnboarding(null); postMessage("aws.onboarding"); }} style={{ marginTop: 9, border: "1px solid rgba(78,201,176,.45)", borderRadius: 5, padding: "6px 11px", fontSize: 12, cursor: busy ? "wait" : "pointer", background: "transparent", color: "var(--vscode-charts-green, #4ec9b0)" }}>{busy ? "여는 중…" : "원클릭 IAM 셋업 (브라우저)"}</button>
      {onboarding?.error && <div style={{ marginTop: 8, fontSize: 11, color: "var(--vscode-errorForeground, #f48771)" }}>{onboarding.error}</div>}
      {onboarding?.steps && <div style={{ marginTop: 8, fontSize: 11, color: "var(--vscode-descriptionForeground, #999)", lineHeight: 1.6 }}>{onboarding.steps.map(step => <div key={step}>{step}</div>)}</div>}
    </div>
    {profiles.length > 0 && <div style={{ marginBottom: 14, padding: "10px 11px", borderRadius: 6, background: "rgba(55,148,255,.08)", border: "1px solid rgba(55,148,255,.25)" }}>
      {/*
        **이 컴퓨터에 이미 자격증명이 있으면 키를 다시 입력받지 않는다.**
        AWS CLI 사용자는 ~/.aws 에 프로필이 있다 — 그걸 두고 콘솔에서 키를
        다시 찾아 붙여넣게 하는 것은 마찰일 뿐 보안 이득이 없다. 비밀 값은
        이 화면을 거치지 않고 코어가 파일에서 직접 읽는다.
      */}
      <div style={{ fontSize: 12, fontWeight: 650 }}>이 컴퓨터의 AWS 프로필 발견</div>
      <div style={{ marginTop: 4, fontSize: 11, color: "var(--vscode-descriptionForeground, #999)", lineHeight: 1.5 }}>키 입력 없이 클릭 한 번으로 연결합니다.</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 7, marginTop: 9 }}>
        {profiles.map(profile => (
          <button key={profile} disabled={busy} onClick={() => connectProfile(profile)} style={{
            border: "1px solid rgba(55,148,255,.4)", borderRadius: 5, padding: "6px 11px", fontSize: 12,
            cursor: busy ? "wait" : "pointer", background: "transparent",
            color: "var(--vscode-textLink-foreground, #75beff)", opacity: busy ? .6 : 1,
          }}>{busy ? "연결 중…" : `${profile} 로 연결`}</button>
        ))}
      </div>
      <div style={{ marginTop: 10, fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>또는 아래에서 직접 키를 입력하세요.</div>
    </div>}
    <label style={{ display: "block", fontSize: 12 }}>Access Key ID
      <input autoComplete="off" spellCheck={false} value={accessKeyId} onChange={e => setAccessKeyId(e.target.value)} placeholder="AKIA..." style={input} />
    </label>
    <label style={{ display: "block", marginTop: 11, fontSize: 12 }}>Secret Access Key
      <input autoComplete="off" spellCheck={false} type="password" value={secretAccessKey} onChange={e => setSecretAccessKey(e.target.value)} placeholder="비밀 키를 붙여 넣으세요" style={input} />
    </label>
    <label style={{ display: "block", marginTop: 11, fontSize: 12 }}>리전
      <input autoComplete="off" spellCheck={false} value={region} onChange={e => { regionTouchedRef.current = true; setRegion(e.target.value); }} placeholder={status?.region || "예: us-east-1"} style={input} />
    </label>
    {error && <div role="alert" style={{ marginTop: 10, color: "var(--vscode-errorForeground, #f48771)", fontSize: 12, lineHeight: 1.45 }}>{error}</div>}
    {status?.message && !error && <div style={{ marginTop: 10, color: "var(--vscode-descriptionForeground, #999)", fontSize: 11 }}>{status.message}</div>}
    <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 15 }}>
      <button disabled={busy} onClick={connect} style={button}>{busy ? "STS로 확인 중…" : "연결"}</button>
      <a href="https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_access-keys.html" target="_blank" rel="noreferrer" style={{ color: "var(--vscode-textLink-foreground, #75beff)", fontSize: 11 }}>키 만드는 법 보기</a>
    </div>
    {/*
      **연결 전에 가장 필요하다.** 키를 만들려면 어떤 권한을 붙일지부터
      알아야 하는데, 지금까지는 그 답이 제품 안에 있으면서도 화면에 없었다.
      그래서 사용자는 AdministratorAccess 를 붙이는 쪽으로 갔다.
    */}
    <AwsPolicyGuide region={region.trim() || status?.region} ecsContext={ecsPolicyContext} />
  </div>;
};

export default AwsConnection;
