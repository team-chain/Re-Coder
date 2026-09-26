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


def test_vite_config_uses_static_runtime_and_explicit_output(tmp_path):
    package(tmp_path,dependencies={'vite':'5.4.0'},scripts={'build':'vite build'})
    assert static_frontend_output(str(tmp_path))=='dist'
    (tmp_path/'vite.config.ts').write_text('export default {build:{outDir:"site"}}')
    assert static_frontend_output(str(tmp_path)) == 'dist'
    content, template = frontend_dockerfile(str(tmp_path))
    assert template == 'Dockerfile.node-static'
    assert 'npm run build -- --outDir /app/dist' in content
    assert 'test -s /app/dist/index.html' in content
    assert '/app/dist/' in content
    assert '"index.js"' not in content


def test_standard_react_vite_config_is_not_an_express_server(tmp_path):
    package(tmp_path, dependencies={'react':'18','vite':'5'}, scripts={'build':'vite build'})
    (tmp_path/'vite.config.js').write_text("import react from '@vitejs/plugin-react'; export default {plugins:[react()],server:{port:3000}}")
    content, template = _dockerfile_from_template(str(tmp_path), StackType.NODE_EXPRESS, SimpleNamespace(default_port=3000))
    assert template == 'Dockerfile.node-static' and 'nginx' in content


def test_older_static_images_keep_their_compose_probe_after_template_update(tmp_path):
    from static_frontend import generated_static_runtime_port
    package(tmp_path)
    content, _ = frontend_dockerfile(str(tmp_path), 8081)
    previous = content.replace('RUN apk upgrade --no-cache && printf', 'RUN printf')
    previous = previous.replace('RUN test -s /app/build/index.html\n', '')
    path = tmp_path/'Dockerfile'
    path.write_text(previous)
    assert generated_static_runtime_port(path) == 8081


@pytest.mark.parametrize('port',[0,65536,'3000; injected',True])
def test_invalid_ports_cannot_enter_nginx_config(tmp_path,port):
    package(tmp_path)
    with pytest.raises(ValueError):frontend_dockerfile(str(tmp_path),port)


def test_plain_html_builds_without_ai_node_or_python_and_keeps_secrets_out(tmp_path):
    from static_frontend import generated_static_runtime_port
    from infra_agent import compose_health_check_block, generate_dockerfile
    (tmp_path/'index.html').write_text('<link href="styles.css"><script src="app.js"></script>')
    (tmp_path/'app.js').write_text('localStorage.setItem("test","ok")')
    (tmp_path/'styles.css').write_text('body { color: blue; }')
    (tmp_path/'.env').write_text('TOKEN=example')
    (tmp_path/'secrets.json').write_text('{"key":"private"}')
    (tmp_path/'docs').mkdir()
    (tmp_path/'docs'/'private.html').write_text('not public')
    assert static_frontend_output(str(tmp_path)) == '.'
    content, template = _dockerfile_from_template(str(tmp_path), StackType.UNKNOWN)
    assert template == 'Dockerfile.html-static'
    assert 'test -s /site/index.html' in content and 'COPY --from=assets' in content
    assert '*/secrets.json' in content and './docs/*' in content
    assert 'npm' not in content and 'python' not in content
    dockerfile = tmp_path/'Dockerfile'
    dockerfile.write_text(content)
    assert generated_static_runtime_port(dockerfile) == 3000
    assert 'wget' in compose_health_check_block('unknown', '3000', workspace_path=str(tmp_path))
    assert generate_dockerfile(workspace_path=str(tmp_path)).base_template == template


def test_plain_html_route_saves_ready_to_build_proposal_without_llm(tmp_path, monkeypatch):
    from api.routes import deploy
    (tmp_path/'index.html').write_text('<h1>Board</h1>')
    monkeypatch.setattr(deploy, '_get_infra_agent', lambda: None)
    proposal = asyncio.run(deploy.generate_dockerfile(deploy.DockerfileRequest(workspace_path=str(tmp_path))))
    assert proposal.base_template == 'Dockerfile.html-static'
    saved = deploy._write_proposal_to_workspace(proposal, str(tmp_path), proposal.proposal_id)
    assert saved['status'] == 'saved' and (tmp_path/'.dockerignore').exists()


@pytest.mark.parametrize('marker', ['requirements.txt', 'pyproject.toml', 'package.json', 'composer.json'])
def test_plain_html_does_not_override_unknown_backend_or_build_tool(tmp_path, marker):
    (tmp_path/'index.html').write_text('<h1>not enough evidence</h1>')
    (tmp_path/marker).write_text('{}')
    assert frontend_dockerfile(str(tmp_path)) is None


def test_s3_preflight_has_no_container_requirement_even_if_dockerfile_exists(tmp_path):
    from api.routes import deploy
    (tmp_path/'index.html').write_text('<h1>Board</h1>')
    (tmp_path/'Dockerfile').write_text('FROM scratch')
    (tmp_path/'.gitignore').write_text('.env\n.env.*\n')
    result = asyncio.run(deploy.deploy_preflight(deploy.DeployPreflightRequest(workspace_path=str(tmp_path), target='s3')))
    issues = result['reasons'] + result['warnings']
    assert not any('Runtime Preflight' in issue['message'] or 'Dockerfile' in issue['message'] or '포트' in issue['message'] for issue in issues)
    (tmp_path/'.gitignore').unlink()
    result = asyncio.run(deploy.deploy_preflight(deploy.DeployPreflightRequest(workspace_path=str(tmp_path), target='s3')))
    assert not result['blocked'], result['reasons']
    (tmp_path/'.env').write_text('API_KEY=fixture')
    result = asyncio.run(deploy.deploy_preflight(deploy.DeployPreflightRequest(workspace_path=str(tmp_path), target='s3')))
    assert any(issue['code'] == 'ENV_FILE_NOT_GITIGNORED' for issue in result['reasons'])
