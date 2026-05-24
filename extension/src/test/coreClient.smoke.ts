/**
 * P0-12 smoke #4 (TS):
 *   `node:test` runner 로 CoreClient 의 healthCheck / analyze / getDeployStatus
 *   /api/security/scan / getReady 의 직렬화·파싱이 server.py 응답 스키마와
 *   일치하는지 확인한다.
 *
 * VSCode runtime 없이 동작하도록 `vscode` import 가 없는 코드 (api/coreClient) 만 의존.
 *
 * 실행: `npm run smoke` (package.json scripts 에 추가) 또는
 *      `npm run compile && node out/test/coreClient.smoke.js`
 */
import * as http from 'http';
import { test } from 'node:test';
import * as assert from 'node:assert/strict';

import { CoreClient } from '../api/coreClient';

const TOKEN = 'test-token-123';

interface MockHandler {
    method: 'GET' | 'POST';
    path: string;
    respond: (body: any) => any;
}

function startMock(handlers: MockHandler[]): Promise<{ server: http.Server; port: number }> {
    return new Promise((resolve) => {
        const server = http.createServer((req, res) => {
            // Token 검증
            if (req.headers['x-session-token'] !== TOKEN && req.url !== '/api/health') {
                res.writeHead(403, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ detail: 'forbidden' }));
                return;
            }
            const handler = handlers.find(h => h.method === req.method && req.url === h.path);
            if (!handler) {
                res.writeHead(404);
                res.end();
                return;
            }
            let body = '';
            req.on('data', (c) => body += c);
            req.on('end', () => {
                try {
                    const parsed = body ? JSON.parse(body) : {};
                    const out = handler.respond(parsed);
                    res.writeHead(200, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify(out));
                } catch (e: any) {
                    res.writeHead(500);
                    res.end(JSON.stringify({ detail: e.message }));
                }
            });
        });
        server.listen(0, '127.0.0.1', () => {
            const addr = server.address() as any;
            resolve({ server, port: addr.port });
        });
    });
}

test('healthCheck returns true on 200/ok', async () => {
    const { server, port } = await startMock([
        { method: 'GET', path: '/api/health', respond: () => ({ status: 'ok', version: '6.4', state: 'idle', port: 0 }) },
    ]);
    try {
        const client = new CoreClient(port, TOKEN);
        const ok = await client.healthCheck();
        assert.equal(ok, true);
    } finally {
        server.close();
    }
});

test('analyze deserializes PatchProposal directly', async () => {
    const fakeProposal = {
        schema_version: '6.4',
        proposal_id: 'p-1',
        summary: 'fix import',
        risk_level: 'low',
        risk_reasons: [],
        approval_level: 1,
        test_command: 'pytest -q',
        patches: [{ file: 'main.py', base_sha256: '0'.repeat(64), unified_diff: '--- a\n+++ b\n', reason: 'r' }],
    };
    const { server, port } = await startMock([
        { method: 'POST', path: '/api/analyze', respond: () => fakeProposal },
    ]);
    try {
        const client = new CoreClient(port, TOKEN);
        const out = await client.analyze({ workspace_path: '/tmp/x', terminal_output: 'err' });
        assert.equal(out.proposal_id, 'p-1');
        assert.equal(out.patches.length, 1);
        assert.equal(out.patches[0].file, 'main.py');
        assert.equal(out.approval_level, 1);
    } finally {
        server.close();
    }
});

test('approveInfra returns plan in response (Dockerfile path)', async () => {
    const planObj = {
        schema_version: '6.4',
        plan_id: 'plan-1',
        method: 'local_docker',
        action: 'build_and_run',
        image: 'recoder-app:latest',
        container_name: 'recoder-app',
        command_template_id: 'docker_build',
        risk_level: 'low',
        risk_reasons: [],
        approval_level: 2,
        ports: [{ host: 8000, container: 8000 }],
        env: [],
        health_check_path: '/health',
        rollback_image: '',
    };
    const { server, port } = await startMock([
        {
            method: 'POST', path: '/api/infra/approve',
            respond: () => ({ status: 'ok', saved_path: '/tmp/Dockerfile', proposal_id: 'p-2', plan: planObj, message: 'saved' }),
        },
    ]);
    try {
        const client = new CoreClient(port, TOKEN);
        const out = await client.approveInfra('p-2');
        assert.equal(out.status, 'ok');
        assert.notEqual(out.plan, null);
        assert.equal(out.plan!.plan_id, 'plan-1');
        assert.equal(out.plan!.ports[0].host, 8000);
    } finally {
        server.close();
    }
});

test('getDeployStatus parses stage/log_tail/health/finished', async () => {
    const fake = {
        stage: 'done', log_tail: ['BUILD ok', 'RUN ok'], health: true,
        finished: true, error: '', started_at: 't0', finished_at: 't1', state: 'deployed',
    };
    const { server, port } = await startMock([
        { method: 'GET', path: '/api/deploy/status', respond: () => fake },
    ]);
    try {
        const client = new CoreClient(port, TOKEN);
        const out = await client.getDeployStatus();
        assert.equal(out.stage, 'done');
        assert.equal(out.finished, true);
        assert.equal(out.health, true);
        assert.deepEqual(out.log_tail, ['BUILD ok', 'RUN ok']);
    } finally {
        server.close();
    }
});

test('runSecurityScan parses trivy + hadolint nested results', async () => {
    const fake = {
        passed: true,
        results: {
            trivy: { tool: 'trivy', passed: true, critical_count: 0, high_count: 0, findings: [], summary: 'clean' },
            hadolint: { tool: 'hadolint', passed: true, findings: [], summary: 'clean' },
        },
    };
    const { server, port } = await startMock([
        { method: 'POST', path: '/api/security/scan', respond: () => fake },
    ]);
    try {
        const client = new CoreClient(port, TOKEN);
        const out = await client.runSecurityScan('recoder-app:latest', '/tmp/Dockerfile');
        assert.equal(out.passed, true);
        assert.equal(out.results.trivy?.tool, 'trivy');
        assert.equal(out.results.hadolint?.passed, true);
    } finally {
        server.close();
    }
});

test('getReady parses 3-state Ready chips', async () => {
    const fake = { core_ready: 'ok', ai_ready: 'partial', docker_ready: 'fail', issues: ['no docker'] };
    const { server, port } = await startMock([
        { method: 'GET', path: '/api/ready', respond: () => fake },
    ]);
    try {
        const client = new CoreClient(port, TOKEN);
        const out = await client.getReady();
        assert.equal(out.core_ready, 'ok');
        assert.equal(out.ai_ready, 'partial');
        assert.equal(out.docker_ready, 'fail');
    } finally {
        server.close();
    }
});
