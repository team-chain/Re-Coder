import {promises as fs} from 'fs';
import * as path from 'path';
import {createHash} from 'crypto';
const OMIT_DIR = new Set(['.git','.aws','.ssh','.recoder','.vscode','node_modules','.venv','venv','dist','build','coverage','__pycache__']);
const OMIT_FILE = /(?:^\.env(?:\.|$)|\.(?:pem|key|p12|pfx|keystore|sqlite|db|log|vsix|bundle)$|^(?:credentials|id_rsa|id_ed25519|\.npmrc|\.pypirc)$)/i;
export interface Bundle {data:Buffer; digest:string; files:string[]; bytes:number}
function crc32(b:Buffer) {let n=0xffffffff;for(const x of b){n^=x;for(let i=0;i<8;i++)n=(n>>>1)^((n&1)?0xedb88320:0);}return(n^0xffffffff)>>>0;}
/** Store-only ZIP: no dependency on Git, a local archiver, npm or shell commands. */
export function zipFiles(files:Array<{name:string;data:Buffer}>):Buffer {
    const records:Buffer[]=[], directory:Buffer[]=[];let offset=0;
    for(const file of files){if(!/^(?:source|runtime)\//.test(file.name)||file.name.split('/').some(x=>!x||x==='.'||x==='..')||file.name.includes('\\'))throw new Error('업로드 경로가 올바르지 않습니다.');
        const name=Buffer.from(file.name), crc=crc32(file.data), h=Buffer.alloc(30);h.writeUInt32LE(0x04034b50);h.writeUInt16LE(20,4);h.writeUInt16LE(0x800,6);h.writeUInt32LE(crc,14);h.writeUInt32LE(file.data.length,18);h.writeUInt32LE(file.data.length,22);h.writeUInt16LE(name.length,26);
        const c=Buffer.alloc(46);c.writeUInt32LE(0x02014b50);c.writeUInt16LE(20,4);c.writeUInt16LE(20,6);c.writeUInt16LE(0x800,8);c.writeUInt32LE(crc,16);c.writeUInt32LE(file.data.length,20);c.writeUInt32LE(file.data.length,24);c.writeUInt16LE(name.length,28);c.writeUInt32LE(offset,42);
        records.push(h,name,file.data);directory.push(c,name);offset+=h.length+name.length+file.data.length;}
    const central=Buffer.concat(directory), end=Buffer.alloc(22);end.writeUInt32LE(0x06054b50);end.writeUInt16LE(files.length,8);end.writeUInt16LE(files.length,10);end.writeUInt32LE(central.length,12);end.writeUInt32LE(offset,16);
    return Buffer.concat([...records,central,end]);
}
export async function bundleSources(workspace:string):Promise<{files:Array<{name:string;data:Buffer}>;bytes:number}> {
    const root=await fs.realpath(workspace), files:Array<{name:string;data:Buffer}>=[];let bytes=0;
    async function walk(dir:string, prefix='') {
        for(const entry of (await fs.readdir(dir,{withFileTypes:true})).sort((a,b)=>a.name.localeCompare(b.name))){
            const rel=prefix+entry.name;if(OMIT_DIR.has(entry.name)||OMIT_FILE.test(entry.name)||(entry.name.startsWith('.')&&!['.dockerignore','.gitignore'].includes(entry.name)))continue;
            if(entry.isSymbolicLink())throw new Error('심볼릭 링크가 있는 프로젝트는 검사 사본을 만들 수 없습니다. 링크를 제외한 프로젝트 폴더를 선택하세요.');
            const p=path.join(dir,entry.name), actual=await fs.realpath(p);if(!actual.startsWith(root+path.sep))throw new Error('프로젝트 밖 파일은 업로드할 수 없습니다.');
            if(entry.isDirectory()){await walk(p,rel+'/');continue;}if(!entry.isFile())continue;
            const stat=await fs.lstat(p);if(stat.isSymbolicLink()||stat.size>5*1024*1024)throw new Error(`업로드할 수 없는 파일: ${rel}`);
            const handle=await fs.open(p,'r');let data:Buffer;
            try {
                const opened=await handle.stat();
                if(!opened.isFile()||opened.ino!==stat.ino||opened.dev!==stat.dev||opened.size>5*1024*1024)throw new Error(`스냅샷 도중 파일이 변경되었습니다: ${rel}`);
                // Allocate per file, not 5MB per entry: slices retain the whole backing buffer.
                const buffer=Buffer.alloc(opened.size+1);let offset=0;
                while(offset<buffer.length){const read=await handle.read(buffer,offset,buffer.length-offset,offset);if(!read.bytesRead)break;offset+=read.bytesRead;}
                const after=await handle.stat();
                if(offset!==opened.size||after.size!==opened.size||after.mtimeMs!==opened.mtimeMs||await fs.realpath(p)!==actual)throw new Error(`스냅샷 도중 파일이 변경되었습니다: ${rel}`);
                data=buffer.subarray(0,offset);
            } finally {await handle.close();}
            bytes+=data.length;if(bytes>32*1024*1024||files.length>=4000)throw new Error('검사 사본은 최대 32MB·4,000개 파일입니다. 검사할 하위 프로젝트를 선택하세요.');
            files.push({name:'source/'+rel,data});
        }
    }
    await walk(root);
    return {files,bytes};
}
export async function projectBundle(workspace:string, runtimeDir:string, config:unknown):Promise<Bundle> {
    const {files,bytes}=await bundleSources(workspace);
    if(!['audit','scans'].includes((config as {mode?:string})?.mode||'')&&!files.some(f=>f.name==='source/Dockerfile'))throw new Error('프로젝트 루트에 Dockerfile이 필요합니다. Deploy에서 먼저 Dockerfile을 준비하세요.');
    const names=files.map(f=>f.name.slice(7));
    for(const name of ['managedPentagiWorker.js','managedAudit.js','managedScans.js','localPentagi.js','pentagiClient.js','pentagiAssessment.js','localRedTeamScope.js','redTeamAccess.js','redTeamCapacity.js']) files.push({name:'runtime/'+name,data:await fs.readFile(path.join(runtimeDir,name))});
    files.push({name:'runtime/policy.rego',data:await fs.readFile(path.resolve(runtimeDir,'../../media/security-audit/deploy.rego'))});
    for(const name of ['SKILL.md','RECONNAISSANCE.md','HUNTING.md','ATTACK-CLASSES.md','VALIDATION-AND-REPORTING.md','LICENSE','REVISION']) files.push({name:'runtime/audit/'+name,data:await fs.readFile(path.resolve(runtimeDir,'../../media/security-audit',name))});
    files.push({name:'runtime/request.json',data:Buffer.from(JSON.stringify(config))});
    const data=zipFiles(files);return {data,digest:createHash('sha256').update(data).digest('hex'),files:names,bytes};
}
