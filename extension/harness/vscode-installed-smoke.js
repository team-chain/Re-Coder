'use strict';
// Executed by the real VS Code Extension Test Host, not a vscode mock.
const vscode=require('vscode'),assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
exports.run=async()=>{
 const resultPath=process.env.RECODER_VSCODE_EVIDENCE;
 assert.ok(resultPath,'explicit evidence path required');
 const ext=vscode.extensions.getExtension('recoder-team.recoder');assert.ok(ext,'VSIX not installed');
 assert.equal(ext.packageJSON.version,process.env.RECODER_EXPECTED_VERSION,'incorrect installed version');
 const evidence={version:ext.packageJSON.version,installedPath:ext.extensionPath,vscode:vscode.version,home:os.homedir(),trusted:vscode.workspace.isTrusted,workspace:vscode.workspace.workspaceFolders?.map(f=>f.uri.fsPath)};
 const step=async(name,action)=>{evidence.stage=name;await fs.writeFile(resultPath,JSON.stringify(evidence,null,2));let timer;try{return await Promise.race([action(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('Timeout: '+name)),90000);})]);}finally{clearTimeout(timer);}};
 try{
  await step('activate',()=>ext.activate());assert.ok(ext.isActive);evidence.activated=true;
  assert.equal(vscode.workspace.getConfiguration('editor').get('fontSize'),15,'existing test profile setting was reset');
  evidence.existingSettingPreserved=true;
  const commands=await vscode.commands.getCommands(true);
  for(const name of ['recoder.openWorkbench','recoder.open.scan','recoder.open.code','recoder.open.deploy','recoder.stopCore'])assert.ok(commands.includes(name),name);
  await step('workbench',()=>vscode.commands.executeCommand('recoder.openWorkbench'));
  await step('start-core',()=>vscode.commands.executeCommand('recoder.startCore'));
  for(const name of ['recoder.open.code','recoder.open.scan','recoder.open.deploy'])await step(name,()=>vscode.commands.executeCommand(name));
  const runtime=path.join(os.homedir(),'.recoder/runtime.json');let metadata;
  for(let i=0;i<120;i++){
   try {metadata=JSON.parse(await fs.readFile(runtime,'utf8'));const response=await fetch(`http://127.0.0.1:${metadata.port}/api/health`,{signal:AbortSignal.timeout(2000)});if(response.ok)break;}catch{}
   await new Promise(r=>setTimeout(r,1000));
  }
  assert.ok(metadata,'Core did not start from actual VS Code host');
  assert.ok(path.resolve(metadata.entrypoint).toLowerCase().startsWith(path.resolve(ext.extensionPath).toLowerCase()),'host did not use installed Core');
  const response=await fetch(`http://127.0.0.1:${metadata.port}/api/status`,{headers:{'X-Session-Token':metadata.session_token},signal:AbortSignal.timeout(10000)});
  assert.equal(response.status,200);evidence.authenticatedCore=true;
  evidence.webviewOpened=vscode.window.tabGroups.all.some(g=>g.tabs.some(t=>t.input instanceof vscode.TabInputWebview));
  assert.ok(evidence.webviewOpened,'Workbench webview tab missing');
  await vscode.commands.executeCommand('recoder.stopCore');evidence.stopCommand=true;
  await fs.writeFile(resultPath,JSON.stringify(evidence,null,2));
 }catch(error){await fs.writeFile(resultPath,JSON.stringify({...evidence,error:error.message},null,2));throw error;}
};
