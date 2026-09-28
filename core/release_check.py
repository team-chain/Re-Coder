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
    'llm.bedrock_provider', 'llm.gemini_provider', 'llm.provider_router',
    'preflight.contract_loader', 'persistence', 'nacl.public', 'paramiko',
    'google.genai', 'google.generativeai',
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
        from botocore.session import Session
        session = Session()
        for service in ('sts', 'ecs', 'ecr', 's3', 'iam', 'elbv2', 'budgets', 'bedrock-runtime'):
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
                         '/api/code/generate', '/api/deploy/dockerfile', '/api/deploy/execute',
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
