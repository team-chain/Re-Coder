const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),cp=require('node:child_process');
const {previewCommit,commitSelected}=require('../out/sidebar/githubCommit');
const {DiscordWebhook,webhookUrl}=require('../out/sidebar/discordWebhook');
const {adrStatus}=require('../out/webview-test/components/adrStatus');
function repository(t){
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-commit-'));
 t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 const git=(...args)=>cp.execFileSync('git',['-C',root,...args],{encoding:'utf8',windowsHide:true,stdio:['ignore','pipe','pipe']}).trim();
 git('init','--initial-branch=main');
 return {root,git,write:(name,text)=>fs.writeFileSync(path.join(root,name),text)};
}
test('first commit includes selected HTML files, leaves unrelated staged files, then pushes to a bare repository',async t=>{
 const s=repository(t);s.write('index.html','<h1>Board</h1>');s.write('unrelated.txt','keep staged');s.write('.env','TOKEN=fixture');
 s.git('add','unrelated.txt');
 const preview=await previewCommit(s.root);
 assert.ok(preview.files.find(f=>f.path==='.env').blocked);
 await commitSelected(s.root,preview,['index.html'],'Initial board','Test Author','test@example.test');
 assert.equal(s.git('show','HEAD:index.html'),'<h1>Board</h1>');
 assert.equal(s.git('diff','--cached','--name-only'),'unrelated.txt');
 assert.throws(()=>s.git('show','HEAD:.env'));
 const bare=path.join(s.root,'remote.git');cp.execFileSync('git',['init','--bare',bare],{windowsHide:true,stdio:'ignore'});
 s.git('remote','add','origin',bare);s.git('push','-u','origin','main');
 assert.equal(cp.execFileSync('git',['--git-dir',bare,'show','main:index.html'],{encoding:'utf8'}),'<h1>Board</h1>');
});
test('edits after review and unreviewed sensitive files cannot enter a commit',async t=>{
 const s=repository(t);s.write('app.js','version 1');s.write('.env','fixture');
 const old=await previewCommit(s.root);s.write('app.js','version 2');
 await assert.rejects(commitSelected(s.root,old,['app.js'],'Update','Tester','test@example.test'),/변경되었습니다/);
 await assert.rejects(commitSelected(s.root,await previewCommit(s.root),['.env'],'Update','Tester','test@example.test'),/선택/);
 assert.throws(()=>s.git('rev-parse','--verify','HEAD'));
});
test('subsequent commit handles deletions and preserves existing staged work',async t=>{
 const s=repository(t);s.write('delete.js','old');s.write('keep.js','old');
 await commitSelected(s.root,await previewCommit(s.root),['delete.js','keep.js'],'Initial','Tester','test@example.test');
 fs.unlinkSync(path.join(s.root,'delete.js'));s.write('keep.js','new');s.git('add','keep.js');
 await commitSelected(s.root,await previewCommit(s.root),['delete.js'],'Remove old file','Tester','test@example.test');
 assert.equal(s.git('show','HEAD:keep.js'),'old');assert.equal(s.git('diff','--cached','--name-only'),'keep.js');
});

test('first commit after replacing a staged Vite project with HTML omits cancelled additions',async t=>{
 const s=repository(t);
 for(const name of ['Dockerfile','package.json','old.jsx']) {s.write(name,'old');s.git('add',name);fs.unlinkSync(path.join(s.root,name));}
 s.write('index.html','new board');s.write('app.js','new app');s.write('unrelated.txt','keep staged');s.git('add','unrelated.txt');
 const preview=await previewCommit(s.root);
 assert.ok(preview.files.some(f=>f.status==='AD'));
 const selected=preview.files.filter(f=>f.path!=='unrelated.txt').map(f=>f.path);
 assert.equal(await commitSelected(s.root,preview,selected,'Save replacement','Tester','test@example.test'),true);
 assert.deepEqual(s.git('ls-tree','--name-only','HEAD').split('\n'),['app.js','index.html']);
 assert.equal(s.git('diff','--cached','--name-only'),'unrelated.txt');
});

test('only cancelled additions are cleaned without claiming an empty first commit',async t=>{
 const s=repository(t);s.write('old.js','old');s.git('add','old.js');fs.unlinkSync(path.join(s.root,'old.js'));
 assert.equal(await commitSelected(s.root,await previewCommit(s.root),['old.js'],'Save','Tester','test@example.test'),false);
 assert.equal(s.git('status','--porcelain'),'');assert.throws(()=>s.git('rev-parse','--verify','HEAD'));
});

const Module=require('node:module'),resolve=Module._resolveFilename;
Module._resolveFilename=function(name,...args){return name==='vscode'?path.join(__dirname,'../harness/vscode-mock.js'):resolve.call(this,name,...args);};
const vscode=require('vscode'),{CanvasHost}=require('../out/sidebar/canvasHost');
Module._resolveFilename=resolve;
test('connected repository can change or create a new target, preserving local commits and requiring explicit replacement',async t=>{
 const s=repository(t);s.write('index.html','board');await commitSelected(s.root,await previewCommit(s.root),['index.html'],'Initial','Tester','test@example.test');
 const head=s.git('rev-parse','HEAD');s.git('remote','add','origin','https://github.com/tester/old.git');
 vscode.workspace.workspaceFolders=[{uri:{fsPath:s.root}}];
 const requests=[],events=[],host=new CanvasHost({getGithubStatus:async()=>({status:'authenticated',user:'tester'}),listGithubRepos:async()=>({repos:[]}),githubConnectRepository:async p=>{requests.push(p);return {status:'ok'};}});
 const send=p=>host.handle('canvas.github.connect',{workspace:s.root,repository:'tester/new',...p},(type,payload)=>events.push({type,payload}),()=>'',async()=>assert.fail('must not deploy/push'));
 await send({});assert.equal(requests.length,0);assert.equal(events.at(-1).type,'canvas.error');
 await send({replace:true,previousRepository:'tester/old'});assert.equal(s.git('remote','get-url','origin'),'https://github.com/tester/new.git');
 await send({repository:'tester/fresh',replace:true,previousRepository:'tester/new',create:true});
 assert.equal(requests.at(-1).create,true);assert.equal(events.at(-1).type,'canvas.github.result');
 assert.equal(s.git('remote','get-url','origin'),'https://github.com/tester/fresh.git');assert.equal(s.git('rev-parse','HEAD'),head);
});
const url='https://discord.com/api/webhooks/123456789012345678/'+ 'a'.repeat(60);
function discord(){
 const saved=new Map(),calls=[];
 const secrets={get:async k=>saved.get(k),store:async(k,v)=>saved.set(k,v),delete:async k=>saved.delete(k)};
 const request=async(address,options)=>{calls.push({address,options});return new Response(JSON.stringify(options.method==='GET'?{type:1,channel_id:'234567890123456789',guild_id:'345678901234567890',name:'release-alerts',token:'do-not-expose'}:{id:'message-id'}),{status:200});};
 return {saved,calls,secrets,request,service:new DiscordWebhook(secrets,request)};
}
test('Discord setup validates without sending, scopes secrets and reconnects after restart',async()=>{
 const s=discord();assert.equal((await s.service.status('/first')).active_channel_id,'');assert.equal(s.calls.length,0);
 const state=await s.service.connect('/first',url);
 assert.equal(state.channel_name,'release-alerts');assert.equal(JSON.stringify(state).includes('do-not-expose'),false);
 assert.ok(s.calls.every(c=>c.options.method==='GET'));
 assert.equal((await new DiscordWebhook(s.secrets,s.request).status('/first')).active_channel_id,'234567890123456789');
 assert.equal((await s.service.status('/second')).active_channel_id,'');
 await s.service.send('/first','Deploy complete','@everyone');
 const sent=s.calls.at(-1);assert.equal(sent.options.method,'POST');assert.ok(sent.address.endsWith('?wait=true'));
 assert.deepEqual(JSON.parse(sent.options.body).allowed_mentions,{parse:[]});
 await s.service.disconnect('/first');assert.equal((await s.service.status('/first')).active_channel_id,'');
});
test('Discord rejects other hosts, credentials and redirects; errors never expose secret URL',async()=>{
 for(const raw of [url.replace('discord.com','evil.test'),url.replace('https:','http:'),url+'?x=1',url.replace('discord.com','user@discord.com')])assert.throws(()=>webhookUrl(raw));
 const s=discord();const bad=new DiscordWebhook(s.secrets,async()=>{throw new TypeError('failed: '+url)});
 await assert.rejects(bad.connect('/first',url),e=>!e.message.includes('a'.repeat(60))&&e.message.includes('연결하지 못했습니다'));
 assert.equal(s.saved.size,0);
});
test('ADR uses the saved approval, handles bold labels, and never declares drafts approved',()=>{
 assert.deepEqual(adrStatus('# ADR-013\n- 상태: 승인됨\n- 요청: 게시판'),{label:'설계 확정',accepted:true});
 assert.equal(adrStatus('- **상태:** Accepted').accepted,true);
 assert.equal(adrStatus('- 상태: 검토 대기\n검토자: (미정)').accepted,false);
 assert.equal(adrStatus('- 상태: 폐기').accepted,false);
 assert.equal(adrStatus('검토자: 민수').label,'검토 완료');
});
