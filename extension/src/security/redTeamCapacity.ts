import {promises as fs} from 'fs';
import * as path from 'path';
import * as os from 'os';
const GiB=1024**3;
/** Check the host volume, not the virtual free space advertised inside WSL. */
export async function ensureRedTeamCapacity(reportRoot:string,env:NodeJS.ProcessEnv=process.env,platform=process.platform,io=fs):Promise<void> {
    const root=await io.statfs(reportRoot);
    if(root.bavail*root.bsize<256*1024**2)throw new Error('검사 기록 저장 공간이 부족합니다. 해당 드라이브에 최소 256MB를 확보하세요.');
    let candidates:string[];
    if(platform==='win32') {
        const local=env.LOCALAPPDATA||path.join(os.homedir(),'AppData','Local');
        candidates=[path.join(local,'Docker','wsl','disk'),path.join(local,'Docker','wsl','data')];
        try {
            const settings=JSON.parse(await io.readFile(path.join(env.APPDATA||path.join(os.homedir(),'AppData','Roaming'),'Docker','settings-store.json'),'utf8'));
            for(const k of ['DataFolder','dataFolder','CustomWslDistroDir','customWslDistroDir']) {
                const value=settings[k];if(typeof value==='string'&&path.isAbsolute(value))candidates.unshift(value,path.join(value,'disk'));
            }
        } catch { /* Defaults remain usable, including a verified junction to another drive. */ }
    } else if(platform==='darwin') candidates=[path.join(os.homedir(),'Library','Containers','com.docker.docker','Data','vms','0','data')];
    else candidates=['/var/lib/docker',path.join(os.homedir(),'.docker','desktop','vms','0','data')];
    let available:number|undefined;
    for(const candidate of candidates) {
        try {const actual=await io.realpath(candidate);const stat=await io.statfs(actual);available=stat.bavail*stat.bsize;break;}catch{}
    }
    if(available===undefined)throw new Error('Docker 데이터 저장 드라이브의 여유 공간을 확인하지 못했습니다. Docker Desktop의 저장 위치와 접근 권한을 확인하세요.');
    // The pinned toolbox expands to about 17GB and also exists inside the disposable
    // engine. Leave room for compressed layers, both copies, the app and its database.
    if(available<50*GiB)throw new Error('Docker 데이터 저장 드라이브에 최소 50GB가 필요합니다. 공간을 확보하거나 Docker 저장 위치를 변경한 뒤 다시 실행하세요.');
}
