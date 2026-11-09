/** AWS CodeBuild entry point. Executed only in the ephemeral build, never by the extension. */
import {promises as fs} from 'fs';
import * as path from 'path';
import {spawn} from 'child_process';
import {LocalPentagiRuntime, docker} from './localPentagi';
import {PentagiClient} from './pentagiClient';
import {runManagedAudit} from './managedAudit';
import {runManagedScans} from './managedScans';

export const WORKER_IMAGE='vxcontrol/kali-linux@sha256:7f803e5be80ec33dcf4228d9702ea1264658b43301e9d6bf3bce6cf8b1077399';
async function publishProgress(bucket:string,id:string):Promise<void> {
    if(!/^recoder-security-\d{12}-[a-z0-9-]+$/.test(bucket))throw new Error('결과 저장소가 올바르지 않습니다.');
    await new Promise<void>((resolve,reject)=>{
        const p=spawn('aws',['s3api','put-object','--bucket',bucket,'--key',`reports/${id}/progress.json`,'--body',path.resolve('report.json'),'--server-side-encryption','AES256','--no-cli-pager'],{shell:false,stdio:'ignore',env:{...process.env,AWS_MAX_ATTEMPTS:'1'}});
        const timer=setTimeout(()=>{p.kill();reject(new Error('진행 저장 시간 초과'));},15000);
        p.on('error',()=>{clearTimeout(timer);reject(new Error('진행 저장 실패'));});p.on('close',code=>{clearTimeout(timer);code?reject(new Error('진행 저장 실패')):resolve();});
    });
}
const MODELS=/^(?:(?:global|us|eu|apac)\.)?anthropic\.claude-[a-z0-9.:-]+$/;
export function validateManagedInput(x:any):{id:string;mode:'audit'|'redteam'|'scans';port:number;model:string} {
    if(!x||!/^rc-rt-[a-f0-9]{24}$/.test(x.id)||(x.mode!==undefined&&!['audit','redteam','scans'].includes(x.mode))||(!['audit','scans'].includes(x.mode)&&(!Number.isInteger(x.port)||x.port<1||x.port>65535))||(x.mode!=='scans'&&(typeof x.model!=='string'||!MODELS.test(x.model))))throw new Error('검사 요청 형식이 올바르지 않습니다.');
    return {id:x.id,mode:x.mode||'redteam',port:['audit','scans'].includes(x.mode)?3000:x.port,model:x.mode==='scans'?'':x.model};
}
async function credentials():Promise<{AccessKeyId:string;SecretAccessKey:string;SessionToken:string}> {
    return new Promise((resolve,reject)=>{
        const p=spawn('aws',['configure','export-credentials','--format','process'],{stdio:['ignore','pipe','ignore'],shell:false});let data='';
        const timer=setTimeout(()=>{p.kill();reject(new Error('작업 역할 인증 시간 초과'));},15000);
        p.stdout.on('data',c=>{data+=c;if(data.length>32768){p.kill();reject(new Error('작업 역할 인증 응답이 잘못되었습니다.'));}});
        p.on('error',()=>{clearTimeout(timer);reject(new Error('작업 역할을 확인하지 못했습니다.'));});
        p.on('close',code=>{clearTimeout(timer);try{const x=JSON.parse(data);if(code!==0||!x.AccessKeyId||!x.SecretAccessKey||!x.SessionToken)throw 0;resolve(x);}catch{reject(new Error('작업 역할을 확인하지 못했습니다.'));}});
    });
}
export async function runManagedWorker():Promise<void> {
    if(!process.env.CODEBUILD_BUILD_ID)throw new Error('관리형 작업 환경에서만 실행할 수 있습니다.');
    const input=validateManagedInput(JSON.parse(await fs.readFile(path.join(__dirname,'request.json'),'utf8')));
    if(input.id!==process.env.RECODER_RUN_ID||!process.env.RECODER_SOURCE_SHA256)throw new Error('작업 요청과 업로드 정보가 일치하지 않습니다.');
    const report:any={schema:1,runId:input.id,sourceSha256:process.env.RECODER_SOURCE_SHA256,mode:input.mode,status:'preparing',stage:'작업 준비',createdAt:new Date().toISOString(),tasks:[]};
    const secrets:string[]=[];let connection:{server:string;token:string}|undefined, client:PentagiClient|undefined, flowId:string|undefined,lastPublished=0;
    const persist=async()=>{
        let json=JSON.stringify({...report,updatedAt:new Date().toISOString()});for(const s of secrets)if(s)json=json.split(s).join('[REDACTED]');
        await fs.writeFile('report.json',json);console.log(JSON.stringify({stage:report.stage,status:report.status}));
        if(Date.now()-lastPublished>=5000&&process.env.RECODER_REPORT_BUCKET){lastPublished=Date.now();await publishProgress(process.env.RECODER_REPORT_BUCKET,input.id).catch(()=>{});}
    };
    const controller=new AbortController();const deadline=setTimeout(()=>controller.abort(),50*60*1000);
    process.once('SIGTERM',()=>controller.abort());process.once('SIGINT',()=>controller.abort());
    if(input.mode==='scans'){
        try {report.status='running';report.scans=await runManagedScans(input.id,async stage=>{report.stage=stage;await persist();},controller.signal);report.status=report.scans.checks.some((c:any)=>c.status==='failed')?'incomplete':'completed';report.stage='기본 검사 완료';}
        catch{report.status=controller.signal.aborted?'cancelled':'failed';report.error='기본 검사를 완료하지 못했습니다. 이미지 다운로드·빌드 또는 스캐너 상태를 확인하세요.';}
        finally{clearTimeout(deadline);await persist();}
        if(report.status!=='completed')process.exitCode=1;return;
    }
    if(input.mode==='audit'){
        try {
            report.status='running';report.audit=await runManagedAudit(input,async change=>{Object.assign(report,change);await persist();},controller.signal);
            report.status='completed';report.stage='소스 감사 완료 · 실행 검증 전';
        }catch(error){report.status=controller.signal.aborted?'cancelled':'failed';report.error=error instanceof Error&&error.message.startsWith('Bedrock ')?error.message:'코드 감사를 완료하지 못했습니다. Bedrock 모델 권한·지역 및 결과 형식을 확인하세요.';}
        finally{clearTimeout(deadline);await persist();}
        if(report.status!=='completed')process.exitCode=1;return;
    }
    const run:typeof docker=(args,options)=>docker(args,{...options,signal:controller.signal});
    const runtime=new LocalPentagiRuntime(input.id,run);
    try {
        await persist();const role=await credentials();secrets.push(role.AccessKeyId,role.SecretAccessKey,role.SessionToken);
        const configPath=path.resolve('recoder-bedrock.yml');
        const roles=['simple','simple_json','primary_agent','assistant','generator','refiner','adviser','reflector','searcher','enricher','coder','installer','pentester'];
        await fs.writeFile(configPath,roles.map(r=>`${r}:\n  model: ${input.model}\n  temperature: 1.0\n  max_tokens: 4096\n${r==='simple_json'?'  json: true\n':''}`).join(''),{mode:0o600});
        connection=await runtime.prepare('', 'bedrock',async stage=>{report.stage=stage;await persist();},{configPath,workerImage:WORKER_IMAGE,
            bedrockEnv:{BEDROCK_REGION:process.env.AWS_REGION||process.env.AWS_DEFAULT_REGION||'',BEDROCK_ACCESS_KEY_ID:role.AccessKeyId,BEDROCK_SECRET_ACCESS_KEY:role.SecretAccessKey,BEDROCK_SESSION_TOKEN:role.SessionToken},
            prepareTarget:async engine=>{
                const inner=(args:string[],timeout=60000)=>run(['exec',engine,'docker',...args],{timeout});
                report.stage='검사 도구 준비';await persist();await inner(['pull',WORKER_IMAGE],15*60*1000);
                // Dependency installation has internet access but cannot reach local/private/metadata addresses.
                for(const cidr of ['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','169.254.0.0/16','127.0.0.0/8'])
                    await run(['exec',engine,'iptables','-I','DOCKER-USER','-i','docker0','-d',cidr,'-j','REJECT']);
                // Keep the transfer outside DinD's private /tmp mount namespace.
                report.stage='프로젝트 사본 빌드';await persist();await run(['cp',path.resolve('source')+path.sep+'.',`${engine}:/recoder-source`]);
                await inner(['build','--network=default','-t','recoder-test-app','/recoder-source'],15*60*1000);
                // Isolated gateway mode leaves no host bridge address reachable from workload containers.
                await inner(['network','create','--internal','--subnet','172.30.0.0/24','-o','com.docker.network.bridge.gateway_mode_ipv4=isolated','recoder-isolated']);
                await inner(['run','-d','--name','recoder-test-app','--network','recoder-isolated','--ip','172.30.0.10','--cap-drop=ALL','--security-opt=no-new-privileges',
                    '--pids-limit','256','--memory','512m','--cpus','1','-e',`PORT=${input.port}`,'recoder-test-app']);
                report.stage='대상 접근·네트워크 격리 확인';await persist();
                // The test exits non-zero if ANY forbidden route or external DNS lookup works.
                const probe=`import socket,time\nfor host,port in [('1.1.1.1',443),('169.254.169.254',80),('172.30.0.1',2375),('172.17.0.1',2375)]:\n try:\n  s=socket.create_connection((host,port),2);s.close()\n except OSError: continue\n raise SystemExit('egress reachable')\ntry: socket.getaddrinfo('example.com',443)\nexcept OSError: pass\nelse: raise SystemExit('external DNS reachable')\nfor attempt in range(30):\n try:\n  s=socket.create_connection(('172.30.0.10',${input.port}),2);s.close();break\n except OSError: time.sleep(2)\nelse: raise SystemExit('target not ready')`;
                await inner(['run','--rm','--network','recoder-isolated','--cap-drop=ALL','--security-opt=no-new-privileges',WORKER_IMAGE,'python3','-c',probe],120000);
                report.isolationVerified=true;
            }});
        secrets.push(connection.token);client=new PentagiClient(connection.server,connection.token);
        if(!(await client.providers()).includes('bedrock'))throw new Error('PentAGI Bedrock 공급자 준비 실패');
        report.stage='레드팀 검사';report.status='running';await persist();const flow=await client.start(`http://172.30.0.10:${input.port}`,'bedrock');flowId=flow.id;
        const until=Date.now()+20*60*1000;
        while(!controller.signal.aborted&&Date.now()<until){
            const snapshot=await client.snapshot(flowId);report.tasks=snapshot.tasks;report.flow=snapshot.flow;await persist();
            if(['finished','failed','waiting'].includes(snapshot.flow.status)){report.status=snapshot.flow.status==='finished'?'completed':snapshot.flow.status==='waiting'?'needs_input':'failed';break;}
            await new Promise(r=>setTimeout(r,5000));
        }
        if(report.status==='running')report.status=controller.signal.aborted?'cancelled':'timed_out';
    } catch {report.status=controller.signal.aborted?'cancelled':'failed';report.error=`${report.stage} 단계에서 완료하지 못했습니다. AWS 모델 권한·앱 시작 조건·검사 환경을 확인하세요.`;}
    finally {
        clearTimeout(deadline);if(client&&flowId)await client.finish(flowId).catch(()=>{});
        report.stage='환경 정리';await persist();
        try {await new LocalPentagiRuntime(input.id).cleanup();report.cleaned=true;}catch{report.cleaned=false;report.status='cleanup_failed';}
        report.stage='결과 저장';await persist();
    }
    if(report.status!=='completed')process.exitCode=1;
}
if(require.main===module)void runManagedWorker().catch(()=>{console.error('검사 작업을 초기화하지 못했습니다.');process.exitCode=1;});
