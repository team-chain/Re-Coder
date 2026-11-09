import {spawn} from 'child_process';
import {randomBytes} from 'crypto';
import {promises as fs} from 'fs';
import * as path from 'path';
import {PentagiClient, PentagiFlow, PentagiTask, PentagiEvidence, pentagiOutcome, testTarget} from './pentagiClient';

// Official Docker Hub index digests; updates require an integration check against the upstream release.
export const PENTAGI_IMAGES = {
    app: 'vxcontrol/pentagi@sha256:0abdf8e9e9e19839dccc5d2ec365060e5c41941588279897cabd628d29a9afd7',
    db: 'vxcontrol/pgvector@sha256:09afd689127bdb42319a133b2ef7cd4318b7ebda58412644975d6eacd2367fb0',
    engine: 'docker@sha256:686d2c5464787b0ca42420e0aa03a94fc9781ff9bf8b5ff947614b9fa4c3c3e0',
};
const OWNER = 'io.recoder.redteam.session';
export type LocalProvider = 'openai'|'anthropic';
export interface ManagedPentagiOptions {
    bedrockEnv: Record<string,string>; configPath: string; workerImage: string;
    prepareTarget(engine: string): Promise<void>;
}
export type LocalPhase = 'preparing'|'running'|'saving'|'cleaning'|'removed'|'cleanup_failed';
export interface LocalReport {
    session: string; workspace: string; target: string; createdAt: string; updatedAt: string;
    phase: LocalPhase; step: string; pid?: number; server?: string; provider?: LocalProvider;
    outcome?: 'finished'|'failed'|'cancelled'|'timed_out'|'interrupted'|'needs_input'; error?: string;
    flow?: PentagiFlow; tasks?: PentagiTask[]; evidence?:PentagiEvidence[];
}
export interface DockerOptions {env?: Record<string,string>; input?: string; timeout?: number; signal?: AbortSignal;}
export type DockerRun = (args: string[], options?: DockerOptions) => Promise<string>;
export const docker: DockerRun = (args, options = {}) => new Promise((resolve, reject) => {
    const child = spawn('docker', args, {windowsHide: true, shell: false, env: {...process.env, ...options.env}, stdio: ['pipe','pipe','pipe'], signal:options.signal});
    let output = '', done = false;
    const finish = (error?: Error) => {if (done) return; done = true; clearTimeout(timer); error ? reject(error) : resolve(output.trim());};
    const timer = setTimeout(() => {child.kill(); finish(new Error('Docker 작업 시간이 초과되었습니다. Docker Desktop 상태와 인터넷 연결을 확인하세요.'));}, options.timeout ?? 60000);
    child.stdout.on('data', data => {output += data.toString(); if (output.length > 4 * 1024 * 1024) {child.kill(); finish(new Error('Docker 응답이 너무 큽니다.'));}});
    // Never surface stderr: Docker errors can contain environment values or SQL input.
    child.stderr.resume();
    child.on('error', () => finish(new Error('Docker를 실행하지 못했습니다. Docker Desktop을 설치하고 Linux 컨테이너를 시작하세요.')));
    child.on('close', code => finish(code === 0 ? undefined : new Error(`Docker ${args[0]} 작업 실패 (${code ?? '종료'}). Docker Desktop의 해당 검사 환경을 확인하세요.`)));
    child.stdin.on('error', () => {}); child.stdin.end(options.input || '');
});
export function localTarget(input: string): string {
    const u = new URL(testTarget(input));
    if (!['localhost','127.0.0.1','[::1]','host.docker.internal'].includes(u.hostname) || !u.port) throw new Error('로컬 테스트 앱 주소와 포트를 입력하세요. 예: http://localhost:3000');
    u.hostname = 'host.docker.internal'; return u.toString();
}
export function sessionPaths(root: string, session: string) {
    if (!/^rc-rt-[a-f0-9]{24}$/.test(session)) throw new Error('검사 환경 식별자가 올바르지 않습니다.');
    const dir = path.join(path.resolve(root), session);
    return {dir, report: path.join(dir, 'report.json'), cancel: path.join(dir, 'cancel')};
}
export async function saveReport(root: string, report: LocalReport) {
    const p = sessionPaths(root, report.session); await fs.mkdir(p.dir, {recursive:true});
    report.updatedAt = new Date().toISOString();
    await fs.writeFile(p.report + '.tmp', JSON.stringify(report), {mode:0o600});
    try { await fs.rename(p.report + '.tmp', p.report); }
    catch (error) {
        // Windows does not replace an existing destination with rename(). Keep the complete
        // journal temp as a recovery copy if copying to the canonical path is interrupted.
        const code = (error as NodeJS.ErrnoException).code;
        if (process.platform !== 'win32' || !['EPERM','EEXIST'].includes(code || '')) throw error;
        await fs.copyFile(p.report + '.tmp', p.report);
        await fs.unlink(p.report + '.tmp');
    }
}
export async function readReport(root: string, session: string): Promise<LocalReport> {
    const p = sessionPaths(root, session);
    let data: LocalReport;
    try { data = JSON.parse(await fs.readFile(p.report, 'utf8')); }
    catch {
        // A completed temp file is the recovery point for a Windows process exit during copy.
        data = JSON.parse(await fs.readFile(p.report + '.tmp', 'utf8'));
    }
    if (data.session !== session || !['preparing','running','saving','cleaning','removed','cleanup_failed'].includes(data.phase)) throw new Error('검사 기록 형식이 올바르지 않습니다.');
    return data;
}
export function redactReport(report: LocalReport, secrets: string[]): LocalReport {
    const redact=(value:unknown):unknown=>{
        if(typeof value==='string') {let cleaned=value;for(const secret of secrets) if(secret)cleaned=cleaned.split(secret).join('[REDACTED]');return cleaned;}
        if(Array.isArray(value))return value.map(redact);
        if(value && typeof value==='object')return Object.fromEntries(Object.entries(value).map(([key,item])=>[key,redact(item)]));
        return value;
    };
    return redact(report) as LocalReport;
}
export class LocalPentagiRuntime {
    constructor(readonly session: string, private run: DockerRun = docker) {sessionPaths('.', session);}
    private name(suffix: string) {return `${this.session}-${suffix}`;}
    private labels() {return ['--label', `${OWNER}=${this.session}`];}
    private async owned(kind: string, name: string) {
        const ids = await this.run([kind, 'ls', '--filter', `label=${OWNER}=${this.session}`, '--format', '{{.Name}}']);
        return ids.split(/\r?\n/).includes(name);
    }
    async cleanup(): Promise<void> {
        // Only exact resources created by this session AND bearing its owner label.
        // Never compose down, prune, glob names, remove shared images or touch target containers.
        for (const suffix of ['app','bootstrap','engine','db']) {
            const name = this.name(suffix);
            const names = await this.run(['container','ls','-a','--filter',`label=${OWNER}=${this.session}`,'--format','{{.Names}}']);
            if (names.split(/\r?\n/).includes(name)) await this.run(['container','rm','-f','-v',name]);
        }
        for (const suffix of ['engine-data','db-data','app-data']) {
            const name = this.name(suffix); if (await this.owned('volume',name)) await this.run(['volume','rm',name]);
        }
        for (const suffix of ['runtime','database']) {
            const name = this.name(suffix); if (await this.owned('network',name)) await this.run(['network','rm',name]);
        }
    }
    async prepare(apiKey: string, provider: LocalProvider|'bedrock', progress: (step: string) => Promise<void>, managed?: ManagedPentagiOptions): Promise<{server: string; token: string}> {
        if(provider==='bedrock' && !managed) throw new Error('검사 관리형 실행 설정이 필요합니다.');
        await progress('Docker 확인');
        const endpoint=process.env.DOCKER_HOST || await this.run(['context','inspect','--format','{{(index .Endpoints "docker").Host}}']);
        if (!/^(npipe:\/\/|unix:\/\/)/.test(endpoint)) throw new Error('로컬 Docker Desktop 컨텍스트를 선택하세요. 원격 Docker 엔진에서는 자동 검사를 실행하지 않습니다.');
        const info = await this.run(['info','--format','{{.OSType}}|{{.MemTotal}}|{{.NCPU}}']);
        const [os,memoryText,cpuText] = info.split('|');
        if (os !== 'linux') throw new Error('Docker Desktop을 Linux 컨테이너 모드로 전환하세요.');
        const memory = Number(memoryText), cpus = Number(cpuText);
        if (!Number.isFinite(memory) || !Number.isFinite(cpus)) throw new Error('Docker의 메모리·CPU 정보를 확인할 수 없습니다. Docker Desktop을 다시 시작하고 재확인하세요.');
        if (memory < 6 * 1024 ** 3 || cpus < 2) throw new Error('PentAGI 격리 환경은 Docker에 최소 메모리 6GB와 CPU 2개를 할당해야 합니다. Docker Desktop 리소스 설정을 늘린 뒤 다시 시도하세요.');
        for (const [name,image] of Object.entries(PENTAGI_IMAGES)) {
            await progress(`검사 환경 다운로드 · ${name}`);
            try {await this.run(['image','inspect',image,'--format','{{.Id}}']);}
            catch {await this.run(['pull',image], {timeout:20*60*1000});}
        }
        await progress('검사 전용 엔진과 DB 준비');
        await this.run(['network','create',...this.labels(),this.name('runtime')]);
        await this.run(['network','create','--internal',...this.labels(),this.name('database')]);
        for (const suffix of ['engine-data','db-data','app-data']) await this.run(['volume','create',...this.labels(),this.name(suffix)]);
        const dbPassword = randomBytes(24).toString('hex');
        const common = ['--detach','--restart=no','--log-opt','max-size=5m','--log-opt','max-file=2',...this.labels()];
        await this.run(['run',...common,'--name',this.name('db'),'--network',this.name('database'),'--network-alias','pgvector','--memory','768m','--pids-limit','256',
            '-e','POSTGRES_PASSWORD','-e','POSTGRES_DB=pentagidb','-v',`${this.name('db-data')}:/var/lib/postgresql/data`,PENTAGI_IMAGES.db], {env:{POSTGRES_PASSWORD:dbPassword}});
        await this.run(['run',...common,'--name',this.name('engine'),'--network',this.name('runtime'),'--network-alias','engine','--privileged','--memory','3g','--cpus','2','--pids-limit','2048',
            '-e','DOCKER_TLS_CERTDIR=','-v',`${this.name('engine-data')}:/var/lib/docker`,PENTAGI_IMAGES.engine,'dockerd','--host=unix:///var/run/docker.sock','--host=tcp://0.0.0.0:2375','--tls=false',...(managed?['--dns=1.1.1.1']:[])]);
        await this.until(async () => {await this.run(['exec',this.name('db'),'pg_isready','-U','postgres','-d','pentagidb']);}, progress, 'DB 시작 대기', 120000);
        await this.until(async () => {await this.run(['exec',this.name('engine'),'docker','info','--format','{{.ServerVersion}}']);}, progress, '검사 엔진 시작 대기', 120000);
        if(managed) await managed.prepareTarget(this.name('engine'));
        // No host Docker socket, no project directory mount. The nested engine owns all workers.
        const providerEnv:Record<string,string> = managed ? managed.bedrockEnv : provider === 'openai' ? {OPEN_AI_KEY:apiKey} : {ANTHROPIC_API_KEY:apiKey};
        const providerEnvArgs = Object.keys(providerEnv).flatMap(k=>['-e',k]);
        const env = {...providerEnv, DATABASE_URL:`postgres://postgres:${dbPassword}@pgvector:5432/pentagidb?sslmode=disable`, COOKIE_SIGNING_SALT:randomBytes(32).toString('hex')};
        const appArgs = ['--network',this.name('runtime'),'--memory','1536m','--cpus','2','--pids-limit','512','--user','root:root',
            ...providerEnvArgs,'-e','DATABASE_URL','-e','COOKIE_SIGNING_SALT','-e','DOCKER_HOST=tcp://engine:2375','-e','DOCKER_INSIDE=false','-e','DOCKER_NET_ADMIN=false',
            // "host" here is the dedicated DinD network namespace, NOT the user's host.
            // It preserves Docker Desktop's host.docker.internal DNS for nested workers.
            '-e',`DOCKER_NETWORK=${managed?'recoder-isolated':'host'}`,
            ...(managed?['-e',`DOCKER_DEFAULT_IMAGE=${managed.workerImage}`,'-e',`DOCKER_DEFAULT_IMAGE_FOR_PENTEST=${managed.workerImage}`,
                '-e',`DOCKER_ALLOWED_IMAGES=${managed.workerImage}`,'-e','DOCKER_IMAGE_SELECTION_MODE=fixed',
                '-e','BEDROCK_CONFIG_PATH=/opt/pentagi/recoder-bedrock.yml','--mount',`type=bind,src=${managed.configPath},dst=/opt/pentagi/recoder-bedrock.yml,readonly`,
                // Empty URLs remove the browser tool from upstream's advertised tools.
                // A dummy URL advertises a broken tool and can cause repeated agent failures.
                '-e','DUCKDUCKGO_ENABLED=false','-e','SPLOITUS_ENABLED=false','-e','SCRAPER_PRIVATE_URL=',
                '-e','SCRAPER_PUBLIC_URL=','-e','TERMINAL_TOOL_TIMEOUT=120','-e','HTTP_CLIENT_TIMEOUT=120',
                '-e','EMBEDDING_PROVIDER=bedrock','-e','EMBEDDING_MODEL=amazon.titan-embed-text-v2:0',
                '-e','EXECUTION_MONITOR_ENABLED=true','-e','EXECUTION_MONITOR_TOTAL_TOOL_LIMIT=80']:[]),
            '-e','SERVER_PORT=8443','-e','SERVER_USE_SSL=false','-e','CORS_ORIGINS=http://127.0.0.1','-e','MAX_GENERAL_AGENT_TOOL_CALLS=50','-e','MAX_LIMITED_AGENT_TOOL_CALLS=20',
            '-v',`${this.name('app-data')}:/opt/pentagi/data`];
        // Run migrations without any published port. Replace the default password before exposing API.
        await this.run(['create',...common.filter(a=>a!=='--detach'),'--name',this.name('bootstrap'),...appArgs,PENTAGI_IMAGES.app], {env});
        await this.run(['network','connect',this.name('database'),this.name('bootstrap')]);
        await this.run(['start',this.name('bootstrap')]);
        await this.until(async () => {
            const count = await this.sql("SELECT count(*) FROM users WHERE mail='admin@pentagi.com';");
            if (count.trim() !== '1') throw new Error();
        }, progress, 'PentAGI 초기화', 180000);
        await this.run(['stop','--time','15',this.name('bootstrap')]);
        const password = 'Rc!' + randomBytes(24).toString('hex');
        await this.sql(`CREATE EXTENSION IF NOT EXISTS pgcrypto; UPDATE users SET password=crypt('${password}',gen_salt('bf',10)), password_change_required=false WHERE mail='admin@pentagi.com';`);
        await this.run(['rm','-v',this.name('bootstrap')]);
        await this.run(['create',...common.filter(a=>a!=='--detach'),'--name',this.name('app'),'-p','127.0.0.1::8443',...appArgs,PENTAGI_IMAGES.app], {env});
        await this.run(['network','connect',this.name('database'),this.name('app')]);
        await this.run(['start',this.name('app')]);
        const mapped = await this.run(['port',this.name('app'),'8443/tcp']);
        if (!/^127\.0\.0\.1:\d+$/.test(mapped)) throw new Error('로컬 API 포트 확인에 실패했습니다.');
        const server = `http://${mapped}`;
        await this.until(async () => {const r = await fetch(server+'/api/v1/info',{signal:AbortSignal.timeout(5000)}); if (!r.ok) throw new Error();}, progress, 'PentAGI API 시작 대기', 180000);
        const login = await fetch(server+'/api/v1/auth/login',{method:'POST',redirect:'error',signal:AbortSignal.timeout(15000), headers:{'Content-Type':'application/json'},body:JSON.stringify({mail:'admin@pentagi.com',password})});
        const cookie = login.headers.get('set-cookie')?.split(';')[0];
        if (!login.ok || !cookie) throw new Error('PentAGI 초기 인증에 실패했습니다.');
        const response = await fetch(server+'/api/v1/tokens/',{method:'POST',redirect:'error',signal:AbortSignal.timeout(15000),headers:{'Content-Type':'application/json',Cookie:cookie},body:JSON.stringify({name:'ReCoder temporary test',ttl:7200})});
        if (!response.ok) throw new Error('PentAGI 임시 API 토큰 생성에 실패했습니다.');
        const body = await response.json() as {data?:{token?:string}};
        if (typeof body.data?.token !== 'string') throw new Error('PentAGI 임시 토큰 응답 형식이 올바르지 않습니다.');
        return {server,token:body.data.token};
    }
    private async sql(input: string) {return this.run(['exec','-i',this.name('db'),'psql','-U','postgres','-d','pentagidb','-At','-v','ON_ERROR_STOP=1'],{input});}
    private async until(check: () => Promise<void>, progress: (s:string)=>Promise<void>, message: string, timeout: number) {
        const deadline = Date.now()+timeout;
        while (Date.now()<deadline) {
            await progress(message);
            try {await check(); return;} catch {await new Promise(r=>setTimeout(r,2000));}
        }
        throw new Error(`${message} 시간이 초과되었습니다. 검사 환경을 정리합니다.`);
    }
}
export interface LocalRequest {root:string; session:string; workspace:string; target:string; apiKey:string; provider:LocalProvider;}
export async function executeLocalTest(request: LocalRequest): Promise<void> {
    const {root,session,workspace,apiKey,provider} = request;
    const paths = sessionPaths(root,session); const target = localTarget(request.target);
    const controller=new AbortController();
    const runtime = new LocalPentagiRuntime(session,(args,options)=>docker(args,{...options,signal:controller.signal}));
    const report: LocalReport = {session,workspace,target,provider,createdAt:new Date().toISOString(),updatedAt:new Date().toISOString(),pid:process.pid,phase:'preparing',step:'검사 환경 준비'};
    const secrets=[apiKey];
    const persist=()=>saveReport(root,redactReport(report,secrets));
    await persist(); // Ownership journal exists before Docker mutations.
    let client: PentagiClient | undefined; let lastSaved = 0;
    const cancelled = async () => !!(await fs.stat(paths.cancel).catch(()=>null));
    let deadline=Date.now()+60*60*1000;
    const stopWatcher=setInterval(()=>{void cancelled().then(stop=>{if(stop||Date.now()>deadline)controller.abort();});},1000);
    const progress = async (step: string) => {
        if (controller.signal.aborted || await cancelled()) throw new Error('검사 중단 또는 시간 제한');
        report.step=step; await persist();
    };
    try {
        const connection = await runtime.prepare(apiKey,provider,progress); report.server=connection.server;
        secrets.push(connection.token);
        client = new PentagiClient(connection.server,connection.token);
        const providers = await client.providers();
        if (!providers.includes(provider)) throw new Error(`PentAGI ${provider} 공급자가 준비되지 않았습니다.`);
        deadline=Date.now()+30*60*1000;
        await progress('검사 시작');
        report.flow = await client.start(target,provider); report.phase='running'; await persist();
        while (true) {
            if (await cancelled()) {report.outcome='cancelled'; break;}
            if (Date.now()>deadline) {report.outcome='timed_out'; break;}
            const snapshot = await client.snapshot(report.flow.id);
            report.flow=snapshot.flow; report.tasks=snapshot.tasks; report.step=snapshot.flow.status==='created'?'검사 도구 준비':snapshot.flow.status==='waiting'?'입력 대기':'검사 중';
            await persist(); lastSaved=Date.now();
            // No interactive reply channel is exposed here. Retain the partial
            // evidence and release paid resources instead of idling for 30 min.
            const outcome=pentagiOutcome(snapshot);
            if (outcome) {
                report.outcome=outcome;
                if(report.outcome==='failed')report.error='PentAGI가 검사를 완료하지 못했습니다. 저장된 결과와 API 키·모델 사용 가능 여부를 확인하세요.';
                break;
            }
            await new Promise(r=>setTimeout(r,5000));
        }
    } catch (error) {
        report.outcome = await cancelled() ? 'cancelled' : Date.now()>deadline ? 'timed_out' : 'failed';
        // No upstream body, process error or key can end up in a report.
        if(report.outcome==='failed') report.error = error instanceof Error && /^(Docker|로컬|검사|PentAGI)/.test(error.message) ? error.message : `${report.step} 단계에서 완료하지 못했습니다. Docker 상태와 선택한 모델 공급자 사용 가능 여부를 확인하세요.`;
    } finally {
        clearInterval(stopWatcher);
        if(client&&report.flow) {
            try {report.evidence=await client.evidence(report.flow.id);}
            catch {report.error='상세 보고서를 가져오지 못했습니다. 마지막 저장된 작업 결과만 보관합니다.';}
        }
        if (client && report.flow && !['finished','failed'].includes(report.flow.status)) {
            await client.finish(report.flow.id).catch(()=>{});
            try {const snapshot = await client.snapshot(report.flow.id); report.flow=snapshot.flow; report.tasks=snapshot.tasks;} catch {if (lastSaved) report.step='마지막으로 저장한 결과';}
        }
        report.phase='saving'; report.step='결과 저장';
        // If the final write fails, keep earlier snapshots; still stop paid work and remove credentials.
        let saved = true;
        try {await persist();} catch {saved=false;}
        report.phase='cleaning'; report.step='검사 환경 자동 삭제'; await persist().catch(()=>{});
        try {await new LocalPentagiRuntime(session).cleanup(); report.phase='removed'; report.step='검사 환경 삭제 완료';}
        catch {report.phase='cleanup_failed'; report.step='환경 정리 재시도 필요';}
        if (!saved) report.error='최종 결과 저장에 실패했습니다. 마지막 저장 결과를 확인하세요.';
        await persist();
    }
}
