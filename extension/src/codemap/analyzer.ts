/**
 * ReCoder — Code Map analyzer (구조 지도)  — 확장 내부 실행, 서버 불필요.
 *
 * code_map.py 의 TS 포팅. LLM·AWS·Core 없이 파일만 읽어 정적 분석한다.
 *   - Python: 정규식 + 들여쓰기 기반 (ast 없이 근사).
 *   - JS/TS/HTML/CSS: 정규식 + 중괄호 매칭.
 *
 * 선은 사실(import/호출/include), 색·위치는 해석(고립/과부하/계층).
 */
import * as fs from 'fs';
import * as path from 'path';

const OVERLOAD_IMPORTS = 3;
const OVERLOAD_CALLS = 3;
const MAX_FILE_BYTES = 1_500_000;
const MAX_FILES = 4000;

const SKIP_DIRS = new Set([
    '.git', '__pycache__', '.venv', 'venv', 'env', 'node_modules',
    'build', 'dist', '.aws-sam', 'site-packages', '.mypy_cache',
    '.pytest_cache', '.idea', '.vscode', 'migrations',
    'out', '.next', 'target', 'coverage', 'vendor', '.tox', '__pypackages__',
]);
const PY_EXT = ['.py'];
const WEB_EXT = ['.js', '.mjs', '.jsx', '.ts', '.tsx', '.html', '.htm', '.css'];
const ALL_EXT = [...PY_EXT, ...WEB_EXT];

const ENTRY_NAMES = new Set([
    'main.py', 'app.py', 'server.py', '__main__.py', 'manage.py',
    'wsgi.py', 'asgi.py', 'index.py', 'run.py',
]);
const DATA_HINTS = ['repository', 'repo', 'models', 'model', 'db', 'database', 'dao', 'store', 'schema', 'entities', 'orm'];
const SERVICE_HINTS = ['service', 'handler', 'controller', 'usecase', 'use_case', 'logic', 'manager', 'agent', 'router', 'routes', 'views', 'api', 'core'];

const JS_KEYWORDS = new Set([
    'if', 'for', 'while', 'switch', 'catch', 'function', 'return', 'else', 'do',
    'with', 'typeof', 'new', 'delete', 'void', 'in', 'of', 'instanceof', 'await',
    'yield', 'case', 'default', 'const', 'let', 'var', 'class', 'extends',
    'super', 'this', 'true', 'false', 'null', 'undefined', 'throw', 'try',
    'finally', 'break', 'continue',
]);

export interface MapNode {
    id: string; name: string; module?: string; cls?: string | null;
    layer?: string; in_degree: number; out_degree: number; flags: string[];
}
export interface MapEdge { from: string; to: string; }
export interface Finding { severity: string; kind: string; node: string; title: string; detail: string; fix: string; }
export interface ProjectResult { kind: 'project'; root: string; files_scanned: number; nodes: MapNode[]; edges: MapEdge[]; findings: Finding[]; }
export interface FileResult { kind: 'file'; path: string; name: string; functions_scanned: number; nodes: MapNode[]; edges: MapEdge[]; findings: Finding[]; }

interface DefMeta { name: string; cls: string | null; }

function iterFiles(root: string, exts: string[], out: string[]): void {
    let entries: fs.Dirent[];
    try { entries = fs.readdirSync(root, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
        if (out.length >= MAX_FILES) { return; }
        const full = path.join(root, e.name);
        if (e.isDirectory()) {
            if (SKIP_DIRS.has(e.name) || e.name.startsWith('.')) { continue; }
            iterFiles(full, exts, out);
        } else if (e.isFile()) {
            const low = e.name.toLowerCase();
            if (exts.some((x) => low.endsWith(x))) { out.push(full); }
        }
    }
}

function readText(p: string): string | null {
    try {
        const st = fs.statSync(p);
        if (st.size > MAX_FILE_BYTES) { return null; }
        return fs.readFileSync(p, 'utf-8');
    } catch { return null; }
}

function toPosix(p: string): string { return p.split(path.sep).join('/'); }

function moduleName(relPosix: string): string {
    let parts = relPosix.replace(/\.py$/i, '').split('/');
    if (parts.length && parts[parts.length - 1] === '__init__') { parts = parts.slice(0, -1); }
    return parts.join('.');
}

function layerOf(filename: string): string {
    const low = filename.toLowerCase();
    if (low.endsWith('.html') || low.endsWith('.htm')) { return 'entry'; }
    if (/\.(js|mjs|jsx|ts|tsx)$/.test(low)) { return 'service'; }
    if (low.endsWith('.css')) { return 'data'; }
    if (ENTRY_NAMES.has(filename)) { return 'entry'; }
    const stem = low.endsWith('.py') ? low.slice(0, -3) : low;
    if (DATA_HINTS.some((h) => stem.includes(h))) { return 'data'; }
    if (SERVICE_HINTS.some((h) => stem.includes(h))) { return 'service'; }
    return 'other';
}

function stripJs(src: string): string {
    const out: string[] = []; let i = 0; const n = src.length;
    while (i < n) {
        const two = src.substr(i, 2); const c = src[i];
        if (two === '//') { let j = src.indexOf('\n', i); if (j < 0) { j = n; } out.push(' '.repeat(j - i)); i = j; continue; }
        if (two === '/*') { let j = src.indexOf('*/', i + 2); j = j < 0 ? n : j + 2; for (let k = i; k < j; k++) { out.push(src[k] === '\n' ? '\n' : ' '); } i = j; continue; }
        if (c === '"' || c === "'" || c === '`') {
            const q = c; let j = i + 1;
            while (j < n) { if (src[j] === '\\') { j += 2; continue; } if (src[j] === q) { j++; break; } j++; }
            for (let k = i; k < j; k++) { out.push(src[k] === '\n' ? '\n' : ' '); } i = j; continue;
        }
        out.push(c); i++;
    }
    return out.join('');
}

function stripPy(src: string): string {
    const out: string[] = []; let i = 0; const n = src.length;
    while (i < n) {
        const c = src[i];
        if (c === '#') { let j = src.indexOf('\n', i); if (j < 0) { j = n; } out.push(' '.repeat(j - i)); i = j; continue; }
        if (c === '"' || c === "'") {
            const triple = src.substr(i, 3);
            if (triple === "'''" || triple === '"""') {
                let j = src.indexOf(triple, i + 3); j = j < 0 ? n : j + 3;
                for (let k = i; k < j; k++) { out.push(src[k] === '\n' ? '\n' : ' '); } i = j; continue;
            }
            const q = c; let j = i + 1;
            while (j < n) { if (src[j] === '\\') { j += 2; continue; } if (src[j] === q) { j++; break; } if (src[j] === '\n') { break; } j++; }
            for (let k = i; k < j; k++) { out.push(src[k] === '\n' ? '\n' : ' '); } i = j; continue;
        }
        out.push(c); i++;
    }
    return out.join('');
}

function collectImportsPy(src: string, selfMod: string): Set<string> {
    const out = new Set<string>();
    const pkgParts = selfMod.split('.').slice(0, -1);
    const s = stripPy(src);
    for (const raw of s.split(/\r?\n/)) {
        const line = raw.trim();
        let m = line.match(/^import\s+(.+)$/);
        if (m) {
            for (const part of m[1].split(',')) {
                const name = part.trim().split(/\s+as\s+/)[0].trim();
                if (name) { out.add(name); out.add(name.split('.')[0]); }
            }
            continue;
        }
        m = line.match(/^from\s+(\.*)([\w.]*)\s+import\s+(.+)$/);
        if (m) {
            const dots = m[1].length; const mod = m[2] || '';
            let baseParts: string[] = [];
            if (dots > 0) { baseParts = pkgParts.slice(0, pkgParts.length - (dots - 1)).filter(Boolean); }
            const base = [...baseParts, ...(mod ? [mod] : [])].join('.');
            if (base) { out.add(base); }
            for (const nm of m[3].split(',')) {
                const name = nm.trim().split(/\s+as\s+/)[0].trim().replace(/[()]/g, '');
                if (!name || name === '*') { continue; }
                if (base) { out.add(`${base}.${name}`); } else { out.add(name); }
            }
        }
    }
    return out;
}

function analyzePy(src: string): { defs: Map<string, DefMeta>; edges: Array<[string, string]> } {
    const s = stripPy(src);
    const lines = s.split(/\r?\n/);
    const defList: Array<{ name: string; indent: number; start: number; end: number; cls: string | null }> = [];
    const classStack: Array<{ name: string; indent: number }> = [];
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (!line.trim()) { continue; }
        const indent = (line.match(/^(\s*)/) as RegExpMatchArray)[1].length;
        while (classStack.length && indent <= classStack[classStack.length - 1].indent) { classStack.pop(); }
        const mc = line.match(/^(\s*)class\s+([A-Za-z_]\w*)/);
        if (mc) { classStack.push({ name: mc[2], indent: mc[1].length }); continue; }
        const md = line.match(/^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(/);
        if (md) {
            const cls = classStack.length ? classStack[classStack.length - 1].name : null;
            defList.push({ name: md[2], indent: md[1].length, start: i, end: lines.length, cls });
        }
    }
    for (const d of defList) {
        for (let j = d.start + 1; j < lines.length; j++) {
            const l = lines[j];
            if (!l.trim()) { continue; }
            const ind = (l.match(/^(\s*)/) as RegExpMatchArray)[1].length;
            if (ind <= d.indent) { d.end = j; break; }
        }
    }
    const names = new Set(defList.map((d) => d.name));
    const defs = new Map<string, DefMeta>();
    for (const d of defList) { if (!defs.has(d.name)) { defs.set(d.name, { name: d.name, cls: d.cls }); } }
    const edgeSet = new Set<string>();
    for (const d of defList) {
        const body = lines.slice(d.start + 1, d.end).join('\n');
        const re = /(?:self\s*\.\s*|\b)([A-Za-z_]\w*)\s*\(/g; let m: RegExpExecArray | null;
        while ((m = re.exec(body))) {
            const callee = m[1];
            if (names.has(callee) && callee !== d.name) { edgeSet.add(d.name + ' ' + callee); }
        }
    }
    return { defs, edges: [...edgeSet].map((e) => e.split(' ') as [string, string]) };
}

function bodySpan(s: string, start: number): [number, number] | null {
    let i = start; const n = s.length;
    while (i < n && s[i] !== '{' && s[i] !== ';') { i++; }
    if (i >= n || s[i] === ';') { return null; }
    let depth = 0; let j = i;
    while (j < n) {
        if (s[j] === '{') { depth++; } else if (s[j] === '}') { depth--; if (depth === 0) { return [i + 1, j]; } }
        j++;
    }
    return [i + 1, n];
}

function jsDefs(stripped: string): Array<[string, number]> {
    const defs: Array<[string, number]> = [];
    let m: RegExpExecArray | null;
    let re = /\bfunction\s+([A-Za-z_$][\w$]*)\s*\(/g;
    while ((m = re.exec(stripped))) { defs.push([m[1], re.lastIndex]); }
    re = /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^()]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)/g;
    while ((m = re.exec(stripped))) { defs.push([m[1], re.lastIndex]); }
    re = /\b([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function\b/g;
    while ((m = re.exec(stripped))) { defs.push([m[1], re.lastIndex]); }
    re = /(?:^|[{},;)])\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*\{/g;
    while ((m = re.exec(stripped))) {
        const name = m[1];
        if (JS_KEYWORDS.has(name)) { continue; }
        defs.push([name, m.index + m[0].indexOf(name)]);
    }
    return defs;
}

function analyzeJs(src: string): { defs: Map<string, DefMeta>; edges: Array<[string, string]> } {
    const s = stripJs(src);
    const raw = jsDefs(s);
    const names = new Set(raw.map((r) => r[0]));
    const defs = new Map<string, DefMeta>();
    for (const [nm] of raw) { if (!defs.has(nm)) { defs.set(nm, { name: nm, cls: null }); } }
    const edgeSet = new Set<string>();
    for (const [nm, start] of raw) {
        const span = bodySpan(s, start);
        if (!span) { continue; }
        const body = s.slice(span[0], span[1]);
        const re = /(?:\.|\b)([A-Za-z_$][\w$]*)\s*\(/g; let m: RegExpExecArray | null;
        while ((m = re.exec(body))) {
            const callee = m[1];
            if (names.has(callee) && callee !== nm && !JS_KEYWORDS.has(callee)) { edgeSet.add(nm + ' ' + callee); }
        }
    }
    return { defs, edges: [...edgeSet].map((e) => e.split(' ') as [string, string]) };
}

function extractInlineJs(html: string): string {
    const blocks: string[] = [];
    const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi; let m: RegExpExecArray | null;
    while ((m = re.exec(html))) {
        if (/\bsrc\s*=/i.test(m[1] || '')) { continue; }
        blocks.push(m[2]);
    }
    return blocks.join('\n');
}

function htmlRefs(html: string): string[] {
    const refs: string[] = []; let m: RegExpExecArray | null;
    let re = /<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["']/gi;
    while ((m = re.exec(html))) { refs.push(m[1]); }
    re = /<link\b[^>]*\bhref\s*=\s*["']([^"']+)["']/gi;
    while ((m = re.exec(html))) { refs.push(m[1]); }
    return refs;
}

function jsImportRefs(src: string): string[] {
    const s = stripJs(src); const refs: string[] = []; let m: RegExpExecArray | null;
    let re = /\bimport\b[^;]*?\bfrom\s*["']([^"']+)["']/g;
    while ((m = re.exec(s))) { refs.push(m[1]); }
    re = /\brequire\s*\(\s*["']([^"']+)["']\s*\)/g;
    while ((m = re.exec(s))) { refs.push(m[1]); }
    re = /\bimport\s*\(\s*["']([^"']+)["']\s*\)/g;
    while ((m = re.exec(s))) { refs.push(m[1]); }
    return refs;
}

function resolveRef(importerId: string, ref: string, idSet: Set<string>): string | null {
    ref = ref.split('?')[0].split('#')[0].trim();
    if (!ref || /^(https?:)?\/\//.test(ref) || ref.startsWith('data:')) { return null; }
    const base = importerId.includes('/') ? importerId.slice(0, importerId.lastIndexOf('/')) : '';
    const cand = toPosix(path.posix.normalize(path.posix.join(base, ref)));
    if (idSet.has(cand)) { return cand; }
    for (const ext of ['.js', '.mjs', '.jsx', '.ts', '.tsx', '/index.js']) {
        if (idSet.has(cand + ext)) { return cand + ext; }
    }
    return null;
}

function finalizeFile(defs: Map<string, DefMeta>, rawEdges: Array<[string, string]>, p: string, name: string): FileResult {
    const edges: MapEdge[] = []; const seen = new Set<string>();
    const inDeg = new Map<string, number>(); const outDeg = new Map<string, number>();
    for (const id of defs.keys()) { inDeg.set(id, 0); outDeg.set(id, 0); }
    for (const [caller, callee] of rawEdges) {
        if (!defs.has(caller) || !defs.has(callee) || caller === callee) { continue; }
        const key = caller + ' ' + callee;
        if (seen.has(key)) { continue; }
        seen.add(key);
        edges.push({ from: caller, to: callee });
        outDeg.set(caller, (outDeg.get(caller) || 0) + 1);
        inDeg.set(callee, (inDeg.get(callee) || 0) + 1);
    }
    const nodes: MapNode[] = [];
    for (const [id, meta] of defs) {
        const flags: string[] = []; const ind = inDeg.get(id) || 0;
        if (ind >= OVERLOAD_CALLS) { flags.push('overloaded'); }
        if (ind === 0 && meta.name.startsWith('_') && !meta.name.startsWith('__')) { flags.push('orphan'); }
        nodes.push({ id, name: meta.name, cls: meta.cls, in_degree: ind, out_degree: outDeg.get(id) || 0, flags });
    }
    const findings: Finding[] = [];
    for (const n of [...nodes].sort((a, b) => b.in_degree - a.in_degree)) {
        if (n.flags.includes('overloaded')) {
            const callers = edges.filter((e) => e.to === n.id).map((e) => e.from);
            const who = callers.slice(0, 4).join(', ') + (callers.length > 4 ? '…' : '');
            findings.push({
                severity: 'warn', kind: 'overloaded', node: n.id,
                title: `과부하 함수 — ${n.name}()`,
                detail: `${n.in_degree}곳(${who})에서 이 하나로 호출이 몰립니다. 여기서 버그가 나면 호출하는 기능이 동시에 깨집니다.`,
                fix: '공통/개별 책임을 분리하거나 기능별로 쪼개는 것을 검토',
            });
        }
    }
    for (const n of nodes) {
        if (n.flags.includes('orphan')) {
            findings.push({
                severity: 'bad', kind: 'orphan', node: n.id,
                title: `미사용 내부 함수 — ${n.name}()`,
                detail: '이 파일 안에서 아무도 호출하지 않는 비공개 함수입니다. 죽은 코드일 가능성이 높습니다.',
                fix: '삭제 후보 · 또는 호출 누락 확인',
            });
        }
    }
    return { kind: 'file', path: p, name, functions_scanned: nodes.length, nodes, edges, findings };
}

export function analyzeFile(p: string): FileResult {
    if (!fs.existsSync(p)) { throw new Error(`파일 없음: ${p}`); }
    const src = readText(p);
    const name = path.basename(p);
    if (src === null) { throw new Error(`읽기 실패(너무 큰 파일일 수 있음): ${name}`); }
    const ext = path.extname(p).toLowerCase();
    if (ext === '.py') {
        const r = analyzePy(src);
        return finalizeFile(r.defs, r.edges, p, name);
    }
    if (ext === '.html' || ext === '.htm') {
        const js = extractInlineJs(src);
        const r = js.trim() ? analyzeJs(js) : { defs: new Map<string, DefMeta>(), edges: [] as Array<[string, string]> };
        return finalizeFile(r.defs, r.edges, p, name);
    }
    if (['.js', '.mjs', '.jsx', '.ts', '.tsx'].includes(ext)) {
        const r = analyzeJs(src);
        return finalizeFile(r.defs, r.edges, p, name);
    }
    return finalizeFile(new Map(), [], p, name);
}

export function analyzeProject(root: string): ProjectResult {
    if (!fs.existsSync(root)) { throw new Error(`경로 없음: ${root}`); }
    const absFiles: string[] = [];
    iterFiles(root, ALL_EXT, absFiles);

    const files: Array<{ id: string; abs: string; ext: string; mod: string }> = [];
    const byModule = new Map<string, string>();
    const byTail = new Map<string, string[]>();
    const idSet = new Set<string>();
    for (const abs of absFiles) {
        const rel = toPosix(path.relative(root, abs));
        const ext = path.extname(abs).toLowerCase();
        const mod = ext === '.py' ? moduleName(rel) : '';
        files.push({ id: rel, abs, ext, mod });
        idSet.add(rel);
        if (mod) {
            byModule.set(mod, rel);
            const tail = mod.split('.').pop() as string;
            if (!byTail.has(tail)) { byTail.set(tail, []); }
            (byTail.get(tail) as string[]).push(rel);
        }
    }

    const edges: MapEdge[] = []; const seen = new Set<string>();
    const inDeg = new Map<string, number>(); const outDeg = new Map<string, number>();
    for (const f of files) { inDeg.set(f.id, 0); outDeg.set(f.id, 0); }
    const addEdge = (a: string, b: string) => {
        if (a === b) { return; }
        const key = a + ' ' + b;
        if (seen.has(key)) { return; }
        seen.add(key);
        edges.push({ from: a, to: b });
        outDeg.set(a, (outDeg.get(a) || 0) + 1);
        inDeg.set(b, (inDeg.get(b) || 0) + 1);
    };

    for (const f of files) {
        const src = readText(f.abs);
        if (src === null) { continue; }
        if (f.ext === '.py') {
            for (const t of collectImportsPy(src, f.mod)) {
                let dst = byModule.get(t) || null;
                if (!dst) {
                    const cands = byTail.get(t.split('.').pop() as string) || [];
                    dst = cands.length === 1 ? cands[0] : null;
                }
                if (dst) { addEdge(f.id, dst); }
            }
        } else if (f.ext === '.html' || f.ext === '.htm') {
            for (const ref of htmlRefs(src)) { const dst = resolveRef(f.id, ref, idSet); if (dst) { addEdge(f.id, dst); } }
        } else if (['.js', '.mjs', '.jsx', '.ts', '.tsx'].includes(f.ext)) {
            for (const ref of jsImportRefs(src)) { const dst = resolveRef(f.id, ref, idSet); if (dst) { addEdge(f.id, dst); } }
        }
    }

    const nodes: MapNode[] = [];
    for (const f of files) {
        const name = path.basename(f.id);
        const layer = layerOf(name);
        const flags: string[] = [];
        const ind = inDeg.get(f.id) || 0;
        const isEntry = layer === 'entry';
        const isPkgInit = name === '__init__.py';
        if (ind === 0 && !isEntry && !isPkgInit && files.length > 1) { flags.push('orphan'); }
        if (ind >= OVERLOAD_IMPORTS) { flags.push('overloaded'); }
        nodes.push({ id: f.id, name, module: f.mod, layer, in_degree: ind, out_degree: outDeg.get(f.id) || 0, flags });
    }

    const findings: Finding[] = [];
    for (const n of [...nodes].sort((a, b) => b.in_degree - a.in_degree)) {
        if (n.flags.includes('orphan')) {
            findings.push({
                severity: 'bad', kind: 'orphan', node: n.id,
                title: `고립된 파일 — ${n.name}`,
                detail: '어떤 파일도 이 파일을 import/include 하지 않습니다. 교체 후 연결이 끊긴 잔재이거나, 붙였어야 할 연결이 누락된 것일 수 있습니다.',
                fix: '삭제 후보 · 또는 연결 누락',
            });
        }
    }
    for (const n of [...nodes].sort((a, b) => b.in_degree - a.in_degree)) {
        if (n.flags.includes('overloaded')) {
            findings.push({
                severity: 'warn', kind: 'overloaded', node: n.id,
                title: `의존 집중 — ${n.name}`,
                detail: `${n.in_degree}개 파일이 이 파일에 의존합니다. 이 파일 하나가 바뀌면 그만큼 넓게 영향이 번집니다(단일 실패 지점).`,
                fix: '책임 분리 검토 — 너무 많은 역할이 한 곳에 몰렸는지 확인',
            });
        }
    }

    return { kind: 'project', root, files_scanned: files.length, nodes, edges, findings };
}
