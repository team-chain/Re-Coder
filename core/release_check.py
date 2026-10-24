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
    'preflight.contract_loader', 'persistence', 'nacl.public',
    'google.genai', 'google.generativeai',
    #: 대규모 코드 생성(팀 모드) — code_agent 가 필요할 때 불러오므로 번들 분석이 놓치지 않게 명시한다.
    'gen_engine', 'deploy_settings', 'generation_jobs', 'generation_progress',
    #: 2.0.6 — 빠진 하위 package.json·빌드 설정 교정, ReCoder 결제 모듈(고정 파일)
    'node_manifests', 'node_fixups', 'payment_kit', 'payment_contract', 'unused_files',
    #: 2.0.7 — ReCoder 검증 Dockerfile(화면·서버 폴더형 Node 앱)
    'docker_kit',
    #: 2.0.12 — 번들 데이터 위치(core.* 이름으로 불려도 찾음)
    'resources',
    #: 2.0.12 — ECS 에서 앱이 시작하도록 DB·서명 키·모의 결제를 같은 태스크에 준비
    'agents.ecs_runtime',
    #: 2.0.18 — 생성 직후 실행 점검(빌드한 이미지를 ECS staging 구성으로 띄워 사용자 흐름 API 확인)
    'runtime_check',
    #: 2.0.23 — 설치 전에 수상한 npm 패키지(갓 생긴 무명·짝퉁 이름) 차단
    'package_trust',
    #: 2.0.24 — 마을 공용 게시판(서버 API 모양 추출) — gen_engine 이 필요할 때 불러온다
    'board',
    #: 2.0.25 — 기본 방패(nginx)·공격·장애 감지 알림
    'shield', 'ops_alerts', 'api.routes.ops_alerts',
    'aws_edge', 'aws_cleanup', 'api.routes.aws_cleanup',
    'aws_cdn', 'aws_rds',
    'code_security', 'vuln_recheck', 'api.routes.security_checks',
    #: 2.0.32 — 상태 파일 안전 쓰기, 시간 제한이 지켜지는 프로세스 실행(함수 안에서만 불러온다)
    'state_io', 'proc_tools', 'dockerfile_guard', 'context_gate',
    #: 2.0.36 — 작업별 AI 공급자·비용 원장·공급자 비교(라우터가 함수 안에서 불러온다)
    'llm.task_tier', 'llm.pricing', 'llm.scrub', 'llm.cost_ledger', 'model_compare', 'api.routes.models', 'code_spec',
    #: 2.0.45 — 테스트 리코더(완성 기준 시험) — 생성·실행 점검이 함수 안에서 불러온다
    'code_test',
    #: 2.0.48 — 모니터 · 24시간 감시자
    'watcher', 'api.routes.ops_watch',
    #: 3.0.2 — 마지막 성공 배포 기록(롤백 대상) · 5xx 오류 ID 응답
    'ecs_stable', 'api.errors',
)

#: 실행 파일에 넣는 AWS 서비스 정의. Core 가 부르는 서비스와 자격증명 해석(SSO·로그인)에
#: 필요한 것만 담는다 — botocore 전체(1,900개 파일)를 넣으면 한 파일 실행 파일이 시작할
#: 때마다 임시 폴더에 풀고 백신이 하나하나 검사해 Windows 에서 Core 시작이 12~50초 걸렸다.
BUNDLED_AWS_SERVICES = (
    'sts', 'ecs', 'ecr', 's3', 'iam', 'elbv2', 'budgets', 'bedrock-runtime', 'bedrock',
    'logs', 'ec2', 'dynamodb', 'cloudformation', 'sso', 'sso-oidc', 'signin',
    #: 2.0.25 공격·장애 감지 알림(지표·경보·메일)
    'cloudwatch', 'sns',
    #: 2.0.26 대규모 방패(WAF)
    'wafv2',
    #: 2.0.27 HTTPS 주소·관리형 DB
    'cloudfront', 'acm', 'route53', 'rds', 'secretsmanager',
    #: 2.0.48 24시간 감시자(사용자 계정 안 Lambda · 깨우는 규칙)
    'lambda', 'events',
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
        import package_trust
        assert len(package_trust.popular()) > 1000, 'popular npm package list missing'
        checks.append('package trust data')
        #: 같은 모듈이 core.* 이름으로도 불린다(배포 라우트) — 그때도 번들의 데이터 파일을 찾아야 한다.
        #: 실기기 2.0.11: core.agents.ecs_agent 가 _MEI…/core/registry/… 를 찾아 ECS 태스크 정의 단계에서 실패했다.
        def _both(name: str):
            """번들에 있는 이름으로만 — core.* 로 쓰이지 않는 모듈은 번들에 그 이름이 없다(그건 정상)."""
            for candidate in (name, 'core.' + name):
                try:
                    yield candidate, importlib.import_module(candidate)
                except ModuleNotFoundError as exc:
                    #: 그 모듈(이나 상위 패키지) 자체가 core.* 로 없을 때만 건너뛴다 — 그 안에서 부르는 다른 모듈이 없으면 실패다.
                    missing = str(exc.name or '')
                    if candidate == name or not (candidate == missing or candidate.startswith(missing + '.')) \
                            or missing == 'core':
                        raise
        for name, module in _both('agents.ecs_agent'):
            assert module._TEMPLATE_PATH.is_file(), f'{name}: ECS task definition template missing'
        for name, module in _both('agents.ecs_runtime'):
            assert 'listen' in module.deploy_settings.MOCK_PAYMENT_JS and module.plan(None, key='k', environment='staging',
                   provided=(), container_port=3000).summary()['sidecars'] == [], f'{name}: ECS runtime plan broken'
        for name, module in _both('commerce_starter'):
            assert (module.ROOT / 'backend').is_dir(), f'{name}: commerce foundation missing'
        for name, module in _both('registry'):
            assert module.FileTemplateRegistry()._templates, f'{name}: file templates missing'
        for name, module in _both('grounded_repair.knowledge'):
            assert module._sources_json().is_file(), f'{name}: knowledge sources missing'
        for name, module in _both('watcher'):
            assert module._handler_file().is_file() and len(module.package()) > 1000, f'{name}: watcher Lambda code missing'
        checks.append('bundled data from both import names')
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
        for endpoint in ('/api/health', '/api/status', '/api/chat', '/api/code/plan', '/api/code/spec',
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
