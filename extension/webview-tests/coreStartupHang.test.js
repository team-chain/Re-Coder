// 실기기(2026-09-30): 작업 폴더를 바꿔 확장이 다시 뜬 뒤 Core 준비가 끝나지 않아 개발 요청이
// "응답이 없습니다"로만 끝났다. 준비 경로의 기다림마다 상한이 있어야 한다.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),Module=require('node:module');
const resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const {CoreManager}=require('../out/core/CoreManager');
const {withTimeout,withFallback,TimeoutError}=require('../out/core/timeouts');
Module._resolveFilename=resolve;

test('withTimeout 은 끝나지 않는 작업을 기다리는 쪽을 풀어 준다', async()=>{
  await assert.rejects(withTimeout(new Promise(()=>{}),50,'멈춤'),e=>e instanceof TimeoutError&&/멈춤/.test(e.message));
  assert.equal(await withTimeout(Promise.resolve(7),50,'x'),7);
  assert.equal(await withFallback(new Promise(()=>{}),50,'기본'),'기본');
  assert.equal(await withFallback(Promise.reject(new Error('x')),50,'기본'),'기본');
});

test('보안 저장소 읽기가 끝나지 않아도 Core 시작 환경 준비는 끝나고 core.log 에 남는다', async t=>{
  const home=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-hang-'));
  const oldHome=process.env.HOME, oldProfile=process.env.USERPROFILE;
  process.env.HOME=home; process.env.USERPROFILE=home;
  t.after(()=>{process.env.HOME=oldHome; process.env.USERPROFILE=oldProfile; fs.rmSync(home,{recursive:true,force:true});});
  fs.mkdirSync(path.join(home,'.recoder'));
  const context={secrets:{get:()=>new Promise(()=>{}),store:async()=>{},delete:async()=>{}},globalState:{get:(k,d)=>d,update:async()=>{}},extensionPath:home};
  const manager=new CoreManager(context);
  const started=Date.now();
  const env=await manager._awsEnv();
  assert.deepEqual(env,{});
  assert.ok(Date.now()-started<9000,'보안 저장소를 끝없이 기다렸다');
  const log=fs.readFileSync(path.join(home,'.recoder','core.log'),'utf-8');
  assert.match(log,/secret read skipped key=recoder\.aws\.accessKeyId/);
});
