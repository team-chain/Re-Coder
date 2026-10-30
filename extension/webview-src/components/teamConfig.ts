/**
 * 팀 구성(2.0.45) — 입력창의 「팀」 창이 다루는 설정과 순수 계산. React 없이 테스트한다.
 *
 *  - 프리셋: 품질 우선 / 균형(추천) / 비용 절약 / 직접 설정 — 역할(설계·개발·검토·테스트)마다 쓸 AI 공급자를 한꺼번에 정한다.
 *  - 역할별 모델: 코어 task_tier.ROLE_SPECS 와 같은 이름(quality·fast·anthropic·openai·compatible). 키가 없는 공급자는 고를 수 없다.
 *  - 추천 인원: 요청 글의 길이·기능 수로 본 대략의 크기(정확한 크기는 설계가 끝나야 안다 — 화면도 "요청 길이로 본 추천"이라고 쓴다).
 *  - 예상 시간·비용: **지난 실행 기록**의 범위만 보여 준다. 기록이 없으면 지어내지 않고 "첫 실행 뒤 표시".
 */
import type { RoutingChoice } from "./modelRoutingModel";

export type ModelSpec = "quality" | "fast";
export type TeamRoleKey = "planner" | "dev" | "review" | "test";
export type Preset = "quality" | "balanced" | "economy" | "custom";

export const ROLE_KEYS: TeamRoleKey[] = ["planner", "dev", "review", "test"];
export const MODEL_SPECS: ModelSpec[] = ["quality", "fast"];
export const ROLE_TEXT: Record<TeamRoleKey, { name: string; detail: string }> = {
  planner: { name: "설계", detail: "파일과 작업 순서를 정함" },
  dev: { name: "개발", detail: "파일을 나눠 동시에 작성" },
  review: { name: "검토", detail: "코드를 읽고 문법·비밀값 수정" },
  test: { name: "테스트", detail: "앱을 실행해 완성 기준 확인" },
};
export const PRESET_TEXT: Record<Preset, string> = { quality: "품질 우선", balanced: "균형", economy: "비용 절약", custom: "직접 설정" };

export const PRESETS: Record<Exclude<Preset, "custom">, Record<TeamRoleKey, ModelSpec>> = {
  quality: { planner: "quality", dev: "quality", review: "quality", test: "quality" },
  //: 코드를 쓰는 역할(설계·개발)은 품질 모델, 읽고 확인하는 역할(검토·테스트)은 빠르고 싼 모델.
  balanced: { planner: "quality", dev: "quality", review: "fast", test: "fast" },
  economy: { planner: "fast", dev: "fast", review: "fast", test: "fast" },
};

export interface TeamConfig { auto: boolean; preset: Preset; devs: number; test: boolean; models: Record<TeamRoleKey, ModelSpec> }
export const MIN_DEVS = 1;
export const MAX_DEVS = 6;

export function defaultConfig(): TeamConfig {
  return { auto: true, preset: "balanced", devs: 3, test: true, models: { ...PRESETS.balanced } };
}

function presetOf(models: Record<TeamRoleKey, ModelSpec>): Preset {
  for (const p of ["balanced", "quality", "economy"] as const) {
    if (ROLE_KEYS.every((r) => PRESETS[p][r] === models[r])) return p;
  }
  return "custom";
}

/** 저장해 둔 값(비신뢰) → 알맞은 설정. 모르는 값은 기본으로. */
export function cleanConfig(raw: unknown): TeamConfig {
  const base = defaultConfig();
  if (!raw || typeof raw !== "object") return base;
  const r = raw as Partial<TeamConfig>;
  const models = { ...base.models };
  for (const role of ROLE_KEYS) {
    const v = (r.models as Record<string, unknown> | undefined)?.[role];
    if (typeof v === "string" && (MODEL_SPECS as string[]).includes(v)) models[role] = v as ModelSpec;
  }
  const devs = typeof r.devs === "number" && Number.isFinite(r.devs) ? Math.max(MIN_DEVS, Math.min(MAX_DEVS, Math.round(r.devs))) : base.devs;
  const preset = typeof r.preset === "string" && ["quality", "balanced", "economy", "custom"].includes(r.preset)
    ? r.preset : presetOf(models);
  return { auto: typeof r.auto === "boolean" ? r.auto : true, preset, devs, test: r.test !== false,
    models: preset === "custom" ? models : { ...PRESETS[preset] } };
}

/** 직접 설정은 현재 표시값을 이어받고, 추천 프리셋을 고르면 자동 적용을 켠다. */
export function applyPreset(cfg: TeamConfig, preset: Preset, instruction = "", lockSensitive = true, autoRecommend = true): TeamConfig {
  const current = effectiveConfig(cfg, instruction, lockSensitive, autoRecommend);
  if (preset === "custom") return { ...current, auto: false, preset: "custom", models: { ...current.models } };
  return { ...cfg, auto: preset === recommendPreset(instruction), preset, models: { ...PRESETS[preset] } };
}

export function setModel(cfg: TeamConfig, role: TeamRoleKey, spec: ModelSpec): TeamConfig {
  return { ...cfg, auto: false, preset: "custom", models: { ...cfg.models, [role]: spec } };
}

export type SensitiveKind = "결제" | "인증" | "개인정보";
export function sensitiveOf(instruction: string): SensitiveKind[] {
  const rules: [SensitiveKind, RegExp][] = [
    ["결제", /결제|환불|카드|청구|송금|계좌/],
    ["인증", /로그인|회원\s*가입|인증|비밀번호|토큰|권한|세션|oauth/i],
    ["개인정보", /개인\s*정보|주민\s*번호|전화\s*번호|주소록|위치\s*정보/],
  ];
  return rules.filter(([, pattern]) => pattern.test(instruction || "")).map(([name]) => name);
}

export function recommendPreset(instruction: string): Exclude<Preset, "custom"> {
  const { size } = recommend(instruction);
  return sensitiveOf(instruction).length || size === "large" ? "quality" : size === "small" ? "economy" : "balanced";
}

export function lockedRoles(instruction: string, lockSensitive = true): TeamRoleKey[] {
  return lockSensitive && sensitiveOf(instruction).length ? ["planner", "dev"] : [];
}

/** 표시·전송에 함께 쓰는 모델. 저장한 직접 설정을 고정 규칙으로 덮어쓰지 않는다. */
export function effectiveConfig(cfg: TeamConfig, instruction: string, lockSensitive = true, autoRecommend = true): TeamConfig {
  const preset = cfg.auto && autoRecommend && cfg.preset !== "custom" ? recommendPreset(instruction) : cfg.preset;
  const models = { ...(preset === "custom" ? cfg.models : PRESETS[preset]) };
  for (const role of lockedRoles(instruction, lockSensitive)) models[role] = "quality";
  return { ...cfg, preset, models };
}

export function restoreRecommendation(cfg: TeamConfig, instruction: string): TeamConfig {
  const preset = recommendPreset(instruction);
  return { ...cfg, auto: true, preset, models: { ...PRESETS[preset] } };
}

/** 추천이 달라질 때 직접 설정 화면은 유지하고 모델만 새 추천으로 바꾼다. */
export function updateRecommendation(cfg: TeamConfig, previous: string, instruction: string, autoRecommend = true): TeamConfig {
  if (!autoRecommend || recommendPreset(previous) === recommendPreset(instruction)) return cfg;
  const recommended = restoreRecommendation(cfg, instruction);
  return cfg.preset === "custom" ? { ...recommended, auto: false, preset: "custom" } : recommended;
}

export function setDevs(cfg: TeamConfig, devs: number): TeamConfig {
  return { ...cfg, devs: Math.max(MIN_DEVS, Math.min(MAX_DEVS, Math.round(devs))) };
}

/** 지금 쓸 수 없는 공급자(키 없음)를 고른 역할은 기본(quality)으로 — 요청에 쓸 수 없는 값을 싣지 않는다. */
export function usableModel(spec: ModelSpec, ready: ReadonlySet<string> | null): ModelSpec {
  if (spec === "quality") return spec;
  return !ready || ready.has(spec) ? spec : "quality";
}

/**
 * 생성 요청에 싣는 역할별 공급자 — quality 도 명시해 절약 작업의 기본 경로보다 우선한다. 팀 모드가 꺼져 있으면
 * 설계·검토 리코더가 따로 없으므로 개발(혼자 만드는 리코더)과 테스트만.
 */
export function modelsForRequest(cfg: TeamConfig, teamMode: boolean, ready: ReadonlySet<string> | null, instruction?: string, lockSensitive = true, autoRecommend = true): Partial<Record<TeamRoleKey, ModelSpec>> | null {
  cfg = instruction === undefined ? cfg : effectiveConfig(cfg, instruction, lockSensitive, autoRecommend);
  const roles: TeamRoleKey[] = teamMode ? ROLE_KEYS : ["dev", "test"];
  const out: Partial<Record<TeamRoleKey, ModelSpec>> = {};
  for (const role of roles) {
    if (role === "test" && !cfg.test) continue;
    const spec = usableModel(cfg.models[role], ready);
    out[role] = spec;
  }
  return Object.keys(out).length ? out : null;
}

// ── 모델 이름 ─────────────────────────────────────────────────────────────

/** 모델 ID → 사람이 읽는 이름(예: us.anthropic.claude-sonnet-4-5-20250929-v1:0 → Claude Sonnet 4.5). */
export function prettyModel(model: string): string {
  const id = (model || "").toLowerCase();
  const claude = /claude-(?:(\d)(?:-(\d))?-)?(opus|sonnet|haiku)(?:-(\d)(?:-(\d))?(?=-|$|:))?/.exec(id);
  if (claude) {
    const family = claude[3][0].toUpperCase() + claude[3].slice(1);
    const ver = claude[4] ? `${claude[4]}${claude[5] ? `.${claude[5]}` : ""}` : claude[1] ? `${claude[1]}${claude[2] ? `.${claude[2]}` : ""}` : "";
    return `Claude ${family}${ver ? ` ${ver}` : ""}`;
  }
  const gpt = /^(gpt-[\w.-]+?)(?:-(\d{4}-\d{2}-\d{2}))?$/.exec(id);
  if (gpt) return gpt[1].replace(/^gpt/, "GPT").replace(/-mini$/, " mini").replace(/-nano$/, " nano").replace(/-(sol|terra|luna|astra)$/, (_, name: string) => ` ${name[0].toUpperCase()}${name.slice(1)}`);
  return model.length > 28 ? `${model.slice(0, 27)}…` : model || "기본 모델";
}

export interface ModelOption { spec: ModelSpec; label: string; detail: string; ready: boolean }

const SPEC_DETAIL: Record<ModelSpec, string> = { quality: "복잡", fast: "간단" };

/** 팀 창은 두 등급만 선택한다. 실제 모델명은 routing choices에서 가져온다. */
export function modelOptions(choices: RoutingChoice[] | null | undefined): ModelOption[] {
  return MODEL_SPECS.map((spec) => {
    const c = choices?.find((choice) => choice.spec === spec);
    return { spec, label: c?.model ? prettyModel(c.model) : spec === "quality" ? "복잡한 작업 모델" : "간단한 작업 모델",
      detail: SPEC_DETAIL[spec], ready: c ? !!c.ready : true };
  });
}

export function readySpecs(options: ModelOption[]): Set<string> {
  return new Set(options.filter((o) => o.ready).map((o) => o.spec));
}

// ── 추천 · 예상 ──────────────────────────────────────────────────────────

export type RequestSize = "small" | "medium" | "large";
export const SIZE_TEXT: Record<RequestSize, string> = { small: "작음", medium: "중간", large: "큼" };

/** 요청 글로 본 대략의 크기 → 추천 개발 인원. 분당 호출 한도 때문에 4명을 넘겨 추천하지 않는다. */
export function recommend(instruction: string): { size: RequestSize; devs: number } {
  const text = (instruction || "").trim();
  if (!text) return { size: "medium", devs: 3 };
  //: 기능 수 — 쉼표·줄바꿈, 그리고 "댓글과 좋아요"처럼 낱말에 붙는 와/과/랑/하고, 띄어 쓴 및/그리고.
  const features = text.split(/[,，·\n]|(?<=[가-힣])(?:와|과|이랑|랑|하고)\s|\s(?:및|그리고)\s/).map((s) => s.trim()).filter((s) => s.length > 1).length;
  const big = /쇼핑몰|커머스|관리자|대시보드|결제|전체|풀스택|full ?stack|서비스|플랫폼|앱\s*전체/i.test(text);
  if (text.length > 160 || features >= 5 || (big && text.length > 40)) return { size: "large", devs: 4 };
  if (text.length < 30 && features <= 1 && !big) return { size: "small", devs: 2 };
  return { size: "medium", devs: 3 };
}

/** team: 팀 모드로 만들었는지 — 혼자 만든 기록과 섞어 예상하지 않는다. */
export interface RunStat { seconds: number; costUsd: number | null; files: number; devs: number; preset: Preset; at: number; team?: boolean }
export const MAX_HISTORY = 12;

export function cleanHistory(raw: unknown): RunStat[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((r): r is RunStat => !!r && typeof r === "object" && typeof (r as RunStat).seconds === "number"
    && (r as RunStat).seconds > 0 && typeof (r as RunStat).files === "number")
    .map((r) => ({ seconds: r.seconds, costUsd: typeof r.costUsd === "number" && r.costUsd >= 0 ? r.costUsd : null, files: r.files,
      devs: typeof r.devs === "number" ? r.devs : 3, preset: (["quality", "balanced", "economy", "custom"] as Preset[]).includes(r.preset) ? r.preset : "custom",
      at: typeof r.at === "number" ? r.at : 0, team: r.team !== false }))
    .slice(-MAX_HISTORY);
}

export function addRun(history: RunStat[], run: RunStat): RunStat[] {
  return [...history, run].slice(-MAX_HISTORY);
}

/**
 * 같은 방식(팀·혼자)으로 만든 지난 실행(같은 프리셋이 2번 이상이면 그것만, 아니면 전부)의 최근 5개 범위. 기록이 없으면 null —
 * 화면은 숫자를 지어내지 않고 "첫 실행 뒤 표시"라고 쓴다.
 */
export function estimate(history: RunStat[], cfg: TeamConfig, team = true): { minutes: [number, number]; cost: [number, number] | null; runs: number } | null {
  const mode = history.filter((h) => (h.team !== false) === team);
  const same = mode.filter((h) => h.preset === cfg.preset);
  const pool = (same.length >= 2 ? same : mode).slice(-5);
  if (!pool.length) return null;
  const mins = pool.map((h) => h.seconds / 60);
  const costs = pool.map((h) => h.costUsd).filter((c): c is number => c !== null);
  const lo = Math.max(1, Math.floor(Math.min(...mins)));
  const hi = Math.max(lo, Math.ceil(Math.max(...mins)));
  return { minutes: [lo, hi], cost: costs.length ? [Math.min(...costs), Math.max(...costs)] : null, runs: pool.length };
}

export function formatCost(usd: number): string {
  if (usd < 0.01) return "<$0.01";
  return `$${usd < 10 ? usd.toFixed(2) : usd.toFixed(1)}`;
}
