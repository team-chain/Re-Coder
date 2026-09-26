// 채팅 승인 → 빈 창에 폴더 추가 → 확장 호스트 재시작에서 요청이 살아남는지.
//
// 실기기에서 실제로 터진 P0: 폴더가 하나도 없는 창에서 승인 카드를 누르면
// VSCode 가 워크스페이스 전환으로 확장을 재시작하고, chat.actionAccepted 가
// 발송되지 못해 화면은 "설계 결정을 준비하는 중…" 스피너만 영원히 돈다.
// (코어에는 /api/code/plan 이 한 번도 도착하지 않는다.)
//
// 계약: 승인 핸들러는 워크스페이스 변경 **직전에** globalState 에 요청을
// 적고, 재시작 후 첫 webview.ready 가 그 메모를 지운 뒤 이어서 발송한다.
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const provider = fs.readFileSync(
    path.join(__dirname, '../src/sidebar/SidebarProvider.ts'), 'utf8');
const extension = fs.readFileSync(
    path.join(__dirname, '../src/extension.ts'), 'utf8');

test('승인 핸들러는 워크스페이스 추가 직전에 요청을 globalState 에 적는다', () => {
    const start = provider.indexOf('private async handleChatApproveAction(');
    assert.ok(start > 0, 'handleChatApproveAction 정의가 없다');
    const end = provider.indexOf('\n    }', provider.indexOf('chat.actionAccepted', start));
    const body = provider.slice(start, end);

    const persistAt = body.indexOf('PENDING_CHAT_ACTION_KEY');
    const mutateAt = body.indexOf('updateWorkspaceFolders(');
    assert.ok(persistAt > 0, '승인 핸들러가 재시작 인계 메모를 남기지 않는다');
    assert.ok(mutateAt > 0, '워크스페이스 추가 호출이 없다');
    assert.ok(
        persistAt < mutateAt,
        '인계 메모가 워크스페이스 추가 **뒤에** 있다 — 재시작이 먼저 오면 유실된다'
    );
    //: 재시작이 없었던 경로에서는 메모를 지워 중복 발송을 막는다.
    const clearAt = body.lastIndexOf('PENDING_CHAT_ACTION_KEY');
    const acceptAt = body.indexOf('chat.actionAccepted');
    assert.ok(clearAt > acceptAt, '정상 발송 후 인계 메모를 지우지 않는다');
});

test('webview.ready 가 인계 메모를 한 번만 이어받아 발송한다', () => {
    const start = provider.indexOf("case 'webview.ready'");
    assert.ok(start > 0);
    const end = provider.indexOf("case 'aws.onboarding'", start);
    const body = provider.slice(start, end);

    assert.match(body, /PENDING_CHAT_ACTION_KEY/, 'ready 가 인계 메모를 확인하지 않는다');
    const clearAt = body.indexOf('update(SidebarProvider.PENDING_CHAT_ACTION_KEY, undefined)');
    const sendAt = body.indexOf("'chat.actionAccepted'");
    assert.ok(clearAt > 0, 'ready 가 메모를 지우지 않는다 — 웹뷰 두 개가 중복 발송한다');
    assert.ok(sendAt > 0, 'ready 가 chat.actionAccepted 를 재발송하지 않는다');
    assert.ok(clearAt < sendAt, '발송보다 삭제가 먼저여야 중복이 없다');
    assert.match(body, /restoredAfterReload/, '재발송임을 표시하지 않는다');
    //: 오래된 메모는 버린다 — 다른 작업 중인 창에 옛 요청이 끼어들지 않게.
    assert.match(body, /pending\.ts/, '메모 나이를 확인하지 않는다');
});

test('extension.ts 가 globalState 를 SidebarProvider 에 넘긴다', () => {
    const start = extension.indexOf('new SidebarProvider(');
    assert.ok(start > 0);
    //: 인자 중간의 콜백에도 ');' 가 있어서, 생성자 호출의 끝은
    //: 줄 시작의 ');' 로 찾는다.
    const end = extension.indexOf('\n    );', start);
    const args = extension.slice(start, end);
    assert.match(args, /context\.globalState/,
        'globalState 를 안 넘기면 인계 메모가 저장될 곳이 없다');
});
