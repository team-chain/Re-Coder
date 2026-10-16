/**
 * ReCoder — 실행 흐름 그래프(dry run 다이어그램용, 2.0.34). 확장 내부 정적 분석 — 코드를 실행하지 않는다.
 *
 * 구조 지도(analyzer.ts)는 파일 사이 import 와 파일 안 호출만 본다. dry run 은 "요청 하나가 어디를 지나가나"를
 * 보여 줘야 하므로 파일을 넘나드는 함수 호출을 잇는다.
 *   - 진입점: 서버 API 경로(Express·Fastify·Koa 식 app.get / FastAPI·Flask 데코레이터 / Next.js API·route 파일),
 *            화면 동작(JSX onClick·addEventListener·el.onclick), 실행 파일의 최상위 코드
 *   - 노드: 함수·메서드·경로 핸들러. 노드마다 하는 일(DB·외부 API·AWS·비밀값·파일·명령 실행)
 *   - 선: 호출. 호출 위치(줄)·순서·갈림(조건·반복·오류 처리·콜백)·확실성(직접/추정)
 *
 * 선은 import 로 이름이 이어지는 것만 "직접", 객체 메서드처럼 이름만으로 고른 것은 "추정"이다. 화면은 추정 선을 점선으로 그린다.
 * 그래프의 모양(FlowGraph)은 소스가 아닌 분석(나중의 바이너리 분석)도 같은 화면에 그릴 수 있게 source 필드를 둔다.
 */
import * as fs from 'fs';
import * as path from 'path';
import { iterFiles, moduleName, readText, stripJs, stripPy, toPosix } from './analyzer';

export type EffectKind = 'db' | 'http' | 'aws' | 'secret' | 'fs' | 'exec';
export type BranchKind = 'if' | 'else' | 'loop' | 'catch' | 'case' | 'cond' | 'callback';

export interface FlowEffect { kind: EffectKind; detail: string; line: number; }
export interface FlowNode {
    id: string; file: string; name: string; cls?: string | null;
    line: number; endLine: number;
    kind: 'function' | 'handler' | 'route' | 'main';
    effects: FlowEffect[];
    /** 본문 지문 — 적용 전 미리 보기에서 바뀐 함수를 찾는다. */
    hash: string;
}
export interface FlowEdge {
    from: string; to: string; line: number; order: number;
    branch?: BranchKind; certainty: 'direct' | 'inferred';
}
export interface FlowEntry {
    id: string; label: string; kind: 'route' | 'ui' | 'main';
    method?: string; path?: string; file: string; line: number;
}
export interface FlowGraph {
    kind: 'flow'; source: 'source'; root: string; files_scanned: number;
    nodes: FlowNode[]; edges: FlowEdge[]; entries: FlowEntry[];
    truncated: boolean; notes: string[];
}

const JS_EXT = ['.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx', '.mts', '.cts'];
const PY_EXT = ['.py'];
const MAX_NODES = 6000;
const MAX_EDGES = 30000;

const JS_RESERVED = new Set([
    'if', 'for', 'while', 'switch', 'catch', 'function', 'return', 'else', 'do', 'with', 'typeof', 'new', 'delete',
    'void', 'in', 'of', 'instanceof', 'await', 'yield', 'case', 'default', 'const', 'let', 'var', 'class', 'extends',
    'super', 'this', 'true', 'false', 'null', 'undefined', 'throw', 'try', 'finally', 'break', 'continue', 'import',
    'export', 'constructor', 'async', 'static', 'get', 'set',
]);
const PY_RESERVED = new Set(['if', 'elif', 'while', 'for', 'return', 'print', 'with', 'assert', 'not', 'and', 'or', 'in', 'is',
    'lambda', 'yield', 'await', 'raise', 'del', 'super', 'isinstance', 'len', 'str', 'int', 'float', 'dict', 'list', 'set',
    'tuple', 'range', 'enumerate', 'zip', 'map', 'filter', 'sorted', 'min', 'max', 'sum', 'any', 'all', 'open', 'type']);

// ── 하는 일(효과) ────────────────────────────────────────────────────────────

const SECRET_NAME = /(SECRET|TOKEN|PASSWORD|PASSWD|PASS\b|PRIVATE|CREDENTIAL|API_?KEY|ACCESS_?KEY|DATABASE_URL|DB_URL|DSN|JWT|SIGNING|WEBHOOK)/i;

interface EffectRule { kind: EffectKind; re: RegExp; detail: (m: RegExpExecArray) => string; }

const EFFECT_RULES: EffectRule[] = [
    // DB
    { kind: 'db', re: /['"`]\s*(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE|UPSERT|REPLACE\s+INTO)\b/gi, detail: m => `SQL ${m[1].split(/\s+/)[0].toUpperCase()}` },
    { kind: 'db', re: /\bprisma\s*\.\s*(\w+)\s*\.\s*(find\w*|create\w*|update\w*|delete\w*|upsert|count|aggregate|groupBy)\s*\(/g, detail: m => `Prisma ${m[1]}.${m[2]}` },
    { kind: 'db', re: /\.\s*(findOne|findMany|findUnique|findFirst|findById|findAll|insertOne|insertMany|updateOne|updateMany|deleteOne|deleteMany|aggregate|bulkWrite|countDocuments)\s*\(/g, detail: m => `DB ${m[1]}` },
    { kind: 'db', re: /\b(pool|client|db|conn|connection|knex|sql|database|cursor|tx|trx|sequelize)\s*\.\s*(query|execute|executemany|exec|prepare|transaction)\s*\(/g, detail: m => `DB ${m[1]}.${m[2]}` },
    { kind: 'db', re: /\b(db|database|sqlite|stmt)\s*\.\s*(get|run|all)\s*\(/g, detail: m => `DB ${m[1]}.${m[2]}` },
    { kind: 'db', re: /\b(session|db)\s*\.\s*(add|add_all|commit|rollback|merge|scalars?|execute|query|delete|flush|refresh)\s*\(/g, detail: m => `DB ${m[1]}.${m[2]}` },
    { kind: 'db', re: /\.\s*from\s*\(\s*['"`](\w+)['"`]\s*\)\s*\.\s*(select|insert|update|delete|upsert)\b/g, detail: m => `DB ${m[1]} ${m[2]}` },
    { kind: 'db', re: /\b(redis|cache)\s*\.\s*(get|set|del|hget|hset|incr|expire|lpush|rpush)\s*\(/g, detail: m => `캐시 ${m[1]}.${m[2]}` },
    // 외부 API
    { kind: 'http', re: /\bfetch\s*\(\s*(['"`]?)([^'"`),\s]*)/g, detail: m => `HTTP 요청${m[2] && /^https?:/.test(m[2]) ? ` ${host(m[2])}` : ''}` },
    { kind: 'http', re: /\baxios\s*(?:\.\s*(get|post|put|patch|delete|request))?\s*\(/g, detail: m => `HTTP ${m[1] ? m[1].toUpperCase() : '요청'}` },
    { kind: 'http', re: /\b(requests|httpx)\s*\.\s*(get|post|put|patch|delete|request)\s*\(\s*(['"]?)([^'"),\s]*)/g, detail: m => `HTTP ${m[2].toUpperCase()}${m[4] && /^https?:/.test(m[4]) ? ` ${host(m[4])}` : ''}` },
    { kind: 'http', re: /\b(?:urllib\.request\.)?urlopen\s*\(|\bhttps?\s*\.\s*(?:request|get)\s*\(/g, detail: () => 'HTTP 요청' },
    { kind: 'http', re: /\bstripe\s*\.\s*(\w+)\s*\.\s*(create|retrieve|update|list|confirm|capture)\s*\(/g, detail: m => `결제 API ${m[1]}.${m[2]}` },
    // AWS
    { kind: 'aws', re: /\bnew\s+(\w+)Command\s*\(/g, detail: m => `AWS ${m[1]}` },
    { kind: 'aws', re: /\bboto3\s*\.\s*(client|resource)\s*\(\s*['"](\w+)['"]/g, detail: m => `AWS ${m[2]}` },
    { kind: 'aws', re: /\b(s3|dynamodb|sqs|sns|ses|lambda_client|secretsmanager|ssm)\s*\.\s*(\w+)\s*\(/g, detail: m => `AWS ${m[1]}.${m[2]}` },
    // 비밀값
    { kind: 'secret', re: /\bprocess\s*\.\s*env\s*(?:\.\s*([A-Z_][A-Z0-9_]*)|\[\s*['"`]([A-Z_][A-Z0-9_]*)['"`]\s*\])/g, detail: m => m[1] || m[2] },
    { kind: 'secret', re: /\bimport\.meta\.env\.([A-Z_][A-Z0-9_]*)/g, detail: m => m[1] },
    { kind: 'secret', re: /\bos\s*\.\s*(?:environ\s*(?:\.\s*get\s*\(|\[)|getenv\s*\()\s*['"]([A-Z_][A-Z0-9_]*)['"]/g, detail: m => m[1] },
    { kind: 'secret', re: /\b(?:settings|config)\s*\.\s*([A-Z_][A-Z0-9_]*)\b/g, detail: m => m[1] },
    // 파일
    { kind: 'fs', re: /\bfs(?:\.promises)?\s*\.\s*(writeFile|writeFileSync|appendFile|appendFileSync|unlink|unlinkSync|rm|rmSync|rename|renameSync|createWriteStream|mkdir|mkdirSync)\s*\(/g, detail: m => `파일 ${m[1].replace(/Sync$/, '')}` },
    { kind: 'fs', re: /\bopen\s*\([^)\n]*,\s*['"](w|a|wb|ab|w\+|a\+)['"]/g, detail: m => `파일 쓰기(${m[1]})` },
    { kind: 'fs', re: /\b(shutil\s*\.\s*(rmtree|move|copy\w*)|os\s*\.\s*(remove|unlink|rename|makedirs))\s*\(/g, detail: m => `파일 ${m[2] || m[3]}` },
    // 명령 실행
    { kind: 'exec', re: /\b(exec|execSync|spawn|spawnSync|execFile|execFileSync)\s*\(/g, detail: m => `명령 실행 ${m[1]}` },
    { kind: 'exec', re: /\b(subprocess\s*\.\s*(run|Popen|call|check_output|check_call)|os\s*\.\s*(system|popen))\s*\(/g, detail: m => `명령 실행 ${m[2] || m[3]}` },
    { kind: 'exec', re: /(?<![\w.])eval\s*\(|\bnew\s+Function\s*\(/g, detail: () => '코드 실행(eval)' },
];

function host(url: string): string {
    try { return new URL(url).hostname; } catch { return ''; }
}

function effectsIn(text: string, base: number, lineAt: (o: number) => number): FlowEffect[] {
    const out: FlowEffect[] = [];
    const seen = new Set<string>();
    for (const rule of EFFECT_RULES) {
        rule.re.lastIndex = 0;
        let m: RegExpExecArray | null;
        while ((m = rule.re.exec(text))) {
            let detail = rule.detail(m);
            if (rule.kind === 'secret') {
                if (!SECRET_NAME.test(detail)) { continue; }
                detail = `비밀값 ${detail}`;
            }
            const line = lineAt(base + m.index);
            const key = rule.kind + ' ' + detail;
            //: 같은 줄의 같은 종류는 하나만(`db.query('INSERT …')` 를 SQL·DB 두 번 세지 않는다 — 앞의 더 구체적인 규칙이 남는다)
            if (seen.has(key) || out.some(f => f.kind === rule.kind && f.line === line)) { continue; }
            seen.add(key);
            out.push({ kind: rule.kind, detail, line });
            if (out.length >= 24) { return out; }
        }
    }
    return out.sort((a, b) => a.line - b.line);
}

// ── 공통 ─────────────────────────────────────────────────────────────────────

function fnv(text: string): string {
    let h = 0x811c9dc5;
    for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    return h.toString(16).padStart(8, '0');
}

function lineIndex(src: string): (offset: number) => number {
    const starts = [0];
    for (let i = 0; i < src.length; i++) { if (src[i] === '\n') { starts.push(i + 1); } }
    return (offset: number) => {
        let lo = 0, hi = starts.length - 1;
        while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (starts[mid] <= offset) { lo = mid; } else { hi = mid - 1; } }
        return lo + 1;
    };
}

function matchClose(s: string, open: number, a: string, b: string): number {
    let depth = 0;
    for (let i = open; i < s.length; i++) {
        if (s[i] === a) { depth++; } else if (s[i] === b) { depth--; if (depth === 0) { return i; } }
    }
    return s.length;
}

function joinRoute(prefix: string, route: string): string {
    const joined = `/${[prefix, route].map(p => (p || '').replace(/^\/+|\/+$/g, '')).filter(Boolean).join('/')}`;
    return joined === '/' && route.endsWith('/') ? '/' : joined;
}

interface Def {
    key: string; name: string; cls: string | null; start: number; bodyStart: number; bodyEnd: number;
    kind: FlowNode['kind']; exported: boolean;
}
interface Binding { file: string | null; name: string; }       // name: 정의 이름 · '*'(모듈 전체) · 'default'
interface CallSite { name: string; obj: string | null; thisCall: boolean; at: number; }
interface RouteDecl { method: string; path: string; router: string; at: number; handlers: Array<{ ref?: string; inline?: Def }>; deps?: string[]; }
interface UiDecl { event: string; at: number; owner: string | null; ref?: string; inline?: Def; }
interface Mount { prefix: string; binding: string; middlewares: string[]; at: number; }

interface FileInfo {
    id: string; lang: 'js' | 'py'; src: string; code: string; keep: string; lineAt: (o: number) => number;
    defs: Def[]; imports: Map<string, Binding>; defaultExport: string | null; exportNames: Map<string, string>;
    objects: Map<string, { binding: string | null; cls: string }>; routes: RouteDecl[]; ui: UiDecl[];
    mounts: Mount[]; routerPrefix: Map<string, string>; main: Def | null;
}

// ── JS/TS ────────────────────────────────────────────────────────────────────

function skipWs(s: string, i: number): number { while (i < s.length && /\s/.test(s[i])) { i++; } return i; }

/** `(...)` 다음의 함수 본문 범위. 화살표면 식 끝까지. */
function jsBodyAfterParams(code: string, parenOpen: number): [number, number] | null {
    if (code[parenOpen] !== '(') { return null; }
    let i = matchClose(code, parenOpen, '(', ')') + 1;
    // 반환 타입 주석 `): Promise<X> {`
    i = skipWs(code, i);
    if (code[i] === ':') {
        let depth = 0;
        for (; i < code.length; i++) {
            const c = code[i];
            if (c === '<' || c === '(' || c === '[') { depth++; } else if (c === '>' || c === ')' || c === ']') { depth--; }
            else if (depth <= 0 && (c === '{' || (c === '=' && code[i + 1] === '>'))) { break; }
        }
    }
    i = skipWs(code, i);
    if (code.startsWith('=>', i)) { return jsArrowBody(code, skipWs(code, i + 2)); }
    if (code[i] === '{') { return [i + 1, matchClose(code, i, '{', '}')]; }
    return null;
}

function jsArrowBody(code: string, i: number): [number, number] {
    if (code[i] === '{') { return [i + 1, matchClose(code, i, '{', '}')]; }
    let depth = 0, end = i;
    for (; end < code.length; end++) {
        const c = code[end];
        if (c === '(' || c === '{' || c === '[') { depth++; }
        else if (c === ')' || c === '}' || c === ']') { if (depth === 0) { break; } depth--; }
        else if (depth === 0 && (c === ';' || c === ',')) { break; }
        else if (depth === 0 && c === '\n') {
            //: 다음 줄이 이어지는 식(.then · ? : · 연산자)이 아니면 끝. 남은 파일을 잘라 보지 않는다(큰 파일에서 제곱 시간).
            let k = end + 1;
            while (k < code.length && /\s/.test(code[k])) { k++; }
            if (k >= code.length || !/[.?:+\-*/&|=]/.test(code[k])) { break; }
        }
    }
    return [i, end];
}

function jsDefs(code: string): Def[] {
    const defs: Def[] = [];
    const add = (name: string, start: number, body: [number, number] | null, cls: string | null, exported: boolean) => {
        if (!body || JS_RESERVED.has(name)) { return; }
        if (defs.some(d => d.bodyStart === body[0])) { return; }
        defs.push({ key: '', name, cls, start, bodyStart: body[0], bodyEnd: body[1], kind: 'function', exported });
    };
    let m: RegExpExecArray | null;
    // function name(
    let re = /(\bexport\s+(?:default\s+)?)?\b(?:async\s+)?function\s*(?:\*\s*)?([A-Za-z_$][\w$]*)\s*(?:<[^>()\n]{0,80}>\s*)?\(/g;
    while ((m = re.exec(code))) { add(m[2], m.index, jsBodyAfterParams(code, re.lastIndex - 1), null, !!m[1]); }
    // const name = function / arrow
    re = /(\bexport\s+)?\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:async\s+)?(?:function\b[^(]*\(|(\([^()]*(?:\([^()]*\)[^()]*)*\))\s*(?::\s*[^=]+)?=>|([A-Za-z_$][\w$]*)\s*=>)/g;
    while ((m = re.exec(code))) {
        let body: [number, number] | null;
        if (m[3] || m[4]) { body = jsArrowBody(code, skipWs(code, m.index + m[0].length)); }
        else { body = jsBodyAfterParams(code, re.lastIndex - 1); }
        add(m[2], m.index, body, null, !!m[1]);
    }
    // exports.name = function / module.exports.name = / X.prototype.name =
    re = /\b(?:module\.)?exports\s*\.\s*([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function\b[^(]*\(|(\([^()]*\))\s*=>)/g;
    while ((m = re.exec(code))) {
        const body = m[2] ? jsArrowBody(code, skipWs(code, code.indexOf('=>', m.index + m[0].length - 2) + 2)) : jsBodyAfterParams(code, re.lastIndex - 1);
        add(m[1], m.index, body, null, true);
    }
    // 클래스 메서드
    re = /\bclass\s+([A-Za-z_$][\w$]*)[^{;]{0,400}\{/g;
    while ((m = re.exec(code))) {
        const cls = m[1];
        const open = re.lastIndex - 1;
        const close = matchClose(code, open, '{', '}');
        const body = code.slice(open + 1, close);
        const mre = /(?:^|[;}\n])[ \t]*(?:(?:public|private|protected|static|async|override|readonly)[ \t]+)*(?:\*[ \t]*)?(#?[A-Za-z_$][\w$]*)[ \t]*(?:<[^>()\n]{0,80}>[ \t]*)?\(/g;
        let mm: RegExpExecArray | null;
        let depth = 0, last = 0;
        while ((mm = mre.exec(body))) {
            // 메서드 안의 호출이 아니라 클래스 몸통 바로 아래(깊이 0)만
            const namePos = mm.index + mm[0].lastIndexOf(mm[1]);
            for (let k = last; k < namePos; k++) { if (body[k] === '{') { depth++; } else if (body[k] === '}') { depth--; } }
            last = namePos;
            if (depth !== 0) { continue; }
            const name = mm[1].replace(/^#/, '');
            const parenAt = open + 1 + mre.lastIndex - 1;
            const span = jsBodyAfterParams(code, parenAt);
            if (span && code[span[0] - 1] === '{') { add(name, open + 1 + mm.index, span, cls, false); }
        }
        // 화살표 프로퍼티 `handle = async (req) => {`
        const pre = /(?:^|[;}\n])[ \t]*(?:(?:public|private|protected|static|readonly)[ \t]+)*([A-Za-z_$][\w$]*)[ \t]*(?::[^=\n]{0,120})?=[ \t]*(?:async[ \t]+)?(?:\([^()]{0,300}\)|[A-Za-z_$][\w$]*)\s*=>/g;
        depth = 0; last = 0;
        while ((mm = pre.exec(body))) {
            const namePos = mm.index + mm[0].indexOf(mm[1]);
            for (let k = last; k < namePos; k++) { if (body[k] === '{') { depth++; } else if (body[k] === '}') { depth--; } }
            last = namePos;
            if (depth !== 0) { continue; }
            const arrow = open + 1 + mm.index + mm[0].length - 2;
            add(mm[1], open + 1 + mm.index, jsArrowBody(code, skipWs(code, arrow + 2)), cls, false);
        }
    }
    // 객체 리터럴 메서드 `name(args) {` · `name: function(` · `name: async (x) =>`
    re = /(?:^|[{,\n])[ \t]*(?:async[ \t]+)?([A-Za-z_$][\w$]*)[ \t]*\(([^()]{0,300})\)\s*\{/g;
    while ((m = re.exec(code))) {
        const name = m[1];
        if (JS_RESERVED.has(name)) { continue; }
        const paren = code.indexOf('(', m.index + m[0].indexOf(name) + name.length);
        add(name, m.index + m[0].indexOf(name), jsBodyAfterParams(code, paren), null, false);
    }
    re = /(?:^|[{,\n])[ \t]*([A-Za-z_$][\w$]*)[ \t]*:[ \t]*(?:async\s+)?(?:function\b[^(\n]{0,80}\(|(\([^()]{0,300}\)|[A-Za-z_$][\w$]*)\s*=>)/g;
    while ((m = re.exec(code))) {
        if (JS_RESERVED.has(m[1])) { continue; }
        let body: [number, number] | null;
        if (m[2]) { const arrow = code.indexOf('=>', m.index); body = jsArrowBody(code, skipWs(code, arrow + 2)); }
        else { body = jsBodyAfterParams(code, re.lastIndex - 1); }
        add(m[1], m.index, body, null, false);
    }
    return defs.sort((a, b) => a.bodyStart - b.bodyStart);
}

/** 이름 없는 함수 표현식 `(req, res) => {…}` · `function (e) {…}` 의 범위. */
function jsInlineFn(code: string, at: number): [number, number] | null {
    const i = skipWs(code, at);
    const head = code.slice(i, i + 200);
    let m = head.match(/^async\s+/);
    const j = i + (m ? m[0].length : 0);
    if (code.startsWith('function', j)) {
        const paren = code.indexOf('(', j);
        return paren >= 0 ? jsBodyAfterParams(code, paren) : null;
    }
    if (code[j] === '(') {
        const close = matchClose(code, j, '(', ')');
        const after = skipWs(code, close + 1);
        if (code.startsWith('=>', after)) { return jsArrowBody(code, skipWs(code, after + 2)); }
        if (code[after] === ':') { const arrow = code.indexOf('=>', after); if (arrow > 0 && arrow - after < 80) { return jsArrowBody(code, skipWs(code, arrow + 2)); } }
        return null;
    }
    m = code.slice(j, j + 80).match(/^([A-Za-z_$][\w$]*)\s*=>/);
    if (m) { return jsArrowBody(code, skipWs(code, j + m[0].length)); }
    return null;
}

/** 인자 목록을 깊이 0 의 쉼표로 나눈다 — [시작, 끝] 오프셋. */
function splitArgs(code: string, open: number, close: number): Array<[number, number]> {
    const out: Array<[number, number]> = [];
    let depth = 0, start = open + 1;
    for (let i = open + 1; i < close; i++) {
        const c = code[i];
        if (c === '(' || c === '{' || c === '[') { depth++; }
        else if (c === ')' || c === '}' || c === ']') { depth--; }
        else if (c === ',' && depth === 0) { out.push([start, i]); start = i + 1; }
    }
    if (code.slice(start, close).trim()) { out.push([start, close]); }
    return out;
}

function parseJs(id: string, src: string): FileInfo {
    const code = stripJs(src);
    const keep = stripJs(src, true);
    const lineAt = lineIndex(src);
    const defs = jsDefs(code);
    const imports = new Map<string, Binding>();
    const exportNames = new Map<string, string>();
    let defaultExport: string | null = null;
    let m: RegExpExecArray | null;

    // import 바인딩
    //: import 문은 `import` 뒤 가까운 `from '…'` 까지만 본다(정규식 하나로 [\s\S]*? 를 쓰면 긴 공백에서 제곱 시간).
    let re = /\bimport\b/g;
    while ((m = re.exec(keep))) {
        const window = keep.slice(m.index + 6, m.index + 2000);
        const from = window.search(/\bfrom[ \t\n]*['"]/);
        if (from < 0) { continue; }
        const quote = window.slice(from).match(/^from[ \t\n]*(['"])([^'"\n]{1,300})\1/);
        if (!quote) { continue; }
        const clause = window.slice(0, from).trim().replace(/^type\s/, '').trim();
        if (!clause || /[;=]/.test(clause)) { continue; }
        const spec = quote[2];
        const named = clause.match(/\{([\s\S]*)\}/);
        const head = clause.replace(/\{[\s\S]*\}/, '').replace(/,\s*$/, '').trim();
        const ns = head.match(/\*\s+as\s+([A-Za-z_$][\w$]*)/);
        if (ns) { imports.set(ns[1], { file: spec, name: '*' }); }
        else if (head && /^[A-Za-z_$][\w$]*$/.test(head.split(',')[0].trim())) { imports.set(head.split(',')[0].trim(), { file: spec, name: 'default' }); }
        if (named) {
            for (const part of named[1].split(',')) {
                const p = part.trim().replace(/^type\s+/, ''); if (!p) { continue; }
                const [orig, alias] = p.replace(/\s+/g, ' ').split(' as ').map(x => x.trim());
                imports.set(alias || orig, { file: spec, name: orig === 'default' ? 'default' : orig });
            }
        }
    }
    re = /\b(?:const|let|var)\s+(\{[^}]*\}|[A-Za-z_$][\w$]*)\s*=\s*(?:await\s+)?require\s*\(\s*['"]([^'"]+)['"]\s*\)(\s*\.\s*([A-Za-z_$][\w$]*))?/g;
    while ((m = re.exec(keep))) {
        const target = m[1]; const spec = m[2];
        if (target.startsWith('{')) {
            for (const part of target.slice(1, -1).split(',')) {
                const p = part.trim(); if (!p) { continue; }
                const [orig, alias] = p.split(':').map(x => x.trim());
                imports.set(alias || orig, { file: spec, name: orig });
            }
        } else { imports.set(target, { file: spec, name: m[4] || '*' }); }
    }
    // export
    re = /\bexport[ \t]+default[ \t]+(?:async[ \t]+)?(?:function[ \t]*(?:\*[ \t]*)?([A-Za-z_$][\w$]*)|class[ \t]+([A-Za-z_$][\w$]*)|([A-Za-z_$][\w$]*)[ \t]*(?:;[ \t]*)?$)/gm;
    while ((m = re.exec(code))) { defaultExport = m[1] || m[2] || m[3] || defaultExport; }
    re = /\bmodule\.exports\s*=\s*([A-Za-z_$][\w$]*)\s*;?/g;
    while ((m = re.exec(code))) { defaultExport = m[1]; }
    re = /\bmodule\.exports\s*=\s*\{([^}]*)\}/g;
    while ((m = re.exec(code))) {
        for (const part of m[1].split(',')) {
            const [k, v] = part.split(':').map(x => x.trim());
            if (k && /^[A-Za-z_$][\w$]*$/.test(k)) { exportNames.set(k, v && /^[A-Za-z_$][\w$]*$/.test(v) ? v : k); }
        }
    }
    re = /\bexport\s*\{([^}]*)\}/g;
    while ((m = re.exec(code))) {
        for (const part of m[1].split(',')) {
            const [orig, alias] = part.trim().replace(/\s+/g, ' ').split(' as ').map(x => x.trim());
            if (!orig) { continue; }
            if (alias === 'default') { defaultExport = orig; } else { exportNames.set(alias || orig, orig); }
        }
    }
    re = /\bexport\s+(?:async\s+)?(?:function\s*(?:\*\s*)?|const\s+|let\s+|class\s+)([A-Za-z_$][\w$]*)/g;
    while ((m = re.exec(code))) { exportNames.set(m[1], m[1]); }
    for (const d of defs) { if (d.exported && !exportNames.has(d.name)) { exportNames.set(d.name, d.name); } }
    re = /\bexport\s+default\s+(?=(?:async\s+)?(?:function\s*\(|\(|[A-Za-z_$][\w$]*\s*=>))/g;
    if ((m = re.exec(code))) {
        const body = jsInlineFn(code, m.index + m[0].length);
        if (body && !defs.some(d => d.bodyStart === body[0])) {
            defs.push({ key: '', name: 'default', cls: null, start: m.index, bodyStart: body[0], bodyEnd: body[1], kind: 'function', exported: true });
            defaultExport = 'default';
        }
    }

    // new 로 만든 객체 → 클래스(객체 메서드 호출을 잇는다)
    const objects = new Map<string, { binding: string | null; cls: string }>();
    re = /\b(?:(?:const|let|var)\s+|this\s*\.\s*)([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*new\s+([A-Za-z_$][\w$]*)\s*\(/g;
    while ((m = re.exec(code))) { objects.set(m[1], { binding: imports.has(m[2]) ? m[2] : null, cls: m[2] }); }

    // 서버 경로
    const routes: RouteDecl[] = [];
    const inlineDefs: Def[] = [];
    re = /\b([A-Za-z_$][\w$]*)\s*\.\s*(get|post|put|patch|delete|options|head|all)\s*\(\s*(['"`])([^'"`\n]*)\3\s*(,|\))/gi;
    while ((m = re.exec(keep))) {
        if (m[5] === ')' || (m[3] === '`' && m[4].includes('${'))) { continue; }
        const open = keep.indexOf('(', m.index + m[1].length);
        const close = matchClose(code, open, '(', ')');
        const args = splitArgs(code, open, close).slice(1);
        const handlers: RouteDecl['handlers'] = [];
        for (const [a, b] of args) {
            const text = code.slice(a, b).trim();
            const span = jsInlineFn(code, a);
            if (span && span[1] <= close) {
                const def: Def = { key: '', name: `${m[2].toUpperCase()} ${m[4]} 처리`, cls: null, start: a, bodyStart: span[0], bodyEnd: span[1], kind: 'handler', exported: false };
                inlineDefs.push(def); handlers.push({ inline: def });
            } else if (/^[A-Za-z_$][\w$.]*$/.test(text)) { handlers.push({ ref: text }); }
            else {
                const call = text.match(/^([A-Za-z_$][\w$.]*)\s*\(/);   // authorize('admin') 같은 미들웨어 팩토리
                if (call) { handlers.push({ ref: call[1] }); }
            }
        }
        routes.push({ method: m[2].toUpperCase(), path: m[4], router: m[1], at: m.index, handlers });
    }
    // app.use('/api/cart', requireAuth, cartRouter) — 마지막 인자가 붙이는 라우터, 그 앞은 그 경로 전체의 미들웨어
    const mounts: Mount[] = [];
    re = /\b[A-Za-z_$][\w$]*\s*\.\s*(?:use|register)\s*\(\s*(['"`])([^'"`]*)\1\s*,/g;
    while ((m = re.exec(keep))) {
        const open = keep.indexOf('(', m.index);
        const close = matchClose(code, open, '(', ')');
        const args = splitArgs(code, open, close).slice(1).map(([a, b]) => code.slice(a, b).trim());
        const last = args.pop() ?? '';
        if (!/^[A-Za-z_$][\w$.]*$/.test(last)) { continue; }
        const middlewares = args.map(x => (x.match(/^([A-Za-z_$][\w$.]*)\s*(\(|$)/) || [])[1]).filter(Boolean) as string[];
        mounts.push({ prefix: m[2], binding: last, middlewares, at: m.index });
    }

    // 화면 동작
    const ui: UiDecl[] = [];
    const ownerAt = (at: number) => {
        const owner = defs.filter(d => d.bodyStart <= at && at <= d.bodyEnd).sort((a, b) => (b.bodyStart - a.bodyStart))[0];
        return owner ? owner.name : null;
    };
    re = /\bon([A-Z][A-Za-z]*)\s*=\s*\{/g;
    while ((m = re.exec(code))) {
        const open = re.lastIndex - 1;
        const close = matchClose(code, open, '{', '}');
        const inner = code.slice(open + 1, close).trim();
        const event = m[1].replace(/^./, c => c.toLowerCase());
        const span = jsInlineFn(code, open + 1);
        if (span && span[1] <= close) {
            const def: Def = { key: '', name: `${ownerAt(m.index) ?? path.posix.basename(id)} · ${event}`, cls: null, start: open + 1, bodyStart: span[0], bodyEnd: span[1], kind: 'handler', exported: false };
            inlineDefs.push(def); ui.push({ event, at: m.index, owner: ownerAt(m.index), inline: def });
        } else if (/^[A-Za-z_$][\w$.]*$/.test(inner)) { ui.push({ event, at: m.index, owner: ownerAt(m.index), ref: inner.replace(/^(this|props)\./, '') }); }
    }
    re = /\.\s*addEventListener\s*\(\s*['"`](\w+)['"`]\s*,/g;
    while ((m = re.exec(keep))) {
        const open = keep.indexOf('(', m.index);
        const close = matchClose(code, open, '(', ')');
        const args = splitArgs(code, open, close);
        if (args.length < 2) { continue; }
        const [a, b] = args[1];
        const span = jsInlineFn(code, a);
        const owner = ownerAt(m.index);
        const target = keep.slice(Math.max(0, m.index - 160), m.index).match(/(?:getElementById|querySelector(?:All)?)\s*\(\s*['"`]([^'"`]+)['"`]\s*\)\s*$/);
        const where = target ? (target[1].startsWith('#') || target[1].startsWith('.') ? target[1] : `#${target[1]}`) : (owner ?? path.posix.basename(id));
        if (span && span[1] <= close) {
            const def: Def = { key: '', name: `${where} · ${m[1]}`, cls: null, start: a, bodyStart: span[0], bodyEnd: span[1], kind: 'handler', exported: false };
            inlineDefs.push(def); ui.push({ event: m[1], at: m.index, owner: where, inline: def });
        } else {
            const ref = code.slice(a, b).trim();
            if (/^[A-Za-z_$][\w$.]*$/.test(ref)) { ui.push({ event: m[1], at: m.index, owner: where, ref: ref.replace(/^this\./, '') }); }
        }
    }
    re = /\.\s*on(click|submit|change|input|keydown|keyup|load)\s*=\s*/g;
    while ((m = re.exec(code))) {
        const at = re.lastIndex;
        const span = jsInlineFn(code, at);
        const owner = ownerAt(m.index);
        if (span) {
            const def: Def = { key: '', name: `${owner ?? path.posix.basename(id)} · ${m[1]}`, cls: null, start: at, bodyStart: span[0], bodyEnd: span[1], kind: 'handler', exported: false };
            inlineDefs.push(def); ui.push({ event: m[1], at: m.index, owner, inline: def });
        } else {
            const ref = code.slice(at, at + 80).match(/^([A-Za-z_$][\w$]*)\s*[;\n]/);
            if (ref) { ui.push({ event: m[1], at: m.index, owner, ref: ref[1] }); }
        }
    }

    const all = [...defs, ...inlineDefs].sort((a, b) => a.bodyStart - b.bodyStart);
    // Next.js: pages/api/** 는 default, app/**/route.* 는 HTTP 메서드 이름 export 가 경로다.
    const main: Def = { key: '', name: '파일 실행', cls: null, start: 0, bodyStart: 0, bodyEnd: code.length, kind: 'main', exported: false };
    return {
        id, lang: 'js', src, code, keep, lineAt, defs: all, imports, defaultExport, exportNames, objects,
        routes, ui, mounts, routerPrefix: new Map(), main,
    };
}

// ── Python ───────────────────────────────────────────────────────────────────

function parsePy(id: string, src: string, mod: string): FileInfo {
    const code = stripPy(src);
    const keep = stripPy(src, true);
    const lineAt = lineIndex(src);
    const lines = code.split('\n');
    const offsets: number[] = []; { let o = 0; for (const l of lines) { offsets.push(o); o += l.length + 1; } }
    const indentOf = (l: string) => (l.match(/^(\s*)/) as RegExpMatchArray)[1].replace(/\t/g, '    ').length;
    const defs: Def[] = [];
    const classStack: Array<{ name: string; indent: number }> = [];
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (!line.trim()) { continue; }
        const indent = indentOf(line);
        while (classStack.length && indent <= classStack[classStack.length - 1].indent) { classStack.pop(); }
        const mc = line.match(/^\s*class\s+([A-Za-z_]\w*)/);
        if (mc) { classStack.push({ name: mc[1], indent }); continue; }
        const md = line.match(/^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(/);
        if (!md) { continue; }
        // 시그니처가 여러 줄일 수 있다 — 콜론까지
        let sigEnd = i;
        let depth = 0;
        for (let j = i; j < lines.length; j++) {
            for (const c of lines[j]) { if (c === '(' || c === '[') { depth++; } else if (c === ')' || c === ']') { depth--; } }
            if (depth <= 0 && /:\s*$/.test(lines[j])) { sigEnd = j; break; }
            if (j - i > 40) { sigEnd = j; break; }
        }
        let end = lines.length;
        for (let j = sigEnd + 1; j < lines.length; j++) {
            if (!lines[j].trim()) { continue; }
            if (indentOf(lines[j]) <= indent) { end = j; break; }
        }
        const cls = classStack.length && classStack[classStack.length - 1].indent < indent ? classStack[classStack.length - 1].name : null;
        const bodyEnd = end < lines.length ? offsets[end] - 1 : code.length;
        defs.push({
            key: '', name: md[1], cls, start: offsets[i], bodyStart: Math.min(sigEnd + 1 < lines.length ? offsets[sigEnd + 1] : code.length, bodyEnd),
            bodyEnd, kind: 'function', exported: indent === 0,
        });
    }

    // import
    const imports = new Map<string, Binding>();
    const pkgParts = mod.split('.').slice(0, id.endsWith('__init__.py') ? undefined : -1);
    const resolveBase = (dots: number, name: string) => {
        if (!dots) { return name; }
        const base = pkgParts.slice(0, pkgParts.length - (dots - 1)).filter(Boolean);
        return [...base, ...(name ? [name] : [])].join('.');
    };
    let m: RegExpExecArray | null;
    let re = /^[ \t]*from[ \t]+(\.+[\w.]*|[\w.]+)[ \t]+import[ \t]*(?:\(([^)]{0,4000})\)|([^\s(][^\n(]*)$)/gm;
    while ((m = re.exec(code))) {
        const names = m[2] ?? m[3] ?? '';
        const dots = (m[1].match(/^\.*/) as RegExpMatchArray)[0].length;
        const base = resolveBase(dots, m[1].slice(dots));
        m[3] = names;
        for (const part of m[3].split(/[,\n]/)) {
            const p = part.trim().replace(/\\$/, '').trim(); if (!p || p === '*') { continue; }
            const [orig, alias] = p.replace(/\s+/g, ' ').split(' as ').map(x => x.trim());
            if (!/^\w+$/.test(orig)) { continue; }
            imports.set(alias || orig, { file: `py:${base}`, name: orig });
        }
    }
    re = /^[ \t]*import[ \t]+([\w.][\w., \t]*)$/gm;
    while ((m = re.exec(code))) {
        for (const part of m[1].split(',')) {
            const [orig, alias] = part.trim().replace(/\s+/g, ' ').split(' as ').map(x => x.trim());
            if (!orig) { continue; }
            imports.set(alias || orig.split('.')[0], { file: `py:${alias ? orig : orig.split('.')[0]}`, name: '*' });
        }
    }

    // self.x = Cls(...) · x = Cls(...)
    const objects = new Map<string, { binding: string | null; cls: string }>();
    re = /(?:\bself\s*\.\s*|^[ \t]*)([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*([A-Z]\w*)\s*\(/gm;
    while ((m = re.exec(code))) { objects.set(m[1], { binding: imports.has(m[2]) ? m[2] : null, cls: m[2] }); }

    // 경로 데코레이터 (FastAPI · Flask)
    const routes: RouteDecl[] = [];
    const defAtLine = (lineNo: number) => defs.find(d => lineAt(d.start) >= lineNo);
    re = /^[ \t]*@([A-Za-z_]\w*)\s*\.\s*(get|post|put|patch|delete|options|head|route|api_route|websocket)\s*\(\s*(['"])([^'"]*)\3([^\n]*)/gm;
    while ((m = re.exec(keep))) {
        const lineNo = lineAt(m.index);
        const target = defAtLine(lineNo + 1);
        if (!target) { continue; }
        let method = m[2].toUpperCase();
        if (method === 'ROUTE' || method === 'API_ROUTE') {
            const methods = (m[5].match(/methods\s*=\s*\[([^\]]*)\]/) || [])[1];
            method = methods ? methods.replace(/['"\s]/g, '').split(',').filter(Boolean).join('|').toUpperCase() : 'GET';
        }
        if (method === 'WEBSOCKET') { method = 'WS'; }
        // def 시그니처의 Depends(…) — 인증 같은 의존 함수가 핸들러보다 먼저 돈다.
        const sig = code.slice(target.start, target.bodyStart);
        const deps = [...sig.matchAll(/\b(?:Depends|Security)\s*\(\s*([A-Za-z_][\w.]*)/g)].map(x => x[1]);
        const deco = [...(m[5].matchAll(/\b(?:Depends|Security)\s*\(\s*([A-Za-z_][\w.]*)/g))].map(x => x[1]);
        routes.push({ method, path: m[4], router: m[1], at: m.index, handlers: [{ ref: target.name }], deps: [...deco, ...deps] });
    }
    const routerPrefix = new Map<string, string>();
    re = /\b([A-Za-z_]\w*)\s*=\s*(?:APIRouter|Blueprint)\s*\(([^)]*)\)/g;
    while ((m = re.exec(keep))) {
        const pre = m[2].match(/\b(?:prefix|url_prefix)\s*=\s*['"]([^'"]*)['"]/);
        if (pre) { routerPrefix.set(m[1], pre[1]); }
    }
    const mounts: Mount[] = [];
    re = /\.\s*(?:include_router|register_blueprint)\s*\(\s*([A-Za-z_][\w.]*)\s*(?:,([^)]*))?\)/g;
    while ((m = re.exec(keep))) {
        const pre = (m[2] || '').match(/\b(?:prefix|url_prefix)\s*=\s*['"]([^'"]*)['"]/);
        const deps = [...((m[2] || '').matchAll(/\b(?:Depends|Security)\s*\(\s*([A-Za-z_][\w.]*)/g))].map(x => x[1]);
        mounts.push({ prefix: pre ? pre[1] : '', binding: m[1], middlewares: deps, at: m.index });
    }

    // if __name__ == "__main__": 블록 또는 모듈 최상위
    let main: Def | null = null;
    const mainAt = keep.search(/^if\s+__name__\s*==\s*['"]__main__['"]\s*:/m);
    if (mainAt >= 0) {
        const after = keep.indexOf('\n', mainAt);
        const start = after < 0 ? code.length : after + 1;
        main = { key: '', name: '파일 실행', cls: null, start: mainAt, bodyStart: start, bodyEnd: code.length, kind: 'main', exported: false };
    } else {
        main = { key: '', name: '파일 실행', cls: null, start: 0, bodyStart: 0, bodyEnd: code.length, kind: 'main', exported: false };
    }
    const exportNames = new Map<string, string>();
    for (const d of defs) { if (d.exported) { exportNames.set(d.name, d.name); } }
    return {
        id, lang: 'py', src, code, keep, lineAt, defs, imports, defaultExport: null, exportNames, objects,
        routes, ui: [], mounts, routerPrefix, main,
    };
}

// ── 갈림(조건·반복·오류 처리·콜백) ─────────────────────────────────────────────

/**
 * 함수 본문 안 위치별 갈림 종류. 본문을 한 번 훑어 중괄호마다 종류를 붙여 두고(if·else·반복·catch·switch·콜백),
 * 호출 위치는 이분 탐색으로 찾는다 — 호출마다 본문을 다시 훑지 않는다.
 */
function jsBranchIndex(code: string, bodyStart: number, bodyEnd: number): (at: number) => BranchKind | undefined {
    const events: Array<{ pos: number; kind: BranchKind | undefined }> = [];
    const stack: Array<BranchKind | null> = [];
    const effective = () => { for (let k = stack.length - 1; k >= 0; k--) { if (stack[k]) { return stack[k] as BranchKind; } } return undefined; };
    for (let i = bodyStart; i < bodyEnd; i++) {
        const c = code[i];
        if (c !== '{' && c !== '}') { continue; }
        if (c === '}') { stack.pop(); events.push({ pos: i, kind: effective() }); continue; }
        let j = i - 1;
        while (j >= bodyStart && /\s/.test(code[j])) { j--; }
        let kind: BranchKind | null = null;
        const tail = code.slice(Math.max(bodyStart, j - 7), j + 1);
        if (/\belse$/.test(tail)) { kind = 'else'; }
        else if (/\bdo$/.test(tail)) { kind = 'loop'; }
        else if (/=>$/.test(tail)) { kind = 'callback'; }
        else if (code[j] === ')') {
            const open = findOpenParen(code, j, bodyStart);
            let w = open - 1;
            while (w >= bodyStart && /\s/.test(code[w])) { w--; }
            let ws = w;
            while (ws >= bodyStart && /[\w$]/.test(code[ws])) { ws--; }
            const word = code.slice(ws + 1, w + 1);
            if (word === 'if') { kind = 'if'; }
            else if (word === 'for' || word === 'while') { kind = 'loop'; }
            else if (word === 'catch') { kind = 'catch'; }
            else if (word === 'switch') { kind = 'case'; }
            else if (word === 'function' || /^function\b/.test(code.slice(ws - 8 < bodyStart ? bodyStart : ws - 8, ws + 1).trim())) { kind = 'callback'; }
        }
        stack.push(kind);
        events.push({ pos: i, kind: effective() });
    }
    return (at: number) => {
        let lo = 0, hi = events.length - 1, found = -1;
        while (lo <= hi) { const mid = (lo + hi) >> 1; if (events[mid].pos < at) { found = mid; lo = mid + 1; } else { hi = mid - 1; } }
        const inBlock = found >= 0 ? events[found].kind : undefined;
        if (inBlock) { return inBlock; }
        // 중괄호 없는 if · 한 줄 조건 `a && f()` · `c ? f() : g()`
        const stmtStart = Math.max(code.lastIndexOf(';', at - 1), code.lastIndexOf('{', at - 1), code.lastIndexOf('}', at - 1), bodyStart - 1);
        const stmt = code.slice(stmtStart + 1, at);
        if (/\b(if|else)\b/.test(stmt)) { return 'if'; }
        if (/\b(for|while)\s*\(/.test(stmt)) { return 'loop'; }
        if (/&&|\|\||\?\?|\?(?![.:])/.test(stmt)) { return 'cond'; }
        return undefined;
    };
}

function findOpenParen(code: string, close: number, floor: number): number {
    let depth = 0;
    for (let i = close; i >= floor; i--) {
        if (code[i] === ')') { depth++; } else if (code[i] === '(') { depth--; if (depth === 0) { return i; } }
    }
    return floor;
}

function pyBranchAt(code: string, defStart: number, at: number): BranchKind | undefined {
    const lineStart = code.lastIndexOf('\n', at - 1) + 1;
    const cur = code.slice(lineStart, at);
    let indent = (cur.match(/^(\s*)/) as RegExpMatchArray)[1].length;
    let i = lineStart - 1;
    while (i > defStart) {
        const s = code.lastIndexOf('\n', i - 1) + 1;
        const line = code.slice(s, i);
        i = s - 1;
        if (!line.trim()) { continue; }
        const ind = (line.match(/^(\s*)/) as RegExpMatchArray)[1].length;
        if (ind >= indent) { continue; }
        indent = ind;
        const head = line.trim();
        if (/^(if|elif)\b/.test(head)) { return 'if'; }
        if (/^else\b/.test(head)) { return 'else'; }
        if (/^(for|while|async\s+for)\b/.test(head)) { return 'loop'; }
        if (/^except\b/.test(head)) { return 'catch'; }
        if (/^(case|match)\b/.test(head)) { return 'case'; }
        if (/^(async\s+)?def\b/.test(head)) { break; }
    }
    if (/\bif\b.+\belse\b|\b(and|or)\b/.test(cur)) { return 'cond'; }
    return undefined;
}

// ── 그래프 ───────────────────────────────────────────────────────────────────

export interface FlowOptions {
    /** 적용 전 미리 보기: 상대 경로 → 바뀐 내용(null 이면 지움). 디스크 파일보다 먼저 쓴다. */
    overrides?: Map<string, string | null>;
}

export function analyzeFlow(root: string, options: FlowOptions = {}): FlowGraph {
    if (!fs.existsSync(root)) { throw new Error(`경로 없음: ${root}`); }
    const abs: string[] = [];
    iterFiles(root, [...JS_EXT, ...PY_EXT], abs);
    const sources = new Map<string, string>();
    for (const file of abs) {
        const rel = toPosix(path.relative(root, file));
        if (/\.d\.ts$/.test(rel) || /(^|\/)(__tests__|tests?)\//.test(rel) || /\.(test|spec)\.[jt]sx?$/.test(rel) || /(^|\/)test_[^/]*\.py$/.test(rel)) { continue; }
        const text = readText(file);
        if (text !== null) { sources.set(rel, text); }
    }
    for (const [rel, content] of options.overrides ?? []) {
        if (!/\.(m|c)?[jt]sx?$|\.py$/.test(rel)) { continue; }
        if (content === null) { sources.delete(rel); } else { sources.set(rel, content); }
    }

    const files = new Map<string, FileInfo>();
    const byModule = new Map<string, string>();
    for (const [rel, text] of sources) {
        const ext = path.posix.extname(rel).toLowerCase();
        if (PY_EXT.includes(ext)) {
            const mod = moduleName(rel);
            byModule.set(mod, rel);
            files.set(rel, parsePy(rel, text, mod));
        } else {
            files.set(rel, parseJs(rel, text));
        }
    }
    const ids = new Set(files.keys());
    const resolveJsSpec = (from: string, spec: string): string | null => {
        if (!spec.startsWith('.') && !spec.startsWith('@/') && !spec.startsWith('~/') && !spec.startsWith('/')) { return null; }
        const base = from.includes('/') ? from.slice(0, from.lastIndexOf('/')) : '';
        const raw = spec.startsWith('@/') || spec.startsWith('~/') ? path.posix.join('src', spec.slice(2)) : spec.startsWith('/') ? spec.slice(1) : path.posix.join(base, spec);
        const cand = path.posix.normalize(raw).replace(/\.(js|mjs|cjs)$/, m => m);
        const tries = [cand, cand.replace(/\.(m|c)?js$/, '.ts'), cand.replace(/\.(m|c)?js$/, '.tsx')];
        for (const t of tries) {
            if (ids.has(t)) { return t; }
            for (const ext of [...JS_EXT, '/index.ts', '/index.tsx', '/index.js', '/index.jsx', '/index.mjs']) { if (ids.has(t + ext)) { return t + ext; } }
        }
        return null;
    };
    const resolvePyMod = (mod: string): string | null => byModule.get(mod) ?? null;
    const bindingFile = (info: FileInfo, b: Binding): { file: string | null; name: string } => {
        if (!b.file) { return { file: null, name: b.name }; }
        if (info.lang === 'js') { return { file: resolveJsSpec(info.id, b.file), name: b.name }; }
        const mod = b.file.slice(3);
        if (b.name !== '*') {
            const sub = resolvePyMod(`${mod}.${b.name}`);
            if (sub) { return { file: sub, name: '*' }; }
        }
        return { file: resolvePyMod(mod), name: b.name };
    };

    // 노드
    const nodes: FlowNode[] = [];
    const nodeOf = new Map<Def, string>();
    const defsByFile = new Map<string, Array<{ def: Def; id: string }>>();
    let truncated = false;
    for (const info of files.values()) {
        const list: Array<{ def: Def; id: string }> = [];
        const used = new Set<string>();
        for (const def of info.defs) {
            if (nodes.length >= MAX_NODES) { truncated = true; break; }
            const label = def.cls ? `${def.cls}.${def.name}` : def.name;
            let key = `${info.id}#${label}`;
            if (used.has(key)) { key = `${key}@${info.lineAt(def.start)}`; }
            used.add(key);
            def.key = key;
            nodeOf.set(def, key);
            list.push({ def, id: key });
        }
        defsByFile.set(info.id, list);
    }

    const edges: FlowEdge[] = [];
    const edgeSeen = new Set<string>();
    const pushEdge = (e: FlowEdge) => {
        if (e.from === e.to) { return; }
        const k = `${e.from} ${e.to} ${e.line}`;
        if (edgeSeen.has(k)) { return; }
        if (edges.length >= MAX_EDGES) { truncated = true; return; }
        edgeSeen.add(k);
        edges.push(e);
    };

    /** 파일 file 에서 이름 name 으로 정의를 찾는다(내보낸 이름·default 포함). */
    const lookupExport = (file: string, name: string): { id: string; direct: boolean } | null => {
        const info = files.get(file);
        const list = defsByFile.get(file) ?? [];
        if (!info) { return null; }
        const target = name === 'default' ? (info.defaultExport ?? 'default') : (info.exportNames.get(name) ?? name);
        const hit = list.find(x => !x.def.cls && x.def.name === target && x.def.kind !== 'handler')
            ?? list.find(x => x.def.cls === target && x.def.name === 'constructor');
        return hit ? { id: hit.id, direct: true } : null;
    };
    const lookupMethod = (file: string, cls: string | null, method: string): string | null => {
        const list = defsByFile.get(file) ?? [];
        const hit = list.find(x => x.def.name === method && (cls ? x.def.cls === cls : true) && x.def.kind !== 'handler');
        return hit ? hit.id : null;
    };

    /** 호출 하나를 잇는다. scope: 호출이 있는 정의(최상위면 null). */
    const resolveCall = (info: FileInfo, scope: Def | null, call: CallSite): { id: string; certainty: FlowEdge['certainty'] } | null => {
        const local = defsByFile.get(info.id) ?? [];
        if (call.thisCall) {
            const cls = scope?.cls ?? null;
            const id = lookupMethod(info.id, cls, call.name);
            return id ? { id, certainty: cls ? 'direct' : 'inferred' } : null;
        }
        if (!call.obj) {
            const hit = local.find(x => x.def.name === call.name && !x.def.cls && x.def.kind === 'function')
                ?? local.find(x => x.def.name === call.name && x.def.cls === (scope?.cls ?? '\0'));
            if (hit) { return { id: hit.id, certainty: 'direct' }; }
            const b = info.imports.get(call.name);
            if (b) {
                const t = bindingFile(info, b);
                if (t.file) {
                    const ex = lookupExport(t.file, t.name === '*' ? call.name : t.name);
                    if (ex) { return { id: ex.id, certainty: 'direct' }; }
                }
            }
            return null;
        }
        // 객체.메서드(…)
        const head = call.obj.split('.')[0];
        const tail = call.obj.split('.').slice(1).join('.');
        const b = info.imports.get(head);
        if (b && !tail) {
            const t = bindingFile(info, b);
            if (t.file) {
                if (t.name === '*') {
                    const ex = lookupExport(t.file, call.name);
                    if (ex) { return { id: ex.id, certainty: 'direct' }; }
                } else {
                    // default 로 받은 객체·클래스의 메서드 · module.exports = { fn }
                    const target = files.get(t.file);
                    const def = t.name === 'default' ? target?.defaultExport : t.name;
                    const ex = lookupExport(t.file, call.name);
                    const id = lookupMethod(t.file, def ?? null, call.name) ?? (ex ? ex.id : null);
                    if (id) { return { id, certainty: 'inferred' }; }
                }
            }
        }
        const objName = head === 'this' || head === 'self' ? tail : (tail ? null : head);
        const obj = objName ? info.objects.get(objName) : undefined;
        if (obj) {
            if (obj.binding) {
                const t = bindingFile(info, info.imports.get(obj.binding) as Binding);
                if (t.file) {
                    const cls = t.name === 'default' ? (files.get(t.file)?.defaultExport ?? obj.cls) : t.name === '*' ? obj.cls : t.name;
                    const id = lookupMethod(t.file, cls, call.name);
                    if (id) { return { id, certainty: 'inferred' }; }
                }
            }
            const id = lookupMethod(info.id, obj.cls, call.name);
            if (id) { return { id, certainty: 'inferred' }; }
        }
        return null;
    };

    /**
     * 본문 안의 호출 `name(` · `obj.name(` · `this.name(`. 이름 하나만 정규식으로 찾고 앞의 `a.b.` 는 거꾸로 읽는다 —
     * 객체 사슬까지 정규식에 넣으면 큰 파일에서 되돌아가기(backtracking)가 폭발했다(실측 40 파일 16초).
     */
    const callsIn = (info: FileInfo, from: number, to: number, exclude: Array<[number, number]>): CallSite[] => {
        const out: CallSite[] = [];
        const code = info.code;
        const body = code.slice(from, to);
        const js = info.lang === 'js';
        const re = js ? /(?<![\w$])([A-Za-z_$][\w$]*)\s*(?:<[^<>()\n]{0,80}>\s*)?\(/g : /(?<![\w])([A-Za-z_]\w*)\s*\(/g;
        const reserved = js ? JS_RESERVED : PY_RESERVED;
        const isId = js ? /[\w$]/ : /\w/;
        let m: RegExpExecArray | null;
        while ((m = re.exec(body))) {
            const at = from + m.index;
            if (exclude.some(([a, b]) => at >= a && at < b)) { continue; }
            const name = m[1];
            // 앞쪽 객체 사슬 읽기: a . b . name(
            const chain: string[] = [];
            let i = at - 1;
            for (let hops = 0; hops < 3; hops++) {
                let j = i;
                while (j >= 0 && (code[j] === ' ' || code[j] === '\t')) { j--; }
                if (code[j] !== '.' || code[j - 1] === '.') { break; }
                j--;
                while (j >= 0 && /\s/.test(code[j])) { j--; }
                let k = j;
                while (k >= 0 && isId.test(code[k])) { k--; }
                const word = code.slice(k + 1, j + 1);
                if (!word || !/^[A-Za-z_$]/.test(word)) { chain.length = 0; chain.push('\0'); break; }
                chain.unshift(word);
                i = k;
            }
            if (chain[0] === '\0') { continue; }                 // (식).name( · a[0].name( — 따라갈 수 없다
            const thisCall = chain.length === 1 && (chain[0] === 'this' || chain[0] === 'self');
            if (!chain.length) {
                if (reserved.has(name)) { continue; }
                const before = code.slice(Math.max(0, at - 12), at);
                if (/\b(function|def|class|new)\s*$/.test(before)) { continue; }
            }
            out.push({ name, obj: thisCall || !chain.length ? null : chain.join('.'), thisCall, at });
        }
        return out;
    };

    const nodeFor = (info: FileInfo, def: Def, exclude: Array<[number, number]>): FlowNode => {
        const start = info.lineAt(def.start);
        const end = info.lineAt(Math.max(def.bodyStart, def.bodyEnd - 1));
        let text = info.keep.slice(def.bodyStart, def.bodyEnd);
        for (const [a, b] of exclude) {
            if (a >= def.bodyStart && b <= def.bodyEnd) {
                text = text.slice(0, a - def.bodyStart) + ' '.repeat(b - a) + text.slice(b - def.bodyStart);
            }
        }
        return {
            id: def.key, file: info.id, name: def.name, cls: def.cls, line: start, endLine: Math.max(start, end),
            kind: def.kind, effects: effectsIn(text, def.bodyStart, info.lineAt),
            hash: fnv(info.code.slice(def.bodyStart, def.bodyEnd).replace(/\s+/g, ' ')),
        };
    };

    for (const info of files.values()) {
        const list = defsByFile.get(info.id) ?? [];
        for (const { def } of list) {
            // 안쪽 정의(이름 있는 함수·경로·화면 핸들러)는 그 노드의 몫 — 바깥 함수의 호출로 세지 않는다.
            const inner = list.filter(x => x.def !== def && x.def.bodyStart > def.bodyStart && x.def.bodyEnd <= def.bodyEnd)
                .map(x => [x.def.start, x.def.bodyEnd] as [number, number]);
            nodes.push(nodeFor(info, def, inner));
            let order = 0;
            const branchOf = info.lang === 'js' ? jsBranchIndex(info.code, def.bodyStart, def.bodyEnd) : (at: number) => pyBranchAt(info.code, def.start, at);
            for (const call of callsIn(info, def.bodyStart, def.bodyEnd, inner)) {
                const hit = resolveCall(info, def, call);
                if (!hit) { continue; }
                const branch = branchOf(call.at);
                pushEdge({ from: def.key, to: hit.id, line: info.lineAt(call.at), order: order++, branch, certainty: hit.certainty });
            }
        }
    }

    // 경로 접두사: app.use('/api', router) · include_router(router, prefix=…) — 붙인 파일이 또 붙어 있으면 이어 붙인다.
    const mountsInto = new Map<string, Array<{ parent: string; prefix: string; middlewares: string[]; at: number }>>();
    const localPrefix = new Map<string, string>();          // `${file} ${routerVar}` → 접두사
    for (const info of files.values()) {
        for (const [v, p] of info.routerPrefix) { localPrefix.set(`${info.id} ${v}`, p); }
    }
    for (const info of files.values()) {
        for (const mount of info.mounts) {
            const head = mount.binding.split('.')[0];
            const b = info.imports.get(head);
            if (b) {
                const t = bindingFile(info, b);
                if (t.file && t.file !== info.id) {
                    const list = mountsInto.get(t.file) ?? [];
                    list.push({ parent: info.id, prefix: mount.prefix, middlewares: mount.middlewares, at: mount.at });
                    mountsInto.set(t.file, list);
                }
            } else {
                localPrefix.set(`${info.id} ${head}`, joinRoute(mount.prefix, localPrefix.get(`${info.id} ${head}`) ?? ''));
            }
        }
    }
    /** 이 파일이 붙은 경로들 — 접두사와, 그 경로에 걸린 미들웨어(바깥 것부터). */
    type Mounted = { prefix: string; chain: Array<{ file: string; ref: string; at: number }> };
    const prefixesFor = (file: string, depth = 0): Mounted[] => {
        const own = mountsInto.get(file);
        if (!own || depth > 4) { return [{ prefix: '', chain: [] }]; }
        const out: Mounted[] = [];
        for (const { parent, prefix, middlewares, at } of own) {
            for (const pp of prefixesFor(parent, depth + 1)) {
                out.push({ prefix: joinRoute(pp.prefix, prefix), chain: [...pp.chain, ...middlewares.map(ref => ({ file: parent, ref, at }))] });
            }
        }
        return out;
    };

    const entries: FlowEntry[] = [];
    const entryIds = new Set<string>();
    const addEntry = (e: FlowEntry) => { if (!entryIds.has(e.id)) { entryIds.add(e.id); entries.push(e); } };
    const synthetic = (info: FileInfo, id: string, name: string, line: number, kind: FlowNode['kind']): FlowNode => {
        const n: FlowNode = { id, file: info.id, name, cls: null, line, endLine: line, kind, effects: [], hash: fnv(id) };
        nodes.push(n);
        return n;
    };

    for (const info of files.values()) {
        // 서버 경로
        for (const route of info.routes) {
            const own = localPrefix.get(`${info.id} ${route.router}`) ?? '';
            for (const mounted of prefixesFor(info.id)) {
                const full = joinRoute(joinRoute(mounted.prefix, own), route.path);
                const id = `${info.id}#route:${route.method} ${full}@${info.lineAt(route.at)}`;
                if (entryIds.has(id)) { continue; }
                const line = info.lineAt(route.at);
                //: 핸들러가 함수로 이어지지 않으면 경로 선언이 아니다 — 화면 코드의 axios.get('/api/x', 설정) 같은 호출.
                const targets: Array<{ id: string; certainty: FlowEdge['certainty'] }> = [];
                for (const h of route.handlers) {
                    if (h.inline) { targets.push({ id: h.inline.key, certainty: 'direct' }); continue; }
                    if (!h.ref) { continue; }
                    const parts = h.ref.split('.');
                    const name = parts.pop() as string;
                    const hit = resolveCall(info, null, { name, obj: parts.length ? parts.join('.') : null, thisCall: false, at: route.at });
                    if (hit) { targets.push(hit); }
                }
                if (!targets.length) { continue; }
                synthetic(info, id, `${route.method} ${full}`, line, 'route');
                let order = 0;
                for (const mw of mounted.chain) {
                    const owner = files.get(mw.file);
                    if (!owner) { continue; }
                    const parts = mw.ref.split('.');
                    const hit = resolveCall(owner, null, { name: parts.pop() as string, obj: parts.length ? parts.join('.') : null, thisCall: false, at: mw.at });
                    if (hit) { pushEdge({ from: id, to: hit.id, line, order: order++, certainty: hit.certainty }); }
                }
                for (const dep of route.deps ?? []) {
                    const parts = dep.split('.');
                    const hit = resolveCall(info, null, { name: parts.pop() as string, obj: parts.length ? parts.join('.') : null, thisCall: false, at: route.at });
                    if (hit) { pushEdge({ from: id, to: hit.id, line, order: order++, certainty: hit.certainty }); }
                }
                for (const target of targets) { pushEdge({ from: id, to: target.id, line, order: order++, certainty: target.certainty }); }
                addEntry({ id, label: `${route.method} ${full}`, kind: 'route', method: route.method, path: full, file: info.id, line });
            }
        }
        // Next.js 경로 파일
        const next = info.id.match(/(?:^|\/)pages\/api\/(.+)\.(?:[jt]sx?)$/);
        if (next && info.defaultExport) {
            const ex = lookupExport(info.id, 'default');
            if (ex) {
                const full = '/api/' + next[1].replace(/(^|\/)index$/, '').replace(/\[([^\]]+)\]/g, ':$1');
                addEntry({ id: ex.id, label: `API ${full}`, kind: 'route', method: 'ANY', path: full, file: info.id, line: (nodes.find(n => n.id === ex.id)?.line ?? 1) });
            }
        }
        const appRoute = info.id.match(/(?:^|\/)app\/(.*?)\/?route\.(?:[jt]sx?)$/);
        if (appRoute) {
            const full = '/' + appRoute[1].split('/').filter(s => s && !/^\(.*\)$/.test(s)).map(s => s.replace(/\[([^\]]+)\]/g, ':$1')).join('/');
            for (const method of ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']) {
                const ex = info.exportNames.has(method) ? lookupExport(info.id, method) : null;
                if (ex) { addEntry({ id: ex.id, label: `${method} ${full}`, kind: 'route', method, path: full, file: info.id, line: nodes.find(n => n.id === ex.id)?.line ?? 1 }); }
            }
        }
        // 화면 동작
        for (const u of info.ui) {
            let target: string | null = null;
            if (u.inline) { target = u.inline.key; }
            else if (u.ref) {
                const parts = u.ref.split('.');
                const name = parts.pop() as string;
                const scope = (defsByFile.get(info.id) ?? []).map(x => x.def)
                    .filter(d => d.bodyStart <= u.at && u.at <= d.bodyEnd).sort((x, y) => y.bodyStart - x.bodyStart)[0] ?? null;
                const hit = resolveCall(info, scope, { name, obj: parts.length ? parts.join('.') : null, thisCall: false, at: u.at });
                target = hit ? hit.id : null;
            }
            if (!target) { continue; }
            const label = `${u.owner ?? path.posix.basename(info.id)} · ${u.event}${u.inline ? ` (${info.lineAt(u.at)}행)` : ` → ${u.ref}`}`;
            addEntry({ id: target, label, kind: 'ui', file: info.id, line: info.lineAt(u.at) });
        }
    }

    // 실행 파일의 최상위 코드
    const entryFiles = mainFiles(root, ids);
    for (const info of files.values()) {
        if (!info.main) { continue; }
        const isMainBlock = info.lang === 'py' && info.main.bodyStart > 0;
        if (!isMainBlock && !entryFiles.has(info.id)) { continue; }
        const list = defsByFile.get(info.id) ?? [];
        const exclude = list.map(x => [x.def.start, x.def.bodyEnd] as [number, number]);
        if (info.lang === 'py') {
            // 클래스 몸통도 최상위 실행이 아니다
            const cls = /^class\s+\w+[^\n]*:\s*$/gm; let m: RegExpExecArray | null;
            while ((m = cls.exec(info.code))) {
                const after = info.code.slice(m.index + m[0].length);
                const end = after.search(/\n(?=\S)/);
                exclude.push([m.index, m.index + m[0].length + (end < 0 ? after.length : end)]);
            }
        }
        const id = `${info.id}#<main>`;
        const calls = callsIn(info, info.main.bodyStart, info.main.bodyEnd, exclude);
        const resolved = calls.map(c => ({ c, hit: resolveCall(info, null, c) })).filter(x => x.hit);
        let text = info.keep.slice(info.main.bodyStart, info.main.bodyEnd);
        for (const [a, b] of exclude) {
            if (a >= info.main.bodyStart && b <= info.main.bodyEnd) { text = text.slice(0, a - info.main.bodyStart) + ' '.repeat(b - a) + text.slice(b - info.main.bodyStart); }
        }
        const effects = effectsIn(text, info.main.bodyStart, info.lineAt);
        if (!resolved.length && !effects.length) { continue; }
        const node = synthetic(info, id, `${path.posix.basename(info.id)} 실행`, info.lineAt(info.main.bodyStart), 'main');
        node.effects = effects;
        let order = 0;
        const branchOf = info.lang === 'js' ? jsBranchIndex(info.code, info.main.bodyStart, info.main.bodyEnd) : (at: number) => pyBranchAt(info.code, info.main.bodyStart, at);
        for (const { c, hit } of resolved) {
            const branch = branchOf(c.at);
            pushEdge({ from: id, to: hit!.id, line: info.lineAt(c.at), order: order++, branch, certainty: hit!.certainty });
        }
        addEntry({ id, label: `실행: ${info.id}`, kind: 'main', file: info.id, line: node.line });
    }

    const notes: string[] = [];
    if (truncated) { notes.push(`프로젝트가 커서 노드 ${MAX_NODES}개·연결 ${MAX_EDGES}개까지만 분석했습니다.`); }
    if (!entries.length) { notes.push('서버 경로·화면 동작·실행 파일을 찾지 못했습니다. 아무 함수나 골라 dry run 할 수 있습니다.'); }
    const kindOrder = { route: 0, ui: 1, main: 2 } as const;
    entries.sort((a, b) => kindOrder[a.kind] - kindOrder[b.kind] || a.label.localeCompare(b.label));
    return { kind: 'flow', source: 'source', root, files_scanned: files.size, nodes, edges, entries, truncated, notes };
}

/** package.json main·bin·scripts 의 직접 실행 파일과 흔한 서버 파일 이름. */
function mainFiles(root: string, ids: Set<string>): Set<string> {
    const out = new Set<string>();
    const common = /^(?:(?:server|backend|api|src)\/)?(?:server|index|app|main)\.(?:[mc]?[jt]s)$|^(?:main|app|server|run|manage|wsgi|asgi)\.py$/;
    for (const id of ids) { if (common.test(id)) { out.add(id); } }
    for (const pkgPath of [...ids].map(id => path.posix.dirname(id)).filter((v, i, a) => a.indexOf(v) === i).map(d => d === '.' ? 'package.json' : `${d}/package.json`)) {
        const text = readText(path.join(root, pkgPath));
        if (!text) { continue; }
        try {
            const pkg = JSON.parse(text);
            const dir = path.posix.dirname(pkgPath) === '.' ? '' : path.posix.dirname(pkgPath);
            const add = (v: unknown) => {
                if (typeof v !== 'string') { return; }
                const rel = path.posix.normalize(path.posix.join(dir, v.replace(/\\/g, '/')));
                for (const c of [rel, `${rel}.js`, `${rel}.ts`, `${rel}/index.js`]) { if (ids.has(c)) { out.add(c); } }
            };
            add(pkg.main);
            if (typeof pkg.bin === 'string') { add(pkg.bin); } else if (pkg.bin && typeof pkg.bin === 'object') { Object.values(pkg.bin).forEach(add); }
            for (const script of Object.values(pkg.scripts ?? {})) {
                const m = typeof script === 'string' ? script.trim().match(/^(?:node|nodejs|tsx|ts-node|nodemon)\s+(?:--\S+\s+)*([^\s;&|]+)/) : null;
                if (m) { add(m[1]); }
            }
        } catch { /* 깨진 package.json 은 무시 */ }
    }
    return out;
}

// ── 적용 전 미리 보기 ────────────────────────────────────────────────────────

export interface FlowPreview {
    graph: FlowGraph;
    changed: Record<string, 'added' | 'modified'>;
    removed: Array<{ id: string; file: string; name: string }>;
    /** 바뀐 함수를 지나가는 진입점 */
    affected: string[];
}

export function previewFlow(root: string, overrides: Map<string, string | null>): FlowPreview {
    const base = analyzeFlow(root);
    const graph = analyzeFlow(root, { overrides });
    const before = new Map(base.nodes.map(n => [n.id, n]));
    const after = new Map(graph.nodes.map(n => [n.id, n]));
    const touched = new Set(overrides.keys());
    const changed: Record<string, 'added' | 'modified'> = {};
    for (const n of graph.nodes) {
        if (!touched.has(n.file) || n.kind === 'route' || n.kind === 'main') { continue; }
        const old = before.get(n.id);
        if (!old) { changed[n.id] = 'added'; } else if (old.hash !== n.hash) { changed[n.id] = 'modified'; }
    }
    // 경로·실행 노드는 이어지는 함수가 바뀌었는지로 본다(새로 생긴 경로는 '추가')
    for (const n of graph.nodes) {
        if ((n.kind === 'route' || n.kind === 'main') && touched.has(n.file) && !before.has(n.id)) { changed[n.id] = 'added'; }
    }
    const removed = base.nodes.filter(n => touched.has(n.file) && !after.has(n.id) && n.kind !== 'route' && n.kind !== 'main')
        .map(n => ({ id: n.id, file: n.file, name: n.cls ? `${n.cls}.${n.name}` : n.name }));
    const out = new Map<string, string[]>();
    for (const e of graph.edges) { const l = out.get(e.from) ?? []; l.push(e.to); out.set(e.from, l); }
    const affected: string[] = [];
    for (const entry of graph.entries) {
        const seen = new Set<string>([entry.id]);
        const queue = [entry.id];
        let hit = !!changed[entry.id];
        while (queue.length && !hit) {
            const id = queue.shift() as string;
            for (const next of out.get(id) ?? []) {
                if (seen.has(next)) { continue; }
                if (changed[next]) { hit = true; break; }
                seen.add(next); queue.push(next);
            }
        }
        if (hit) { affected.push(entry.id); }
    }
    return { graph, changed, removed, affected };
}
