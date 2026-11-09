const test=require('node:test'),assert=require('node:assert/strict');
const {generateKeyPairSync,createPublicKey,verify}=require('node:crypto');
const {browserCallback,dpopProof,AwsConsoleSession}=require('../out/cloud/awsConsoleLogin');
const {CredentialBridge,removeAmbientAwsCredentials}=require('../out/cloud/credentialBridge');
test('OAuth callback rejects wrong state without consuming the legitimate login',async()=>{
 const login=await browserCallback('ap-northeast-2','v'.repeat(48),5000);const url=new URL(login.url),redirect=url.searchParams.get('redirect_uri');
 try{assert.equal(url.searchParams.get('code_challenge_method'),'SHA-256');assert.notEqual(url.searchParams.get('code_challenge'),'v'.repeat(48));
 assert.equal((await fetch(redirect+'?code=bad&state=incorrect')).status,400);
 const response=await fetch(redirect+'?code=actual-code&state='+encodeURIComponent(url.searchParams.get('state')));assert.equal(response.status,200);assert.equal((await login.result).code,'actual-code');
 }finally{login.cancel();}
});
test('DPoP proof binds an ephemeral public key and AWS token endpoint',()=>{
 const privateKey=generateKeyPairSync('ec',{namedCurve:'prime256v1'}).privateKey.export({type:'pkcs8',format:'pem'}).toString();const endpoint='https://ap-northeast-2.signin.aws.amazon.com/v1/token';
 const token=dpopProof(privateKey,endpoint),[header,payload,signature]=token.split('.');const h=JSON.parse(Buffer.from(header,'base64url')),p=JSON.parse(Buffer.from(payload,'base64url'));
 assert.equal(h.jwk.d,undefined);assert.equal(p.htm,'POST');assert.equal(p.htu,endpoint);assert.ok(verify('sha256',Buffer.from(header+'.'+payload),{key:createPublicKey({key:h.jwk,format:'jwk'}),dsaEncoding:'ieee-p1363'},Buffer.from(signature,'base64url')));
});
test('credential bridge refuses browser origins, wrong tokens and paths and masks provider errors',async()=>{
 let calls=0,fail=false;const bridge=new CredentialBridge(async()=>{calls++;if(fail)throw Error('SECRET_MUST_STAY_PRIVATE');return {accessKeyId:'test',secretAccessKey:'test-secret',sessionToken:'session',expiration:new Date(Date.now()+3600000).toISOString()};});
 const env=await bridge.start(),url=env.AWS_CONTAINER_CREDENTIALS_FULL_URI,Authorization=env.AWS_CONTAINER_AUTHORIZATION_TOKEN;
 try{for(const headers of [{},{Authorization:'wrong'},{Authorization,Origin:'https://evil.example'}])assert.equal((await fetch(url,{headers})).status,403);assert.equal(calls,0);
 assert.equal((await fetch(url+'?x=1',{headers:{Authorization}})).status,403);
 const accepted=await fetch(url,{headers:{Authorization}});assert.equal(accepted.status,200);assert.equal(accepted.headers.get('cache-control'),'no-store');
 fail=true;const denied=await fetch(url,{headers:{Authorization}});assert.equal(denied.status,503);assert.doesNotMatch(await denied.text(),/SECRET_MUST/);
 }finally{bridge.dispose();}await assert.rejects(()=>bridge.start());
});
test('corrupt session data cannot expose stored secrets; ambient credentials are removed',async()=>{
 const session=new AwsConsoleSession({get:async()=>'{SECRET_MUST_STAY_PRIVATE',delete:async()=>{}});await assert.rejects(()=>session.metadata(),e=>!e.message.includes('SECRET_MUST'));await session.logout();
 const env={AWS_PROFILE:'wrong',AWS_ACCESS_KEY_ID:'secret',AWS_ENDPOINT_URL:'http://evil',AWS_REGION:'ap-northeast-2',PATH:'keep',RECODER_ASSUME_ROLE_ARN:'wrong'};removeAmbientAwsCredentials(env);assert.deepEqual(env,{AWS_REGION:'ap-northeast-2',PATH:'keep'});
});
