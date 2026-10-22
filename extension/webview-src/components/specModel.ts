/**
 * 명세 · 합격 조건 (2.0.43) — 화면 상태와 규칙(순수 함수).
 *
 * 설계 결정 → **명세 확인·수정** → 생성. AI 초안을 사람이 고치고 승인한 것만 생성 요청에 실린다.
 * 코어(code_spec.normalize_spec)가 한 번 더 정규화하므로, 여기서는 화면에서 다루기 쉬운 모양과
 * "확인할 수 없는 조건"을 미리 알려 주는 일만 한다.
 */

export type SpecKind = "feature" | "api" | "screen" | "data" | "security" | "quality";
export const SPEC_KINDS: SpecKind[] = ["feature", "api", "screen", "data", "security", "quality"];
export const KIND_LABEL: Record<SpecKind, string> = {
  feature: "기능", api: "API", screen: "화면", data: "데이터", security: "보안", quality: "품질",
};

export interface EditableCriterion {
  /** 화면 안에서만 쓰는 고유 키(번호 AC-n 은 코어가 다시 매긴다). */
  key: string;
  text: string;
  kind: SpecKind;
  check: string;
  source: "ai" | "user";
  included: boolean;
}

export interface EditableSpec {
  goal: string;
  /** 한 줄에 하나 — 입력 중인 빈 줄을 지우지 않으려고 글 그대로 둔다. */
  inScope: string;
  outOfScope: string;
  criteria: EditableCriterion[];
  seq: number;
}

export interface SpecPayload {
  goal: string;
  in_scope: string[];
  out_of_scope: string[];
  criteria: Array<{ text: string; kind: SpecKind; check: string; source: "ai" | "user" }>;
}

export interface SpecIssue {
  /** 특정 조건의 문제면 그 키. */
  key?: string;
  /** block — 이대로는 생성하지 않는다. warn — 생성은 되지만 고치길 권한다. */
  level: "block" | "warn";
  message: string;
}

export const MAX_CRITERIA = 15;

const str = (v: unknown, limit: number) => (typeof v === "string" ? v : "").replace(/\s+/g, " ").trim().slice(0, limit);
const lines = (v: unknown) => (Array.isArray(v) ? v : []).map((x) => str(x, 160)).filter(Boolean);

export function emptySpec(): EditableSpec {
  return { goal: "", inScope: "", outOfScope: "", criteria: [], seq: 0 };
}

/** 코어 초안(/api/code/spec 의 spec) → 화면 상태. 모르는 값은 버리고, 모든 조건은 처음엔 포함. */
export function fromDraft(raw: unknown): EditableSpec {
  const d = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const spec = emptySpec();
  spec.goal = str(d.goal, 200);
  spec.inScope = lines(d.in_scope).join("\n");
  spec.outOfScope = lines(d.out_of_scope).join("\n");
  for (const item of Array.isArray(d.criteria) ? d.criteria : []) {
    if (spec.criteria.length >= MAX_CRITERIA) break;
    const c = (item && typeof item === "object" ? item : { text: item }) as Record<string, unknown>;
    const text = str(c.text, 240);
    if (!text) continue;
    spec.seq += 1;
    spec.criteria.push({
      key: `c${spec.seq}`,
      text,
      kind: SPEC_KINDS.includes(c.kind as SpecKind) ? (c.kind as SpecKind) : "feature",
      check: str(c.check, 200),
      source: c.source === "user" ? "user" : "ai",
      included: true,
    });
  }
  return spec;
}

/** 조건 하나를 고친다. AI 가 쓴 문장을 사람이 고치면 "직접 쓴 조건"으로 바뀐다. */
export function updateCriterion(spec: EditableSpec, key: string, patch: Partial<Omit<EditableCriterion, "key" | "source">>): EditableSpec {
  return {
    ...spec,
    criteria: spec.criteria.map((c) => {
      if (c.key !== key) return c;
      const edited = (patch.text !== undefined && patch.text !== c.text) || (patch.check !== undefined && patch.check !== c.check);
      return { ...c, ...patch, source: edited ? "user" : c.source };
    }),
  };
}

export function addCriterion(spec: EditableSpec): EditableSpec {
  if (spec.criteria.length >= MAX_CRITERIA) return spec;
  const seq = spec.seq + 1;
  return { ...spec, seq, criteria: [...spec.criteria, { key: `c${seq}`, text: "", kind: "feature", check: "", source: "user", included: true }] };
}

export function removeCriterion(spec: EditableSpec, key: string): EditableSpec {
  return { ...spec, criteria: spec.criteria.filter((c) => c.key !== key) };
}

/** 생성 요청에 싣는 명세. 포함한 조건이 하나도 없으면 null(명세 없이 생성). */
export function specForRequest(spec: EditableSpec | null | undefined): SpecPayload | null {
  if (!spec) return null;
  const criteria = spec.criteria
    .filter((c) => c.included && c.text.trim())
    .map((c) => ({ text: c.text.trim(), kind: c.kind, check: c.check.trim(), source: c.source }));
  if (!criteria.length) return null;
  const split = (text: string) => text.split(/\r?\n/).map((s) => s.replace(/^\s*[-*•]\s+/, "").trim()).filter(Boolean);
  return { goal: spec.goal.trim(), in_scope: split(spec.inScope), out_of_scope: split(spec.outOfScope), criteria };
}

//: 확인할 수 없는 말 — "잘 동작한다"는 아무도 합격·불합격을 판정할 수 없다.
const VAGUE = /(잘\s*(동작|작동|되|돌아)|적절(히|한)|원활|깔끔|빠르게|충분(히|한)|정상적으로|문제\s*없이|사용자\s*친화|직관적)/;
//: 실패 흐름(잘못된 입력·권한 없음·없는 데이터)을 다루는 조건인가.
const FAILURE = /(거절|거부|막|실패|오류|에러|잘못|없(는|으면|을)|권한|로그인하지|401|403|404|409|422|400|초과|부족|중복|빈\s)/;

export function vagueWord(text: string): string | null {
  const m = VAGUE.exec(text);
  return m ? m[0] : null;
}

/** 승인 전에 알려 줄 문제. block 이 있으면 [명세대로 생성] 을 잠근다. */
export function specIssues(spec: EditableSpec | null | undefined): SpecIssue[] {
  if (!spec) return [{ level: "block", message: "명세가 아직 없습니다." }];
  const out: SpecIssue[] = [];
  const included = spec.criteria.filter((c) => c.included);
  const written = included.filter((c) => c.text.trim());
  if (!written.length) out.push({ level: "block", message: "합격 조건을 하나 이상 남겨 주세요. 무엇이 되면 완성인지 정해야 생성할 수 있습니다." });
  for (const c of included) {
    if (!c.text.trim()) { out.push({ key: c.key, level: "warn", message: "빈 조건은 빼고 보냅니다." }); continue; }
    const vague = vagueWord(c.text);
    if (vague) out.push({ key: c.key, level: "warn", message: `확인할 수 없는 말("${vague}") — 숫자·상태 코드·화면 문구처럼 확인할 수 있게 써 주세요.` });
    else if (!c.check.trim()) out.push({ key: c.key, level: "warn", message: "확인 방법이 비어 있습니다." });
  }
  if (written.length >= 2 && !written.some((c) => FAILURE.test(`${c.text} ${c.check}`))) {
    out.push({ level: "warn", message: "실패 흐름(잘못된 입력·권한 없음·없는 데이터) 조건이 없습니다. 출시 수준이라면 하나 이상 넣는 것을 권합니다." });
  }
  if (!spec.goal.trim()) out.push({ level: "warn", message: "목표가 비어 있습니다." });
  return out;
}

export function hasBlocking(issues: SpecIssue[]): boolean {
  return issues.some((i) => i.level === "block");
}
