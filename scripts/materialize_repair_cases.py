"""Create 36 real faulty projects plus external immutable reference/oracle files.
Run validate_repair_cases.py to record actual broken and known-good executions.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'benchmarks/repair'
BASE = 'node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c'
CASES = []

def js(value):
    return json.dumps(value, indent=2) + '\n'

def node(files=None, deps=None, run='node app.cjs'):
    return {'package.json':js({'name':'repair-fixture','version':'1.0.0','private':True,'scripts':{'start':run},'dependencies':deps or {}}),
            '.dockerignore':'node_modules\n.git\n.recoder\n',
            'Dockerfile':f'FROM {BASE}\nWORKDIR /app\nCOPY package*.json ./\nRUN npm install --ignore-scripts --no-audit --no-fund\nCOPY . .\nCMD ["node", "app.cjs"]\n',
            'app.cjs':'console.log("ready");\n', **(files or {})}

def add(id,category,broken,fix,oracle,*,kind='docker-runtime',stage='build',note=''):
    directory=ROOT/'fixtures'/id
    directory.mkdir(parents=True,exist_ok=True)
    for name,text in broken.items():
        path=directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    (ROOT/'references').mkdir(exist_ok=True)
    (ROOT/'oracles').mkdir(exist_ok=True)
    (ROOT/'references'/f'{id}.json').write_text(js(fix))
    (ROOT/'oracles'/f'{id}.json').write_text(js(oracle))
    CASES.append(dict(id=id,category=category,stage=stage,status='unvalidated',workspace=f'fixtures/{id}',
                      reference=f'references/{id}.json',oracle=f'oracles/{id}.json',log_file=f'logs/{id}.log',
                      verification_kind=kind,note=note or 'Synthetic injected fault; independent oracle kept outside model workspace.'))

# npm: genuine npm errors, including a deterministic local 401 registry.
b=node(deps={'is-number':'99.0.0'});good=json.loads(b['package.json']);good['dependencies']['is-number']='7.0.0'
add('npm-unpublished-version','npm',b,{'package.json':js(good)},{'node':"require('node:assert').equal(require('is-number')(3),true)"})
b=node(deps={'@types/nodex':'22.0.0'});good=json.loads(b['package.json']);good['dependencies']={'@types/node':'22.0.0'}
add('npm-scoped-package-typo','npm',b,{'package.json':js(good)},{'node':"require.resolve('@types/node/package.json')"})
b=node(deps={'is-number':'6.0.0'});b['Dockerfile']=b['Dockerfile'].replace('npm install','npm ci')
b['package-lock.json']=js({'name':'repair-fixture','version':'1.0.0','lockfileVersion':3,'requires':True,'packages':{'':{'name':'repair-fixture','version':'1.0.0','dependencies':{'is-number':'7.0.0'}},'node_modules/is-number':{'version':'7.0.0','resolved':'https://registry.npmjs.org/is-number/-/is-number-7.0.0.tgz'}}})
good=json.loads(b['package.json']);good['dependencies']['is-number']='7.0.0'
add('npm-lockfile-out-of-sync','npm',b,{'package.json':js(good)},{'node':"require('node:assert').equal(require('is-number')(3),true)"})
b=node(deps={'react':'18.2.0','react-dom':'17.0.2'});good=json.loads(b['package.json']);good['dependencies']['react-dom']='18.2.0'
add('npm-peer-dependency-conflict','npm',b,{'package.json':js(good)},{'node':"require('node:assert').equal(require('react/package.json').version.split('.')[0],require('react-dom/package.json').version.split('.')[0])"})
b=node();pkg=json.loads(b['package.json']);pkg['engines']={'node':'>=99'};b['package.json']=js(pkg);b['Dockerfile']=b['Dockerfile'].replace('npm install','npm install --engine-strict');pkg['engines']={'node':'>=22'}
b['requirements.md']='The supported production runtime is Node.js 22 or newer.\n'
add('npm-unsupported-node-engine','npm',b,{'package.json':js(pkg)},{'node':"require('node:assert').ok(+process.versions.node.split('.')[0]>=22)"})
b=node(deps={'is-number':'7.0.0'});normal=b['Dockerfile'];b['registry.cjs']="require('node:http').createServer((q,s)=>{s.writeHead(401);s.end('authentication required')}).listen(4873,'127.0.0.1');\n"
b['Dockerfile']=normal.replace('RUN npm install', 'COPY registry.cjs ./\nRUN node registry.cjs &\nRUN npm install') # server must live in same RUN
b['Dockerfile']=normal.replace('RUN npm install --ignore-scripts --no-audit --no-fund','COPY registry.cjs ./\nRUN node registry.cjs & sleep 1; npm install --registry=http://127.0.0.1:4873 --fetch-retries=0 --ignore-scripts --no-audit --no-fund')
b['requirements.md']='is-number is a public package. No private packages or credentials are required.\n'
add('npm-private-registry-auth','npm',b,{'Dockerfile':normal},{'node':"require('node:assert').equal(require('is-number')(3),true)"})

# Frameworks / modules: run real Node, Express 5 and Vite.
for suffix,bad,good,route in [('unnamed-wildcard','/*','/{*splat}','/'),('optional-route','/:file.:ext?','/:file{.:ext}','/readme')]:
    app="const app=require('express')();\napp.get(%s,(req,res)=>res.send('ok'));\nmodule.exports=app;\n"
    b=node({'app.cjs':app % json.dumps(bad)},deps={'express':'5.1.0'})
    oracle=f"const a=require('node:assert');const app=require('./app.cjs');const s=app.listen(0,'0.0.0.0',async()=>{{try{{let r=await fetch('http://127.0.0.1:'+s.address().port+{json.dumps(route)});a.equal(r.status,200);a.equal(await r.text(),'ok')}}finally{{s.close()}}}})"
    add('node-framework-express5-'+suffix,'node-framework',b,{'app.cjs':app % json.dumps(good)},{'node':oracle})
b=node({'app.js':"const fs = require('node:fs'); console.log('module loaded');\n"});pkg=json.loads(b['package.json']);pkg['type']='module';b['package.json']=js(pkg)
add('node-framework-esm-commonjs-mismatch','node-framework',b,{'app.js':"import fs from 'node:fs'; console.log('module loaded');\n"},{'command':['node','app.js']})
b=node({'app.cjs':"const value=require('./value.mjs');console.log(value.default);\n",'value.mjs':'export default await Promise.resolve(42);\n'})
add('node-framework-top-level-await-require','node-framework',b,{'app.cjs':"import('./value.mjs').then(value=>console.log(value.default));\n"},{'command':['node','app.cjs'],'stdout':'42'})
vite=node({'app.js':"export default import.meta.env.API_URL;\n",'vite.config.js':"import {defineConfig} from 'vite';\nexport default defineConfig({build:{lib:{entry:'app.js',formats:['cjs'],fileName:()=> 'client.cjs'}}});\n"},deps={'vite':'6.1.0'},run='node app.js');p=json.loads(vite['package.json']);p['type']='module';p['scripts']['build']='vite build';vite['package.json']=js(p);vite['Dockerfile']=vite['Dockerfile'].replace('COPY . .','COPY . .\nENV VITE_API_URL=https://api.example.test\nRUN npm run build')
add('node-framework-vite-build-time-env','node-framework',vite,{'app.js':"export default import.meta.env.VITE_API_URL;\n"},{'node':"require('node:assert').equal(require('./dist/client.cjs'),'https://api.example.test')"},stage='run')
b=dict(vite);b['app.js']="export default import.meta.env.VITE_API_URL;\n";correct=b['vite.config.js'];b['vite.config.js']=correct.replace("'vite'","'vite/dist/node'")
add('node-framework-vite-dependency-api-change','node-framework',b,{'vite.config.js':correct},{'node':"require('node:assert').equal(require('./dist/client.cjs'),'https://api.example.test')"})

# Docker: build failures and actual isolated runtime failures.
b=node({'payload.txt':'required artifact\n'});correct=b['Dockerfile'];b['Dockerfile']=correct.replace('COPY . .','COPY ../outside/payload.txt /app/payload.txt')
add('docker-copy-outside-context','docker',b,{'Dockerfile':correct},{'node':"require('node:assert').equal(require('node:fs').readFileSync('/app/payload.txt','utf8'),'required artifact\\n')"})
b=node({'payload.txt':'required artifact\n'});b['Dockerfile']=b['Dockerfile'].replace('COPY . .','COPY payload.txt /app/payload.txt');correct=b['.dockerignore'];b['.dockerignore']+='payload.txt\n'
add('docker-dockerignore-required-file','docker',b,{'.dockerignore':correct},{'node':"require('node:assert').equal(require('node:fs').readFileSync('/app/payload.txt','utf8'),'required artifact\\n')"})
b=node();b['Dockerfile']=f'FROM {BASE} AS builder\nWORKDIR /build\nRUN mkdir out && echo artifact > out/result.txt\nFROM {BASE}\nCOPY --from=builder /build/dist/result.txt /app/result.txt\nWORKDIR /app\n'
add('docker-wrong-multistage-source','docker',b,{'Dockerfile':b['Dockerfile'].replace('/build/dist/','/build/out/')},{'node':"require('node:assert').equal(require('node:fs').readFileSync('result.txt','utf8').trim(),'artifact')"})
b=node({'native.c':'#include <stdio.h>\nint main(void){puts("native ready");return 0;}\n'});b['Dockerfile']=b['Dockerfile'].replace('COPY . .','COPY . .\nRUN cc native.c -o /app/native')
add('docker-native-addon-toolchain','docker',b,{'Dockerfile':b['Dockerfile'].replace('RUN cc','RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*\nRUN cc')},{'command':['/app/native'],'stdout':'native ready'},note='Real C compiler dependency failure; no native npm addon ABI claim.')
b=node({'app.cjs':"require('node:fs').readFileSync('/app/private.txt');console.log('readable');\n",'private.txt':'non-secret fixture\n'});b['Dockerfile']+='RUN chmod 600 /app/private.txt\nUSER node\n'
add('docker-runtime-user-permission','docker',b,{'Dockerfile':b['Dockerfile'].replace('chmod 600','chown node:node')},{'command':['node','app.cjs'],'stdout':'readable','required_user':'node'},stage='run')
b=node({'app.cjs':"require('node:http').createServer((q,s)=>{s.statusCode=q.url==='/health'?200:404;s.end('ok')}).listen(3000,'0.0.0.0');\n"});b['Dockerfile']+='HEALTHCHECK CMD curl -f http://127.0.0.1:3000/health || exit 1\n'
add('docker-probe-tool-missing','docker',b,{'Dockerfile':b['Dockerfile'].replace('curl -f http://127.0.0.1:3000/health', "node -e \"fetch('http://127.0.0.1:3000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))\"")},{'healthcheck':True},stage='run')

# ECS config contracts use actual container requests, plus live IAM simulation.
server="const http=require('node:http');const c=require('./service.json');http.createServer((q,s)=>{s.statusCode=q.url===c.path?200:404;s.end('ok')}).listen(c.port,'0.0.0.0');\n"
for suffix,key,bad,good in [('container-port-mismatch','containerPort',8080,3000),('health-path-mismatch','path','/wrong','/health'),('startup-grace-period','startPeriod',0,15)]:
    task={'containerPort':3000,'path':'/health','startPeriod':15};task[key]=bad
    b=node({'app.cjs':server,'service.json':js({'port':3000,'path':'/health','startupSeconds':12}),'task-definition.json':js(task)})
    fixed=dict(task);fixed[key]=good
    add('ecs-ecr-'+suffix,'ecs-ecr',b,{'task-definition.json':js(fixed)},{'ecs_runtime':True},kind='ecs-local-contract',stage='ecs',note='Docker HTTP replay + ECS configuration contract; not a live Fargate deployment.')

def policy(actions,resource,effect='Allow'):
    return {'Version':'2012-10-17','Statement':[{'Effect':effect,'Action':actions,'Resource':resource}]}

def iam(id,category,action,resource,bad,good,stage='iam'):
    b=node({'policy.json':js(bad),'request.json':js({'action':action,'resource':resource})})
    add(id,category,b,{'policy.json':js(good)},{'iam':{'action':action,'resource':resource}},kind='aws-iam-simulation',stage=stage,
        note='Live AWS SimulateCustomPolicy; no resource created. Does not prove SCP, KMS grant, VPC endpoint, or cross-account authorization.')
repo='arn:aws:ecr:ap-northeast-2:111122223333:repository/repair-fixture'
obj='arn:aws:s3:::repair-fixture/assets/config.json'
iam('ecs-ecr-execution-role-image-pull','ecs-ecr','ecr:BatchGetImage',repo,policy('ecr:DescribeRepositories',repo),policy(['ecr:DescribeRepositories','ecr:BatchGetImage'],repo),stage='ecs')
b=node({'task-definition.json':js({'image':'repair-fixture:missing'}),'registry.json':js({'availableImages':['repair-fixture:v1']})})
add('ecs-ecr-image-tag-missing','ecs-ecr',b,{'task-definition.json':js({'image':'repair-fixture:v1'})},{'registry_contract':True},kind='ecr-local-contract',stage='ecs',note='Offline registry manifest contract; does not claim a real ECR pull.')
secret='arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:repair-fixture-AbCdEf'
iam('ecs-ecr-secrets-initialization','ecs-ecr','secretsmanager:GetSecretValue',secret,policy('secretsmanager:DescribeSecret',secret),policy(['secretsmanager:DescribeSecret','secretsmanager:GetSecretValue'],secret),stage='ecs')
iam('iam-s3-task-role-s3-read','iam-s3','s3:GetObject',obj,policy('s3:ListBucket','arn:aws:s3:::repair-fixture'),policy(['s3:GetObject'],obj))
p=policy('s3:GetObject',obj);p['Statement'].append({'Effect':'Deny','Action':'s3:GetObject','Resource':obj})
iam('iam-s3-bucket-explicit-deny','iam-s3','s3:GetObject',obj,p,policy('s3:GetObject',obj),stage='s3')
kms='arn:aws:kms:ap-northeast-2:111122223333:key/12345678-1234-1234-1234-123456789012'
iam('iam-s3-kms-decrypt-permission','iam-s3','kms:Decrypt',kms,policy('kms:DescribeKey',kms),policy(['kms:DescribeKey','kms:Decrypt'],kms))
iam('iam-s3-ecr-resource-scope','iam-s3','ecr:GetAuthorizationToken','*',policy('ecr:GetAuthorizationToken',repo),policy('ecr:GetAuthorizationToken','*'))
iam('iam-s3-s3-endpoint-policy','iam-s3','s3:GetObject',obj,policy('s3:GetObject','arn:aws:s3:::repair-fixture/other/*'),policy('s3:GetObject','arn:aws:s3:::repair-fixture/assets/*'),stage='s3')
iam('iam-s3-cross-account-bucket','iam-s3','s3:GetObject',obj,policy('s3:GetObject','arn:aws:s3:::other-account-bucket/*'),policy('s3:GetObject',obj),stage='s3')

# Six logic controls: immutable external assertions, not model-editable tests.
logic=[
('off-by-one-pagination',"exports.page=(a,n,size)=>a.slice(n*size,(n+1)*size);","exports.page=(a,n,size)=>a.slice((n-1)*size,n*size);","assert.deepEqual(m.page([1,2,3,4,5],1,2),[1,2]);assert.deepEqual(m.page([1,2,3,4,5],3,2),[5]);",'Page numbers start at 1.'),
('async-race',"exports.total=async values=>{let s=0;values.forEach(async x=>{s+=await Promise.resolve(x)});return s};","exports.total=async values=>(await Promise.all(values)).reduce((a,b)=>a+b,0);","assert.equal(await m.total([Promise.resolve(2),Promise.resolve(3)]),5);assert.equal(await m.total([]),0);",'Sum all asynchronously resolved values before returning.'),
('wrong-tax-rounding',"exports.tax=cents=>Math.floor(cents*7/100);","exports.tax=cents=>Math.round(cents*7/100);","assert.equal(m.tax(150),11);assert.equal(m.tax(100),7);assert.equal(m.tax(8),1);",'Tax is 7 percent, round to nearest cent with halves upward.'),
('invalid-state-transition',"exports.can=(a,b)=>['paid','shipped','cancelled'].includes(b);","exports.can=(a,b)=>({pending:['paid','cancelled'],paid:['shipped','cancelled'],shipped:[],cancelled:[]}[a]||[]).includes(b);","assert.equal(m.can('shipped','paid'),false);assert.equal(m.can('pending','shipped'),false);assert.equal(m.can('paid','shipped'),true);",'Allowed transitions: pending->paid/cancelled, paid->shipped/cancelled. Shipped and cancelled are terminal.'),
('timezone-boundary',"exports.day=s=>new Date(s).toLocaleDateString('en-CA',{timeZone:'Asia/Seoul'});","exports.day=s=>new Date(s).toISOString().slice(0,10);","assert.equal(m.day('2026-01-01T23:30:00Z'),'2026-01-01');assert.equal(m.day('2026-01-02T08:00:00+09:00'),'2026-01-01');",'Accounting days use UTC, regardless of viewer timezone.'),
('missing-null-branch',"exports.label=u=>u.name.toUpperCase();","exports.label=u=>u?.name ? u.name.toUpperCase() : 'ANONYMOUS';","assert.equal(m.label(null),'ANONYMOUS');assert.equal(m.label({name:'Sam'}),'SAM');assert.equal(m.label({}),'ANONYMOUS');",'Missing user or name displays ANONYMOUS.'),
]
for suffix,bad,good,checks,requirement in logic:
 b=node({'app.cjs':bad+'\n','requirements.md':requirement+'\n'})
 add('logic-control-'+suffix,'logic-control',b,{'app.cjs':good+'\n'},{'node':"const assert=require('node:assert/strict');const m=require('./app.cjs');(async()=>{"+checks+"})().catch(e=>{console.error(e);process.exitCode=1})"})

if __name__=='__main__':
 assert len(CASES)==36 and len({c['id'] for c in CASES})==36
 (ROOT/'cases.json').write_text(js(CASES))
 print('Materialized 36 fault projects; validation pending.')
