/**
 * 공격 테스트 환경 미리 받기 — 검사 도구 이미지(약 17GB)를 보관 볼륨에 미리 넣어 둔다.
 *
 * 실행 중인 앱도, 모델 키도, 검사 대상도 쓰지 않는다. 고정 digest 이미지만 받고 내용 ID 를 대조한 뒤
 * 엔진을 정상 종료해 지운다. 남는 것은 보관 볼륨(이미지)과 호스트 이미지 캐시뿐이다.
 */
import {promises as fs} from 'fs';
import * as path from 'path';
import {DockerRun, LOCAL_ENGINE_STORAGE, PENTAGI_IMAGES, assertLocalDocker, claimEngineCache, docker, engineCacheWarm, ensureHostImages, markEngineCache, resetEngineState} from './localPentagi';
import {ensureToolbox} from './localRedTeamScope';
import {ensureRedTeamCapacity} from './redTeamCapacity';

export const PREWARM_ENGINE = 'recoder-redteam-prewarm';
const PREWARM_LABEL = 'io.recoder.redteam.prewarm';
export type PrewarmPhase = 'running'|'done'|'failed'|'cancelled';
export interface PrewarmState {phase: PrewarmPhase; step: string; pid?: number; updatedAt: string; error?: string}
export const prewarmPaths = (root: string) => ({
    state: path.join(path.resolve(root), 'prewarm.json'), cancel: path.join(path.resolve(root), 'prewarm.cancel'),
});
export function alive(pid?: number) {if (!pid) return false; try {process.kill(pid,0); return true;} catch {return false;}}

export async function readPrewarm(root: string): Promise<PrewarmState|undefined> {
    try {
        const data = JSON.parse(await fs.readFile(prewarmPaths(root).state,'utf8')) as PrewarmState;
        if (!['running','done','failed','cancelled'].includes(data.phase)) return undefined;
        //: 작업 프로세스가 사라졌는데 "받는 중" 으로 남아 있으면 중단된 것이다.
        if (data.phase === 'running' && !alive(data.pid)) return {...data, phase:'failed', step:'미리 받기가 중간에 멈췄습니다', error:'다시 시작할 수 있습니다.'};
        return data;
    } catch {return undefined;}
}
async function writePrewarm(root: string, state: PrewarmState) {
    await fs.mkdir(path.resolve(root),{recursive:true});
    state.updatedAt = new Date().toISOString();
    const file = prewarmPaths(root).state;
    await fs.writeFile(file+'.tmp', JSON.stringify(state), {mode:0o600});
    await fs.rm(file,{force:true}); await fs.rename(file+'.tmp', file);
}
export async function prewarmRunning(root: string) {return (await readPrewarm(root))?.phase === 'running';}

/** 끝나지 못한 미리 받기 엔진(프로세스 종료 등)을 지운다 — 이름과 라벨이 모두 맞을 때만. */
export async function removePrewarmEngine(run: DockerRun = docker): Promise<void> {
    const names = (await run(['container','ls','-a','--filter',`label=${PREWARM_LABEL}=1`,'--format','{{.Names}}'])).split(/\r?\n/).map(x=>x.trim());
    if (!names.includes(PREWARM_ENGINE)) return;
    await run(['stop','--time','20',PREWARM_ENGINE],{timeout:60000}).catch(()=>{});
    await run(['container','rm','-f','-v',PREWARM_ENGINE]);
}

export async function runPrewarm(root: string, run: DockerRun = docker): Promise<PrewarmState> {
    const paths = prewarmPaths(root);
    await fs.rm(paths.cancel,{force:true});
    const state: PrewarmState = {phase:'running', step:'Docker 확인', pid:process.pid, updatedAt:''};
    await writePrewarm(root,state);
    const progress = async (step: string) => {
        if (await fs.stat(paths.cancel).then(()=>true,()=>false)) throw new Error('cancelled');
        state.step = step; await writePrewarm(root,state);
    };
    let started = false;
    try {
        await assertLocalDocker(run);
        await ensureRedTeamCapacity(root,process.env,process.platform,fs,await engineCacheWarm(root,run));
        await ensureHostImages(run,progress);
        await removePrewarmEngine(run);
        const volume = await claimEngineCache(run);
        if (!volume) throw new Error('공격 테스트가 실행 중이라 지금은 미리 받을 수 없습니다. 테스트가 끝난 뒤 다시 시도하세요.');
        await progress('검사 도구 보관 엔진 시작');
        //: 외부 포트·호스트 소켓·프로젝트 폴더 없음. 도구를 받는 동안만 존재한다.
        await run(['run','--detach','--restart=no','--label',`${PREWARM_LABEL}=1`,'--name',PREWARM_ENGINE,'--privileged',
            '--memory','3g','--cpus','2','--pids-limit','2048','--log-opt','max-size=5m','--log-opt','max-file=2',
            '-e','DOCKER_TLS_CERTDIR=','-v',`${volume}:/var/lib/docker`,PENTAGI_IMAGES.engine,
            'dockerd','--host=unix:///var/run/docker.sock',...LOCAL_ENGINE_STORAGE]);
        started = true;
        const deadline = Date.now()+120000;
        for (;;) {
            await progress('검사 도구 보관 엔진 시작 대기');
            try {await run(['exec',PREWARM_ENGINE,'docker','info','--format','{{.ServerVersion}}']); break;}
            catch {
                if (Date.now() > deadline) {
                    //: 보관 볼륨이 손상돼 엔진이 뜨지 않으면 지우고 다음에 새로 받는다.
                    await run(['container','rm','-f','-v',PREWARM_ENGINE]).catch(()=>{}); started = false;
                    await run(['volume','rm',volume]).catch(()=>{});
                    throw new Error('검사 도구 보관 엔진이 시작되지 않았습니다. 보관 공간을 비웠으니 다시 시도하세요.');
                }
                await new Promise(r=>setTimeout(r,2000));
            }
        }
        await progress('이전 검사 흔적 정리 · 도구 이미지만 유지');
        await resetEngineState(PREWARM_ENGINE,run);
        const toolboxId = await ensureToolbox(PREWARM_ENGINE,run,progress);
        await markEngineCache(root,toolboxId);
        state.phase = 'done'; state.step = '검사 도구 준비 완료 — 다음 공격 테스트는 다운로드 없이 시작합니다';
    } catch (error) {
        const cancelled = error instanceof Error && error.message === 'cancelled';
        state.phase = cancelled ? 'cancelled' : 'failed';
        state.error = cancelled ? undefined : error instanceof Error && /^(Docker|로컬|검사|PentAGI|공격)/.test(error.message) ? error.message : `${state.step} 단계에서 멈췄습니다. Docker 상태와 인터넷 연결을 확인하세요.`;
        state.step = cancelled ? '미리 받기를 중단했습니다 — 언제든 다시 시작할 수 있습니다' : '미리 받기 실패';
    } finally {
        if (started) await removePrewarmEngine(run).catch(()=>{});
        await fs.rm(paths.cancel,{force:true}).catch(()=>{});
        delete state.pid;
        await writePrewarm(root,state).catch(()=>{});
    }
    return state;
}
