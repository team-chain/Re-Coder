"""Deterministic Docker runtime for client-only CRA/Vite projects."""
import json
from pathlib import Path
import re

STATIC_IGNORE_NOTICE = '저장 시 .dockerignore도 생성합니다: node_modules, 빌드 결과, Git 기록과 .env 자격증명 파일을 Docker 빌드에서 제외합니다. 기존 .dockerignore는 유지합니다.'
STATIC_DOCKERIGNORE = '''# ReCoder static frontend build context
**/node_modules
**/.git
.recoder
.vscode
build
dist
coverage
*.log
.env
.env.*
!.env.example
'''


def static_frontend_output(workspace: str) -> str | None:
    try:
        package = json.loads((Path(workspace) / 'package.json').read_text(encoding='utf-8-sig'))
        deps = {**package.get('dependencies', {}), **package.get('devDependencies', {})}
        scripts = package.get('scripts', {})
        if any(name in deps for name in ('next', 'nuxt', '@nestjs/core', 'express', 'fastify', 'koa')):
            return None
        build = scripts.get('build', '')
        if 'react-scripts' in deps and re.search(r'\breact-scripts\s+build\b', build):
            return 'build'
        # Custom Vite output/config must be reviewed rather than guessed.
        if 'vite' in deps and re.search(r'\bvite\s+build\b', build) and not any(Path(workspace).glob('vite.config.*')) and '--outDir' not in build and '--config' not in build:
            return 'dist'
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def frontend_dockerfile(workspace: str, port: int = 3000) -> tuple[str, str] | None:
    output = static_frontend_output(workspace)
    if not output:
        return None
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError('유효한 컨테이너 포트가 필요합니다.')
    from registry import FileTemplateRegistry
    template = 'Dockerfile.node-static'
    return FileTemplateRegistry().render(template, {'PORT': str(port), 'OUTPUT_DIR': output}), template


def generated_static_runtime_port(dockerfile: Path) -> int | None:
    """Recognize our runtime exactly; custom nginx images need an explicit check."""
    from deployment_inputs import dockerfile_runtime_port
    from registry import FileTemplateRegistry
    port = dockerfile_runtime_port(dockerfile)
    if port is None:
        return None
    try:
        content = dockerfile.read_text(encoding='utf-8-sig').strip()
    except (OSError, UnicodeError):
        return None
    registry = FileTemplateRegistry()
    for output in ('build', 'dist'):
        expected = registry.render('Dockerfile.node-static', {'PORT': str(port), 'OUTPUT_DIR': output})
        if content == expected.strip():
            return port
    return None
