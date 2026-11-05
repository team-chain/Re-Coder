/** Trusted CodeBuild controller. Project scripts run in a separate, credential-free Docker engine. */
import {promises as fs} from 'fs';
import * as path from 'path';
import {spawn} from 'child_process';
import {createHash} from 'crypto';
import {PENTAGI_IMAGES} from './localPentagi';

export const FRONTEND_IMAGE='node@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914';
export interface FrontendRequest {id:string;projectDir:string;outDir:string;script:string}
export function safeArtifactPath(value:unknown):value is string {
    return typeof value==='string'&&value.length>0&&value.length<=500&&!value.includes('\\')&&!value.includes(':')&&
        value.split('/').every(p=>p!==''&&p!=='.'&&p!=='..'&&!p.startsWith('.')&&!/[\x00-\x1f<>"|?*]/.test(p)&&!/[. ]$/.test(p)&&! /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(p));
}
export function frontendRequest(x:any):FrontendRequest {
    if(!x||!/^rc-rt-[a-f0-9]{24}$/.test(x.id)||(x.projectDir!==''&&!safeArtifactPath(x.projectDir))||!safeArtifactPath(x.outDir)||
       !/^[\w:.-]{1,100}$/.test(x.script)||x.script.startsWith('-')||
       (x.projectDir&&!x.outDir.startsWith(x.projectDir+'/'))||x.outDir===x.projectDir)throw new Error('관리형 프런트엔드 빌드 경로가 올바르지 않습니다.');
    return {id:x.id,projectDir:x.projectDir,outDir:x.outDir,script:x.script};
}
export interface FrontendManifest {schema:1;runId:string;sourceSha256:string;status:'completed';files:Array<{path:string;size:number;sha256:string}>}
export function frontendManifest(x:any,id:string,digest:string):FrontendManifest {
    if(!x||x.schema!==1||x.runId!==id||x.sourceSha256!==digest||x.status!=='completed'||!Array.isArray(x.files)||!x.files.length||x.files.length>2000)throw new Error('원격 빌드 결과를 확인하지 못했습니다.');
    let total=0;const names=new Set<string>();
    for(const f of x.files){
        if(!f||!safeArtifactPath(f.path)||!Number.isInteger(f.size)||f.size<0||f.size>5*1024*1024||!/^[a-f0-9]{64}$/.test(f.sha256))throw new Error('빌드 산출물 정보가 올바르지 않습니다.');
        const key=f.path.toLowerCase();if(names.has(key))throw new Error('빌드 산출물 경로가 중복됩니다.');names.add(key);total+=f.size;
    }
    if(total>32*1024*1024||!names.has('index.html'))throw new Error('정적 사이트 결과가 없거나 크기 제한(32MB)을 초과했습니다.');
    // No file may be a parent directory of another file (including Windows case folding).
    for(const name of names){const parts=name.split('/');parts.pop();while(parts.length){if(names.has(parts.join('/')))throw new Error('빌드 산출물 파일과 폴더가 충돌합니다.');parts.pop();}}
    return x;
}
// This code runs in a NEW trusted Node container with a read-only output volume, never in the build process.
export const COLLECT_OUTPUT = `const fs=require('fs'),path=require('path');
const root=path.resolve('/work',process.argv[1]);let size=0;const files=[];
const rootStat=fs.lstatSync(root);if(!rootStat.isDirectory()||rootStat.isSymbolicLink()||fs.realpathSync(root)!==root)throw Error('Output directory');
function walk(dir,prefix=''){for(const e of fs.readdirSync(dir,{withFileTypes:true})){const name=prefix+e.name,p=path.join(dir,e.name);const st=fs.lstatSync(p);if(st.isSymbolicLink())throw Error('Output symlink');if(st.isDirectory()){walk(p,name+'/');continue;}if(!st.isFile()||st.size>5*1024*1024)throw Error('Output file size');if(e.name.endsWith('.map'))continue;size+=st.size;if(size>32*1024*1024||files.length>=2000)throw Error('Output total size');const data=fs.readFileSync(p);files.push({path:name,data:data.toString('base64')});}}
walk(root);process.stdout.write(JSON.stringify(files));`;

function command(file:string,args:string[],timeout=60000):Promise<string>{
    return new Promise((resolve,reject)=>{
        const child=spawn(file,args,{shell:false,stdio:['ignore','pipe','pipe']});let output='';let done=false;
        const finish=(error?:Error)=>{if(done)return;done=true;clearTimeout(timer);error?reject(error):resolve(output);};
        const timer=setTimeout(()=>{child.kill();finish(new Error('관리형 빌드 실행 시간이 초과되었습니다.'));},timeout);
        child.stdout.on('data',c=>{output+=c;if(output.length>48*1024*1024){child.kill();finish(new Error('빌드 출력 크기를 초과했습니다.'));}});
        // Retain no project stdout/stderr in reports: npm scripts can print embedded secrets.
        child.stderr.resume();
        child.on('error',()=>finish(new Error('관리형 빌드 도구를 실행할 수 없습니다.')));
        child.on('close',code=>finish(code===0?undefined:new Error(`${file} 작업 실패 (${code}).`)));
    });
}
export async function runFrontendWorker(){
    if(!process.env.CODEBUILD_BUILD_ID)throw new Error('AWS 관리형 빌드 환경이 아닙니다.');
    const request=frontendRequest(JSON.parse(await fs.readFile(path.join(__dirname,'request.json'),'utf8')));
    const bucket=process.env.RECODER_REPORT_BUCKET||'',digest=process.env.RECODER_SOURCE_SHA256||'';
    if(request.id!==process.env.RECODER_RUN_ID||!/^[a-f0-9]{64}$/.test(digest)||!/^recoder-build-\d{12}-[a-z0-9-]+$/.test(bucket))throw new Error('빌드 요청 식별자가 다릅니다.');
    const engine=request.id+'-frontend',volume=request.id+'-data',label=['--label',`io.recoder.build=${request.id}`];
    const report:any={schema:1,runId:request.id,sourceSha256:digest,status:'running',stage:'빌드 환경 준비',files:[]};
    const put=(file:string,key:string)=>command('aws',['s3api','put-object','--bucket',bucket,'--key',`reports/${request.id}/${key}`,'--body',file,'--server-side-encryption','AES256','--no-cli-pager']);
    const persist=async(stage:string)=>{report.stage=stage;await fs.writeFile('report.json',JSON.stringify(report));await put(path.resolve('report.json'),'progress.json');};
    const docker=(args:string[],timeout?:number)=>command('docker',args,timeout);
    const inner=(args:string[],timeout?:number)=>docker(['exec',engine,'docker',...args],timeout);
    let engineCreated=false,volumeCreated=false;
    try{
        await persist('독립된 빌드 환경 준비');
        await docker(['pull',PENTAGI_IMAGES.engine],600000);
        await docker(['volume','create',...label,volume]);volumeCreated=true;
        await docker(['run','-d','--name',engine,...label,'--privileged','--memory','5g','--cpus','2','--pids-limit','2048','-e','DOCKER_TLS_CERTDIR=','-v',volume+':/var/lib/docker',PENTAGI_IMAGES.engine,'dockerd','--host=unix:///var/run/docker.sock']);engineCreated=true;
        let ready=false;for(let i=0;i<30;i++){try{await inner(['info'],5000);ready=true;break;}catch{await new Promise(r=>setTimeout(r,1000));}}
        if(!ready)throw new Error('독립 빌드 엔진이 준비되지 않았습니다.');
        // App dependency downloads are permitted, access to private networks/AWS task credentials is denied.
        for(const cidr of ['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','169.254.0.0/16','127.0.0.0/8'])await docker(['exec',engine,'iptables','-I','DOCKER-USER','-i','docker0','-d',cidr,'-j','REJECT']);
        await inner(['pull',FRONTEND_IMAGE],600000);
        // Verify the credential endpoint cannot be read before copying the project's files.
        await inner(['run','--rm','--cap-drop=ALL',FRONTEND_IMAGE,'node','-e',`fetch('http://169.254.170.2',{signal:AbortSignal.timeout(3000)}).then(()=>process.exit(1),()=>process.exit(0))`],15000);
        await docker(['cp',path.resolve('source')+'/.',engine+':/source']);
        const script='if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi && npm run "$1"';
        await inner(['create','--name','frontend','--cap-drop=ALL','--security-opt=no-new-privileges','--memory','3g','--cpus','2','--pids-limit','512','-v','output:/work','-w','/work/'+request.projectDir,'-e','GENERATE_SOURCEMAP=false','-e','CI=false','-e','BROWSER=none',FRONTEND_IMAGE,'sh','-c',script,'build',request.script]);
        await inner(['cp','/source/.','frontend:/work']);
        await persist('의존성 설치 및 화면 빌드 중');
        await inner(['start','-a','frontend'],30*60*1000);
        const exit=(await inner(['inspect','--format','{{.State.ExitCode}}','frontend'])).trim();
        if(exit!=='0')throw new Error('프로젝트 빌드 명령이 실패했습니다. package.json·잠금 파일·공개 환경변수를 확인하세요.');
        await persist('정적 파일 검증 및 저장');
        const files=JSON.parse(await inner(['run','--rm','--network','none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--memory','256m','--pids-limit','64','-v','output:/work:ro',FRONTEND_IMAGE,'node','-e',COLLECT_OUTPUT,request.outDir],120000));
        if(!Array.isArray(files))throw new Error('빌드 산출물을 읽지 못했습니다.');
        report.files=files.map(f=>{const data=Buffer.from(f.data,'base64');return {path:f.path,size:data.length,sha256:createHash('sha256').update(data).digest('hex')};});
        frontendManifest({...report,status:'completed'},request.id,digest);
        for(let i=0;i<files.length;i++){
            // Untrusted paths never become host paths or S3 keys.
            await fs.writeFile('artifact.bin',Buffer.from(files[i].data,'base64'));await put(path.resolve('artifact.bin'),`files/${i}`);
        }
        report.status='completed';report.stage='화면 빌드 완료';
    }catch(e){report.status='failed';report.files=[];report.stage=e instanceof Error?e.message:'원격 빌드를 완료하지 못했습니다.';process.exitCode=1;}
    finally{
        // Names were generated by the host and never supplied by project code.
        if(engineCreated)await docker(['rm','-f','-v',engine]).catch(()=>{});
        if(volumeCreated)await docker(['volume','rm',volume]).catch(()=>{});
        await fs.writeFile('report.json',JSON.stringify(report));await put(path.resolve('report.json'),'report.json');
    }
}
if(require.main===module)runFrontendWorker().catch(()=>{console.error('관리형 빌드를 완료하지 못했습니다.');process.exitCode=1;});
