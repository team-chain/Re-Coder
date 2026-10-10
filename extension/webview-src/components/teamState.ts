/**
 * 팀 모드 화면 상태 — 코어의 대규모 생성 진행 이벤트(SSE)를 화면 상태로 접는 순수 함수.
 * 화면에 보이는 모든 숫자(완료 수·에이전트별 작업·고친 문제)는 실제 이벤트에서만 나온다.
 */
import { AnimalKind, pickAnimal } from "./teamAnimals";

export type TeamRole = "planner" | "dev" | "review";
export interface TeamMember { id: string; role: TeamRole; animal: AnimalKind; }

export interface TeamEvent {
  step: string; message?: string; job_id?: string; agent?: string; file?: string; part?: number; lines?: number;
  layer?: number; agents?: number; seconds?: number; done_count?: number; total?: number; elapsed?: number;
  summary?: string; files?: Array<{ file: string; layer?: number; purpose?: string }>;
  kind?: string; attempt?: number;
}

/** failed: 정해진 횟수를 넘겨 실패 — 완료로 세지 않는다(사용자가 다시 쓰기·빼고 받기를 고른다). */
export type FileState = "waiting" | "writing" | "parts" | "fixing" | "done" | "issue" | "failed";
export type AgentState = "idle" | "writing" | "parts" | "fixing" | "waiting" | "done";

export interface TeamAgentView { id: string; state: AgentState; file?: string; part?: number; lines?: number; done: number; }

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
  issues: number;
  retries: number;
  elapsed: number;
  log: string[];
  /** 진행 기록 — 실제 이벤트를 "누가 무엇을 했는지" 한 줄로(최근 30개). 에이전트끼리의 대화가 아니다. */
  records: TeamRecord[];
}

export interface TeamRecord { who: string; role: TeamRole | "system"; text: string; tone?: "ok" | "warn" }

export const MAX_DEV_AGENTS = 6;
export const DEFAULT_DEV_AGENTS = 3;

export function emptyTeamView(): TeamView {
  return { jobId: "", engaged: false, phase: "planning", summary: "", files: [], total: 0, done: 0, layer: null, agents: {},
    fixes: 0, secretFixes: 0, issues: 0, retries: 0, elapsed: 0, log: [], records: [] };
}

/** 에이전트들이 실제로 일하고 있는지 — 검증된 기반(쇼핑몰 등)처럼 AI 가 생성하지 않는 경로에서는 보드를 띄우지 않는다. */
export function teamWorking(view: TeamView | null | undefined): boolean {
  return !!view && (!!view.engaged || view.total > 0 || Object.keys(view.agents).length > 0);
}

/** 팀 구성: 설계 1 + 개발 N + 검토(보안·문법) 1. 동물은 겹치지 않게 무작위. */
export function buildRoster(devCount: number, rand: () => number = Math.random, keep: TeamMember[] = []): TeamMember[] {
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
  return roster;
}

const STATE_OF_STEP: Record<string, AgentState | undefined> = {
  file_start: "writing", file_split: "parts", file_part: "parts", fixing: "fixing", retry: "waiting", waiting: "waiting",
  file_retry: "writing", part_retry: "parts",
};

const ENGINE_STEPS = new Set(["planning", "planned", "wave", "file_start", "file_split", "file_part", "file_done",
  "fixing", "verified", "verify_failed", "split", "resumed", "generated"]);

/** 이벤트의 agent 값 → 화면 이름(개발 N · 검토 · 설계). 슬롯 이름은 코어의 agent-1, agent-2 … */
export function agentName(id: string | undefined): { who: string; role: TeamRole | "system" } {
  if (!id) return { who: "", role: "system" };
  if (id === "review") return { who: "검토", role: "review" };
  if (id === "planner") return { who: "설계", role: "planner" };
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
    case "fixing": return { who, role, tone: "warn", text: `자동 검사에서 문제 발견 → 고치는 중${msg.includes("—") ? ` (${msg.split("—").slice(1).join("—").trim().slice(0, 60)})` : ""}` };
    case "verified": return event.file ? { who, role, tone: "ok", text: `${base(event.file)} 고친 뒤 확인 통과` } : null;
    case "verify_failed": return { who, role, tone: "warn", text: msg ? `확인 필요 — ${msg.slice(0, 80)}` : "확인 필요" };
    case "file_done": return event.file ? { who, role, tone: "ok", text: `${event.file} 완료${event.lines ? ` (${event.lines}줄)` : ""}` } : null;
    case "retry": return { who, role, tone: "warn", text: msg || `일시적 오류 — ${event.seconds ?? "잠시"}초 뒤 다시 시도` };
    case "part_retry": case "file_retry": return { who, role, tone: "warn", text: msg || `${base(event.file)} — 방법을 바꿔 다시 씁니다` };
    case "file_failed": return { who, role, tone: "warn", text: msg || `${base(event.file)} — 만들지 못함` };
    case "file_fallback": return { who, role, tone: "warn", text: msg || `${base(event.file)} — 설계로 기본 문서를 자동 작성` };
    case "paused": return { who: "", role: "system", tone: "warn", text: msg || "멈춤" };
    case "waiting": return { who, role, tone: "warn", text: msg || `분당 호출 한도 — ${event.seconds ?? "잠시"}초 대기` };
    case "generated": case "consistency": return { who: "검토", role: "review", text: msg || "전체 점검 — 컨테이너 빌드로 확인" };
    case "resumed": return { who: "", role: "system", text: msg || "멈춘 지점부터 이어서 만듭니다" };
    case "done": return { who: "", role: "system", tone: "ok", text: msg || "완료" };
    default: return null;
  }
}

export function reduceTeam(view: TeamView, event: TeamEvent): TeamView {
  const next: TeamView = { ...view, agents: { ...view.agents }, files: view.files, log: view.log, records: view.records ?? [] };
  if (event.job_id) next.jobId = event.job_id;
  if (ENGINE_STEPS.has(event.step)) next.engaged = true;
  if (typeof event.total === "number" && event.total > 0) next.total = event.total;
  if (typeof event.done_count === "number") next.done = Math.max(next.done, event.done_count);
  if (typeof event.elapsed === "number") next.elapsed = event.elapsed;
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
    case "resumed": next.phase = "building"; break;
    case "wave": next.phase = "building"; next.layer = typeof event.layer === "number" ? event.layer : next.layer; break;
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
    case "file_retry": setFile(event.file, "writing"); next.retries += 1; break;
    case "file_failed": setFile(event.file, "failed"); break;
    case "file_done": setFile(event.file, view.files.find(f => f.file === event.file)?.state === "issue" ? "issue" : "done"); break;
    case "retry": next.retries += 1; break;
    case "generated": case "consistency": next.phase = "checking"; break;
    case "done":
      next.phase = "done"; next.done = Math.max(next.done, next.total);
      if (next.files.some(f => f.polish)) next.files = next.files.map(f => f.polish ? { ...f, polish: false } : f);
      break;
  }
  const record = recordOf(event, next.files);
  if (record) next.records = [...next.records, record].slice(-30);
  if (agent) {
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
