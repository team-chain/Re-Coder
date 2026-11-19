// 워크스페이스 폴더를 바꾸면 VS Code 가 확장을 재시작해 진행 중인 요청이 사라진다(실기기 P0).
// 어떤 ReCoder 동작도 워크스페이스 폴더를 바꾸지 않고, 재시작을 넘는 저장소(globalState)는 확장이 넘겨준다.
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const provider = fs.readFileSync(
    path.join(__dirname, '../src/sidebar/SidebarProvider.ts'), 'utf8');
const extension = fs.readFileSync(
    path.join(__dirname, '../src/extension.ts'), 'utf8');

test('어떤 ReCoder 동작도 워크스페이스 폴더를 바꾸지 않는다(확장 재시작 금지)', () => {
    const files = ['../src/sidebar/SidebarProvider.ts', '../src/activeProject.ts', '../src/extension.ts', '../src/sidebar/canvasHost.ts']
        .map((rel) => [rel, fs.readFileSync(path.join(__dirname, rel), 'utf8')]);
    for (const [rel, src] of files) {
        assert.ok(!/updateWorkspaceFolders\(/.test(src), `${rel} 가 워크스페이스 폴더를 바꾼다`);
    }
});

test('extension.ts 가 globalState 를 SidebarProvider 에 넘긴다', () => {
    const start = extension.indexOf('new SidebarProvider(');
    assert.ok(start > 0);
    //: 인자 중간의 콜백에도 ');' 가 있어서, 생성자 호출의 끝은
    //: 줄 시작의 ');' 로 찾는다.
    const end = extension.indexOf('\n    );', start);
    const args = extension.slice(start, end);
    assert.match(args, /context\.globalState/,
        'globalState 를 안 넘기면 화면 상태 · AI 공급자 선택이 저장될 곳이 없다');
});
