/**
 * 2.0.26 AWS 리소스 정리 — ReCoder 가 만든(태그가 붙은) 리소스만 고르고, 승인한 것만 지운다.
 *  · 목록 불러오기는 아무것도 바꾸지 않는다.
 *  · 「중지」는 태스크 수만 0 으로(되살릴 수 있음). 「완전 삭제」는 항목을 직접 체크하고 승인 카드에서 한 번 더 확인해야 한다.
 *  · 태그 없는 옛 리소스는 "수동 확인 필요" 로 보여 주기만 한다(코어도 지우지 않는다).
 */
import React, { useCallback, useEffect, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { Checkbox } from "./Check";

export interface CleanupItemView {
  id: string; kind: string; label: string; name: string; service: string; cluster: string; tagged: boolean;
  deletable: boolean; detail: string; cost: string; note: string;
}
export interface CleanupPlanView {
  region: string;
  groups: Array<{ service: string; items: CleanupItemView[] }>;
  untagged: CleanupItemView[];
  summary: { running_tasks: number; load_balancers: number; web_acls: number; image_mb: number; tagged: number; untagged: number };
  gaps: string[];
}
interface ResultView { id: string; label?: string; name?: string; ok: boolean; skipped?: boolean; message: string }

export const CLEANUP_REGIONS = ["us-east-1", "us-west-2", "ap-northeast-2", "ap-northeast-1", "eu-west-1"];
/** 리전 이름 형식(코어와 같은 규칙). 2.0.32 — 정해 둔 5곳 밖(eu-central-1·ap-southeast-1 등)도 직접 넣을 수 있다. */
export const REGION_RE = /^[a-z]{2}(-gov)?-[a-z]+-\d$/;

/** 고를 리전 목록 — 배포한 적 있는 리전을 앞에, 기본 몇 곳을 뒤에. */
export function cleanupRegionChoices(defaultRegion: string, deployed: string[]): string[] {
  return [...new Set([defaultRegion, ...deployed, ...CLEANUP_REGIONS].filter(r => REGION_RE.test(r)))];
}

interface CleanupJobView { id: string; region: string; status: string; progress?: { done: number; total: number; current?: string }; error?: string }

const box: React.CSSProperties = { border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 8, padding: 12, marginBottom: 10 };
const muted: React.CSSProperties = { color: "var(--vscode-descriptionForeground, #999)", fontSize: 11, lineHeight: 1.5 };
const btn: React.CSSProperties = { padding: "5px 10px", borderRadius: 5, border: "1px solid var(--vscode-button-border, transparent)",
  background: "var(--vscode-button-secondaryBackground, #3a3d41)", color: "var(--vscode-button-secondaryForeground, #fff)", cursor: "pointer", fontSize: 12 };
const danger: React.CSSProperties = { ...btn, background: "var(--vscode-inputValidation-errorBackground, #5a1d1d)",
  border: "1px solid var(--vscode-inputValidation-errorBorder, #be1100)" };

/** 체크한 항목 중 지울 수 있는 것만(태그 확인된 것). 화면이 오래돼도 코어가 다시 확인한다. */
export function selectedIds(plan: CleanupPlanView | null, picked: Record<string, boolean>): string[] {
  if (!plan) return [];
  return plan.groups.flatMap(g => g.items).filter(i => i.deletable && picked[i.id]).map(i => i.id);
}

export function CleanupGroup({ group, picked, onPick, onStop, disabled }: {
  group: { service: string; items: CleanupItemView[] }; picked: Record<string, boolean>;
  onPick: (id: string, v: boolean) => void; onStop: (item: CleanupItemView) => void; disabled: boolean;
}) {
  return (
    <div style={box} data-testid="cleanup-group">
      <b>{group.service}</b>
      {group.items.map(item => (
        <div key={item.id} style={{ display: "flex", gap: 8, alignItems: "flex-start", marginTop: 6 }}>
          <Checkbox checked={!!picked[item.id]} disabled={disabled || !item.deletable} onChange={v => onPick(item.id, v)}
            ariaLabel={`${item.label} ${item.name} 삭제 선택`}
            label={<span>{item.label} <code>{item.name}</code></span>}
            description={[item.detail, item.cost].filter(Boolean).join(" · ")} />
          {item.kind === "ecs_service" && (
            <button style={{ ...btn, marginLeft: "auto", whiteSpace: "nowrap", flexShrink: 0 }} disabled={disabled} onClick={() => onStop(item)}>중지</button>
          )}
        </div>
      ))}
    </div>
  );
}

/** 2.0.31 뒤에서 지우는 동안의 진행 문장. */
export function cleanupProgressText(p: Record<string, unknown>): string {
  const done = Number(p.done ?? 0), total = Number(p.total ?? 0);
  const current = typeof p.current === "string" && p.current ? ` — ${p.current}` : "";
  return `지우는 중 ${Math.min(done + 1, Math.max(total, 1))}/${Math.max(total, 1)}${current} (DB·CloudFront 는 수 분~15분 걸립니다. 이 탭을 떠나도 코어가 계속 지우지만, VS Code 를 닫으면 멈춥니다 — 다시 열어 목록을 불러오면 이어서 지울 수 있습니다.)`;
}

export function DeleteApproval({ items, busy, onApprove, onCancel }: {
  items: CleanupItemView[]; busy: boolean; onApprove: () => void; onCancel: () => void;
}) {
  return (
    <div style={{ ...box, borderColor: "var(--vscode-inputValidation-errorBorder, #be1100)" }} data-testid="cleanup-approval">
      <b>승인 필요 — {items.length}개 완전 삭제</b>
      <div style={{ fontSize: 12, margin: "6px 0" }}>삭제는 되돌릴 수 없습니다. 이미지·로그·S3 파일도 함께 사라집니다.</div>
      <ul style={{ margin: "4px 0 8px 16px", padding: 0, fontSize: 12 }}>
        {items.map(i => <li key={i.id}>{i.label} <code>{i.name}</code></li>)}
      </ul>
      <div style={{ display: "flex", gap: 6 }}>
        <button style={danger} disabled={busy} onClick={onApprove}>{busy ? "지우는 중…(몇 분 걸릴 수 있음)" : "삭제 승인"}</button>
        <button style={btn} disabled={busy} onClick={onCancel}>취소</button>
      </div>
    </div>
  );
}

export const AwsCleanupPanel: React.FC<{ defaultRegion?: string }> = ({ defaultRegion = "us-east-1" }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [region, setRegion] = useState(defaultRegion);
  const [plan, setPlan] = useState<CleanupPlanView | null>(null);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [results, setResults] = useState<ResultView[]>([]);
  const [deployed, setDeployed] = useState<string[]>([]);
  const [customRegion, setCustomRegion] = useState("");

  useMessage(useCallback((event) => {
    const p = (event.payload ?? {}) as Record<string, unknown>;
    if (event.type === "aws.cleanup.regionsResult") { setDeployed(((p.regions as string[]) ?? []).filter(r => REGION_RE.test(r))); }
    if (event.type === "aws.cleanup.jobsResult") {
      //: 2.0.32 — 탭을 다시 열었을 때 진행 중인 정리에 다시 붙고, 코어가 꺼져 중단된 정리를 알린다.
      const latest = ((p.jobs as CleanupJobView[]) ?? [])[0];
      if (latest?.status === "running") {
        setBusy(true); setConfirming(true);
        setNote(cleanupProgressText((latest.progress ?? {}) as Record<string, unknown>));
        postMessage("aws.cleanup.attach", { region: latest.region, jobId: latest.id });
      } else if (latest?.status === "interrupted") {
        setNote(latest.error || "지난 정리가 중간에 멈췄습니다 — 목록을 불러와 남은 것을 이어서 지우세요.");
      }
    }
    if (event.type === "aws.cleanup.planResult") { setPlan(p as unknown as CleanupPlanView); setBusy(false); setPicked({}); }
    if (event.type === "aws.cleanup.stopResult") { setNote(String(p.message ?? "")); }
    if (event.type === "aws.cleanup.deleteResult") {
      setResults((p.results as ResultView[]) ?? []); setConfirming(false);
      setNote(`${p.deleted ?? 0}개를 지웠습니다${p.failed ? ` · ${p.failed}개는 실패(아래 이유)` : ""}.`);
    }
    if (event.type === "aws.cleanup.progress") { setNote(cleanupProgressText(p)); }
    if (event.type === "aws.cleanup.refreshError") {
      setBusy(false); setConfirming(false);
      setNote(prev => `${prev ? prev + " " : ""}목록은 새로 고치지 못했습니다(${String(p.message ?? "")}) — [목록 불러오기]를 눌러 주세요.`);
    }
    if (event.type === "aws.cleanup.error") { setBusy(false); setConfirming(false); setNote(String(p.message ?? "실패했습니다")); }
  }, [postMessage]));

  const load = () => { setBusy(true); setNote(""); setResults([]); postMessage("aws.cleanup.plan", { region }); };
  useEffect(() => { postMessage("aws.cleanup.regions"); }, [postMessage]);
  useEffect(() => { postMessage("aws.cleanup.jobs", { region }); }, [postMessage, region]);
  const regionChoices = cleanupRegionChoices(defaultRegion, deployed);
  const ids = selectedIds(plan, picked);
  const chosen = plan ? plan.groups.flatMap(g => g.items).filter(i => ids.includes(i.id)) : [];

  return (
    <section data-testid="aws-cleanup" style={{ marginBottom: 18 }}>
      <h3 style={{ margin: "0 0 4px" }}>AWS 리소스 정리</h3>
      <p style={muted}>ReCoder 가 만든 리소스(태그 recoder:managed)만 고를 수 있습니다. 「중지」는 실행 요금만 멈추고 다시 배포하면 살아납니다. 「완전 삭제」는 승인한 항목만 지웁니다.</p>
      <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 8 }}>
        <label style={{ fontSize: 12 }}>리전{" "}
          <select value={region} disabled={busy} onChange={e => { setRegion(e.target.value); setPlan(null); }}>
            {[...new Set([region, ...regionChoices])].map(r => <option key={r} value={r}>{r}{deployed.includes(r) ? " (배포함)" : ""}</option>)}
          </select>
        </label>
        <input value={customRegion} disabled={busy} placeholder="다른 리전 (예: eu-central-1)" aria-label="다른 리전"
          onChange={e => setCustomRegion(e.target.value.trim())} style={{ width: 150, fontSize: 12 }} />
        <button style={btn} disabled={busy || !REGION_RE.test(customRegion)}
          onClick={() => { setRegion(customRegion); setPlan(null); setCustomRegion(""); }}>바꾸기</button>
        <button style={btn} disabled={busy} onClick={load}>{busy && !confirming ? "불러오는 중…" : "목록 불러오기"}</button>
      </div>
      {note && <div style={{ ...box, fontSize: 12 }} data-testid="cleanup-note">{note}</div>}
      {plan && (
        <>
          <div style={{ ...muted, marginBottom: 6 }} data-testid="cleanup-summary">
            실행 중 태스크 {plan.summary.running_tasks}개 · 로드밸런서 {plan.summary.load_balancers}개 · WAF {plan.summary.web_acls}개 · 이미지 {plan.summary.image_mb.toLocaleString()} MB
          </div>
          {plan.gaps.map(g => <div key={g} style={{ ...muted, color: "var(--vscode-editorWarning-foreground, #cca700)" }}>{g}</div>)}
          {plan.groups.length === 0 && <div style={muted}>이 리전에 ReCoder 가 만든 리소스가 없습니다.</div>}
          {plan.groups.map(g => (
            <CleanupGroup key={g.service} group={g} picked={picked} disabled={busy}
              onPick={(id, v) => setPicked(prev => ({ ...prev, [id]: v }))}
              onStop={item => { setNote(""); postMessage("aws.cleanup.stop", { region, cluster: item.cluster, service: item.name }); }} />
          ))}
          {plan.untagged.length > 0 && (
            <div style={box} data-testid="cleanup-untagged">
              <b>수동 확인 필요 ({plan.untagged.length})</b>
              <div style={muted}>{plan.untagged[0].note}</div>
              {plan.untagged.map(i => <div key={i.id} style={{ fontSize: 12 }}>{i.label} <code>{i.name}</code> <span style={muted}>{i.detail}</span></div>)}
            </div>
          )}
          {!confirming && (
            <button style={danger} disabled={busy || ids.length === 0} onClick={() => setConfirming(true)}>
              선택한 {ids.length}개 완전 삭제…
            </button>
          )}
          {confirming && (
            <DeleteApproval items={chosen} busy={busy}
              onApprove={() => { setBusy(true); postMessage("aws.cleanup.delete", { region, ids, approved: true }); }}
              onCancel={() => setConfirming(false)} />
          )}
        </>
      )}
      {results.length > 0 && (
        <div style={box} data-testid="cleanup-results">
          {results.map(r => <div key={r.id} style={{ fontSize: 12 }}>{r.ok ? "✓" : r.skipped ? "–" : "✗"} {r.label ?? r.id} {r.name ? <code>{r.name}</code> : null} <span style={muted}>{r.message}</span></div>)}
        </div>
      )}
    </section>
  );
};
