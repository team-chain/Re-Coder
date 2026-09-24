import asyncio
import json
from types import SimpleNamespace

import pytest

from static_frontend import frontend_dockerfile, static_frontend_output
from api.routes.deploy import _dockerfile_from_template
from schemas import StackType, ProjectProfile


def package(root, **changes):
    value={'dependencies': {'react':'18.2.0','react-scripts':'5.0.1'}, 'scripts': {'build':'react-scripts build','start':'react-scripts start'}}
    value.update(changes)
    (root/'package.json').write_text(json.dumps(value), encoding='utf-8')


def test_cra_uses_built_assets_nonroot_nginx_and_matching_health_port(tmp_path):
    package(tmp_path)
    content, template = _dockerfile_from_template(str(tmp_path), StackType.NODE_EXPRESS, SimpleNamespace(default_port=3000))
    assert template=='Dockerfile.node-static'
    assert '/app/build/' in content
    assert 'npm run build' in content
    assert 'USER 101' in content
    assert 'listen 3000;' in content and '127.0.0.1:3000/health' in content
    assert 'node", "index.js' not in content and '{{' not in content
    assert '--omit=dev' not in content


def test_frontend_agent_does_not_need_llm_or_treat_client_as_express(tmp_path):
    from agents.infra_agent import InfraAgent
    package(tmp_path)
    agent=object.__new__(InfraAgent)  # no provider: this path must be deterministic
    result=asyncio.run(agent.generate_dockerfile(str(tmp_path), ProjectProfile(workspace_path=str(tmp_path),stack=StackType.NODE_EXPRESS,default_port=3000)))
    assert result.base_template=='Dockerfile.node-static'
    assert '/app/build/' in result.content


def test_approved_static_dockerfile_saves_ignore_without_overwriting_user_rules(tmp_path):
    from api.routes.deploy import _write_proposal_to_workspace
    from schemas import InfraFileProposal, FileType
    from static_frontend import STATIC_IGNORE_NOTICE, STATIC_DOCKERIGNORE
    package(tmp_path)
    content, template=frontend_dockerfile(str(tmp_path))
    proposal=InfraFileProposal(file_type=FileType.DOCKERFILE,target_path='Dockerfile',content=content,base_template=template,risk_reasons=[STATIC_IGNORE_NOTICE])
    result=_write_proposal_to_workspace(proposal,str(tmp_path),proposal.proposal_id)
    assert result['status']=='saved'
    assert (tmp_path/'.dockerignore').read_text()==STATIC_DOCKERIGNORE
    assert result['additional_paths']==[str(tmp_path/'.dockerignore')]
    (tmp_path/'.dockerignore').write_text('custom-rules\n')
    _write_proposal_to_workspace(proposal,str(tmp_path),proposal.proposal_id)
    assert (tmp_path/'.dockerignore').read_text()=='custom-rules\n'


def test_frontend_legacy_generator_uses_same_template(tmp_path):
    from infra_agent import generate_dockerfile
    package(tmp_path)
    result=generate_dockerfile(workspace_path=str(tmp_path))
    assert result.base_template=='Dockerfile.node-static'
    assert '/app/build/' in result.content


def test_compose_probe_uses_the_generated_nginx_runtime(tmp_path):
    from infra_agent import compose_health_check_block
    package(tmp_path)
    content,_=frontend_dockerfile(str(tmp_path),3000)
    (tmp_path/'Dockerfile').write_text(content)
    check=compose_health_check_block('node-express','3000',workspace_path=str(tmp_path))
    assert '"wget"' in check and '"node"' not in check
    assert 'http://127.0.0.1:3000/health' in check


@pytest.mark.parametrize('framework',['express','next','fastify','koa','nuxt','@nestjs/core'])
def test_server_frameworks_keep_existing_runtime(tmp_path,framework):
    package(tmp_path,dependencies={'react-scripts':'5.0.1',framework:'1.0.0'})
    assert frontend_dockerfile(str(tmp_path)) is None


def test_vite_default_output_supported_but_custom_config_is_not_guessed(tmp_path):
    package(tmp_path,dependencies={'vite':'5.4.0'},scripts={'build':'vite build'})
    assert static_frontend_output(str(tmp_path))=='dist'
    (tmp_path/'vite.config.ts').write_text('export default {build:{outDir:"site"}}')
    assert static_frontend_output(str(tmp_path)) is None


@pytest.mark.parametrize('port',[0,65536,'3000; injected',True])
def test_invalid_ports_cannot_enter_nginx_config(tmp_path,port):
    package(tmp_path)
    with pytest.raises(ValueError):frontend_dockerfile(str(tmp_path),port)
