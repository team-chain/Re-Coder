import {createHash, createHmac} from 'crypto';
import {awsRegion, CloudCredentials} from './awsTransport';

export class AwsRequestError extends Error {
    constructor(readonly status: number, readonly code: string) {super(`AWS 요청 실패 (${status}, ${code}). 계정 권한과 리전을 확인하세요.`);}
}
const sha = (x: Buffer|string) => createHash('sha256').update(x).digest('hex');
const mac = (key: Buffer|string, x: string) => createHmac('sha256', key).update(x).digest();
const encode = (s: string) => encodeURIComponent(s).replace(/[!'()*]/g, c=>'%'+c.charCodeAt(0).toString(16).toUpperCase());
export function signedManagedRequest(service: 'cloudformation'|'codebuild'|'s3'|'dynamodb', region: string,
    credentials: CloudCredentials, body: Buffer, options: {method?: string; key?: string; bucket?: string; target?: string; contentType?: string} = {}, now=new Date()) {
    awsRegion(region);
    if(options.bucket && !/^recoder-(?:security|build|container)-\d{12}-[a-z0-9-]+$/.test(options.bucket)) throw new Error('관리형 실행 저장소가 올바르지 않습니다.');
    const host = options.bucket ? `${options.bucket}.s3.${region}.amazonaws.com` : `${service}.${region}.amazonaws.com`;
    const uri = '/' + (options.key || '').split('/').map(encode).join('/');
    const date = now.toISOString().replace(/[:-]|\.\d{3}/g, '');
    const headers: Record<string,string> = {host, 'x-amz-date':date, 'x-amz-security-token':credentials.sessionToken,
        'x-amz-content-sha256':sha(body), 'content-type': options.contentType || 'application/octet-stream'};
    if(options.target) headers['x-amz-target']=options.target;
    const keys=Object.keys(headers).sort(), scope=`${date.slice(0,8)}/${region}/${service}/aws4_request`;
    const request=`${options.method||'POST'}\n${uri}\n\n${keys.map(k=>`${k}:${headers[k].trim()}\n`).join('')}\n${keys.join(';')}\n${sha(body)}`;
    const key=mac(mac(mac(mac('AWS4'+credentials.secretAccessKey,date.slice(0,8)),region),service),'aws4_request');
    headers.Authorization=`AWS4-HMAC-SHA256 Credential=${credentials.accessKeyId}/${scope}, SignedHeaders=${keys.join(';')}, Signature=${mac(key,`AWS4-HMAC-SHA256\n${date}\n${scope}\n${sha(request)}`).toString('hex')}`;
    return {url:`https://${host}${uri}`,headers};
}
export class ManagedAwsClient {
    constructor(readonly region: string, private credentials: ()=>Promise<CloudCredentials>, private fetcher: typeof fetch=fetch,
        private relay?:(service:Parameters<typeof signedManagedRequest>[0],body:Buffer,options:Parameters<typeof signedManagedRequest>[4],limit:number)=>Promise<Buffer>) {awsRegion(region);}
    async request(service:'cloudformation'|'codebuild'|'s3'|'dynamodb', body:Buffer, options:Parameters<typeof signedManagedRequest>[4]={}):Promise<string> {
        return (await this.requestBytes(service,body,options,4*1024*1024)).toString('utf8');
    }
    private async requestBytes(service:'cloudformation'|'codebuild'|'s3'|'dynamodb', body:Buffer, options:Parameters<typeof signedManagedRequest>[4],limit:number):Promise<Buffer> {
        if(this.relay)return this.relay(service,body,options,limit);
        const signed=signedManagedRequest(service,this.region,await this.credentials(),body,options);
        let response:Response;
        try { response=await this.fetcher(signed.url,{method:options.method||'POST',headers:signed.headers,
            ...(options.method==='GET'||options.method==='DELETE'?{}:{body:new Uint8Array(body)}),redirect:'error',signal:AbortSignal.timeout(60000)}); }
        catch {throw new Error('AWS 응답을 받지 못했습니다. 실행을 다시 만들기 전에 상태 확인으로 복구하세요.');}
        if(!response.body && response.ok) return Buffer.alloc(0);
        if(!response.body) throw new AwsRequestError(response.status,'EmptyResponse');
        const reader=response.body.getReader(), chunks:Uint8Array[]=[];let size=0;
        try {while(true){const p=await reader.read();if(p.done)break;size+=p.value.length;if(size>(response.ok?limit:4*1024*1024))throw new Error('AWS 응답 크기 제한을 초과했습니다.');chunks.push(p.value);}}
        finally {await reader.cancel().catch(()=>{});}
        const data=Buffer.concat(chunks);
        if(!response.ok){const text=data.toString('utf8');let code=/<Code>([A-Za-z0-9]+)<\/Code>/.exec(text)?.[1]||'RequestFailed';try{code=String(JSON.parse(text).__type||code).split('#').pop()!.replace(/[^A-Za-z0-9]/g,'').slice(0,80);}catch{} throw new AwsRequestError(response.status,code);}
        return data;
    }
    async cloudformation(action:string, values:Record<string,string>={}) {return this.request('cloudformation',Buffer.from(new URLSearchParams({Action:action,Version:'2010-05-15',...values}).toString()),{contentType:'application/x-www-form-urlencoded'});}
    async codebuild(action:string, values:unknown) {return JSON.parse(await this.request('codebuild',Buffer.from(JSON.stringify(values)),{contentType:'application/x-amz-json-1.1',target:`CodeBuild_20161006.${action}`}));}
    async dynamodb(action:string,values:unknown){return JSON.parse(await this.request('dynamodb',Buffer.from(JSON.stringify(values)),{contentType:'application/x-amz-json-1.0',target:`DynamoDB_20120810.${action}`}));}
    async object(bucket:string,key:string,method:'GET'|'PUT'|'DELETE',body:Buffer=Buffer.alloc(0)) {return this.request('s3',body,{method,bucket,key});}
    async objectBytes(bucket:string,key:string) {return this.requestBytes('s3',Buffer.alloc(0),{method:'GET',bucket,key},5*1024*1024);}
}
