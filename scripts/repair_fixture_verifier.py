"""Isolated benchmark verifier with oracles outside the model's editable workspace.
Cloud-labelled contracts/simulation never authorize applying a production repair.
"""
from __future__ import annotations
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from grounded_repair.logs import summarize


class FixtureVerifier:
    supported_stages = {'build', 'run', 'ecs', 'iam', 's3'}

    def __init__(self, oracle: Path, kind: str):
        self.oracle = json.loads(oracle.read_text())
        self.kind = kind
        self.identity = kind + ':' + hashlib.sha256(oracle.read_bytes()).hexdigest()

    def __call__(self, workspace: Path):
        tag = 'recoder-benchmark:' + hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
        def execute(command, timeout=180):
            try:
                with tempfile.TemporaryFile() as output:
                    run = subprocess.run(command, cwd=workspace, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
                    output.seek(0); text=output.read(1_000_000).decode('utf8','replace')
                return run.returncode, text
            except (OSError,subprocess.TimeoutExpired) as exc:
                return 124, str(exc)
        code, output=execute(['docker','build','--progress=plain','-t',tag,'.'],300)
        if code:
            return self.result(False, output, code, available=code!=124)
        try:
            if 'iam' in self.oracle:
                import boto3
                policy=json.loads((workspace/'policy.json').read_text())
                # Do not allow broadening unrelated actions or removing deny controls.
                for statement in policy.get('Statement',[]):
                    actions=statement.get('Action',[])
                    if isinstance(actions,str): actions=[actions]
                    if statement.get('Effect')=='Allow' and any(a=='*' or a.endswith(':*') for a in actions):
                        return self.result(False,'IAM oracle rejects wildcard actions')
                q=self.oracle['iam']
                result=boto3.Session().client('iam',region_name='us-east-1').simulate_custom_policy(
                    PolicyInputList=[json.dumps(policy)],ActionNames=[q['action']],ResourceArns=[q['resource']])
                decisions=[r['EvalDecision'] for r in result['EvaluationResults']]
                passed=bool(decisions) and all(d=='allowed' for d in decisions)
                return self.result(passed,('Allowed' if passed else 'AccessDenied')+' '+q['action']+' on '+q['resource']+': '+','.join(decisions))
            if self.oracle.get('registry_contract'):
                task=json.loads((workspace/'task-definition.json').read_text())
                registry=json.loads((workspace/'registry.json').read_text())
                # Expected registry contents are fixed independently of model edits.
                passed=task.get('image')=='repair-fixture:v1' and registry=={'availableImages':['repair-fixture:v1']}
                return self.result(passed,'CannotPullContainerError: image tag missing; registry availableImages=[repair-fixture:v1]' if not passed else 'Local registry contract passed')
            run=['docker','run','--rm','--network=none','--read-only','--cap-drop=ALL',
                 '--security-opt=no-new-privileges','--memory=256m','--cpus=1','--pids-limit=96','--tmpfs','/tmp:rw,noexec,nosuid,size=16m']
            if self.oracle.get('required_user'):
                _,user=execute(['docker','image','inspect','--format','{{.Config.User}}',tag])
                if user.strip()!=self.oracle['required_user']:
                    return self.result(False,'Runtime security requirement: USER must remain '+self.oracle['required_user'])
            if self.oracle.get('healthcheck'):
                _,raw=execute(['docker','image','inspect','--format','{{json .Config.Healthcheck}}',tag])
                check=json.loads(raw or 'null')
                if not check or not check.get('Test') or check['Test'][0]=='NONE':
                    return self.result(False,'HEALTHCHECK must remain enabled')
                test=check['Test'];cmd=['/bin/sh','-c',test[1]] if test[0]=='CMD-SHELL' else test[1:]
                # Check both directions: the probe must pass for the running
                # app and fail after it stops. An unconditional exit(0) is not
                # a repair, even if Docker calls that container healthy.
                script = """const {spawn,spawnSync}=require('node:child_process');
const app=spawn('node',['app.cjs']);
const probe=()=>{const r=spawnSync(COMMAND[0],COMMAND.slice(1),{timeout:5000});
if(r.stderr.length)console.error(r.stderr.toString().slice(0,2000));return r.status};
setTimeout(()=>{const up=probe();app.kill('SIGTERM');
setTimeout(()=>{const down=probe();console.log('health while running='+up+', after stop='+down);
process.exit(up===0&&down!==0&&down!==null?0:1)},500)},500);
""".replace('COMMAND',json.dumps(cmd))
                code,output=execute(run+['--entrypoint','node',tag,'-e',script],20)
            elif self.oracle.get('ecs_runtime'):
                task=json.loads((workspace/'task-definition.json').read_text())
                service=json.loads((workspace/'service.json').read_text())
                if service!={'port':3000,'path':'/health','startupSeconds':12}:
                    return self.result(False,'Service contract cannot be changed')
                if task.get('startPeriod',0)<service['startupSeconds']:
                    return self.result(False,'ECS health check failed during initialization: startPeriod below required startupSeconds 12')
                url='http://127.0.0.1:'+str(task['containerPort'])+str(task['path'])
                script="require('./app.cjs');setTimeout(async()=>{try{const r=await fetch("+json.dumps(url)+");if(r.status!==200)throw Error('health status '+r.status);process.exit(0)}catch(e){console.error('ECS health check failed',e.message);process.exit(1)}},500)"
                code,output=execute(run+['--entrypoint','node',tag,'-e',script],20)
            elif 'node' in self.oracle:
                code,output=execute(run+['--entrypoint','node',tag,'-e',self.oracle['node']],30)
            else:
                cmd=self.oracle.get('command',['node','app.cjs'])
                code,output=execute(run+['--entrypoint',cmd[0],tag]+cmd[1:],30)
            passed=code==0 and (not self.oracle.get('stdout') or self.oracle['stdout'] in output)
            return self.result(passed,output or 'Independent runtime assertions passed',code)
        except Exception as exc:
            # Infrastructure faults are invalid observations, not model failures.
            return self.result(False,str(exc),available=False)
        finally:
            execute(['docker','image','rm',tag],30)

    def result(self,passed,output,code=None,available=True):
        return {'passed':passed,'output':summarize(output).text,'exit_code':code,
                'available':available,'kind':self.kind,'production_verified':False}
