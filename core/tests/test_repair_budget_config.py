import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from grounded_repair.budget import Budget,BudgetExceeded,BudgetedRouter
from grounded_repair.knowledge import KnowledgeIndex,Passage
from grounded_repair.logs import summarize
from grounded_repair.refresh import approved_source,refresh,update_revision
from llm.base import LLMRequest


def test_budget_reservation_atomic_and_persists(tmp_path):
    path=tmp_path/'budget.db'
    def reserve(_):
        try:return Budget(path,1).reserve(.3)
        except BudgetExceeded:return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys=[k for k in pool.map(reserve,range(8)) if k]
    assert len(keys)==3
    budget=Budget(path,1)
    assert budget.summary()['worst_case_used_usd']==pytest.approx(.9)
    budget.settle(keys[0],.1)
    assert Budget(path,1).reserve(.3)
    with pytest.raises(BudgetExceeded):budget.reserve(.01)


def test_unknown_bill_keeps_worst_case_reservation(tmp_path):
    b=Budget(tmp_path/'budget.db',1)
    class Router:
        def repair_profile(self):return {'fast':'small'}
        async def call_repair(self,*args,**kwargs):raise TimeoutError('uncertain bill')
    r=BudgetedRouter(Router(),b,{'small':{'input':1,'output':5}})
    with pytest.raises(TimeoutError):asyncio.run(r.call_repair(LLMRequest(prompt='hello',system='',max_tokens=20),tier='fast',run_id='r'))
    assert b.summary()['measured_usd']==0
    assert b.summary()['worst_case_used_usd']>0


def test_exact_error_code_beats_generic_dense_and_lexical_match():
    def passage(id,text):return Passage(id,id,'https://docs.npmjs.com/',text,'v1','2026-10-03')
    class Embed:
        def __call__(self,texts):return [[1,0] if 'ETARGET' in t else [0,1] for t in texts]
        def query(self,text):return [0,1]
    index=KnowledgeIndex([passage('specific','ETARGET package version missing'),passage('generic','Docker npm install error build context')],Embed())
    assert index.search('ETARGET npm install error Docker build context',['ETARGET'])[0].id=='specific'
    e=summarize('#8 ERROR: build failed\n#8 0.444 npm error code E404\n#8 0.444 npm error package not found\nENV NODE_ENV=production')
    assert 'E404' in e.keywords and 'ERROR' not in e.keywords and 'ENV' not in e.keywords


def test_only_pinned_reviewed_upstream_sources_allowed():
    assert approved_source('https://raw.githubusercontent.com/nodejs/node/'+'a'*40+'/doc/api/errors.md')
    assert not approved_source('https://raw.githubusercontent.com/nodejs/node/main/doc/api/errors.md')
    assert not approved_source('https://raw.githubusercontent.com/attacker/node/'+'a'*40+'/README.md')


def test_markdown_refresh_preserves_license_and_snapshot():
    e={'id':'x','title':'Node','url':'https://nodejs.org/api/errors.html','source_url':'https://raw.githubusercontent.com/nodejs/node/'+'a'*40+'/doc/api/errors.md','format':'markdown','license_status':'reviewed','license_url':'https://example.test/license','attribution':'Node contributors','reviewed_at':'2026-10-03','fulltext_status':'approved','sections':['ERR_MODULE_NOT_FOUND']}
    result=refresh([e],fetcher=lambda _: '# Other\nNo\n\n## ERR_MODULE_NOT_FOUND\nMissing module.\n\nResolve its path.\n')
    assert len(result)==1 and 'Missing module' in result[0]['text'] and '\nNo\n' not in result[0]['text']
    assert result[0]['source_url']==e['source_url'] and result[0]['kind']=='excerpt'


def test_tracked_license_change_fails_closed(monkeypatch):
    import requests
    import grounded_repair.refresh as mod
    monkeypatch.setattr(requests,'get',lambda *a,**k:SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'sha':'a'*40,'commit':{'committer':{'date':'2026-10-03'}}}))
    monkeypatch.setattr(mod,'fetch',lambda url:'changed terms')
    with pytest.raises(ValueError,match='license changed'):
        update_revision({'tracking_ref':'main','repository':'nodejs/node','license_path':'LICENSE','license_sha256':'old'})


def test_fixture_success_is_separate_from_production_approval():
    from grounded_repair.benchmark import report
    run={'strategy':'D','route':'documents','status':'environment_verification_required','benchmark_solved':True,'llm_calls':1,'elapsed_ms':100,'cost_complete':True,'estimated_cost_usd':.01,'verification_kind':'aws-iam-simulation'}
    assert report([run])['D']['solved']==1
    assert report([run])['D']['verification_kinds']==['aws-iam-simulation']


def test_approval_rejects_another_active_workspace(tmp_path):
    from grounded_repair.store import RepairStore
    from grounded_repair.workspace import manifest
    root=tmp_path/'one';root.mkdir();(root/'app.js').write_text('old')
    store=RepairStore(tmp_path/'db');store.save({'id':'r','status':'ready_for_approval','workspace':str(root),'manifest':manifest(root),'edits':{'app.js':'new'}})
    with pytest.raises(ValueError,match='different workspace'):store.approve('r',str(tmp_path/'two'))
    assert (root/'app.js').read_text()=='old'


@pytest.mark.parametrize('amount',[float('nan'),float('inf'),-1])
def test_invalid_budget_numbers_cannot_bypass_ceiling(tmp_path,amount):
    b=Budget(tmp_path/'db',1)
    with pytest.raises(ValueError):b.reserve(amount)


def test_public_api_does_not_expose_attempt_replacement_files():
    from api.routes.repair import public_result
    result=public_result({'edits':{'app.js':'private'},'manifest':{},'attempts':[{'proposal':{'edits':'private'},'tier':'fast'}]})
    assert result=={'attempts':[{'tier':'fast'}]}


def test_measured_bedrock_is_one_request_with_native_usage(monkeypatch):
    from llm.bedrock_provider import BedrockProvider
    import boto3
    calls=[]
    class Session:
        def client(self,name,config):
            assert config.retries['total_max_attempts']==1
            return SimpleNamespace(converse=lambda **kw:(calls.append(kw) or {'output':{'message':{'content':[{'text':'{"ok":true}'}]}},'usage':{'inputTokens':17,'outputTokens':5},'ResponseMetadata':{'RetryAttempts':0}}))
    monkeypatch.setattr(boto3,'Session',lambda **kw:Session())
    p=BedrockProvider(model_id='model',region='ap-northeast-2')
    r=p.call_measured(LLMRequest(prompt='test',system='system',max_tokens=20))
    assert len(calls)==1 and r.input_tokens==17 and r.output_tokens==5 and r.token_source=='api'
    assert r.metadata['transport_retries']==0 and calls[0]['system']==[{'text':'system'}]


def test_secret_arn_remains_an_identifier_but_credentials_are_redacted():
    from context_gate import mask_secrets
    from grounded_repair.pipeline import masked_values
    arn='arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:example-AbCdEf'
    assert mask_secrets(arn)==arn
    assert 'sensitive-value' not in mask_secrets('secret: sensitive-value')
    assert 'sensitive-value' not in mask_secrets('config:password=sensitive-value')
    assert 'sensitive-value' not in mask_secrets('arn:invalid:secret:sensitive-value')
    value={'edits':[{'content':'secret: sensitive-value\nnext line'}],'explanation':'quoted "string"'}
    redacted=masked_values(value)
    assert json.loads(json.dumps(redacted))==redacted
    assert 'sensitive-value' not in redacted['edits'][0]['content']


def test_refreshed_excerpt_corpus_keeps_local_semantic_anchors(tmp_path):
    corpus=tmp_path/'corpus.json';corpus.write_text('[]')
    index=KnowledgeIndex.load(corpus,model_path='',include_summaries=True)
    assert len(index.passages)==10 and any(p.id=='ecs-stopped' for p in index.passages)
    shipped=KnowledgeIndex.load(model_path='',include_summaries=True)
    assert len(shipped.passages)==10  # no duplicate anchor IDs
