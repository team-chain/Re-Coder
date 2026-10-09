/**
 * 설계 결정 창의 흐름 — 고른 선택에 따라 다음 결정을 이어서 묻는다(순수 함수, 테스트 가능).
 *
 *  · 코어가 `followups` 로 "이 결정에서 이 선택을 고르면 이어서 물을 결정" 을 알려 준다.
 *    값이 결정 목록이면 바로 끼워 넣고, "ai" 면 AI 에게 이어서 받을 결정을 요청한다.
 *  · 앞으로 돌아가 선택을 바꾸면 예전 선택으로 끼워 넣었던 결정을 빼고 새 선택 기준으로 다시 넣는다.
 *  · 고를 설계 갈림길이 없는 요청(예약 id `__` 로 시작하는 확인 카드 하나)은 선택지가 아니라 확인 창으로 보여 준다.
 */
export interface FlowOption { key: string; label: string; summary: string; pros: string[]; cons: string[]; recommended: boolean; }
export interface FlowDecision { id: string; question: string; options: FlowOption[]; impact: string; }
export type Followups = Record<string, Record<string, FlowDecision[] | "ai">>;

export interface FlowState {
  decisions: FlowDecision[];
  selections: Record<string, string>;
  step: number;
  followups?: Followups;
  /** 결정 id → 그 결정에서 어떤 선택으로 무엇을 끼워 넣었는지. */
  expanded?: Record<string, { key: string; ids: string[] }>;
}

/** 확인 카드 하나뿐인가(설계 갈림길 없음). */
export function isConfirmOnly(decisions: FlowDecision[]): boolean {
  return decisions.length === 1 && decisions[0].id.startsWith("__");
}

function defaultChoice(d: FlowDecision): string {
  return d.options.find(o => o.recommended)?.key ?? d.options[0]?.key ?? "";
}

/** 앞에서 선택을 바꿨으면, 예전 선택으로 끼워 넣었던 결정(과 그 선택)을 뺀다. */
export function collapseChanged<T extends FlowState>(state: T): T {
  const expanded = { ...(state.expanded ?? {}) };
  let decisions = state.decisions;
  const selections = { ...state.selections };
  let changed = false;
  for (const [parentId, info] of Object.entries(expanded)) {
    if (state.selections[parentId] === info.key) continue;
    const drop = new Set(info.ids);
    decisions = decisions.filter(d => !drop.has(d.id));
    info.ids.forEach(id => { delete selections[id]; delete expanded[id]; });
    delete expanded[parentId];
    changed = true;
  }
  if (!changed) return state;
  return { ...state, decisions, selections, expanded, step: Math.min(state.step, decisions.length - 1) };
}

/** 지금 결정에서 고른 선택이 이어서 물을 결정을 갖고 있는데 아직 끼워 넣지 않았으면 그것을 돌려준다. */
export function pendingFollowup(state: FlowState): FlowDecision[] | "ai" | null {
  const d = state.decisions[state.step];
  if (!d) return null;
  const key = state.selections[d.id];
  const f = key ? state.followups?.[d.id]?.[key] : undefined;
  if (!f) return null;
  if (state.expanded?.[d.id]?.key === key) return null;
  return f;
}

/** 이어서 물을 결정을 부모 결정 바로 뒤에 끼워 넣고 다음 결정으로 넘어간다(목록이 비면 그대로). */
export function insertFollowups<T extends FlowState>(state: T, parentId: string, list: FlowDecision[]): T {
  const at = state.decisions.findIndex(d => d.id === parentId);
  if (at < 0) return state;
  const taken = new Set(state.decisions.map(d => d.id));
  const added: FlowDecision[] = [];
  for (const d of list) {
    if (!d || d.id.startsWith("__") || !Array.isArray(d.options) || d.options.length < 2) continue;
    let id = d.id;
    for (let n = 2; taken.has(id); n++) id = `${d.id}-${n}`;
    taken.add(id);
    added.push({ ...d, id });
  }
  const selections = { ...state.selections };
  added.forEach(d => { selections[d.id] = defaultChoice(d); });
  const decisions = [...state.decisions.slice(0, at + 1), ...added, ...state.decisions.slice(at + 1)];
  const key = state.selections[parentId];
  return {
    ...state, decisions, selections,
    expanded: { ...(state.expanded ?? {}), [parentId]: { key, ids: added.map(d => d.id) } },
    step: added.length ? at + 1 : state.step,
  };
}
