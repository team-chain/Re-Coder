"""Deterministic Docker runtime for plain HTML and client-only CRA/Vite projects."""
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
    root = Path(workspace)
    # Do not infer a static site from a server's public/index.html or from an
    # incomplete scaffold that still requires a compiler/runtime.
    if not any((root / name).exists() for name in (
        'package.json', 'requirements.txt', 'pyproject.toml', 'go.mod',
        'pom.xml', 'build.gradle', 'Gemfile', 'composer.json',
    )) and (root / 'index.html').is_file():
        html = (root / 'index.html').read_text(encoding='utf-8-sig', errors='replace')
        if not re.search(r'(?:src|href)\s*=\s*[\"\'][^\"\']*\.(?:tsx?|jsx|scss)(?:[?\"\'])', html, re.I):
            return '.'
    try:
        package = json.loads((Path(workspace) / 'package.json').read_text(encoding='utf-8-sig'))
        deps = {**package.get('dependencies', {}), **package.get('devDependencies', {})}
        scripts = package.get('scripts', {})
        if any(name in deps for name in ('next', 'nuxt', '@nestjs/core', 'express', 'fastify', 'koa')):
            return None
        build = scripts.get('build', '')
        if 'react-scripts' in deps and re.search(r'\breact-scripts\s+build\b', build):
            return 'build'
        # Vite normally has a config file (React's plugin lives there). A config
        # does not make it an Express server. The Docker build below explicitly
        # selects its output directory, while Vite evaluates the user's config.
        if 'vite' in deps and re.search(r'\bvite\s+build\b', build) and '--ssr' not in build:
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
    if output == '.':
        template = 'Dockerfile.html-static'
        return FileTemplateRegistry().render(template, {
            'PORT': str(port),
        }), template
    template = 'Dockerfile.node-static'
    content = FileTemplateRegistry().render(template, {'PORT': str(port), 'OUTPUT_DIR': output})
    if output == 'dist':
        content = content.replace('RUN npm run build\n', 'RUN npm run build -- --outDir /app/dist\nRUN test -s /app/dist/index.html\n')
    else:
        content = content.replace('RUN npm run build\n', 'RUN npm run build\nRUN test -s /app/build/index.html\n')
    return content, template


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
    if content == registry.render('Dockerfile.html-static', {'PORT': str(port)}).strip():
        return port
    for output in ('build', 'dist'):
        expected = registry.render('Dockerfile.node-static', {'PORT': str(port), 'OUTPUT_DIR': output})
        checked = expected.replace('RUN npm run build\n', (
            'RUN npm run build -- --outDir /app/dist\nRUN test -s /app/dist/index.html\n'
            if output == 'dist' else 'RUN npm run build\nRUN test -s /app/build/index.html\n'
        ))
        variants = (expected, checked)
        # Keep recognizing already generated 1.1.2–1.1.6 nginx runtimes.
        variants += tuple(value.replace('RUN apk upgrade --no-cache && printf', 'RUN printf') for value in variants)
        if content in (value.strip() for value in variants):
            return port
    return None
