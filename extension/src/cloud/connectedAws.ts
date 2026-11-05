import type {CoreManager} from '../core/CoreManager';
import {ApiClient} from '../core/ApiClient';
import {ManagedAwsClient,AwsRequestError} from './managedAwsClient';
import {awsRegion} from './awsTransport';

export interface ManagedConnection {connected:boolean;account?:string;region?:string;connection_id?:string}
export interface ConnectedManagedAws {metadata:{account:string;region:string};client:ManagedAwsClient}

/** One existing AWS connection for every feature. Keys remain inside the Core/provider. */
export async function connectedManagedAws(core:CoreManager):Promise<ConnectedManagedAws|undefined>{
    await core.ensureRunning();
    const api=new ApiClient(core),session=await api.getManagedAwsSession();
    if(!session.connected)return;
    if(!/^\d{12}$/.test(session.account||'')||!session.region||!session.connection_id||!/^[a-f0-9]{32}$/.test(session.connection_id))throw new Error('기존 AWS 연결 정보를 확인하지 못했습니다.');
    const region=awsRegion(session.region),account=session.account!,connection_id=session.connection_id;
    const client=new ManagedAwsClient(region,async()=>{throw new Error('AWS 자격증명은 Core에서만 사용합니다.');},fetch,async(service,body,options={},limit)=>{
        const result=await api.managedAwsRequest({account,region,connection_id,service,body_base64:body.toString('base64'),limit,
            method:options.method||'POST',bucket:options.bucket||'',key:options.key||'',target:options.target||'',content_type:options.contentType||'application/octet-stream'});
        if(!Number.isInteger(result.status)||result.status<100||result.status>599)throw new Error('관리형 AWS 응답 형식이 잘못되었습니다.');
        if(result.status<200||result.status>=300)throw new AwsRequestError(result.status,result.error_code||'RequestFailed');
        if(typeof result.body_base64!=='string'||result.body_base64.length>Math.ceil(limit/3)*4)throw new Error('AWS 응답 크기 제한을 초과했습니다.');
        const data=Buffer.from(result.body_base64,'base64');if(data.length>limit)throw new Error('AWS 응답 크기 제한을 초과했습니다.');return data;
    });
    return {metadata:{account,region},client};
}
