/**
 * 작업별 AI 공급자(2.0.36) — 화면이 쓰는 형태와 순수 계산(포맷·비교 표). React 없이 테스트한다.
 */
export interface RoutingChoice { spec: string; label: string; ready: boolean; provider: string; model: string }
export interface ModelRouting {
  quality: { provider: string; model: string };
  economy: { spec: string; provider: string; model: string; separate: boolean; scrubbed: boolean; note: string };
  economy_operations: string[];
  choices: RoutingChoice[];
}
export interface CostGroup { tier: "economy" | "quality"; provider: string; model: string; calls: number; input_tokens: number; output_tokens: number; cost_usd: number; unknown_cost_calls: number; estimated_token_calls: number; masked: number }
export interface ModelCosts { calls: number; total_cost_usd: number; unknown_cost_calls: number; economy_saved_usd: number; groups: CostGroup[]; days: number; persisted: boolean }
export interface CompareMetrics {
  calls: number; repair_calls: number; input_tokens: number; output_tokens: number; estimated_token_calls: number;
  cost_usd: number | null; unknown_cost_calls: number; masked: number; used: Array<{ name: string; calls: number }>;
  files: number; remaining_errors: number | null; security_score: number | null; security_findings: number | null;
}
export interface CompareSide { spec: string; provider: string; model: string; status: "waiting" | "running" | "succeeded" | "failed"; metrics: CompareMetrics | null; error: string; seconds: number | null }
export interface CompareJob { id: string; status: "running" | "finished"; started_at: number; finished_at: number | null; sides: CompareSide[]; verdict: Record<string, string | null> | null }
export interface ModelsState { routing: ModelRouting; costs: ModelCosts; compare: CompareJob | null; keys: Record<string, boolean> | null; settings: { economyProvider: string; compatibleBaseUrl: string } }

export const PROVIDER_LABEL: Record<string, string> = {
  bedrock: "AWS Bedrock", gateway: "게이트웨이", anthropic: "Claude API", openai: "OpenAI API", compatible: "OpenAI 호환", gemini: "Gemini(대체)",
};
export const TIER_LABEL: Record<string, string> = { quality: "품질", economy: "절약" };
export const ECONOMY_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "품질과 같은 공급자" },
  { value: "fast", label: "품질 공급자의 빠른 모델" },
  { value: "compatible", label: "OpenAI 호환 API (DeepSeek 등)" },
  { value: "anthropic", label: "Claude API" },
  { value: "openai", label: "OpenAI API" },
];
export const OPERATION_LABEL: Record<string, string> = {
  generate_plan_topics: "설계 질문 주제", summarize_request: "요청 요약", summarize_scan: "스캔 요약",
  summarize_scan_results: "검사 결과 요약", classify_error: "오류 분류", augment_candidates: "원인 후보 문장", generate_test_draft: "테스트 초안",
};

export function providerName(p: string): string { return PROVIDER_LABEL[p] ?? p; }

export function formatUsd(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) { return "단가 미설정"; }
  if (v === 0) { return "$0"; }
  if (v < 0.0001) { return "<$0.0001"; }
  if (v < 0.01) { return `$${v.toFixed(4)}`; }  // 요청 한 번 비용도 비교할 수 있게
  return `$${v.toFixed(v < 1 ? 3 : 2)}`;
}

export function formatTokens(n: number | null | undefined): string {
  if (!n) { return "0"; }
  if (n >= 1_000_000) { return `${(n / 1_000_000).toFixed(1)}M`; }
  if (n >= 1_000) { return `${(n / 1_000).toFixed(1)}K`; }
  return String(n);
}

/** 비용 칸 — 일부 호출의 단가를 모르면 그 사실을 함께 적는다(0 달러처럼 보이지 않게). */
export function costCell(cost: number | null | undefined, unknownCalls: number): string {
  if (cost === null || cost === undefined) { return unknownCalls ? "단가 미설정" : "$0"; }
  return unknownCalls ? `${formatUsd(cost)} + 단가 미설정 ${unknownCalls}회` : formatUsd(cost);
}

export interface CompareRow { key: string; label: string; values: [string, string]; better: 0 | 1 | null }

/** 비교 표 — 항목마다 더 나은 쪽(verdict 기준). 종합 승자는 만들지 않는다. */
export function compareRows(job: CompareJob): CompareRow[] {
  const [a, b] = job.sides;
  if (!a || !b) { return []; }
  const pick = (key: string): 0 | 1 | null => {
    const spec = job.verdict?.[key];
    if (!spec) { return null; }
    return spec === a.spec ? 0 : spec === b.spec ? 1 : null;
  };
  const m = (s: CompareSide) => s.metrics;
  const val = (s: CompareSide, f: (x: CompareMetrics) => string) => (m(s) ? f(m(s) as CompareMetrics) : s.status === "failed" ? "실패" : "—");
  const both = (f: (x: CompareMetrics) => string): [string, string] => [val(a, f), val(b, f)];
  return [
    { key: "cost", label: "비용", values: both((x) => costCell(x.cost_usd, x.unknown_cost_calls)), better: pick("cheaper") },
    { key: "tokens", label: "토큰(입력/출력)", values: both((x) => `${formatTokens(x.input_tokens)} / ${formatTokens(x.output_tokens)}${x.estimated_token_calls ? " (어림)" : ""}`), better: null },
    { key: "calls", label: "AI 호출", values: both((x) => `${x.calls}회`), better: null },
    { key: "repairs", label: "교정 횟수", values: both((x) => `${x.repair_calls}회`), better: pick("fewer_repairs") },
    { key: "errors", label: "남은 오류", values: both((x) => (x.remaining_errors === null ? "—" : `${x.remaining_errors}개`)), better: pick("fewer_errors") },
    { key: "security", label: "보안 점수", values: both((x) => (x.security_score === null ? "점수 없음" : `${x.security_score}점${x.security_findings ? ` · 확인 ${x.security_findings}곳` : ""}`)), better: pick("safer") },
    { key: "files", label: "만든 파일", values: both((x) => `${x.files}개`), better: null },
    { key: "time", label: "걸린 시간", values: [a.seconds === null ? "—" : `${Math.round(a.seconds)}초`, b.seconds === null ? "—" : `${Math.round(b.seconds)}초`], better: null },
  ];
}

/** 비교에 고를 수 있는 공급자 — 준비된 것만, 같은 공급자·모델은 한 번만. */
export function compareChoices(routing: ModelRouting): RoutingChoice[] {
  const seen = new Set<string>();
  const out: RoutingChoice[] = [];
  const economy = routing.economy.separate
    ? [{ spec: "economy", label: "지금 절약 공급자", ready: true, provider: routing.economy.provider, model: routing.economy.model }]
    : [];
  for (const c of [...routing.choices.filter((c) => c.spec === "quality"), ...economy, ...routing.choices.filter((c) => c.spec !== "quality")]) {
    const key = `${c.provider}/${c.model}`;
    if (!c.ready || seen.has(key)) { continue; }
    seen.add(key);
    out.push(c);
  }
  return out;
}
