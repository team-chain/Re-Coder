"""Run reproducible A/B/C/D comparisons with persisted USD reservations and resume.
Only --live enables paid model calls; isolated verifiers never deploy cloud resources.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import random
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core'))
from grounded_repair.benchmark import report


def atomic(path, data):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));tmp.replace(path)


async def execute(args):
    from grounded_repair.config import load_index,repair_router
    from grounded_repair.pipeline import RepairPipeline
    from grounded_repair.store import RepairStore
    from grounded_repair.budget import Budget,BudgetedRouter
    from repair_fixture_verifier import FixtureVerifier
    from grounded_repair.workspace import manifest
    cases=json.loads(args.cases.read_text())
    prepared=[c for c in cases if c.get('status')=='ready']
    if len(prepared)!=len(cases):raise SystemExit('Validate every case before benchmarking')
    if not args.live:
        print(json.dumps({'ready':len(prepared),'max_generation_calls':len(prepared)*8,'budget_usd':args.budget,'message':'Use --live for paid model calls.'},indent=2));return
    if not 0<args.budget<=30:raise SystemExit('This experiment is capped at the approved USD 30 budget')
    args.output.mkdir(parents=True,exist_ok=True)
    prices=json.loads(args.prices.read_text())['models']
    budget=Budget(args.budget_ledger or args.output/'budget.db',args.budget)
    router=BudgetedRouter(repair_router(),budget,prices)
    if router.repair_profile()['fast']==router.repair_profile()['primary']:
        raise SystemExit('A/B/C/D requires distinct fast and primary models')
    index=await asyncio.to_thread(load_index)
    store=RepairStore(args.output/'runs.db')
    runs=json.loads((args.output/'runs.json').read_text()) if (args.output/'runs.json').exists() else []
    completed={(r['case_id'],r['strategy']) for r in runs}
    for old in runs:
        case=next(c for c in cases if c['id']==old['case_id'])
        if old['corpus_version']!=index.version or old.get('manifest')!=manifest((args.cases.parent/case['workspace']).resolve()) or old.get('model_profile')!=router.repair_profile():
            raise SystemExit('Resume inputs changed. Use a fresh run directory with the shared budget ledger.')
    rng=random.Random(args.seed);jobs=[]
    for case in prepared:
        conditions=list('ABCD');rng.shuffle(conditions)
        jobs.extend((case,c) for c in conditions if (case['id'],c) not in completed)
    sem=asyncio.Semaphore(args.jobs);save=asyncio.Lock();stop=asyncio.Event()
    async def run(case,condition):
        async with sem:
            if stop.is_set():return
            verifier=FixtureVerifier(args.cases.parent/case['oracle'],case['verification_kind'])
            pipeline=RepairPipeline(router,index,store,verifier=verifier)
            workspace=(args.cases.parent/case['workspace']).resolve()
            log=(args.cases.parent/case['log_file']).read_text()
            result=await pipeline.run(str(workspace),log,case['stage'],condition,use_cache=False)
            result.update(case_id=case['id'],category=case['category'],order_seed=args.seed,
                          verification_kind=case['verification_kind'],case_stage=case['stage'])
            result['benchmark_solved']=bool(result.get('verification',{}).get('passed') and result.get('edits') and result['status'] in {'ready_for_approval','environment_verification_required'})
            if result['status']=='budget_exhausted':stop.set()
            async with save:
                runs.append(result);atomic(args.output/'runs.json',runs)
                atomic(args.output/'report.json',report(runs));atomic(args.output/'budget.json',budget.summary())
            print(len(runs),case['id'],condition,result['status'],result['llm_calls'],round(result.get('estimated_cost_usd') or 0,5),flush=True)
    await asyncio.gather(*(run(case,c) for case,c in jobs))
    print(json.dumps({'runs':len(runs),'expected':len(prepared)*4,'budget':budget.summary()},indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases',type=Path,default=Path('benchmarks/repair/cases.json'))
    p.add_argument('--output',type=Path,default=Path('benchmarks/repair/results'))
    p.add_argument('--prices',type=Path,default=Path('benchmarks/repair/prices.json'))
    p.add_argument('--budget-ledger',type=Path,help='Shared ledger across pilot and final runs')
    p.add_argument('--budget',type=float,default=30)
    p.add_argument('--jobs',type=int,choices=range(1,5),default=3)
    p.add_argument('--seed',type=int,default=42);p.add_argument('--live',action='store_true')
    asyncio.run(execute(p.parse_args()))
