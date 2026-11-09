'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),yauzl=require('yauzl');
const sha=data=>crypto.createHash('sha256').update(data).digest('hex');

/** Verify the archive itself, not just vsce's proposed file list. */
async function verifyBuiltVsix(file,target){
 const root=path.resolve(__dirname,'..'),seen=new Set();let checked=0;
 await new Promise((resolve,reject)=>yauzl.open(file,{lazyEntries:true},(error,zip)=>{
  if(error)return reject(error);
  const fail=e=>{zip.close();reject(e);};zip.on('error',fail);zip.on('end',resolve);
  zip.on('entry',entry=>{
   const name=entry.fileName.replace(/^extension\//,'');seen.add(name);
   const expected=name==='package.json'||name.startsWith('out/')&&name.endsWith('.js')?path.join(root,name):['bin/recoder-core.exe','bin/recoder-core'].includes(name)?path.join(root,'bin-dist',target,path.basename(name)):null;
   if(!expected){zip.readEntry();return;}
   if(!fs.existsSync(expected))return fail(new Error(`Archive contains stale output: ${name}`));
   zip.openReadStream(entry,(err,stream)=>{
    if(err)return fail(err);const hash=crypto.createHash('sha256');
    stream.on('error',fail);stream.on('data',chunk=>hash.update(chunk));stream.on('end',()=>{
     if(hash.digest('hex')!==sha(fs.readFileSync(expected)))return fail(new Error(`Archive does not match current build: ${name}`));checked++;zip.readEntry();
    });
   });
  });zip.readEntry();
 }));
 for(const name of ['out/deploy/managedFrontend.js','out/security/managedFrontendWorker.js','out/deploy/managedContainer.js','out/security/managedContainerWorker.js','out/security/managedContainerContract.js','out/runtime/portableTools.js','node_modules/yauzl/index.js','node_modules/pend/index.js','out/cloud/connectedAws.js','out/cloud/managedPermissions.js','out/webview/webview.js',target.startsWith('win32-')?'bin/recoder-core.exe':'bin/recoder-core'])if(!seen.has(name))throw Error(`Missing packaged runtime: ${name}`);
 console.log(`VSIX archive verified against current build: ${checked} files`);return {checked};
}
module.exports={verifyBuiltVsix};
if(require.main===module)verifyBuiltVsix(process.argv[2],process.argv[3]||`${process.platform}-${process.arch}`).catch(error=>{console.error(error.message);process.exitCode=1;});
