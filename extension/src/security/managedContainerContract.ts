/** Wire contract shared by the trusted worker and extension. Core independently verifies AWS evidence. */
import {ScanSummary} from './managedScans';
export interface ManagedContainerReference {run_id:string;build_id:string;source_sha256:string}
export interface SourceFile {path:string;size:number;sha256:string}
export interface ContainerReport {
    schema:1;runId:string;sourceSha256:string;buildId:string;status:'completed';
    image:string;imageDigest:string;sourceFiles:SourceFile[];checks:ScanSummary[];
    policy:{decision:'allow'};sbomSha256:string;
}
export function safeSourcePath(name:unknown):name is string {
    return typeof name==='string'&&name.length<=1024&&!name.split('/').some(p=>!p||p==='.'||p==='..'||/[\\:\x00-\x1f<>"|?*]/.test(p)||/[. ]$/.test(p)||/^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(p));
}
export function sourceManifest(value:unknown):SourceFile[]{
    if(!Array.isArray(value)||!value.length||value.length>4000)throw new Error('빌드 소스 목록이 올바르지 않습니다.');
    const names=new Set<string>();let bytes=0;
    for(const f of value){
        if(!f||!safeSourcePath(f.path)||!Number.isInteger(f.size)||f.size<0||f.size>5*1024*1024||typeof f.sha256!=='string'||!/^[a-f0-9]{64}$/.test(f.sha256)||names.has(f.path.toLowerCase()))throw new Error('빌드 소스 파일 정보가 올바르지 않습니다.');
        names.add(f.path.toLowerCase());bytes+=f.size;
    }
    if(bytes>32*1024*1024||!value.some(f=>f.path==='Dockerfile'))throw new Error('Dockerfile이 없거나 빌드 소스가 너무 큽니다.');
    return value;
}
export function containerReport(value:any,ref:ManagedContainerReference,account:string,region:string):ContainerReport {
    if(!/^rc-rt-[a-f0-9]{24}$/.test(ref.run_id)||!/^recoder-container-v1:[a-f0-9-]{36}$/.test(ref.build_id)||!/^[a-f0-9]{64}$/.test(ref.source_sha256))throw new Error('관리형 이미지 식별자가 올바르지 않습니다.');
    if(!value||value.schema!==1||value.runId!==ref.run_id||value.sourceSha256!==ref.source_sha256||value.buildId!==ref.build_id||value.status!=='completed')throw new Error('AWS 이미지 빌드가 완료되지 않았거나 결과가 다릅니다.');
    if(!/^sha256:[a-f0-9]{64}$/.test(value.imageDigest)||value.image!==`${account}.dkr.ecr.${region}.amazonaws.com/recoder-managed-images-v1@${value.imageDigest}`||!/^[a-f0-9]{64}$/.test(value.sbomSha256))throw new Error('이미지 또는 SBOM 식별자가 올바르지 않습니다.');
    sourceManifest(value.sourceFiles);
    if(value.policy?.decision!=='allow'||!Array.isArray(value.checks)||value.checks.length!==4)throw new Error('배포 검사 결과가 없습니다.');
    for(const tool of ['gitleaks','hadolint','dependencies','image']){
        const matches=value.checks.filter((c:any)=>c?.tool===tool);if(matches.length!==1)throw new Error('배포 검사 결과가 중복되거나 없습니다.');const c=matches[0];
        if(tool==='dependencies'&&c.status==='not_applicable'&&Array.isArray(c.findings)&&c.findings.length===0)continue;
        if(c.status!=='completed'||!Array.isArray(c.findings)||c.findings.length>(tool==='image'?5000:200)||['count','criticalCount','highCount','blockingCount'].some(k=>!Number.isInteger(c[k])||c[k]<0)||c.count<c.findings.length||c.blockingCount!==c.criticalCount+c.highCount||c.blockingCount>c.count||tool==='image'&&c.count!==c.findings.length)throw new Error('배포 검사를 완료하지 못했거나 이미지 결과가 너무 큽니다.');
        if(c.findings.some((f:any)=>!f||typeof f.title!=='string'||!['critical','high','medium','low','unknown','negligible'].includes(f.severity)))throw new Error('검사 상세 결과가 올바르지 않습니다.');
        for(const [key,severity] of [['criticalCount','critical'],['highCount','high']])if(c[key]<c.findings.filter((f:any)=>f.severity===severity).length)throw new Error('검사 요약과 상세 결과가 다릅니다.');
        if(tool==='gitleaks'&&c.count!==0||tool==='hadolint'&&c.blockingCount!==0||tool==='image'&&c.criticalCount!==0)throw new Error('배포를 차단하는 보안 문제가 있습니다.');
    }
    return value;
}
