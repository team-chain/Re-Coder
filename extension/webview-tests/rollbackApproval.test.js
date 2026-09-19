/**
 * 롤백 제안 → 승인 UI 배선 — 보드 카드 「롤백 제안 → 승인 UI」의 회귀 잠금.
 *
 * 흐름: ECS 감시가 이상을 제안(rollbackProposal) → EcsRollbackApprovalCard
 * 렌더 → 승인/거부가 workspace.deploy.ecs.rollback 으로 호스트에 전달 →
 * ApiClient 가 POST /api/deploy/ecs/rollback → 코어가 이전 버전으로 복귀.
 * 어느 한 고리라도 빠지면 "감시는 봤는데 아무도 못 되돌리는" 상태가 된다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');
const DC = read('../webview-src/components/DeploymentCenter.tsx');
const HOST = read('../src/sidebar/SidebarProvider.ts');
const API = read('../src/core/ApiClient.ts');
const CORE = fs.readFileSync(path.join(__dirname, '../../core/api/routes/deploy_ecs.py'), 'utf8');

test('제안이 있으면 승인 카드가 렌더되고, 승인/거부 둘 다 보낸다', () => {
  assert.match(DC, /rollbackProposal && <EcsRollbackApprovalCard/, '제안이 와도 카드가 안 뜬다');
  const send = DC.match(/postMessage\("workspace\.deploy\.ecs\.rollback",\s*\{([^}]*)\}/);
  assert.ok(send, '승인 결과를 호스트로 보내지 않는다');
  assert.match(send[1], /approved/, '승인 여부가 본문에 없다 — 거부를 표현할 수 없다');
  assert.match(send[1], /proposal_id/i, '어느 제안에 대한 응답인지 없다');
});

test('호스트가 메시지를 받아 코어 롤백 API 를 부른다', () => {
  const start = HOST.indexOf("case 'workspace.deploy.ecs.rollback'");
  assert.notStrictEqual(start, -1, '호스트 핸들러가 없다 — 카드 버튼이 무반응이 된다');
  assert.match(API, /'POST', '\/api\/deploy\/ecs\/rollback'/, 'ApiClient 가 롤백 경로를 안 부른다');
  assert.ok(CORE.includes('"/api/deploy/ecs/rollback"'), '코어에 롤백 라우트가 없다');
});

test('승인 없이 자동 롤백하지 않는다 — 폴링 경로에 실행 호출이 없어야 한다', () => {
  //: 감시 폴링(workspace.deploy.ecs.status)은 제안만 나르고, 실행은
  //: 반드시 승인 버튼(rollback 메시지)으로만 간다.
  const pollStart = DC.indexOf('workspace.deploy.ecs.status"');
  assert.notStrictEqual(pollStart, -1);
  assert.ok(!/setInterval[^;]*ecs\.rollback/.test(DC), '폴링이 롤백을 직접 부른다 — 승인 우회');
});
