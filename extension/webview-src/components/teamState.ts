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
}

export type FileState = "waiting" | "writing" | "parts" | "fixing" | "done" | "issue";
export type AgentState = "idle" | "writing" | "parts" | "fixing" | "waiting" | "done";

export interface TeamAgentView { id: string; state: AgentState; file?: string; part?: number; lines?: number; done: number; }

export interface TeamView {
  jobId: string;
  /** 대규모 생성 엔진(설계·파일 작업)이 실제로 돌았는지 — 검증된 기반처럼 생성하지 않는 경로는 false. */
  engaged?: boolean;
  phase: "planning" | "building" | "checking" | "done";
  summary: string;
  files: Array<{ file: string; layer: number; state: FileState }>;
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
}

export const MAX_DEV_AGENTS = 6;
export const DEFAULT_DEV_AGENTS = 3;

export function emptyTeamView(): TeamView {
  return { jobId: "", engaged: false, phase: "planning", summary: "", files: [], total: 0, done: 0, layer: null, agents: {},
    fixes: 0, secretFixes: 0, issues: 0, retries: 0, elapsed: 0, log: [] };
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
};

const ENGINE_STEPS = new Set(["planning", "planned", "wave", "file_start", "file_split", "file_part", "file_done",
  "fixing", "verified", "verify_failed", "split", "resumed", "generated"]);

export function reduceTeam(view: TeamView, event: TeamEvent): TeamView {
  const next: TeamView = { ...view, agents: { ...view.agents }, files: view.files, log: view.log };
  if (event.job_id) next.jobId = event.job_id;
  if (ENGINE_STEPS.has(event.step)) next.engaged = true;
  if (typeof event.total === "number" && event.total > 0) next.total = event.total;
  if (typeof event.done_count === "number") next.done = Math.max(next.done, event.done_count);
  if (typeof event.elapsed === "number") next.elapsed = event.elapsed;
  if (event.message) next.log = [...view.log, event.message].slice(-5);
  const setFile = (file: string | undefined, state: FileState) => {
    if (!file) return;
    const idx = next.files.findIndex(f => f.file === file);
    if (idx < 0) { next.files = [...next.files, { file, layer: event.layer ?? 1, state }]; return; }
    if (next.files[idx].state === state) return;
    next.files = next.files.map((f, i) => i === idx ? { ...f, state } : f);
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
    case "fixing":
      setFile(event.file, "fixing");
      next.fixes += 1;
      if (/비밀/.test(event.message ?? "")) next.secretFixes += 1;
      break;
    case "verify_failed": setFile(event.file, "issue"); next.issues += 1; break;
    case "file_done": setFile(event.file, view.files.find(f => f.file === event.file)?.state === "issue" ? "issue" : "done"); break;
    case "retry": next.retries += 1; break;
    case "generated": case "consistency": next.phase = "checking"; break;
    case "done": next.phase = "done"; next.done = Math.max(next.done, next.total); break;
  }
  if (agent) {
    const state = STATE_OF_STEP[event.step];
    if (event.step === "file_done") {
      agent.state = "idle"; agent.done += 1; agent.file = undefined; agent.part = undefined;
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

export function formatElapsed(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
