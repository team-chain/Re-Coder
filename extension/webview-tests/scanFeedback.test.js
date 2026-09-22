const test = require('node:test');
const assert = require('node:assert/strict');
const {canRunSecurityScan,isMatchingScanResult} = require('../out/webview-test/components/ShipMode.js');

test('an existing Dockerfile can be scanned before generation; busy stages are locked',()=>{
  for(const step of ['idle','saved','scanDone','done','error']) assert.equal(canRunSecurityScan(step),true,step);
  for(const step of ['saving','scanning','planning','deploying']) assert.equal(canRunSecurityScan(step),false,step);
});

test('late or unrelated scan responses cannot complete the active scan',()=>{
  assert.equal(isMatchingScanResult('current',{requestId:'current',scan_type:'trivy',status:'error'}),true);
  assert.equal(isMatchingScanResult('current',{requestId:'old',scan_type:'trivy'}),false);
  assert.equal(isMatchingScanResult('current',{requestId:'current',scan_type:'hadolint'}),false);
  assert.equal(isMatchingScanResult(null,{requestId:'current',scan_type:'trivy'}),false);
  assert.equal(isMatchingScanResult('current',null),false);
  assert.equal(isMatchingScanResult('current',{scan_type:'trivy'}),false);
});

test('host echoes scan request identity for both completion and request failure',async()=>{
  const Module=require('node:module'),path=require('node:path'); const resolve=Module._resolveFilename;
  Module._resolveFilename=function(request,...args){return request==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,request,...args);};
  try {
    const vscode=require('vscode'); const {SidebarProvider}=require('../out/sidebar/SidebarProvider.js');
    let fail=false; const calls=[];
    const api={runScan:async(...args)=>{calls.push(args);if(fail)throw new Error('Core unavailable');return {scan_type:'trivy',status:'ok',findings:[]};}};
    const provider=new SidebarProvider(vscode.Uri.file('/tmp/test-extension'),api,{},{});
    const messages=[];provider.postMessage=(type,payload)=>messages.push({type,payload});
    await provider.handleMessage({type:'runScan',payload:{scanType:'trivy',workspacePath:'/sample',requestId:'one'}});
    assert.deepEqual(calls[0],['trivy','/sample',undefined]);
    assert.equal(messages.at(-1).payload.requestId,'one');
    assert.equal(messages.at(-1).payload.status,'ok');
    fail=true;
    await provider.handleMessage({type:'runScan',payload:{scanType:'trivy',workspacePath:'/sample',requestId:'two'}});
    assert.equal(messages.at(-1).type,'scanResult');
    assert.equal(messages.at(-1).payload.requestId,'two');
    assert.equal(messages.at(-1).payload.status,'error');
    assert.equal(messages.at(-1).payload.reason_code,'request_failed');
    assert.equal(messages.at(-1).payload.message,'Core unavailable');
    assert.ok(messages.at(-1).payload.next_action);
  } finally {Module._resolveFilename=resolve;}
});
