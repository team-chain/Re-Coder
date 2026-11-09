import {ManagedAwsClient, AwsRequestError} from '../cloud/managedAwsClient';
import {managedTemplate,ManagedKind} from './managedTemplate';
import {Bundle} from './managedBundle';
import {managedReport,ManagedReport} from './managedReport';
export interface ManagedRun {id:string;createdAt:string;digest:string;files:number;bytes:number;mode?:'audit'|'redteam'|'scans';submission?:'uploading'|'starting';buildId?:string;status:string;stage?:string;terminal?:boolean;uncertain?:boolean;stopRequested?:boolean}
export class ManagedJobs {
    readonly bucket:string;
    private readonly project:string;
    private readonly lock:string;
    constructor(readonly client:ManagedAwsClient, readonly account:string,private kind:ManagedKind='security'){if(!/^\d{12}$/.test(account))throw new Error('AWS 계정이 올바르지 않습니다.');this.bucket=`recoder-${kind}-${account}-${client.region}`;this.project=`recoder-${kind}-v1`;this.lock=`recoder-${kind}-lock-v1`;}
    async status():Promise<{ready:boolean;stackStatus:string}> {
        let xml:string;
        try {xml=await this.client.cloudformation('DescribeStacks',{StackName:this.project});}
        catch(e){if(e instanceof AwsRequestError && e.code==='ValidationError')return {ready:false,stackStatus:'NOT_CREATED'};throw e;}
        const status=/<StackStatus>([A-Z_]+)<\/StackStatus>/.exec(xml)?.[1]||'UNKNOWN';
        // An existing similarly-named stack is not permission to use arbitrary resources.
        if(!xml.includes('<Key>recoder:purpose</Key>')||!xml.includes(`<Value>${this.kind}-runner-v1</Value>`))throw new Error('같은 이름의 AWS 스택이 ReCoder 실행 환경인지 확인할 수 없습니다.');
        return {ready:['CREATE_COMPLETE','UPDATE_COMPLETE'].includes(status),stackStatus:status};
    }
    async provision(){return this.client.cloudformation('CreateStack',{StackName:this.project,TemplateBody:managedTemplate(this.account,this.client.region,this.kind),
        'Capabilities.member.1':'CAPABILITY_IAM','Tags.member.1.Key':'recoder:purpose','Tags.member.1.Value':`${this.kind}-runner-v1`,
        'Tags.member.2.Key':'recoder:managed','Tags.member.2.Value':'true',OnFailure:'ROLLBACK',ClientRequestToken:this.project});}
    async start(run:ManagedRun,bundle:Bundle,beforeDispatch?:()=>Promise<void>):Promise<string> {
        // Cross-window/cross-device lease: CodeBuild's concurrency cap alone only queues duplicates.
        try {await this.claim(run);}
        catch(e){run.terminal=true;run.uncertain=false;run.status='NOT_STARTED';if(e instanceof AwsRequestError&&e.code==='ConditionalCheckFailedException')throw new Error('이 계정에서 다른 검사 요청을 처리 중입니다. 기존 작업 종료 후 다시 시도하세요.');throw e;}
        try {await this.client.object(this.bucket,`sources/${run.id}.zip`,'PUT',bundle.data);}
        catch(e){run.status='UPLOAD_FAILED';run.terminal=true;run.uncertain=false;await this.release(run,true).catch(()=>{});throw e;}
        try {if(beforeDispatch)await beforeDispatch();
            await this.client.dynamodb('UpdateItem',{TableName:this.lock,Key:{pk:{S:'active'}},UpdateExpression:'SET #phase = :starting',ConditionExpression:'runId = :id AND #phase = :claimed',ExpressionAttributeNames:{'#phase':'stage'},ExpressionAttributeValues:{':id':{S:run.id},':claimed':{S:'claimed'},':starting':{S:'starting'}}});
        }catch(e){run.status='NOT_STARTED';run.terminal=true;run.uncertain=false;await this.release(run,true).catch(()=>{});throw e;}
        const response=await this.client.codebuild('StartBuild',{projectName:this.project,idempotencyToken:run.id,
            sourceLocationOverride:`${this.bucket}/sources/${run.id}.zip`,sourceTypeOverride:'S3',
            environmentVariablesOverride:[{name:'RECODER_RUN_ID',value:run.id,type:'PLAINTEXT'},{name:'RECODER_SOURCE_SHA256',value:run.digest,type:'PLAINTEXT'},{name:'RECODER_REPORT_BUCKET',value:this.bucket,type:'PLAINTEXT'}],
            artifactsOverride:{type:'S3',location:this.bucket,path:'reports',namespaceType:'NONE',name:run.id,packaging:'NONE',encryptionDisabled:false}});
        const build=response.build;if(!this.owns(build,run))throw new Error('생성된 작업을 확인하지 못했습니다. 상태 확인으로 복구하세요.');return build.id;
    }
    private owns(build:any,run:ManagedRun):boolean {
        return !!build&&typeof build.id==='string'&&build.id.startsWith(this.project+':')&&build.projectName===this.project&&
            build.source?.location===`${this.bucket}/sources/${run.id}.zip`&&Array.isArray(build.environment?.environmentVariables)&&
            build.environment.environmentVariables.some((v:any)=>v.name==='RECODER_RUN_ID'&&v.value===run.id)&&
            build.environment.environmentVariables.some((v:any)=>v.name==='RECODER_SOURCE_SHA256'&&v.value===run.digest);
    }
    async refresh(run:ManagedRun,includeReport=true):Promise<{run:ManagedRun;report?:ManagedReport}> {
        if(run.terminal&&!run.buildId)return {run};
        let ids=run.buildId?[run.buildId]:(await this.client.codebuild('ListBuildsForProject',{projectName:this.project,sortOrder:'DESCENDING'})).ids||[];
        if(!ids.length)return {run};
        ids=ids.slice(0,100);const builds=(await this.client.codebuild('BatchGetBuilds',{ids})).builds||[];
        const matches=builds.filter((b:any)=>this.owns(b,run));if(matches.length===0&&!run.buildId)return {run};
        if(matches.length!==1)throw new Error('검사 작업을 하나로 확인하지 못했습니다. AWS 작업 목록에서 실행 여부를 확인하세요.');
        const build=matches[0];
        if(!['IN_PROGRESS','SUCCEEDED','FAILED','FAULT','STOPPED','TIMED_OUT'].includes(build.buildStatus))throw new Error('AWS 작업 상태를 읽지 못했습니다.');
        const next={...run,buildId:build.id,uncertain:false,status:build.buildStatus,stage:String(build.currentPhase||''),terminal:build.buildStatus!=='IN_PROGRESS'};
        if(!includeReport)return {run:next};
        return {run:next,report:await this.readReport(next)};
    }
    async readReport(run:ManagedRun):Promise<ManagedReport|undefined> {
        if(!run.buildId)return;
        let report:ManagedReport;
        try {report=managedReport(JSON.parse(await this.client.object(this.bucket,`reports/${run.id}/${run.terminal?'report':'progress'}.json`,'GET')),run);}
        catch(e){if(e instanceof AwsRequestError&&e.status===404)return;throw e;}
        // A worker report can describe failure but cannot turn failed compute into a successful test.
        if(run.status!=='SUCCEEDED'&&report.status==='completed')report={...report,status:run.terminal?'incomplete':'finalizing',error:run.terminal?'AWS 작업이 성공으로 종료되지 않아 완료 결과로 인정하지 않습니다.':undefined};
        return report;
    }
    async stop(run:ManagedRun){
        const checked=await this.refresh(run,false);if(checked.run.terminal)return;
        if(!checked.run.buildId){await this.release(run,true);run.terminal=true;run.uncertain=false;run.status='NOT_STARTED';return;}
        await this.client.codebuild('StopBuild',{id:checked.run.buildId});}
    async release(run:ManagedRun,claimedOnly=false){
        try {await this.client.dynamodb('DeleteItem',{TableName:this.lock,Key:{pk:{S:'active'}},ConditionExpression:'runId = :id'+(claimedOnly?' AND #phase = :claimed':''),ExpressionAttributeValues:{':id':{S:run.id},...(claimedOnly?{':claimed':{S:'claimed'}}:{})},...(claimedOnly?{ExpressionAttributeNames:{'#phase':'stage'}}:{})});}
        catch(e){if(!claimedOnly&&e instanceof AwsRequestError&&e.code==='ConditionalCheckFailedException')return;throw e;}
    }
    private async claim(run:ManagedRun){
        for(let attempt=0;attempt<2;attempt++){
            try {await this.client.dynamodb('PutItem',{TableName:this.lock,Item:{pk:{S:'active'},runId:{S:run.id},digest:{S:run.digest},createdAt:{S:run.createdAt},stage:{S:'claimed'},expiresAt:{N:String(Math.floor(Date.now()/1000)+75*60)}},ConditionExpression:'attribute_not_exists(pk) OR expiresAt < :now',ExpressionAttributeValues:{':now':{N:String(Math.floor(Date.now()/1000))}}});return;}
            catch(e){
                if(attempt||!(e instanceof AwsRequestError)||e.code!=='ConditionalCheckFailedException')throw e;
                const old=(await this.client.dynamodb('GetItem',{TableName:this.lock,Key:{pk:{S:'active'}},ConsistentRead:true})).Item;
                if(!old||!/^rc-rt-[a-f0-9]{24}$/.test(old.runId?.S)||typeof old.digest?.S!=='string')throw e;
                const prior:ManagedRun={id:old.runId.S,digest:old.digest.S,createdAt:old.createdAt?.S||'',files:0,bytes:0,status:'SUBMITTING',uncertain:true};
                const actual=await this.refresh(prior,false);if(!actual.run.terminal)throw e;
                await this.deleteSource(actual.run);
            }
        }
    }
    async deleteSource(run:ManagedRun){if(!run.terminal)throw new Error('진행 중인 검사 사본을 삭제할 수 없습니다.');await this.release(run);await this.client.object(this.bucket,`sources/${run.id}.zip`,'DELETE');}
}
