// Real production bundle, explicit offline host fixtures. No model or cloud calls.
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process'),WebSocket=require('ws');
const root=path.resolve(__dirname,'../../.test-artifacts'),pause=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn,label){const end=Date.now()+30000;while(Date.now()<end){const value=await fn();if(value)return value;await pause(100);}throw Error('Timed out: '+label);}
async function main(){
 await fs.mkdir(root,{recursive:true});const profile=await fs.mkdtemp(path.join(root,'redteam-browser-'));
 const browser=spawn(process.env.RECODER_TEST_BROWSER||'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',['--headless=new','--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','--user-data-dir='+profile,'--no-first-run','--disable-background-networking','--window-size=1440,1000','about:blank'],{windowsHide:true,stdio:'ignore'});
 let socket,id=0,cmd;const requests=new Map(),errors=[];
 const exited=new Promise(resolve=>{browser.once('exit',resolve);browser.once('error',resolve);});
 try{
  const endpoint=await until(async()=>{try{return(await fs.readFile(path.join(profile,'DevToolsActivePort'),'utf8')).trim().split(/\r?\n/);}catch{return null;}},'browser');
  socket=new WebSocket('ws://127.0.0.1:'+endpoint[0]+endpoint[1]);await new Promise((yes,no)=>{socket.once('open',yes);socket.once('error',no);});
  socket.on('message',raw=>{const m=JSON.parse(raw);if(m.id){const p=requests.get(m.id);if(p){requests.delete(m.id);clearTimeout(p.timer);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text);});
  const call=(method,params={},sessionId)=>new Promise((resolve,reject)=>{const next=++id,timer=setTimeout(()=>{requests.delete(next);reject(Error(method+' timeout'));},20000);requests.set(next,{resolve,reject,timer});socket.send(JSON.stringify({id:next,method,params,...(sessionId?{sessionId}:{})}));});
  const target=await call('Target.createTarget',{url:'about:blank'}),attached=await call('Target.attachToTarget',{targetId:target.targetId,flatten:true});cmd=(m,p)=>call(m,p,attached.sessionId);
  await cmd('Runtime.enable');await cmd('Page.enable');
  const evaluate=async expression=>{const r=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
  const click=async label=>{await until(()=>evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.offsetParent!==null&&e.textContent.trim()===${JSON.stringify(label)});if(!b||b.disabled)return false;b.click();return true;})()`),label);};
  const contains=s=>evaluate('document.body.innerText.includes('+JSON.stringify(s)+')');
  const screen=async name=>{const r=await cmd('Page.captureScreenshot',{format:'png'});await fs.writeFile(path.join(root,name+'.png'),Buffer.from(r.data,'base64'));};
  await cmd('Page.addScriptToEvaluateOnNewDocument',{source:`window.uiRequests=[];const f=window.fetch;window.fetch=function(url,o){if(url==='/message'&&o?.body)window.uiRequests.push(JSON.parse(o.body));return f.apply(this,arguments);};`});
  await cmd('Page.navigate',{url:process.env.RECODER_PREVIEW_URL||'http://127.0.0.1:4188/?layout=workspace'});
  await click('Security');await click('레드팀 테스트');await click('내 PC의 테스트 앱');await click('레드팀 테스트 시작');
  await until(()=>contains('SQL 입력 · 대조 응답 비교'),'running task');
  assert.equal(await evaluate(`document.querySelectorAll('.rc-rt-task[data-active=true]').length`),1);
  await screen('redteam-running');
  await until(()=>contains('일시적 연결 끊김'),'transient failure');
  assert.equal(await evaluate(`document.querySelectorAll('.rc-rt-task[data-active=true]').length`),0);
  await until(()=>contains('미검증 범위 5개'),'automatic status recovery');
  assert.equal(await evaluate(`window.uiRequests.filter(m=>m.payload?.action==='localStart').length`),1,'must not retry start');
  assert.equal(await evaluate(`document.querySelectorAll('.rc-rt-flow [data-current=true]').length`),0);
  await screen('redteam-report');
  for(const width of [1200,700,390]){
   await cmd('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
   assert.equal(await evaluate('document.documentElement.scrollWidth<=window.innerWidth'),true,'horizontal overflow at '+width);
  }
  await screen('redteam-narrow');
  await cmd('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
  assert.equal(await evaluate(`getComputedStyle(document.querySelector('.rc-rt-dot')).animationName`),'none');
  await click('다시 테스트');await until(()=>contains('SQL 입력 · 대조 응답 비교'),'second run');await click('중단 · 환경 삭제');
  await until(()=>contains('중단 후 환경 삭제 완료'),'cancel cleanup');
  assert.equal(await evaluate(`document.querySelectorAll('.rc-rt-flow [data-done=true]').length`),0);
  assert.deepEqual(errors,[]);await fs.writeFile(path.join(root,'redteam-ui-result.json'),JSON.stringify({fixture:true,modelCalls:0,passed:['actual task animation','transient failure/recovery','no duplicate start','incomplete legacy report','1200/700/390 layout','reduced motion','stop/cleanup'],requests:await evaluate('window.uiRequests')},null,2));
  console.log('PASS: 7 red-team UI scenarios (offline fixtures; no AI accuracy claim)');await call('Browser.close').catch(()=>{});
 }catch(e){if(cmd){const shot=await cmd('Page.captureScreenshot',{format:'png'}).catch(()=>null);if(shot)await fs.writeFile(path.join(root,'redteam-ui-failure.png'),Buffer.from(shot.data,'base64'));}throw e;}
 finally{if(socket)socket.close();if(browser.exitCode===null)browser.kill();await Promise.race([exited,pause(5000)]);assert.ok(path.resolve(profile).startsWith(root+path.sep)&&path.basename(profile).startsWith('redteam-browser-'));await fs.rm(profile,{recursive:true,force:true,maxRetries:3,retryDelay:500}).catch(()=>{});}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
