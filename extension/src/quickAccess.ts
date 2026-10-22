/**
 * 빠른 이동 (2.0.44) — Alt+Q 로 휠을 띄우고 방향키 하나로 ReCoder 기능 화면을 연다.
 *
 *  - 기본 4칸: ↑ 코드 생성 · → 아키텍처 · ↓ 배포 캔버스 · ← 에러 분석
 *  - 사용자 추가 4칸: Shift+방향키. 설정(recoder.quickAccess)이나 휠의 [편집]에서 채운다.
 *  - VS Code 기본 단축키와 겹치지 않게 Alt+Q 하나만 쓴다(Alt+방향키는 줄 이동·뒤로 가기, Ctrl+Shift+R 은 리팩터링).
 *
 * vscode 를 불러오지 않는 순수 모듈이다 — 설정 해석·검증을 단위 시험으로 고정한다.
 */

export type QuickSlot =
    | 'up' | 'right' | 'down' | 'left'
    | 'shift+up' | 'shift+right' | 'shift+down' | 'shift+left';

export const QUICK_SLOTS: readonly QuickSlot[] = [
    'up', 'right', 'down', 'left', 'shift+up', 'shift+right', 'shift+down', 'shift+left',
];

/** 명령 ID 에 쓰는 이름(점·더하기 없이). package.json 의 recoder.quick.<이름> 과 맞춘다. */
export const SLOT_COMMAND: Record<QuickSlot, string> = {
    'up': 'up', 'right': 'right', 'down': 'down', 'left': 'left',
    'shift+up': 'shiftUp', 'shift+right': 'shiftRight', 'shift+down': 'shiftDown', 'shift+left': 'shiftLeft',
};

const ARROW: Record<string, string> = { up: '↑', right: '→', down: '↓', left: '←' };
/** 휠 안에서 누르는 키(예: "↑", "Shift+→"). */
export function slotKeyLabel(slot: QuickSlot): string {
    const shift = slot.startsWith('shift+');
    const dir = shift ? slot.slice(6) : slot;
    return `${shift ? 'Shift+' : ''}${ARROW[dir]}`;
}

export interface QuickTarget {
    id: string;
    label: string;
    /** 웹뷰 화면 이름(App 의 view 값). */
    view: string;
    /** 아키텍처 화면 안의 모드(흐름 dry run). */
    mapMode?: 'flow' | 'project';
}

/** 열 수 있는 기능. 순서가 설정 화면·휠의 고르기 목록 순서다. */
export const QUICK_TARGETS: readonly QuickTarget[] = [
    { id: 'code', label: '코드 생성 · 수정', view: 'code' },
    { id: 'build', label: '에러 분석', view: 'build' },
    { id: 'map', label: '아키텍처', view: 'map', mapMode: 'project' },
    { id: 'flow', label: '흐름 dry run', view: 'map', mapMode: 'flow' },
    { id: 'adr', label: '설계 기록 (ADR)', view: 'adr' },
    { id: 'models', label: 'AI 공급자 · 비용', view: 'models' },
    { id: 'ship', label: '로컬 Docker 배포', view: 'ship' },
    { id: 'deploy', label: '배포 캔버스', view: 'deploy' },
    { id: 'replay', label: '롤백 · 배포 이력', view: 'replay' },
    { id: 'operate', label: '운영 대응', view: 'operate' },
    { id: 'scan', label: '취약점 스캔', view: 'scan' },
    { id: 'secrets', label: '시크릿 검사', view: 'secrets' },
    { id: 'policy', label: '정책 게이트', view: 'policy' },
    { id: 'home', label: 'ReCoder 홈', view: 'home' },
];
export const TARGET_BY_ID: ReadonlyMap<string, QuickTarget> = new Map(QUICK_TARGETS.map((t) => [t.id, t]));

export const DEFAULT_QUICK_ACCESS: Readonly<Record<QuickSlot, string>> = {
    'up': 'code', 'right': 'map', 'down': 'deploy', 'left': 'build',
    'shift+up': '', 'shift+right': '', 'shift+down': '', 'shift+left': '',
};

export interface QuickAccessState {
    slots: Record<QuickSlot, string>;
    /** 설정에 있었지만 쓸 수 없는 값(모르는 기능·모르는 칸) — 설정 화면에 알린다. */
    problems: string[];
}

/**
 * 설정 값 → 8칸. VS Code 는 객체 설정을 기본값과 합치지 않고 통째로 바꾸므로 여기서 합친다:
 * 설정에 없는 칸은 기본값, 빈 문자열("")은 그 칸을 비운 것으로 본다.
 */
export function resolveQuickAccess(raw: unknown): QuickAccessState {
    const slots: Record<QuickSlot, string> = { ...DEFAULT_QUICK_ACCESS };
    const problems: string[] = [];
    if (raw !== undefined && raw !== null && (typeof raw !== 'object' || Array.isArray(raw))) {
        problems.push('recoder.quickAccess 는 { "up": "code" } 같은 객체여야 합니다 — 기본값을 씁니다.');
        return { slots, problems };
    }
    for (const [key, value] of Object.entries((raw ?? {}) as Record<string, unknown>)) {
        const slot = key.trim().toLowerCase().replace(/\s+/g, '') as QuickSlot;
        if (!QUICK_SLOTS.includes(slot)) {
            problems.push(`"${key}" 는 없는 칸입니다(쓸 수 있는 칸: ${QUICK_SLOTS.join(', ')}).`);
            continue;
        }
        if (value === '' || value === null) { slots[slot] = ''; continue; }
        const id = typeof value === 'string' ? value.trim() : '';
        if (!TARGET_BY_ID.has(id)) {
            problems.push(`${slotKeyLabel(slot)}: "${String(value)}" 는 모르는 기능입니다 — 기본값을 씁니다.`);
            continue;
        }
        slots[slot] = id;
    }
    return { slots, problems };
}

/**
 * 8칸 → 설정에 저장할 값. 기본값과 같은 칸은 적지 않는다(나중에 기본값이 바뀌면 따라간다).
 * 칸을 비우면 "" 로 남긴다 — 적지 않으면 기본값으로 되살아나기 때문이다.
 */
export function toSetting(slots: Record<QuickSlot, string>): Record<string, string> | undefined {
    const out: Record<string, string> = {};
    for (const slot of QUICK_SLOTS) {
        const v = slots[slot] ?? '';
        if (v !== DEFAULT_QUICK_ACCESS[slot]) out[slot] = v;
    }
    return Object.keys(out).length ? out : undefined;
}

/** 한 칸만 바꾼 결과. 모르는 칸·기능이면 그대로 돌려준다. */
export function withSlot(slots: Record<QuickSlot, string>, slot: string, target: string): Record<QuickSlot, string> {
    if (!QUICK_SLOTS.includes(slot as QuickSlot)) return slots;
    if (target !== '' && !TARGET_BY_ID.has(target)) return slots;
    return { ...slots, [slot]: target };
}

/** 휠을 여는 키(표시용). 실제 키는 package.json 의 keybindings 에서 정하고, 사용자가 바꿀 수 있다. */
export function wheelKeyLabel(mac = false): string { return mac ? '⌥Q' : 'Alt+Q'; }
