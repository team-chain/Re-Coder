const test=require('node:test'),assert=require('node:assert/strict'),http=require('node:http');
const {connectedManagedAws}=require('../out/cloud/connectedAws');
const {AwsRequestError}=require('../out/cloud/managedAwsClient');

async function fixture(fn){
 let connected=true,denied=false,changed=false;const requests=[];
 const server=http.createServer(async(req,res)=>{
  assert.equal(req.headers['x-session-token'],'core-session');res.setHeader('Content-Type','application/json');
  if(req.url==='/api/aws/managed/session'){res.end(JSON.stringify(connected?{connected:true,account:'123456789012',region:'us-east-1',connection_id:'a'.repeat(32)}:{connected:false}));return;}
  assert.equal(req.url,'/api/aws/managed/request');const chunks=[];for await(const c of req)chunks.push(c);const body=JSON.parse(Buffer.concat(chunks));requests.push(body);
  if(changed){res.statusCode=409;res.end(JSON.stringify({detail:'AWS 연결이 변경되었습니다.'}));return;}
  res.end(JSON.stringify(denied?{status:403,error_code:'AccessDenied'}:{status:200,body_base64:Buffer.from('<StackStatus>CREATE_COMPLETE</StackStatus>').toString('base64')}));
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const core={ensureRunning:async()=>{},getPort:()=>server.address().port,getSessionToken:()=> 'core-session',refreshToken:async()=>{},isBrowserAwsLogin:()=>{throw Error('must not require browser login');},getBrowserAwsSession:()=>{throw Error('must not read a separate credential store');}};
 try{await fn({core,requests,setConnected:v=>connected=v,setDenied:v=>denied=v,setChanged:v=>changed=v});}
 finally{server.closeAllConnections();await new Promise(r=>server.close(r));}
}
test('all managed namespaces use the existing Core connection without a new login or key export',()=>fixture(async({core,requests})=>{
 const connection=await connectedManagedAws(core);assert.deepEqual(connection.metadata,{account:'123456789012',region:'us-east-1'});
 for(const kind of ['security','build','container'])assert.match(await connection.client.cloudformation('DescribeStacks',{StackName:`recoder-${kind}-v1`}),/CREATE_COMPLETE/);
 assert.equal(requests.length,3);for(const r of requests){assert.equal(r.account,'123456789012');assert.equal(r.connection_id,'a'.repeat(32));assert.equal(r.service,'cloudformation');assert.ok(!('credentials' in r));}
}));
test('permission denial is preserved and a connection change never retries a job request',()=>fixture(async({core,requests,setDenied,setChanged})=>{
 const connection=await connectedManagedAws(core);setDenied(true);
 await assert.rejects(()=>connection.client.cloudformation('DescribeStacks',{StackName:'recoder-security-v1'}),e=>e instanceof AwsRequestError&&e.code==='AccessDenied');
 assert.equal(requests.length,1);setChanged(true);
 await assert.rejects(()=>connection.client.codebuild('StartBuild',{}),/연결이 변경/);assert.equal(requests.length,2);
}));
test('disconnected accounts are not silently reconnected',()=>fixture(async({core,requests,setConnected})=>{
 setConnected(false);assert.equal(await connectedManagedAws(core),undefined);assert.equal(requests.length,0);
}));
