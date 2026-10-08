"""Reproduce every broken fixture, then verify its separate reference repair."""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import json
import shutil
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core'))
from repair_fixture_verifier import FixtureVerifier
from grounded_repair.workspace import manifest,snapshot
from build_readiness import analyze


def validate(case,root):
    workspace=root/case['workspace']; verifier=FixtureVerifier(root/case['oracle'],case['verification_kind'])
    bad=verifier(workspace)
    with tempfile.TemporaryDirectory(prefix='repair-reference-') as tmp:
        good=Path(tmp);snapshot(workspace,good,manifest(workspace))
        for name,content in json.loads((root/case['reference']).read_text()).items():
            (good/name).write_text(content)
        fixed=verifier(good)
    ready=bad.get('available') and not bad['passed'] and fixed['passed']
    case['status']='ready' if ready else 'validation_failed'
    case['validation']={'broken':bad,'reference':fixed}
    case['readiness_codes']=[i.code for i in analyze(workspace,online=True).errors]
    log=root/case['log_file'];log.parent.mkdir(exist_ok=True);log.write_text(bad['output'])
    print(case['id'],case['status'],case['readiness_codes'],flush=True)
    return case

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--cases',type=Path,default=Path('benchmarks/repair/cases.json'));p.add_argument('--jobs',type=int,default=3);p.add_argument('--failed-only',action='store_true');args=p.parse_args()
    cases=json.loads(args.cases.read_text());root=args.cases.parent
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures={pool.submit(validate,c,root):c['id'] for c in cases if not args.failed_only or c['status']!='ready'}
        for f in as_completed(futures):
            result=f.result();cases=[result if c['id']==result['id'] else c for c in cases]
            temp=args.cases.with_suffix('.tmp');temp.write_text(json.dumps(cases,ensure_ascii=False,indent=2));temp.replace(args.cases)
    print('READY',sum(c['status']=='ready' for c in cases),'/',len(cases))
    if any(c['status']!='ready' for c in cases):sys.exit(1)
