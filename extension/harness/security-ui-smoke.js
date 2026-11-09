'use strict';
// Real production webview/host with workspace-preview's explicit offline service fixtures.
// No existing browser profile, cloud account, or paid model is used.
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process'),WebSocket=require('ws');
const root=path.resolve(__dirname,'../../.test-artifacts');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn,label,timeout=20000){const end=Date.now()+timeout;while(Date.now()<end){const r=await fn();if(r)return r;await pause(100);}throw Error('Timed out: '+label);}
async function main(){
 await fs.mkdir(root,{recursive:true});const profile=await fs.mkdtemp(path.join(root,'security-browser-'));
 const browser=spawn(process.env.RECODER_TEST_BROWSER||'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',[
  '--headless=new','--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','--user-data-dir='+profile,
  '--no-first-run','--no-default-browser-check','--disable-background-networking','--window-size=1440,1000','about:blank'],{windowsHide:true,stdio:'ignore'});
 let socket,id=0,failureSnapshot;const requests=new Map(),errors=[];
 const exited=new Promise(resolve=>{browser.once('exit',resolve);browser.once('error',resolve);});
 try{
  const endpoint=await until(async()=>{try{return (await fs.readFile(path.join(profile,'DevToolsActivePort'),'utf8')).trim().split(/\r?\n/);}catch{return null;}},'isolated browser',30000);
  socket=new WebSocket('ws://127.0.0.1:'+endpoint[0]+endpoint[1]);await new Promise((yes,no)=>{socket.once('open',yes);socket.once('error',no);});
  socket.on('message',raw=>{const message=JSON.parse(raw);if(message.id){const p=requests.get(message.id);if(p){requests.delete(message.id);clearTimeout(p.timer);message.error?p.reject(Error(message.error.message)):p.resolve(message.result);}}else if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.text);});
  const call=(method,params={},sessionId)=>new Promise((resolve,reject)=>{const next=++id;const timer=setTimeout(()=>{requests.delete(next);reject(Error(method+' timed out'));},20000);requests.set(next,{resolve,reject,timer});socket.send(JSON.stringify({id:next,method,params,...(sessionId?{sessionId}:{})}));});
  const target=await call('Target.createTarget',{url:'about:blank'}),attached=await call('Target.attachToTarget',{targetId:target.targetId,flatten:true});
  const session=attached.sessionId,cmd=(method,params)=>call(method,params,session);
  await cmd('Runtime.enable');await cmd('Page.enable');
  const evaluate=async expression=>{const r=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
  failureSnapshot=async()=>{await fs.writeFile(path.join(root,'security-ui-failure.txt'),await evaluate('document.body.innerText'));const screen=await cmd('Page.captureScreenshot',{format:'png'});await fs.writeFile(path.join(root,'security-ui-failure.png'),Buffer.from(screen.data,'base64'));};
  await cmd('Page.addScriptToEvaluateOnNewDocument',{source:`window.uiRequests=[];const originalFetch=window.fetch;window.fetch=function(url,options){if(url==='/message'&&options?.body){const m=JSON.parse(options.body);window.uiRequests.push({type:m.type,action:m.payload?.action,kind:m.payload?.kind,scanType:m.payload?.scanType,requestId:m.payload?.requestId});}return originalFetch.apply(this,arguments);};`});
  await cmd('Page.navigate',{url:process.env.RECODER_PREVIEW_URL||'http://127.0.0.1:4188/?layout=workspace'});
  const click=async label=>{await until(()=>evaluate(`(()=>{const matches=[...document.querySelectorAll('button')].filter(e=>e.offsetParent!==null&&e.textContent.trim()===${JSON.stringify(label)});return matches.length===1&&!matches[0].disabled;})()`),'button ready: '+label);return evaluate(`(()=>{const matches=[...document.querySelectorAll('button')].filter(e=>e.offsetParent!==null&&e.textContent.trim()===${JSON.stringify(label)});if(matches.length!==1||matches[0].disabled)throw Error('Button changed before click');matches[0].click();return true;})()`);};
  const visibleText=()=>evaluate('document.body.innerText');
  await until(async()=> (await visibleText()).includes('Security'),'navigation');await click('Security');
  await until(async()=> (await visibleText()).includes('보안 검사 시작'),'security dashboard');await click('보안 검사 시작');
  await until(async()=> (await visibleText()).includes('확인할 보안 항목 2개'),'existing checks finish');
  const sent=await evaluate('window.uiRequests');
  assert.deepEqual(sent.filter(x=>x.type==='runScan').map(x=>x.scanType).sort(),['gitleaks','hadolint','trivy']);
  assert.ok(sent.some(x=>x.type==='security.check'&&x.kind==='code'));assert.ok(sent.some(x=>x.type==='security.check'&&x.kind==='policy'));
  assert.equal(sent.some(x=>x.type==='security.redteam'),false,'basic scan must not require cloud setup');
  console.log('PASS: primary scan uses existing checks without AWS/PentAGI setup');
  await click('AI 감사');await until(()=>evaluate(`(()=>{const b=document.querySelector('section[aria-label="AI 코드 감사"] .rc-btn.p');return !!b&&!b.disabled;})()`),'audit ready');
  await evaluate(`document.querySelector('section[aria-label="AI 코드 감사"] .rc-btn.p').click();true`);
  await until(async()=> (await visibleText()).includes('감사 완료 · 소스 근거와 수정 방향'),'audit report',30000);
  assert.ok((await visibleText()).includes('AI 호출 7회'));assert.equal((await evaluate('window.uiRequests')).some(x=>x.action?.startsWith('managed')),false);
  console.log('PASS: real source audit host runs 7 fixture AI steps and renders persisted result');
  await click('레드팀 테스트');await until(async()=> (await visibleText()).includes('내 PC의 테스트 앱'),'red team options');
  assert.equal((await evaluate('window.uiRequests')).some(x=>x.type==='security.redteam'),false,'opening the red-team entry must not query or provision cloud resources');
  await click('내 PC의 테스트 앱');await until(async()=> (await visibleText()).includes('레드팀 테스트 시작'),'local panel');await click('레드팀 테스트 시작');
  await until(async()=> (await visibleText()).includes('UI 예시 · 검사 환경 삭제 완료'),'fixture report');await click('닫기');
  const screen=await cmd('Page.captureScreenshot',{format:'png'});await fs.writeFile(path.join(root,'security-3.0.7-ui.png'),Buffer.from(screen.data,'base64'));
  console.log('PASS: red-team execution is explicit, independent, and renders fixture evidence');
  await evaluate(`window.dispatchEvent(new MessageEvent('message',{data:{type:'canvas.projectChanged',payload:{workspace:'next-project'}}}));true`);
  await until(async()=> (await visibleText()).includes('아직 확인하지 못한 검사가 있습니다'),'project state reset');
  const oldScan=sent.find(x=>x.type==='runScan');
  await evaluate(`window.dispatchEvent(new MessageEvent('message',{data:{type:'scanResult',payload:${JSON.stringify({scan_type:oldScan.scanType,requestId:oldScan.requestId,status:'ok',findings:[{message:'OLD PROJECT MUST NOT APPEAR'}],critical_count:99})}}}));true`);
  assert.ok((await visibleText()).includes('아직 확인하지 못한 검사가 있습니다'));
  await cmd('Emulation.setDeviceMetricsOverride',{width:700,height:1000,deviceScaleFactor:1,mobile:false});
  assert.equal(await evaluate('document.documentElement.scrollWidth<=window.innerWidth'),true,'narrow layout must not overflow');
  assert.deepEqual(errors,[]);console.log('PASS: old project results ignored; 700px layout fits; no runtime exceptions');
  await fs.writeFile(path.join(root,'security-3.0.7-ui-requests.json'),JSON.stringify(await evaluate('window.uiRequests'),null,2));
  await call('Browser.close').catch(()=>{});
 }catch(error){if(failureSnapshot)await failureSnapshot().catch(()=>{});throw error;}finally{
  if(socket)socket.close();if(browser.exitCode===null)browser.kill();await Promise.race([exited,pause(5000)]);
  assert.ok(path.resolve(profile).startsWith(root+path.sep)&&path.basename(profile).startsWith('security-browser-'));
  await fs.rm(profile,{recursive:true,force:true,maxRetries:5,retryDelay:500}).catch(()=>console.log('Isolated browser profile retained:',profile));
 }
}
main().catch(e=>{console.error(e);process.exitCode=1;});
