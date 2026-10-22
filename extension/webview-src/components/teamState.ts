/**
 * 팀 모드 화면 상태 — 코어의 대규모 생성 진행 이벤트(SSE)를 화면 상태로 접는 순수 함수.
 * 화면에 보이는 모든 숫자(완료 수·에이전트별 작업·고친 문제)는 실제 이벤트에서만 나온다.
 */
import { AnimalKind, pickAnimal } from "./teamAnimals";

export type TeamRole = "planner" | "dev" | "review" | "test";
export interface TeamMember { id: string; role: TeamRole; animal: AnimalKind; }

export interface TeamEvent {
  step: string; message?: string; job_id?: string; agent?: string; file?: string; part?: number; lines?: number;
  layer?: number; agents?: number; seconds?: number; done_count?: number; total?: number; elapsed?: number;
  summary?: string; files?: Array<{ file: string; layer?: number; purpose?: string }>;
  kind?: string; attempt?: number;
  /** 2.0.24 게시판(board_post) 항목 · 관문(gate_check) 결과 */
  items?: BoardItem[]; ok?: boolean; fixed?: string[]; returned?: boolean; by?: string;
  /** 2.0.45 사용량(usage) — 실제 응답의 모델·토큰·비용 */
  model?: string; provider?: string; input_tokens?: number; output_tokens?: number; cost_usd?: number; operation?: string;
  /** 2.0.45 완성 파일 앞부분(file_done) — 비밀값은 코어가 가린다 */
  preview?: string;
  /** 2.0.45 테스트 리코더(acceptance) */
  phase?: string; round?: number; passed?: number; failed?: number; unverifiable?: number; checks?: number;
  criteria?: Array<{ id: string; verdict: string }>;
}

/** 리코더 한 명의 실제 사용량 — usage 이벤트에서만 센다. */
export interface AgentUsage { calls: number; inputTokens: number; outputTokens: number; costUsd: number; unknownCost: number; models: string[] }

/** 테스트 리코더 진행 — acceptance 이벤트에서만. */
export interface TestView {
  phase: "writing" | "written" | "running" | "result";
  checks: number; unverifiable: number; passed: number; failed: number; total: number; round: number;
  criteria: Array<{ id: string; verdict: string }>;
}

/** 게시판 메모 한 장 — 서버 라우트 파일에서 뽑은 실제 경로·응답 모양(지어내지 않는다). */
export interface BoardItem { method: string; path: string; shape?: string | null; auth?: boolean }

/** 마지막 관문 결과 — 검토 리코더가 그 작업대로 가서 도장을 찍는다. */
export interface GateView { file: string; by?: string; ok: boolean; fixed: number; returned: boolean; seq: number }

/** failed: 정해진 횟수를 넘겨 실패 — 완료로 세지 않는다(사용자가 다시 쓰기·빼고 받기를 고른다). */
export type FileState = "waiting" | "writing" | "parts" | "fixing" | "done" | "issue" | "failed";
export type AgentState = "idle" | "writing" | "parts" | "fixing" | "waiting" | "done";

export interface TeamAgentView { id: string; state: AgentState; file?: string; part?: number; lines?: number; done: number;
  /** 2.0.45 — 이 리코더가 처음·마지막으로 움직인 때(경과 초)와 완성한 파일 */
  firstAt?: number; lastAt?: number; files?: string[]; }

export interface TeamView {
  jobId: string;
  /** 대규모 생성 엔진(설계·파일 작업)이 실제로 돌았는지 — 검증된 기반처럼 생성하지 않는 경로는 false. */
  engaged?: boolean;
  phase: "planning" | "building" | "checking" | "done";
  summary: string;
  /** polish: 이미 완성된 파일을 마지막 전체 점검이 다듬는 중 — 완료로 센 채 표시만 붙인다(완료 수가 줄지 않게). */
  files: Array<{ file: string; layer: number; state: FileState; polish?: boolean; by?: string }>;
  total: number;
  done: number;
  layer: number | null;
  agents: Record<string, TeamAgentView>;
  fixes: number;
  secretFixes: number;
  /** 보안 검사가 설치 전에 막은 수상한 패키지 건수(security_block 이벤트) — 2.0.23. */
  securityBlocks?: number;
  /** 2.0.24 공용 게시판 — 경로마다 한 장(같은 경로는 나중 것으로) */
  board?: Array<BoardItem & { file: string; by?: string }>;
  /** 누가 게시판을 보고 어떤 파일을 시작했나(에이전트 → 파일) */
  boardReaders?: Record<string, string>;
  /** 마지막으로 게시판에 붙인 에이전트와 장 수 */
  lastPost?: { agent: string; count: number; seq: number } | null;
  gate?: GateView | null;
  gates?: { passed: number; fixed: number; returned: number };
  issues: number;
  retries: number;
  elapsed: number;
  log: string[];
  /** 진행 기록 — 실제 이벤트를 "누가 무엇을 했는지" 한 줄로(최근 30개). 에이전트끼리의 대화가 아니다. */
  records: TeamRecord[];
  /** 2.0.45 리코더별 사용량(에이전트 ID → 합계) */
  usage?: Record<string, AgentUsage>;
  /** 2.0.45 테스트 리코더 */
  test?: TestView | null;
  /** 2.0.45 완성 파일 앞부분(최근 40개) */
  previews?: Record<string, string>;
  /** 2.0.45 다시 보기용 이벤트(미리보기 글은 빼고 최근 MAX_EVENTS 개) */
  events?: TeamEvent[];
  /** 2.0.45 이어서 만들기 — 새 요청의 시계는 0 부터라 그 전까지 걸린 시간을 더한다. */
  clockBase?: number;
}

export interface TeamRecord { who: string; role: TeamRole | "system"; text: string; tone?: "ok" | "warn" }

export const MAX_DEV_AGENTS = 6;
/** 다시 보기용으로 남기는 이벤트 수(최근 것부터). */
export const MAX_EVENTS = 1000;
export const DEFAULT_DEV_AGENTS = 3;

export function emptyTeamView(): TeamView {
  return { jobId: "", engaged: false, phase: "planning", summary: "", files: [], total: 0, done: 0, layer: null, agents: {},
    fixes: 0, secretFixes: 0, securityBlocks: 0, board: [], boardReaders: {}, lastPost: null, gate: null,
    gates: { passed: 0, fixed: 0, returned: 0 }, issues: 0, retries: 0, elapsed: 0, log: [], records: [],
    usage: {}, test: null, previews: {}, events: [] };
}

/** 에이전트들이 실제로 일하고 있는지 — 검증된 기반(쇼핑몰 등)처럼 AI 가 생성하지 않는 경로에서는 보드를 띄우지 않는다. */
export function teamWorking(view: TeamView | null | undefined): boolean {
  return !!view && (!!view.engaged || view.total > 0 || Object.keys(view.agents).length > 0);
}

/** 팀 구성: 설계 1 + 개발 N + 검토(보안·문법) 1 (+ 테스트 1). 동물은 겹치지 않게 무작위. */
export function buildRoster(devCount: number, rand: () => number = Math.random, keep: TeamMember[] = [], test = false): TeamMember[] {
  const n = Math.max(1, Math.min(MAX_DEV_AGENTS, Math.floor(devCount) || 1));
  const used: AnimalKind[] = [];
  const take = (id: string, role: TeamRole): TeamMember => {
    const existing = keep.find(m => m.id === id);
    const animal = existing && !used.includes(existing.animal) ? existing.animal : pickAnimal(used, rand);
    used.push(animal);
    return { id, role, animal };
  };
  const roster = [take("planner", "planner")];
  for (let i = 1; i <= n; i++) roster.push(take(`agent-${i}`, "dev"));
  roster.push(take("review", "review"));
  if (test) roster.push(take("test", "test"));
  return roster;
}

const STATE_OF_STEP: Record<string, AgentState | undefined> = {
  file_start: "writing", file_split: "parts", file_part: "parts", fixing: "fixing", retry: "waiting", waiting: "waiting",
  file_retry: "writing", part_retry: "parts",
};

const ENGINE_STEPS = new Set(["planning", "planned", "plan_added", "wave", "file_start", "file_split", "file_part", "file_done",
  "fixing", "verified", "verify_failed", "split", "resumed", "generated", "board_post", "board_read", "gate_check"]);

/** 이벤트의 agent 값 → 화면 이름(개발 N · 검토 · 설계). 슬롯 이름은 코어의 agent-1, agent-2 … */
export function agentName(id: string | undefined): { who: string; role: TeamRole | "system" } {
  if (!id) return { who: "", role: "system" };
  if (id === "review") return { who: "검토", role: "review" };
  if (id === "planner") return { who: "설계", role: "planner" };
  if (id === "test") return { who: "테스트", role: "test" };
  const m = /^agent-(\d+)$/.exec(id);
  return m ? { who: `개발 ${m[1]}`, role: "dev" } : { who: id, role: "dev" };
}

const base = (path?: string) => (path ?? "").split("/").pop() || path || "";
const LAYER_TEXT = ["공통 기반", "기능", "화면"];

/** 진행 이벤트 하나 → 기록 한 줄. 에이전트가 한 말처럼 지어내지 않고 일어난 일을 그대로 적는다. */
export function recordOf(event: TeamEvent, files: TeamView["files"]): TeamRecord | null {
  const { who, role } = agentName(event.agent);
  const msg = (event.message ?? "").trim();
  switch (event.step) {
    case "planning": return { who: "설계", role: "planner", text: msg || "요청을 파일 사이의 약속과 작업 목록으로 나누는 중" };
    case "planned": {
      const list = event.files ?? [];
      const n = [0, 1, 2].map(l => list.filter(f => (typeof f.layer === "number" ? f.layer : 1) === l).length);
      return { who: "설계", role: "planner", tone: "ok",
        text: `작업 ${event.total ?? list.length}개로 나눔 — 공통 기반 ${n[0]} → 기능 ${n[1]} → 화면 ${n[2]}` };
    }
    case "wave": {
      if (event.layer === -1 && msg) return { who: "", role: "system", text: msg };
      const l = typeof event.layer === "number" ? event.layer : 1;
      const count = files.filter(f => f.layer === l && f.state !== "done" && f.state !== "issue").length;
      return { who: "", role: "system", text: l === 0
        ? `${LAYER_TEXT[0]} ${count}개 — 다른 파일이 기대므로 한 명이 순서대로 만듭니다`
        : `${LAYER_TEXT[l] ?? "다음 단계"} ${count}개 — ${event.agents ?? "여러"}명이 동시에 만듭니다` };
    }
    case "file_start": return event.file ? { who, role, text: `${event.file} 작성 시작` } : null;
    case "file_split": return event.file ? { who, role, text: /쓰던 (내용|조각)에 이어서|이어 쓰기를 이어서/.test(msg)
      ? `${base(event.file)} 쓰던 내용에 이어서 씁니다`
      : `${base(event.file)} 이(가) 길어 끊기는 만큼 이어 받습니다${/맥락을 줄여/.test(msg) ? " (맥락을 줄여서)" : ""}` } : null;
    case "fixing": return msg.startsWith("전체 점검") ? { who, role, tone: "warn", text: msg.slice(0, 120) } : { who, role, tone: "warn", text: `자동 검사에서 문제 발견 → 고치는 중${msg.includes("—") ? ` (${msg.split("—").slice(1).join("—").trim().slice(0, 60)})` : ""}` };
    case "verified": return event.file ? { who, role, tone: "ok", text: `${base(event.file)} 고친 뒤 확인 통과` } : null;
    case "security_block": return { who: who || "검토", role: role === "system" ? "review" : role, tone: "warn",
      text: msg || "수상한 패키지 차단" };
    case "board_post": return { who, role, tone: "ok", text: `게시판에 API ${(event.items ?? []).length}개 공유 — ${base(event.file)}` };
    case "board_read": return event.file ? { who, role, text: `게시판 확인 후 시작 — ${base(event.file)}` } : null;
    case "gate_check": {
      const n = (event.fixed ?? []).length;
      return { who: "검토", role: "review", tone: event.returned ? "warn" : "ok",
        text: event.returned ? `관문 — ${base(event.file)} 어긋남을 작성자에게 한 번 돌려보냄`
          : n ? `관문 통과 — ${base(event.file)} 자동 수정 ${n}건` : `관문 통과 — ${base(event.file)}` };
    }
    case "verify_failed": return { who, role, tone: "warn", text: msg ? `확인 필요 — ${msg.slice(0, 80)}` : "확인 필요" };
    case "file_done": return event.file ? { who, role, tone: "ok", text: (msg && msg.includes("고정 파일")) ? msg : `${event.file} 완료${event.lines ? ` (${event.lines}줄)` : ""}` } : null;
    case "retry": return { who, role, tone: "warn", text: msg || `일시적 오류 — ${event.seconds ?? "잠시"}초 뒤 다시 시도` };
    case "part_retry": case "file_retry": return { who, role, tone: "warn", text: msg || `${base(event.file)} — 방법을 바꿔 다시 씁니다` };
    case "file_failed": return { who, role, tone: "warn", text: msg || `${base(event.file)} — 만들지 못함` };
    case "file_fallback": return { who, role, tone: "warn", text: msg || `${base(event.file)} — 설계로 기본 문서를 자동 작성` };
    case "paused": return { who: "", role: "system", tone: "warn", text: msg || "멈춤" };
    case "waiting": return { who, role, tone: "warn", text: msg || `분당 호출 한도 — ${event.seconds ?? "잠시"}초 대기` };
    case "generated": case "consistency": return { who: "검토", role: "review", text: msg || "전체 점검 — 컨테이너 빌드로 확인" };
    case "plan_added": return { who: "검토", role: "review", tone: "warn", text: msg || `빠진 파일 ${(event.files ?? []).length}개를 목록에 더함` };
    case "resumed": return { who: "", role: "system", text: msg || "멈춘 지점부터 이어서 만듭니다" };
    case "done": return { who: "", role: "system", tone: "ok", text: msg || "완료" };
    case "acceptance": return event.phase === "result"
      ? { who: "테스트", role: "test", tone: event.failed ? "warn" : "ok", text: msg || `${event.passed ?? 0}/${event.total ?? 0} 통과` }
      : msg ? { who: "테스트", role: "test", text: msg } : null;
    default: return null;
  }
}

//: 사용량은 숫자만 쌓고 화면 기록·다시 보기에는 남기지 않는다(호출마다 와서 기록을 덮는다).
const QUIET_STEPS = new Set(["usage"]);

function addUsage(view: TeamView, event: TeamEvent): Record<string, AgentUsage> {
  const id = event.agent || "system";
  const cur = view.usage?.[id] ?? { calls: 0, inputTokens: 0, outputTokens: 0, costUsd: 0, unknownCost: 0, models: [] };
  const model = event.model || "";
  return {
    ...(view.usage ?? {}),
    [id]: {
      calls: cur.calls + 1,
      inputTokens: cur.inputTokens + Math.max(0, event.input_tokens ?? 0),
      outputTokens: cur.outputTokens + Math.max(0, event.output_tokens ?? 0),
      costUsd: cur.costUsd + (typeof event.cost_usd === "number" && event.cost_usd >= 0 ? event.cost_usd : 0),
      unknownCost: cur.unknownCost + (typeof event.cost_usd === "number" ? 0 : 1),
      models: model && !cur.models.includes(model) ? [...cur.models, model] : cur.models,
    },
  };
}

export function reduceTeam(view: TeamView, event: TeamEvent): TeamView {
  if (typeof event.elapsed === "number" && view.clockBase) event = { ...event, elapsed: event.elapsed + view.clockBase };
  //: 사용량도 다시 보기에 남긴다(그 시점까지의 호출·비용) — 숫자만 담긴 작은 이벤트.
  if (event.step === "usage") return { ...view, usage: addUsage(view, event), events: [...(view.events ?? []), event].slice(-MAX_EVENTS) };
  const next: TeamView = { ...view, agents: { ...view.agents }, files: view.files, log: view.log, records: view.records ?? [] };
  if (!QUIET_STEPS.has(event.step)) {
    const { preview: _p, ...light } = event;
    next.events = [...(view.events ?? []), light].slice(-MAX_EVENTS);
  }
  if (event.job_id) next.jobId = event.job_id;
  if (ENGINE_STEPS.has(event.step)) next.engaged = true;
  //: acceptance 의 total 은 완성 기준 수다 — 파일 수에 섞지 않는다(2.0.45).
  if (event.step !== "acceptance" && typeof event.total === "number" && event.total > 0) next.total = event.total;
  if (typeof event.done_count === "number") next.done = Math.max(next.done, event.done_count);
  if (typeof event.elapsed === "number") next.elapsed = Math.max(next.elapsed, event.elapsed);
  if (event.message) next.log = [...view.log, event.message].slice(-5);
  //: by: 그 파일을 맡은 개발 슬롯(agent-N) — 화면이 완성 카드를 "누구의 작업대에서" 날려 보낼지 안다.
  const by = event.agent && /^agent-\d+$/.test(event.agent) ? event.agent : undefined;
  const setFile = (file: string | undefined, state: FileState) => {
    if (!file) return;
    const idx = next.files.findIndex(f => f.file === file);
    if (idx < 0) { next.files = [...next.files, { file, layer: event.layer ?? 1, state, ...(by ? { by } : {}) }]; return; }
    if (next.files[idx].state === state && (!by || next.files[idx].by === by)) return;
    next.files = next.files.map((f, i) => i === idx ? { ...f, state, ...(by ? { by } : {}) } : f);
  };
  const agent = event.agent ? { ...(next.agents[event.agent] ?? { id: event.agent, state: "idle" as AgentState, done: 0 }) } : null;
  switch (event.step) {
    case "planning": next.phase = "planning"; break;
    case "planned":
      next.phase = "building";
      if (event.summary) next.summary = event.summary;
      if (event.files) next.files = event.files.map(f => ({ file: f.file, layer: typeof f.layer === "number" ? f.layer : 1, state: "waiting" as FileState }));
      next.total = Math.max(next.total, next.files.length);
      break;
    case "plan_added": {
      //: 검토가 찾은 빠진 파일 — 설계 뒤(전체 점검 중)에 더해지면 대기 칸에 붙이고 다시 만드는 단계로 돌아간다.
      const known = new Set(next.files.map(f => f.file));
      const extra = (event.files ?? []).filter(f => !known.has(f.file))
        .map(f => ({ file: f.file, layer: typeof f.layer === "number" ? f.layer : 1, state: "waiting" as FileState }));
      if (extra.length) {
        next.files = [...next.files, ...extra];
        next.total = Math.max(next.total, next.files.length);
        if (next.phase === "checking") next.phase = "building";
      }
      break;
    }
    case "resumed": next.phase = "building"; break;
    //: layer -1 — 의존 순서대로 모든 단계를 동시에 만든다(공통 기반도 여러 명). 모든 칸을 연다.
    case "wave": next.phase = "building"; next.layer = event.layer === -1 ? 2 : typeof event.layer === "number" ? event.layer : next.layer; break;
    case "file_start": setFile(event.file, "writing"); break;
    case "file_split": case "file_part": setFile(event.file, "parts"); break;
    case "fixing": {
      const cur = event.file ? next.files.find(f => f.file === event.file) : undefined;
      if (cur && (cur.state === "done" || cur.state === "issue")) {
        //: 완성된 파일을 전체 점검이 다듬는다 — 완료 수·단계 ✓ 는 그대로 두고 "다듬는 중" 표시만 붙인다.
        if (!cur.polish) next.files = next.files.map(f => f === cur ? { ...f, polish: true } : f);
      } else {
        setFile(event.file, "fixing");
      }
      next.fixes += 1;
      if (/비밀/.test(event.message ?? "")) next.secretFixes += 1;
      break;
    }
    case "verify_failed": setFile(event.file, "issue"); next.issues += 1; break;
    case "security_block": next.securityBlocks = (next.securityBlocks ?? 0) + 1; break;
    case "board_post": {
      const items = (event.items ?? []).filter(i => i && i.method && i.path);
      if (items.length) {
        const keyOf = (i: BoardItem) => `${i.method} ${i.path}`;
        const fresh = new Set(items.map(keyOf));
        next.board = [...(next.board ?? []).filter(b => !fresh.has(keyOf(b))),
          ...items.map(i => ({ ...i, file: event.file ?? "", ...(by ? { by } : {}) }))];
        next.lastPost = by ? { agent: by, count: items.length, seq: (view.lastPost?.seq ?? 0) + 1 } : next.lastPost ?? null;
      }
      break;
    }
    case "board_read": if (by && event.file) next.boardReaders = { ...(next.boardReaders ?? {}), [by]: event.file }; break;
    case "gate_check": {
      const g = next.gates ?? { passed: 0, fixed: 0, returned: 0 };
      const n = (event.fixed ?? []).length;
      next.gates = { passed: g.passed + (event.returned ? 0 : 1), fixed: g.fixed + n, returned: g.returned + (event.returned ? 1 : 0) };
      next.gate = { file: event.file ?? "", by: event.by, ok: !!event.ok, fixed: n, returned: !!event.returned, seq: (view.gate?.seq ?? 0) + 1 };
      if (event.returned && event.by && next.agents[event.by]) {
        //: 카드가 작성자 작업대로 돌아간다 — 그 리코더가 그 문제만 한 번 고친다(엔진 _gate).
        next.agents[event.by] = { ...next.agents[event.by], state: "fixing", file: event.file };
      }
      if (event.returned) setFile(event.file, "fixing");
      break;
    }
    case "file_retry": setFile(event.file, "writing"); next.retries += 1; break;
    case "file_failed": setFile(event.file, "failed"); break;
    case "file_done": setFile(event.file, view.files.find(f => f.file === event.file)?.state === "issue" ? "issue" : "done"); break;
    case "retry": next.retries += 1; break;
    case "generated": case "consistency": next.phase = "checking"; break;
    case "acceptance": {
      const t = view.test ?? { phase: "writing", checks: 0, unverifiable: 0, passed: 0, failed: 0, total: 0, round: 0, criteria: [] };
      const phase = (["writing", "written", "running", "result"] as const).find(p => p === event.phase) ?? t.phase;
      next.test = { ...t, phase,
        ...(typeof event.checks === "number" ? { checks: event.checks } : {}),
        ...(typeof event.unverifiable === "number" ? { unverifiable: event.unverifiable } : {}),
        ...(typeof event.round === "number" ? { round: event.round } : {}),
        ...(phase === "result" ? { passed: event.passed ?? 0, failed: event.failed ?? 0, total: event.total ?? 0,
          criteria: Array.isArray(event.criteria) ? event.criteria.filter(c => c && typeof c.id === "string") : t.criteria } : {}) };
      if (next.phase !== "done") next.phase = "checking";
      break;
    }
    case "done":
      next.phase = "done"; next.done = Math.max(next.done, next.total);
      if (next.files.some(f => f.polish)) next.files = next.files.map(f => f.polish ? { ...f, polish: false } : f);
      break;
  }
  //: 완료 수 — 엔진이 done_count 를 주지 않은 이벤트(이어 만들기 등)에도 완성된 파일 수보다 작게 보이지 않게.
  if (event.step === "file_done") next.done = Math.max(next.done, next.files.filter(f => f.state === "done" || f.state === "issue").length);
  if (event.step === "file_done" && event.file && typeof event.preview === "string" && event.preview) {
    const prev = { ...(view.previews ?? {}) };
    delete prev[event.file];
    prev[event.file] = event.preview;
    const keys = Object.keys(prev);
    for (const k of keys.slice(0, Math.max(0, keys.length - 40))) delete prev[k];
    next.previews = prev;
  }
  const record = recordOf(event, next.files);
  if (record) next.records = [...next.records, record].slice(-30);
  if (agent) {
    if (typeof event.elapsed === "number") {
      if (agent.firstAt === undefined) agent.firstAt = event.elapsed;
      agent.lastAt = event.elapsed;
    }
    if (event.step === "file_done" && event.file) agent.files = [...(agent.files ?? []).filter(f => f !== event.file), event.file];
    const state = STATE_OF_STEP[event.step];
    if (event.step === "file_done") {
      agent.state = "idle"; agent.done += 1; agent.file = undefined; agent.part = undefined;
    } else if (event.step === "file_failed") {
      agent.state = "idle"; agent.file = undefined; agent.part = undefined;
    } else if (state) {
      agent.state = state;
      if (event.file) agent.file = event.file;
      if (event.part) agent.part = event.part;
      if (event.lines) agent.lines = event.lines;
    }
    next.agents[agent.id] = agent;
  }
  return next;
}

/** 전체 점검이 다듬고 있는 완성 파일 수(단계별 또는 전체). */
export function polishingCount(view: TeamView, layer?: number): number {
  return view.files.filter(f => f.polish && (layer === undefined || f.layer === layer)).length;
}

export function formatElapsed(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** 다시 보기 — 기록해 둔 이벤트를 t 초까지 다시 접은 화면 상태(사용량도 그 시점까지, 미리보기는 지금 값). */
export function viewAt(view: TeamView, t: number): TeamView {
  const events = view.events ?? [];
  let v = emptyTeamView();
  for (const e of events) {
    if (typeof e.elapsed === "number" && e.elapsed > t) break;
    v = reduceTeam(v, e);
  }
  return { ...v, previews: view.previews, events, elapsed: Math.min(t, view.elapsed) };
}

/** 리코더가 일한 시간(처음 움직임 → 마지막 움직임, 초). 움직인 적 없으면 0. */
export function agentSeconds(a: TeamAgentView | undefined): number {
  return a && a.firstAt !== undefined && a.lastAt !== undefined ? Math.max(0, a.lastAt - a.firstAt) : 0;
}

/** 팀 전체 사용량 합계. */
export function totalUsage(view: TeamView): AgentUsage {
  const sum: AgentUsage = { calls: 0, inputTokens: 0, outputTokens: 0, costUsd: 0, unknownCost: 0, models: [] };
  for (const u of Object.values(view.usage ?? {})) {
    sum.calls += u.calls; sum.inputTokens += u.inputTokens; sum.outputTokens += u.outputTokens;
    sum.costUsd += u.costUsd; sum.unknownCost += u.unknownCost;
    for (const m of u.models) if (!sum.models.includes(m)) sum.models.push(m);
  }
  return sum;
}
