"""Offline checks executed by the packaged binary; never reads credentials."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sqlite3
import sys

RUNTIME_IMPORTS = (
    'api.routes.canvas', 'github_agent', 'static_frontend', 'deployment_inputs', 'code_agent', 'adr', 'aws_onboarding',
    'agents.ecs_agent', 's3_byo', 'security_scan', 'local_deploy_agent',
    'llm.bedrock_provider', 'llm.gemini_provider', 'llm.provider_router', 'llm.api_key_provider',
    'commerce_starter', 'generated_validation', 'build_readiness', 'screen_check', 'build_failure', 'vuln_advice', 'npm_registry', 'local_services', 'scan_process',
    'preflight.contract_loader', 'persistence', 'nacl.public', 'paramiko',
    'google.genai', 'google.generativeai',
    #: 대규모 코드 생성(팀 모드) — code_agent 가 필요할 때 불러오므로 번들 분석이 놓치지 않게 명시한다.
    'gen_engine', 'deploy_settings', 'generation_jobs', 'generation_progress',
)

#: 실행 파일에 넣는 AWS 서비스 정의. Core 가 부르는 서비스와 자격증명 해석(SSO·로그인)에
#: 필요한 것만 담는다 — botocore 전체(1,900개 파일)를 넣으면 한 파일 실행 파일이 시작할
#: 때마다 임시 폴더에 풀고 백신이 하나하나 검사해 Windows 에서 Core 시작이 12~50초 걸렸다.
BUNDLED_AWS_SERVICES = (
    'sts', 'ecs', 'ecr', 's3', 'iam', 'elbv2', 'budgets', 'bedrock-runtime', 'bedrock',
    'logs', 'ec2', 'dynamodb', 'cloudformation', 'sso', 'sso-oidc', 'signin',
)


def run(app) -> int:
    checks: list[str] = []
    try:
        # Import from the bundle, including modules normally loaded on first use.
        for name in RUNTIME_IMPORTS:
            importlib.import_module(name)
        checks.append('runtime imports')
        from registry import CommandTemplateRegistry, FileTemplateRegistry
        commands = CommandTemplateRegistry()
        files = FileTemplateRegistry()
        assert commands._templates, 'command templates are empty'
        assert files._templates, 'file templates are empty'
        assert 'Dockerfile.node-static' in files._templates, 'frontend Dockerfile template missing'
        checks.append('command and infrastructure templates')
        from commerce_starter import operations
        starter = {op['file'] for op in operations()}
        assert {'Dockerfile', 'backend/src/server.js', 'frontend/src/pages/Checkout.jsx',
                'backend/package-lock.json', 'frontend/package-lock.json', 'backend/src/order_expiry.js'} <= starter, 'commerce foundation data missing'
        checks.append('reviewed commerce foundation')
        from botocore.session import Session
        session = Session()
        available = set(session.get_available_services())
        for service in BUNDLED_AWS_SERVICES:
            if service == 'signin' and service not in available:
                continue  # 오래된 botocore 에는 없다
            assert session.get_service_model(service).operation_names, service
        checks.append('AWS service definitions')
        import certifi
        assert Path(certifi.where()).is_file(), 'TLS certificate bundle missing'
        checks.append('TLS certificates')
        with sqlite3.connect(':memory:') as db:
            assert db.execute('select 1').fetchone() == (1,)
        checks.append('SQLite')
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(lambda: None, 'interval', seconds=60)
        checks.append('scheduler plugins')
        paths = app.openapi()['paths']
        for endpoint in ('/api/health', '/api/status', '/api/chat', '/api/code/plan',
                         '/api/code/generate', '/api/code/generate/stream', '/api/deploy/dockerfile', '/api/deploy/execute', '/api/deploy/settings', '/api/deploy/db-choice',
                         '/api/deploy/canvas', '/api/github/repository/connect', '/api/git/push'):
            assert endpoint in paths, f'route missing: {endpoint}'
        checks.append('application routes')
        from version import VERSION
        print(json.dumps({'ok': True, 'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
                          'checks': checks}))
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'checks': checks, 'error': str(exc)}))
        return 1
