import * as fs from 'fs';
import {promises as fsp} from 'fs';
import * as path from 'path';
import {createHash} from 'crypto';
import {pipeline} from 'stream/promises';

export const PORTABLE_TOOLS={
    git:{version:'2.56.0.4',url:'https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/MinGit-2.56.0.4-64-bit.zip',sha256:'e3bf78351fe777fba47ddee5950082f8a98bb0f02604fdc7ba3ee2cb68ffa877',prefix:'',bin:'cmd',exe:'cmd/git.exe'},
    node:{version:'22.23.4',url:'https://nodejs.org/dist/v22.23.4/node-v22.23.4-win-x64.zip',sha256:'cbc74b5e0982c9dbb41525fd8fb5dcd32fbee237863f7829072f4639a1ce5f20',prefix:'node-v22.23.4-win-x64/',bin:'',exe:'node.exe'},
} as const;
type Tool=keyof typeof PORTABLE_TOOLS;
let home:string|undefined,notice:(text:string)=>void=()=>{};
const pending=new Map<Tool,Promise<string>>(),selected=new Map<Tool,string>();
export function initializePortableTools(storage:string,onProgress:(text:string)=>void){home=path.resolve(storage,'tools');notice=onProgress;}

export function systemTool(tool:Tool,env:NodeJS.ProcessEnv=process.env,platform=process.platform):string|undefined{
    const value=Object.entries(env).find(([key])=>key.toLowerCase()==='path')?.[1]||'';
    for(const entry of value.split(platform==='win32'?';':':')){
        if(!entry||!path.isAbsolute(entry))continue;
        const full=path.join(entry,tool+(platform==='win32'?'.exe':''));
        try{if(fs.statSync(full).isFile()&&(tool!=='node'||platform!=='win32'||fs.statSync(path.join(entry,'npm.cmd')).isFile()))return full;}catch{}
    }
}
export function toolEnvironment(env:NodeJS.ProcessEnv=process.env):NodeJS.ProcessEnv{
    const output={...env};const key=Object.keys(output).find(k=>k.toLowerCase()==='path')||'PATH';
    const additions=[...selected.values()].map(file=>path.dirname(file));
    if(additions.length){const old=output[key]||'';for(const k of Object.keys(output))if(k.toLowerCase()==='path')delete output[k];output.PATH=[...new Set(additions),old].join(path.delimiter);}
    return output;
}
export function gitExecutable():string{return selected.get('git')||systemTool('git')||'git';}

export async function extractToolZip(archive:string,destination:string,prefix=''){
    const yauzl=require('yauzl');const zip=await yauzl.openPromise(archive,{lazyEntries:true,strictFileNames:true,validateEntrySizes:true});
    let total=0,count=0;
    try{
        for await(const entry of zip.eachEntry()){
            if(++count>20000||entry.uncompressedSize>150*1024*1024||(total+=entry.uncompressedSize)>500*1024*1024)throw new Error('실행 도구 압축 파일의 크기 제한을 초과했습니다.');
            if(!entry.fileName.startsWith(prefix))throw new Error('예상하지 않은 실행 도구 패키지입니다.');
            const relative=entry.fileName.slice(prefix.length).replace(/\/$/,'');if(!relative)continue;
            if(relative.split('/').some((p:string)=>!p||p==='.'||p==='..'||/[\\:\x00-\x1f<>"|?*]/.test(p)||/[. ]$/.test(p)||/^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(p))||((entry.externalFileAttributes>>>16)&0xf000)===0xa000)throw new Error('실행 도구 압축 파일에 허용하지 않는 경로가 있습니다.');
            const target=path.resolve(destination,...relative.split('/'));
            if(!target.startsWith(path.resolve(destination)+path.sep))throw new Error('실행 도구 압축 경로가 올바르지 않습니다.');
            if(entry.fileName.endsWith('/')){await fsp.mkdir(target,{recursive:true});continue;}
            await fsp.mkdir(path.dirname(target),{recursive:true});
            const input=await zip.openReadStreamPromise(entry);await pipeline(input,fs.createWriteStream(target,{flags:'wx'}));
        }
    }finally{zip.close();}
}
async function install(tool:Tool):Promise<string>{
    const spec=PORTABLE_TOOLS[tool],root=home!,destination=path.join(root,`${tool}-${spec.version}-win32-x64`),exe=path.join(destination,spec.exe);
    const complete=async()=>{
        try{const manifest=JSON.parse(await fsp.readFile(path.join(destination,'recoder-tool.json'),'utf8'));if(manifest.archiveSha256!==spec.sha256||!fs.statSync(exe).isFile())return false;
            const actual=createHash('sha256').update(await fsp.readFile(exe)).digest('hex');return actual===manifest.executableSha256;
        }catch{return false;}
    };
    if(await complete())return exe;
    await fsp.mkdir(root,{recursive:true});
    const real=await fsp.realpath(root);if(path.relative(real,root)!==''||(await fsp.lstat(root)).isSymbolicLink())throw new Error('실행 도구 저장소가 다른 경로를 가리킵니다.');
    // Atomic mkdir is the cross-window install lease. Interrupted leases are never deleted blindly.
    const lock=destination+'.lock';let owned=false;
    for(let n=0;n<180;n++){
        try{await fsp.mkdir(lock);owned=true;break;}catch(e){if((e as NodeJS.ErrnoException).code!=='EEXIST')throw e;}
        if(await complete())return exe;
        await new Promise(r=>setTimeout(r,1000));
    }
    if(!owned)throw new Error('다른 창에서 실행 도구를 준비 중이거나 이전 준비가 중단됐습니다. ReCoder 실행 도구 복구 명령을 사용하세요.');
    await fsp.writeFile(path.join(lock,'owner.json'),JSON.stringify({pid:process.pid,createdAt:new Date().toISOString()}));
    const staging=await fsp.mkdtemp(path.join(root,`${tool}-download-`));
    try{
        if(await complete())return exe;
        if(fs.existsSync(destination))throw new Error('실행 도구 파일 검증에 실패했습니다. ReCoder 실행 도구 복구 명령을 사용하세요.');
        notice(`${tool==='git'?'Git':'Node.js/npm'} 실행 도구 자동 준비 중 · 별도 설치가 필요 없습니다`);
        const response=await fetch(spec.url,{signal:AbortSignal.timeout(180000)});
        if(!response.ok||!response.body)throw new Error('공식 실행 도구 다운로드에 실패했습니다. 네트워크 연결 후 다시 시도하세요.');
        const final=new URL(response.url);if(final.protocol!=='https:'||!['nodejs.org','github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'].includes(final.hostname))throw new Error('실행 도구 다운로드 주소가 올바르지 않습니다.');
        const archive=path.join(staging,'tool.zip'),handle=await fsp.open(archive,'wx');let size=0;const hash=createHash('sha256');
        try{for await(const chunk of response.body as any){size+=chunk.length;if(size>80*1024*1024)throw new Error('실행 도구 다운로드 크기를 초과했습니다.');hash.update(chunk);await handle.writeFile(chunk);}}finally{await handle.close();}
        if(hash.digest('hex')!==spec.sha256)throw new Error('실행 도구 SHA256이 공식 배포값과 다릅니다. 실행하지 않습니다.');
        const unpacked=path.join(staging,'unpacked');await fsp.mkdir(unpacked);await extractToolZip(archive,unpacked,spec.prefix);
        const executable=path.join(unpacked,spec.exe);const executableSha256=createHash('sha256').update(await fsp.readFile(executable)).digest('hex');
        await fsp.writeFile(path.join(unpacked,'recoder-tool.json'),JSON.stringify({version:spec.version,url:spec.url,archiveSha256:spec.sha256,executableSha256}));
        // Antivirus and OneDrive can hold newly extracted Windows binaries briefly.
        for(let attempt=0;;attempt++){
            try{await fsp.rename(unpacked,destination);break;}
            catch(e){if(attempt>=20||!['EPERM','EACCES','EBUSY'].includes((e as NodeJS.ErrnoException).code||'')||fs.existsSync(destination))throw e;await new Promise(r=>setTimeout(r,500));}
        }
        return exe;
    }finally{
        // staging and lock are direct children created by this invocation under the verified root.
        if(path.dirname(staging)===root)await fsp.rm(staging,{recursive:true,force:true});
        if(owned){await fsp.unlink(path.join(lock,'owner.json'));await fsp.rmdir(lock);}
    }
}
export async function ensurePortableTool(tool:Tool):Promise<string>{
    const installed=systemTool(tool);if(installed)return installed;
    if(!home||process.platform!=='win32'||process.arch!=='x64')return tool;
    if(!pending.has(tool))pending.set(tool,install(tool).then(exe=>{selected.set(tool,exe);return exe;}).finally(()=>pending.delete(tool)));
    return pending.get(tool)!;
}
export async function preparePortableEnvironment():Promise<NodeJS.ProcessEnv>{
    const tools:Tool[]=['git','node'];
    const results=await Promise.allSettled(tools.map(ensurePortableTool));
    // A download outage must not disable AI, AWS login, or existing managed jobs.
    // The affected feature retries ensurePortableTool and reports its own actionable error.
    results.forEach((result,index)=>{if(result.status==='rejected')notice(`${tools[index]} 자동 준비 실패 · 네트워크 연결 후 작업을 다시 시도하세요`);});
    return toolEnvironment();
}
export async function recoverPortableTools():Promise<void>{
    if(!home||pending.size)throw new Error('실행 도구 준비가 끝난 뒤 다시 시도하세요.');
    const root=await fsp.realpath(home);if(path.relative(root,home)!=='')throw new Error('실행 도구 저장 경로를 확인할 수 없습니다.');
    for(const tool of ['git','node'] as Tool[]){
        const destination=path.join(root,`${tool}-${PORTABLE_TOOLS[tool].version}-win32-x64`),lock=destination+'.lock';
        if(fs.existsSync(lock)){
            const stat=await fsp.lstat(lock);if(stat.isSymbolicLink())throw new Error('도구 잠금 경로가 올바르지 않습니다.');
            let owner:{pid?:number}={};try{owner=JSON.parse(await fsp.readFile(path.join(lock,'owner.json'),'utf8'));}catch{}
            if(Number.isInteger(owner.pid)){try{process.kill(owner.pid!,0);throw new Error('다른 VS Code 창에서 실행 도구를 준비 중입니다.');}catch(e){if((e as NodeJS.ErrnoException).code!=='ESRCH')throw e;}}
            else if(Date.now()-stat.mtimeMs<300000)throw new Error('최근 시작한 도구 준비가 있습니다. 잠시 후 다시 시도하세요.');
            if(path.dirname(lock)!==root)throw new Error('잘못된 잠금 경로');await fsp.rm(lock,{recursive:true,force:true});
        }
        if(fs.existsSync(destination)){
            if((await fsp.lstat(destination)).isSymbolicLink())throw new Error('도구 저장 경로가 올바르지 않습니다.');
            // Preserve the prior directory for diagnosis instead of deleting executable data.
            await fsp.rename(destination,destination+'.previous-'+Date.now());selected.delete(tool);
        }
    }
}
