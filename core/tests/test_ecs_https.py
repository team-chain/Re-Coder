import pytest
from unittest.mock import MagicMock
from pydantic import ValidationError
from schemas import ECSDeployRequest
from api.routes.deploy_ecs import ExtensionEcsDeployRequest, to_core_request
from aws_infra import ensure_service

ARN='arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:targetgroup/shop/012abc'

def test_extension_preserves_https_and_private_network():
    body=ExtensionEcsDeployRequest(target_group_arn=ARN,cloudfront_domain='d123.cloudfront.net',assign_public_ip=False,subnet_ids=['subnet-abc'],security_group_ids=['sg-abc'])
    req=to_core_request(body)
    assert req.target_group_arn==ARN
    assert req.cloudfront_domain=='d123.cloudfront.net'
    assert req.assign_public_ip is False
    assert req.subnet_ids==['subnet-abc'] and req.security_group_ids==['sg-abc']

@pytest.mark.parametrize('domain',['http://localhost','d123.cloudfront.net/path','evil.example','d123.cloudfront.net@evil.example'])
def test_health_probe_rejects_arbitrary_endpoint(domain):
    with pytest.raises(ValidationError): ExtensionEcsDeployRequest(cloudfront_domain=domain)

@pytest.mark.parametrize('existing',[False,True])
def test_service_attaches_alb_and_disables_public_ip(existing):
    ecs=MagicMock()
    ecs.describe_services.return_value={'services':[{'serviceName':'shop','status':'ACTIVE'}] if existing else []}
    ecs.create_service.return_value=ecs.update_service.return_value={'service':{'serviceArn':'arn'}}
    ensure_service(ecs,cluster='cluster',service='shop',task_definition='td',subnet_ids=['subnet-abc'],security_group_ids=['sg-abc'],assign_public_ip=False,target_group_arn=ARN,container_name='web',container_port=3001)
    call=(ecs.update_service if existing else ecs.create_service).call_args.kwargs
    assert call['loadBalancers']==[{'targetGroupArn':ARN,'containerName':'web','containerPort':3001}]
    assert call['networkConfiguration']['awsvpcConfiguration']['assignPublicIp']=='DISABLED'
    assert call['healthCheckGracePeriodSeconds']==60

def test_health_path_404_is_not_a_success(monkeypatch):
    import urllib.request, urllib.error
    from agents.ecs_agent import _probe_http
    def fail(*a,**kw): raise urllib.error.HTTPError('https://d123.cloudfront.net/health',404,'missing',{},None)
    monkeypatch.setattr(urllib.request,'urlopen',fail)
    passed,detail=_probe_http('https://d123.cloudfront.net/health',require_success=True,attempts=1)
    assert not passed and '404' in detail
