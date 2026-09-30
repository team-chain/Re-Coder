/**
 * S3 정적 배포 전에 **프런트엔드 빌드**를 찾아 실행한다.
 *
 * 왜 필요한가 (2026-09-27 실기기)
 *   `client/` 에 Create React App 이 있는 쇼핑몰 프로젝트를 S3 로 배포하면, 루트에
 *   dist·build 가 없어 워크스페이스 루트를 통째로 훑고 `client/public/index.html`
 *   (빌드 전 템플릿, `<div id="root">` 만 있음)을 진입 문서로 올렸다. 배포는
 *   "성공" 인데 브라우저에는 제목만 있는 흰 화면이 떴다.
 *
 * 여기서 하는 일
 *   1. 루트와 바로 아래 폴더에서 빌드 도구(react-scripts·vite …)를 쓰는 프로젝트를 찾는다.
 *   2. 산출물이 없거나 소스보다 오래됐으면 `npm ci|install` → `npm run build` 를 실행한다.
 *      PC 에 npm 이 없으면 Docker(node 이미지)로 같은 빌드를 돌린다.
 *   3. 올릴 폴더를 그 산출물(client/build 등)로 정한다.
 */
import * as cp from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

export interface FrontendProject {
    /** 워크스페이스 기준 프로젝트 폴더('' 은 루트). */
    projectDir: string;
    /** 워크스페이스 기준 빌드 산출물 폴더. */
    outDir: string;
    tool: string;
    label: string;
    /** 화면이 API 서버를 부르는 흔적 — S3 에서는 그 기능이 동작하지 않는다. */
    callsApi: boolean;
    /** 화면을 빌드하는 npm 스크립트 이름(보통 build, 없으면 build:client 같은 이름). */
    script?: string;
}

const TOOLS: Array<{ pattern: RegExp; tool: string; label: string; out: string }> = [
    { pattern: /\breact-scripts\s+build\b/, tool: 'react-scripts', label: 'Create React App', out: 'build' },
    { pattern: /\bvite(?:\s+build\b|\s*$|\s+--)/, tool: 'vite', label: 'Vite', out: 'dist' },
    { pattern: /\bvue-cli-service\s+build\b/, tool: 'vue-cli', label: 'Vue CLI', out: 'dist' },
    { pattern: /\bparcel\s+build\b/, tool: 'parcel', label: 'Parcel', out: 'dist' },
    { pattern: /\bastro\s+build\b/, tool: 'astro', label: 'Astro', out: 'dist' },
    { pattern: /\bgatsby\s+build\b/, tool: 'gatsby', label: 'Gatsby', out: 'public' },
];

/** 흔한 프런트엔드 폴더 이름 — 앞쪽이 우선. */
const FRONTEND_DIRS = ['client', 'frontend', 'web', 'ui', 'app', 'site', 'www', 'webapp'];
const SKIP = new Set(['node_modules', '.git', '.recoder', '.vscode', 'dist', 'build', 'out', 'coverage', 'docs']);

function readJson(file: string): Record<string, unknown> | null {
    try {
        const parsed = JSON.parse(fs.readFileSync(file, 'utf-8'));
        return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null;
    } catch {
        return null;
    }
}

function viteOutDir(projectAbs: string): string | null {
    for (const name of ['vite.config.ts', 'vite.config.js', 'vite.config.mjs', 'vite.config.mts', 'vite.config.cjs']) {
        try {
            const text = fs.readFileSync(path.join(projectAbs, name), 'utf-8');
            const m = /\boutDir\s*:\s*['"]([^'"]+)['"]/.exec(text);
            if (m) { return m[1].replace(/^\.\//, ''); }
        } catch { /* 설정 파일이 없으면 기본값 */ }
    }
    return null;
}

function callsApi(projectAbs: string, pkg: Record<string, unknown>): boolean {
    if (typeof pkg.proxy === 'string' && pkg.proxy) { return true; }
    const src = path.join(projectAbs, 'src');
    let seen = 0;
    const walk = (dir: string): boolean => {
        let entries: fs.Dirent[];
        try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return false; }
        for (const entry of entries) {
            if (seen > 400) { return false; }
            const full = path.join(dir, entry.name);
            if (entry.isDirectory()) {
                if (!SKIP.has(entry.name) && walk(full)) { return true; }
            } else if (/\.(jsx?|tsx?|vue|svelte)$/.test(entry.name)) {
                seen++;
                try {
                    const text = fs.readFileSync(full, 'utf-8');
                    if (/['"`](?:https?:\/\/(?:localhost|127\.0\.0\.1)[^'"`]*|\/api\/[^'"`]*)['"`]/.test(text)) { return true; }
                } catch { /* 읽지 못한 파일은 건너뛴다 */ }
            }
        }
        return false;
    };
    return walk(src);
}

function inspect(workspace: string, rel: string): FrontendProject | null {
    const projectAbs = path.join(workspace, rel);
    const pkg = readJson(path.join(projectAbs, 'package.json'));
    const scripts = pkg && typeof pkg.scripts === 'object' && pkg.scripts ? pkg.scripts as Record<string, unknown> : null;
    if (!pkg || !scripts) { return null; }
    //: build 가 먼저다. 없거나 화면 빌드가 아니면 build:client·build:web 처럼 화면을 직접 빌드하는
    //: 스크립트를 쓴다(실기기: build:client 만 있는 프로젝트가 빌드되지 않아 화면이 비었다).
    //: vite 는 `vite build` 일 때만 — 이름이 build: 로 시작해도 `vite`(개발 서버)는 빌드가 아니다.
    const names = ['build', ...Object.keys(scripts).filter(n => n.startsWith('build:')).sort()];
    let script = '';
    let found: typeof TOOLS[number] | undefined;
    for (const name of names) {
        const command = typeof scripts[name] === 'string' ? scripts[name] as string : '';
        if (!command) { continue; }
        const tool = TOOLS.find(t => t.pattern.test(command) && (t.tool !== 'vite' || name === 'build' || /\bvite\s+build\b/.test(command)));
        if (tool) { script = name; found = tool; break; }
    }
    if (!found) { return null; }
    const out = (found.tool === 'vite' ? viteOutDir(projectAbs) : null) || found.out;
    return {
        projectDir: rel,
        outDir: path.posix.join(rel.replace(/\\/g, '/'), out),
        tool: found.tool,
        label: found.label,
        callsApi: callsApi(projectAbs, pkg),
        script,
    };
}

/** 워크스페이스에서 정적 사이트로 빌드되는 프런트엔드 프로젝트를 찾는다. 없으면 null. */
export function detectFrontendProject(workspace: string): FrontendProject | null {
    const root = inspect(workspace, '');
    if (root) { return root; }
    let dirs: string[] = [];
    try {
        dirs = fs.readdirSync(workspace, { withFileTypes: true })
            .filter(e => e.isDirectory() && !SKIP.has(e.name) && !e.name.startsWith('.'))
            .map(e => e.name);
    } catch { return null; }
    const ordered = [...FRONTEND_DIRS.filter(d => dirs.includes(d)), ...dirs.filter(d => !FRONTEND_DIRS.includes(d)).sort()];
    for (const dir of ordered) {
        const found = inspect(workspace, dir);
        if (found) { return found; }
    }
    return null;
}

function newestMtime(target: string, budget = { left: 3000 }): number {
    let newest = 0;
    let stat: fs.Stats;
    try { stat = fs.statSync(target); } catch { return 0; }
    if (!stat.isDirectory()) { return stat.mtimeMs; }
    let entries: fs.Dirent[];
    try { entries = fs.readdirSync(target, { withFileTypes: true }); } catch { return 0; }
    for (const entry of entries) {
        if (budget.left-- <= 0) { break; }
        if (entry.isDirectory() && SKIP.has(entry.name)) { continue; }
        newest = Math.max(newest, newestMtime(path.join(target, entry.name), budget));
    }
    return newest;
}

/** 산출물이 없거나(또는 index.html 이 없거나) 소스보다 오래됐으면 true. */
export function needsBuild(workspace: string, project: FrontendProject): boolean {
    const index = path.join(workspace, project.outDir, 'index.html');
    let built = 0;
    try { built = fs.statSync(index).mtimeMs; } catch { return true; }
    const projectAbs = path.join(workspace, project.projectDir);
    const sources = ['src', 'public', 'index.html', 'package.json', 'vite.config.js', 'vite.config.ts']
        .filter(name => !(project.tool === 'gatsby' && name === 'public'))
        .map(name => newestMtime(path.join(projectAbs, name)));
    return Math.max(0, ...sources) > built;
}

export interface BuildOutcome { ok: boolean; via: 'npm' | 'docker'; output: string }

type Runner = (command: string, cwd: string, env: NodeJS.ProcessEnv, timeoutMs: number) => Promise<{ code: number | null; output: string }>;

const run: Runner = (command, cwd, env, timeoutMs) => new Promise(resolve => {
    const child = cp.spawn(command, { cwd, env, shell: true, windowsHide: true });
    let output = '';
    const keep = (chunk: Buffer) => { output = (output + chunk.toString('utf-8')).slice(-20000); };
    child.stdout?.on('data', keep);
    child.stderr?.on('data', keep);
    const timer = setTimeout(() => { output += `\n시간 초과(${Math.round(timeoutMs / 60000)}분)로 중단했습니다.`; child.kill(); }, timeoutMs);
    child.on('error', err => { clearTimeout(timer); resolve({ code: -1, output: `${output}\n${err.message}` }); });
    child.on('close', code => { clearTimeout(timer); resolve({ code, output }); });
});

/**
 * 프런트엔드를 빌드한다. PC 의 npm 을 먼저 쓰고, 없으면 Docker 로 돌린다.
 * 소스맵은 만들지 않는다 — 공개 버킷에 소스 코드가 그대로 노출된다.
 */
export async function buildFrontend(
    workspace: string,
    project: FrontendProject,
    onProgress: (message: string) => void,
    runner: Runner = run,
): Promise<BuildOutcome> {
    const cwd = path.join(workspace, project.projectDir);
    const env: NodeJS.ProcessEnv = { ...process.env, GENERATE_SOURCEMAP: 'false', CI: 'false', BROWSER: 'none' };
    delete env.NODE_ENV; // NODE_ENV=production 이면 devDependencies(빌드 도구)가 설치되지 않는다
    const hasLock = fs.existsSync(path.join(cwd, 'package-lock.json'));
    const where = project.projectDir || '프로젝트 루트';
    const npm = await runner('npm --version', cwd, env, 30_000);
    if (npm.code === 0) {
        onProgress(`${where} 의존성 설치 중 (npm ${hasLock ? 'ci' : 'install'}) — 처음에는 몇 분 걸릴 수 있습니다`);
        let install = await runner(hasLock ? 'npm ci --no-audit --no-fund' : 'npm install --no-audit --no-fund', cwd, env, 15 * 60_000);
        if (install.code !== 0 && hasLock) {
            //: lock 파일이 package.json 과 어긋나 있으면 npm ci 가 거부한다 — 일반 설치로 한 번 더.
            install = await runner('npm install --no-audit --no-fund', cwd, env, 15 * 60_000);
        }
        if (install.code !== 0) { return { ok: false, via: 'npm', output: install.output }; }
        onProgress(`${where} 빌드 중 (${project.label})`);
        const build = await runner(`npm run ${buildScript(project)}`, cwd, env, 15 * 60_000);
        return { ok: build.code === 0, via: 'npm', output: build.output };
    }
    const docker = await runner('docker info --format "{{.ServerVersion}}"', cwd, env, 30_000);
    if (docker.code !== 0) {
        return {
            ok: false, via: 'npm',
            output: `PC 에서 npm 도 Docker 도 찾지 못해 ${where} 를 빌드할 수 없습니다. Node.js 를 설치하거나 Docker Desktop 을 켠 뒤 다시 시도하세요.`,
        };
    }
    onProgress(`${where} 빌드 중 (Docker · ${project.label}) — 처음에는 몇 분 걸릴 수 있습니다`);
    //: node_modules 는 컨테이너 안에만 둔다(익명 볼륨). 리눅스용 바이너리가 사용자 폴더에 섞이면
    //: PC 에서 개발 서버를 띄울 때 깨진다.
    const mount = cwd.replace(/"/g, '');
    const script = `if [ -f package-lock.json ]; then npm ci --no-audit --no-fund || npm install --no-audit --no-fund; else npm install --no-audit --no-fund; fi && npm run ${buildScript(project)}`;
    const result = await runner(
        `docker run --rm -v "${mount}:/app" -v /app/node_modules -w /app -e GENERATE_SOURCEMAP=false -e CI=false node:22-alpine sh -c "${script}"`,
        cwd, env, 25 * 60_000);
    return { ok: result.code === 0, via: 'docker', output: result.output };
}

/** 실행할 빌드 스크립트 이름. 셸에 그대로 들어가므로 안전한 글자만 허용한다. */
function buildScript(project: FrontendProject): string {
    return project.script && /^[\w:.-]+$/.test(project.script) ? project.script : 'build';
}

/** 빌드 실패 출력에서 사람이 볼 줄만 추린다. */
export function buildFailureSummary(output: string): string {
    const lines = output.split(/\r?\n/).map(l => l.trimEnd()).filter(Boolean);
    const errors = lines.filter(l => /error|ERR!|failed|cannot|not found|ETARGET|Module not found/i.test(l));
    return (errors.length ? errors : lines).slice(-8).join('\n');
}
