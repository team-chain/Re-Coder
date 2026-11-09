'use strict';
const path=require('node:path'),fs=require('node:fs/promises'),cp=require('node:child_process'),assert=require('node:assert/strict');
const tools=require('../out/runtime/portableTools');
async function main(){
 if(process.platform!=='win32'||process.arch!=='x64')throw Error('Windows x64 verification only');
 const root=path.resolve(__dirname,'../../.test-artifacts/portable-tools');await fs.mkdir(root,{recursive:true});
 for(const key of Object.keys(process.env))if(key.toLowerCase()==='path')delete process.env[key];
 process.env.PATH=path.join(process.env.SystemRoot||'C:\\Windows','System32');
 tools.initializePortableTools(root,message=>console.log(message));
 const git=await tools.ensurePortableTool('git'),node=await tools.ensurePortableTool('node'),env=tools.toolEnvironment();
 assert.ok(git.startsWith(root));assert.ok(node.startsWith(root));
 const exec=(command,args,cwd=root)=>cp.execFileSync(command,args,{cwd,env,encoding:'utf8',windowsHide:true,stdio:['ignore','pipe','pipe']}).trim();
 const project=await fs.mkdtemp(path.join(root,'fixture-')),remote=project+'-remote.git';
 await fs.writeFile(path.join(project,'file.txt'),'verified fixture\n');
 exec(git,['init','--initial-branch=main',project]);exec(git,['-C',project,'add','file.txt']);
 exec(git,['-C',project,'-c','user.name=ReCoder verification','-c','user.email=verification@example.invalid','commit','-m','verify portable Git']);
 exec(git,['init','--bare',remote]);exec(git,['-C',project,'push',remote,'main']);
 const head=exec(git,['-C',project,'rev-parse','HEAD']);assert.equal(exec(git,['--git-dir',remote,'rev-parse','main']),head);
 await fs.writeFile(path.join(project,'package.json'),JSON.stringify({private:true,scripts:{build:'node build.js'}}));
 await fs.writeFile(path.join(project,'build.js'),"require('fs').mkdirSync('dist',{recursive:true});require('fs').writeFileSync('dist/index.html','<h1>portable build verified</h1>')");
 const npm=path.join(path.dirname(node),'node_modules/npm/bin/npm-cli.js');exec(node,[npm,'run','build'],project);
 assert.equal(await fs.readFile(path.join(project,'dist/index.html'),'utf8'),'<h1>portable build verified</h1>');
 const result={git:exec(git,['--version']),node:exec(node,['--version']),npm:exec(node,[npm,'--version']),commitAndLocalPush:true,frontendBuild:true,systemPathRemoved:true};
 await fs.writeFile(path.join(root,'verification.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
}
main().catch(e=>{console.error(e.message);process.exitCode=1;});
