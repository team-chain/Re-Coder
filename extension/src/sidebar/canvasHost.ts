import * as vscode from 'vscode';
import * as path from 'path';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { createHash, randomUUID } from 'crypto';
import { ApiClient } from '../core/ApiClient';
import * as fs from 'fs';
import { AnalysisJob } from '../codemap/analysisJob';
import { collectStaticFiles, describeExcludedFiles, pickStaticDir } from '../deploy/staticSite';

const exec = promisify(execFile);
type Send = (type: string, payload: unknown) => void;
type Target = 'ecs' | 's3' | 'github';
export interface CanvasConfig { target: Target; image_name: string; tag: string; aws_region: string; ecs_cluster: string; ecs_service: string; task_family: string; container_port: number; cpu: string; memory: string; environment: string; dir: string }
interface Plan { id: string; config: CanvasConfig; workspace: string; git: Awaited<ReturnType<typeof gitContext>>; account: string; created: number; targetState: Awaited<ReturnType<ApiClient['getCanvasTarget']>> | null; staticDigest?: string }

export function staticPreview(workspace: string, dir: string) {
    const root = fs.realpathSync(workspace), selected = fs.realpathSync(path.resolve(root, dir));
    const relative = path.relative(root, selected);
    if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) throw new Error('프로젝트 안의 빌드 산출물 폴더를 선택하세요.');
    const collected = collectStaticFiles(selected, fs, path.join, dir);
    if (!collected.files.length) throw new Error('올릴 정적 파일이 없습니다. 빌드 산출물 폴더를 확인하세요.');
    return { dir: relative, files: collected.files.map(f => f.path), excluded: describeExcludedFiles(collected), digest: createHash('sha256').update(JSON.stringify(collected.files)).digest('hex') };
}

export async function gitContext(workspace: string) {
    const run = async (args: string[]) => (await exec('git', ['-C', workspace, ...args], { timeout: 5000, windowsHide: true, maxBuffer: 1024 * 1024 })).stdout.trim();
    const empty = { repository: '', branch: '', head: '', dirty: false, connected: false, initialized:false, hasOrigin:false, fingerprint:'', error:'' };
    if (!workspace) return empty;
    try {
        const root = await run(['rev-parse','--show-toplevel']).catch(err=>{if(err.code==='ENOENT')throw err;return '';});
        // A folder inside another repository must not publish the parent's source.
        const canonical = (value:string) => process.platform==='win32' ? fs.realpathSync(value).toLowerCase() : fs.realpathSync(value);
        if (!root || canonical(root) !== canonical(workspace)) return empty;
        const [remote, branch, head, changes] = await Promise.all([run(['remote', 'get-url', 'origin']).catch(() => ''), run(['branch', '--show-current']), run(['rev-parse', '--verify', 'HEAD']).catch(()=>''), run(['status', '--porcelain'])]);
        let repository = '';
        // Never expose tokens embedded in a remote URL to a webview.
        const match = /^(?:https:\/\/(?:[^/@]+@)?github\.com\/|git@github\.com:|ssh:\/\/git@github\.com\/)([a-zA-Z0-9-]+\/[a-zA-Z0-9_.-]+?)(?:\.git)?$/.exec(remote);
        if (match) repository = match[1];
        return { repository, branch, head, dirty: Boolean(changes), connected: Boolean(repository), initialized:true, hasOrigin:Boolean(remote), fingerprint: `${head}|${branch}|${remote}|${changes}`, error:'' };
    } catch { return {...empty,error:'Git 상태를 읽지 못했습니다. Git 설치와 프로젝트 폴더 접근 권한을 확인하세요.'}; }
}

export function githubRepository(value: string): string {
    const match = /^(?:https:\/\/github\.com\/)?([a-zA-Z0-9-]+\/[a-zA-Z0-9_.-]+?)(?:\.git)?\/?$/.exec(value.trim());
    if (!match || match[1].split('/').some(part=>part==='.'||part==='..')) throw new Error('GitHub 저장소를 owner/name 또는 https://github.com/owner/name 형식으로 입력하세요.');
    return match[1];
}

export function publicGit(git: Awaited<ReturnType<typeof gitContext>>) {
    const {fingerprint: _fingerprint, ...state} = git;
    return state;
}

export function validateConfig(raw: Record<string, unknown>): CanvasConfig {
    if (!['ecs','s3','github'].includes(String(raw.target))) throw new Error('지원하지 않는 배포 대상입니다.');
    const get = (key: string, fallback = '') => String(raw[key] ?? fallback).trim();
    const config: CanvasConfig = { target: raw.target as Target, image_name: get('image_name'), tag: get('tag'), aws_region: get('aws_region'), ecs_cluster: get('ecs_cluster'), ecs_service: get('ecs_service'), task_family: get('task_family'), container_port: Number(raw.container_port ?? 8000), cpu: get('cpu','256'), memory: get('memory','512'), environment: get('environment','staging'), dir: get('dir') };
    if (config.target !== 'github' && !/^[a-z]{2}(?:-[a-z]+)+-\d+$/.test(config.aws_region)) throw new Error('유효한 AWS 리전을 입력하세요.');
    if (config.target === 'ecs') {
        for (const key of ['ecs_cluster','ecs_service','task_family'] as const) if (!/^[a-zA-Z0-9_-]{1,255}$/.test(config[key])) throw new Error(`${key}: 리소스 이름을 입력하세요.`);
        if (!config.image_name || !/^[\w][\w.-]{0,127}$/.test(config.tag)) throw new Error('이미지 이름과 고정 태그를 입력하세요.');
        if (config.tag.toLowerCase() === 'latest') throw new Error('재현 가능한 배포를 위해 latest 대신 고정 태그를 입력하세요.');
        if (!Number.isInteger(config.container_port) || config.container_port < 1 || config.container_port > 65535) throw new Error('포트는 1~65535 정수여야 합니다.');
        if (!['staging','production'].includes(config.environment)) throw new Error('배포 환경을 확인하세요.');
        const sizes: Record<string,string[]> = { '256':['512','1024','2048'], '512':['1024','2048','3072','4096'], '1024':['2048','3072','4096','5120','6144','7168','8192'], '2048':Array.from({length:13},(_,i)=>String((i+4)*1024)), '4096':Array.from({length:23},(_,i)=>String((i+8)*1024)) };
        if (!sizes[config.cpu]?.includes(config.memory)) throw new Error('Fargate CPU와 메모리 조합을 확인하세요.');
    }
    return config;
}

/** Per-webview plans bind explicit approval to a frozen request; existing deploy handlers execute it. */
export class CanvasHost {
    private analysis = new AnalysisJob();
    dispose(): void { this.analysis.cancel(); }
    private plans = new Map<string, Plan>();
    private executing = false;
    private prepareGeneration = 0;
    constructor(private api: ApiClient, private readGit = gitContext) {}
    async handle(type: string, raw: unknown, send: Send, project: (workspace: string) => string, execute: (type: string, payload: unknown) => Promise<void>) {
        const p = (raw ?? {}) as Record<string, unknown>, requestId = String(p.requestId || '');
        const workspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        try {
            if (type === 'canvas.graph.cancel') { this.analysis.cancel(); return; }
            if (type === 'canvas.fix') {
                if (!workspace) throw new Error('프로젝트 폴더를 먼저 여세요.');
                const findings = Array.isArray(p.findings) ? p.findings.slice(0,20).map(f => {
                    const item=f as Record<string,unknown>;
                    return `${String(item.tool||'')} ${String(item.location||'')}: ${String(item.fix||'보안 문제 수정')}`.slice(0,500);
                }).join('\n') : '';
                send('chat.actionAccepted', { requestId:Date.now(), instruction:`배포 보안 검사에서 발견한 다음 항목을 해결하는 최소 수정안을 제안해 주세요. 기존 기능을 보존하고 시크릿 원문을 답변에 포함하지 마세요.\n${findings}`, targetFolder:'' });
                return;
            }
            if (type === 'canvas.graph') {
                if (!workspace) throw new Error('분석할 프로젝트 폴더가 없습니다.');
                const id = String(p.file || '');
                send('canvas.graphStarted', { requestId });
                if (!id) { send('canvas.graphResult', { requestId, file: '', graph: await this.analysis.run(workspace) }); return; }
                const root = fs.realpathSync(workspace), file = fs.realpathSync(path.resolve(root,id));
                const relative = path.relative(root,file);
                if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) throw new Error('프로젝트 안의 파일만 분석할 수 있습니다.');
                send('canvas.graphResult', { requestId, file:id, graph:await this.analysis.run(workspace,file) }); return;
            }
            if (type === 'canvas.snapshot') {
                const git = workspace ? await this.readGit(workspace) : await this.readGit('');
                const local = { workspace, projectName: path.basename(workspace), git: publicGit(git) };
                const response = await this.api.getCanvasSnapshot(workspace, workspace ? project(workspace) : '');
                send('canvas.snapshotResult', { requestId, snapshot: { aws: { ready:false,region:'',account:'' }, deployment:{running:false,stage:'idle'}, resource:null,topology:null,scan:null,s3:null, warnings: response.success ? [] : [response.error || 'Core 연결을 확인하세요.'], ...response.data, ...local } });
                return;
            }
            if (type.startsWith('canvas.discord.')) { await this.discord(type,p,send); return; }
            if (!workspace) throw new Error('먼저 프로젝트 폴더를 여세요.');
            if (type.startsWith('canvas.github.')) { await this.github(type,p,workspace,send); return; }
            if (type === 'canvas.prepare') {
                const generation = ++this.prepareGeneration;
                this.plans.clear();
                const config = validateConfig(p.config as Record<string,unknown> || {});
                if (config.target === 's3' && p.autoDir === true) config.dir = pickStaticDir(fs.readdirSync(workspace, {withFileTypes:true}).filter(e=>e.isDirectory()).map(e=>e.name));
                const staticSite = config.target === 's3' ? staticPreview(workspace,config.dir) : undefined;
                if (staticSite) config.dir = staticSite.dir;
                const [git,aws,preflight] = await Promise.all([this.readGit(workspace), config.target === 'github' ? Promise.resolve({ready:false,region:'',identity:{account:''}}) : this.api.getAwsStatus(), config.target === 'github' ? Promise.resolve({blocked:false,summary:'승인한 커밋을 푸시하기 전에 시크릿을 검사합니다.',reasons:[],warnings:[]}) : this.api.getDeployPreflight(workspace)]);
                if (generation !== this.prepareGeneration) return;
                if (config.target !== 'github' && !aws.ready) throw new Error('AWS 계정을 먼저 연결하세요.');
                if (config.target === 'github' && (!git.connected || !git.branch)) throw new Error('GitHub 원격 저장소와 브랜치를 연결하세요.');
                if (config.target === 'github' && !git.head) throw new Error('먼저 소스 제어에서 올릴 파일을 확인하고 첫 커밋을 만드세요.');
                const targetState = config.target === 'ecs' ? await this.api.getCanvasTarget(config.aws_region,config.ecs_cluster,config.ecs_service) : null;
                if (generation !== this.prepareGeneration) return;
                const id=randomUUID();
                this.plans.clear(); // Older approvals must not execute after editing a target.
                this.plans.set(id,{id,config,workspace,git,account:aws.identity?.account || '',created:Date.now(),targetState,staticDigest:staticSite?.digest});
                send('canvas.plan', { requestId, id, config, projectName:path.basename(workspace), repository:git.repository, branch:git.branch, commit:git.head, dirty:git.dirty, coreRegion:aws.ready ? aws.region : '', account:aws.identity?.account || '', preflight, targetState, staticSite:staticSite ? {files:staticSite.files,excluded:staticSite.excluded} : undefined });
                return;
            }
            if (type === 'canvas.cancel') { this.prepareGeneration++; this.plans.delete(String(p.planId)); return; }
            if (type === 'canvas.execute') {
                const plan=this.plans.get(String(p.planId));
                if (p.approved !== true || !plan) throw new Error('승인 카드에서 배포 내용을 다시 확인하세요.');
                if (this.executing) throw new Error('이미 배포 요청을 처리하고 있습니다.');
                this.plans.delete(plan.id);
                if (plan.workspace!==workspace || Date.now()-plan.created>10*60*1000) throw new Error('프로젝트 또는 승인 유효 시간이 바뀌었습니다. 다시 확인하세요.');
                this.executing=true;
                try {
                    const git=await this.readGit(workspace);
                    if (git.fingerprint!==plan.git.fingerprint) throw new Error('승인 이후 Git 상태·브랜치·원격이 변경되었습니다. 다시 확인하세요.');
                    const c=plan.config;
                    if(c.target!=='github') {
                        const aws=await this.api.getAwsStatus();
                        if(!aws.ready || (aws.identity?.account || '')!==plan.account) throw new Error('AWS 연결 계정이 바뀌었습니다. 다시 확인하세요.');
                    }
                    if(c.target!=='github') {
                        const preflight=await this.api.getDeployPreflight(workspace);
                        if(preflight.blocked) { send('canvas.blocked',{requestId,preflight}); return; }
                    }
                    if(c.target==='ecs') {
                        if(plan.targetState?.exists !== null && plan.targetState) {
                            const current = await this.api.getCanvasTarget(c.aws_region,c.ecs_cluster,c.ecs_service,false);
                            if(current.exists!==plan.targetState.exists || current.task_definition!==plan.targetState.task_definition) throw new Error('승인 후 대상 서비스의 태스크 정의가 바뀌었거나 재확인할 수 없습니다. 다시 확인하세요.');
                        }
                        send('canvas.executing',{requestId,message:'대상 리전·권한 확인 중'});
                        const status=await this.api.checkAwsPermissions({ecsCluster:c.ecs_cluster,ecsService:c.ecs_service,taskFamily:c.task_family,awsRegion:c.aws_region});
                        const permission=status.permission_check as (typeof status.permission_check & { advisory_only?: boolean });
                        if(!status.ready || permission?.missing_actions?.length || !(permission?.inspected || permission?.advisory_only)) throw new Error('ECS 배포 권한을 확인하지 못했습니다. AWS 연결에서 권한을 확인하세요.');
                        await execute('workspace.deploy.ecs', {...c, workspace_path:workspace});
                    } else if(c.target==='s3') {
                        if(staticPreview(workspace,c.dir).digest !== plan.staticDigest) throw new Error('승인 후 정적 파일이 변경되었습니다. 배포 내용을 다시 확인하세요.');
                        // Existing handler owns path confinement, sensitive-file filtering and stream.
                        await execute('workspace.deploy.s3',{dir:c.dir,region:c.aws_region});
                    } else {
                        send('canvas.executing',{requestId,message:'gitleaks 검사 중'});
                        const scan=await this.api.runScan('gitleaks',workspace) as {status?:string;critical_count?:number;findings?:unknown;message?:string};
                        if(scan.status!=='ok' || !Array.isArray(scan.findings) || scan.findings.length>0 || (scan.critical_count ?? 0)>0) throw new Error('시크릿 검사를 통과하지 못해 푸시를 중단했습니다. 보안 검사 화면에서 확인하세요.');
                        const result=await this.api.gitPush({workspace_path:workspace,branch:plan.git.branch,force:false,auto_commit:false});
                        if(result.status!=='success' && result.status!=='ok') throw new Error(result.message || '푸시 실패');
                        send('canvas.completed',{requestId,message:result.message || 'GitHub 푸시 완료'});
                    }
                } finally { this.executing=false; }
                return;
            }
        } catch(err) { send('canvas.error',{requestId,context:type,message:err instanceof Error ? err.message : String(err)}); }
    }

    private async github(type:string,p:Record<string,unknown>,workspace:string,send:Send) {
        const requestId=String(p.requestId||'');
        if (p.workspace !== workspace) throw new Error('프로젝트가 바뀌었습니다. GitHub 패널을 다시 여세요.');
        if (type==='canvas.github.sourceControl') {
            await vscode.commands.executeCommand('workbench.view.scm');
            send('canvas.github.result',{requestId,message:'소스 제어에서 올릴 파일을 선택하고 커밋한 뒤 상태 새로고침을 누르세요.'});
            return;
        }
        if (type==='canvas.github.login') await vscode.commands.executeCommand('recoder.githubLogin');
        if (type==='canvas.github.connect') {
            const repository=githubRepository(String(p.repository||''));
            const current=await this.readGit(workspace);
            if (current.error) throw new Error(current.error);
            if (current.hasOrigin && !current.connected) throw new Error('다른 origin이 이미 설정되어 있습니다. 소스 제어에서 확인하세요.');
            if (current.connected && current.repository!==repository) throw new Error('이미 연결된 origin은 자동으로 바꾸지 않습니다. 소스 제어에서 원격 저장소를 확인하세요.');
            const auth=await this.api.getGithubStatus();
            if(auth.status!=='authenticated') throw new Error(auth.message || 'GitHub에 먼저 로그인하세요.');
            // Creating a private repository never commits or pushes local files.
            const result=await this.api.githubConnectRepository({repository,create:p.create===true});
            if(result.status!=='ok') throw new Error(result.message || '저장소를 확인하지 못했습니다.');
            if (vscode.workspace.workspaceFolders?.[0]?.uri.fsPath!==workspace || (await this.readGit(workspace)).fingerprint!==current.fingerprint) throw new Error('연결 중 프로젝트 또는 Git 상태가 바뀌었습니다. 상태를 새로고침하세요.');
            const run=async(args:string[]) => (await exec('git',['-C',workspace,...args],{timeout:10000,windowsHide:true})).stdout.trim();
            if (!current.initialized) await run(['init','--initial-branch=main']);
            const remote=await run(['remote','get-url','origin']).catch(()=>'');
            if(remote && !(await this.readGit(workspace)).connected) throw new Error('다른 origin이 이미 설정되어 있습니다. 소스 제어에서 확인하세요.');
            if(!remote) await run(['remote','add','origin',`https://github.com/${repository}.git`]);
            this.plans.clear();
        }
        const [auth,git]=await Promise.all([this.api.getGithubStatus(type==='canvas.github.login'),this.readGit(workspace)]);
        const repos=auth.status==='authenticated' ? await this.api.listGithubRepos() : {status:'ok',repos:[]};
        send('canvas.github.result',{requestId,workspace,auth,git:publicGit(git),repos:repos.repos,
            message:type==='canvas.github.connect'?'저장소를 연결했습니다. 소스 제어에서 올릴 파일을 커밋한 뒤 푸시하세요.':
                type==='canvas.github.login'&&auth.status!=='authenticated'?'GitHub 로그인이 완료되지 않았습니다. VS Code 알림을 확인하고 다시 시도하세요.':
                repos.status!=='ok'?'저장소 목록을 불러오지 못했습니다. 저장소 주소를 직접 입력하거나 다시 시도하세요.':''});
    }

    private async bot(route: string, method='GET', body?: unknown): Promise<Record<string,unknown>> {
        const cfg=vscode.workspace.getConfiguration('recoder.bridge');
        const host=cfg.get<string>('host') || '127.0.0.1',port=cfg.get<number>('httpPort') ?? 8765;
        const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),5000);
        try {
            const headers:Record<string,string>={'Content-Type':'application/json'},key=cfg.get<string>('registrationKey') || '';
            if(key) headers['X-Registration-Key']=key;
            const response=await fetch(`http://${host}:${port}/api/v1/bridge/${route}`,{method,headers,body:body===undefined ? undefined : JSON.stringify(body),signal:controller.signal});
            const data=await response.json() as Record<string,unknown>;
            if(!response.ok) throw new Error(response.status===401 ? 'Discord 봇 서버 인증에 실패했습니다. 연결 설정의 Registration Key를 확인하세요.' : String(data.error || `Discord HTTP ${response.status}`));
            return data;
        } catch(err) {
            if (err instanceof TypeError || (err instanceof Error && err.name==='AbortError')) throw new Error(`Discord 봇 서버에 연결할 수 없습니다 (${host}:${port}). ReCoder Discord 봇을 실행하거나 연결 설정에서 서버 주소를 확인한 뒤 다시 시도하세요.`);
            throw err;
        } finally {clearTimeout(timer);}
    }
    private async discord(type:string,p:Record<string,unknown>,send:Send) {
        const requestId=String(p.requestId||'');
        if(type==='canvas.discord.settings') { await vscode.commands.executeCommand('workbench.action.openSettings','recoder.bridge'); return; }
        if(type==='canvas.discord.status') send('canvas.discord.statusResult',{requestId,...await this.bot('status')});
        if(type==='canvas.discord.guilds') send('canvas.discord.guildsResult',{requestId,...await this.bot('guilds')});
        if(type==='canvas.discord.channels') send('canvas.discord.channelsResult',{requestId,...await this.bot(`guilds/${encodeURIComponent(String(p.guildId))}/channels`)});
        if(type==='canvas.discord.setChannel') { await this.bot('channel','PUT',{channel_id:String(p.channelId||'')}); send('canvas.discord.statusResult',{requestId,...await this.bot('status')}); }
        if(type==='canvas.discord.invite') {
            const id=vscode.workspace.getConfiguration('recoder.discord').get<string>('clientId') || '';
            const url=id ? `https://discord.com/api/oauth2/authorize?client_id=${encodeURIComponent(id)}&permissions=2147485696&scope=bot%20applications.commands` : String((await this.bot('invite-url')).invite_url || '');
            if(!url.startsWith('https://discord.com/')) throw new Error('봇 초대 주소가 없습니다. Discord Client ID를 설정하세요.');
            await vscode.env.openExternal(vscode.Uri.parse(url));
        }
        if(type==='canvas.discord.event') {
            // Called only after the user enables notifications in the canvas. Do not forward raw logs/secrets.
            if(p.enabled!==true) throw new Error('먼저 이벤트 알림을 켜세요.');
            const result=await this.bot('events','POST',{event_id:String(p.eventId||''),title:String(p.title||'').slice(0,160),detail:String(p.detail||'').slice(0,1000)});
            send('canvas.discord.eventResult',{requestId,...result});
        }
    }
}
