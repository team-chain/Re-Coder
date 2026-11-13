const test=require('node:test'),assert=require('node:assert/strict'),path=require('node:path');
const {ensureRedTeamCapacity}=require('../out/security/redTeamCapacity');
const GiB=1024**3,root=path.resolve('test-reports'),env={LOCALAPPDATA:path.resolve('local-data'),APPDATA:path.resolve('roaming-data')};
function disk(gb){return {bsize:4096,bavail:Math.floor(gb*GiB/4096)};}
test('host free space is checked at resolved Docker junction, not virtual WSL capacity',async()=>{
 const actual=path.resolve('d-drive/docker');const checked=[];
 await ensureRedTeamCapacity(root,env,'win32',{readFile:async()=>{throw 0;},realpath:async()=>actual,statfs:async p=>{checked.push(p);return disk(p===root?1:50);}});
 assert.deepEqual(checked,[root,actual]);
});
test('low report or Docker storage fails before any image download',async()=>{
 for(const [rootGb,dockerGb] of [[0.1,50],[1,2],[1,49]]){
  await assert.rejects(()=>ensureRedTeamCapacity(root,env,'win32',{readFile:async()=>{throw 0;},realpath:async()=>'/disk',statfs:async p=>disk(p===root?rootGb:dockerGb)}),/공간|50GB/);
 }
});
test('unknown Docker data location is explicit and never silently approved',async()=>{
 await assert.rejects(()=>ensureRedTeamCapacity(root,env,'win32',{readFile:async()=>{throw 0;},realpath:async()=>{throw 0;},statfs:async()=>disk(100)}),/확인하지 못했습니다/);
});
