// 채팅 승인 → 웹뷰(React) → code.plan 발송 사슬의 웹뷰 쪽 계약.
//
// 확장 통합 하네스(harness/e2e-extension-flow.js)는 확장 호스트가 code.plan 을
// 받으면 코어까지 정상 동작함을 증명한다. 그런데 그 code.plan 을 **처음 쏘는
// 주체는 React CodeAgent** 다: App 이 chat.actionAccepted 를 받아 externalTurn 을
// 내려주고 → CodeAgent 의 effect 가 code.plan 을 post 한다. 이 고리가 끊기면
// 코어에는 /api/code/plan 이 영영 안 오고 화면은 "준비 중" 스피너만 돈다.
// jsdom 이 없어 렌더 대신 컴파일 전 소스 구조로 계약을 고정한다.
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const app = fs.readFileSync(path.join(__dirname, '../webview-src/App.tsx'), 'utf8');
const codeAgent = fs.readFileSync(path.join(__dirname, '../webview-src/components/CodeAgent.tsx'), 'utf8');

test('App 이 chat.actionAccepted 를 받아 externalTurn 을 세우고 code 화면으로 전환한다', () => {
    const at = app.indexOf('chat.actionAccepted');
    assert.ok(at > 0, 'App 이 chat.actionAccepted 를 처리하지 않는다');
    const block = app.slice(at, at + 400);
    assert.match(block, /setExternalTurn\(/, 'externalTurn 을 세우지 않는다 — CodeAgent 가 요청을 못 받는다');
    assert.match(block, /setView\("code"\)/, 'code 화면으로 전환하지 않는다');
    assert.match(block, /requestId/, 'requestId 를 externalTurn 에 싣지 않는다');
    assert.match(block, /instruction/, 'instruction 을 externalTurn 에 싣지 않는다');
});

test('CodeAgent 의 externalTurn effect 가 code.plan 을 post 한다', () => {
    const eff = codeAgent.indexOf('if (!externalTurn)');
    assert.ok(eff > 0, 'externalTurn effect 가 없다');
    const block = codeAgent.slice(eff, eff + 700);
    assert.match(block, /postMessage\("code\.plan"/, 'externalTurn 을 받고도 code.plan 을 안 보낸다 — 코어에 요청이 안 간다');
    assert.match(block, /requestId/, 'code.plan 에 requestId 를 안 싣는다');
});

test('externalTurn 은 requestId 당 한 번만 발송한다 (중복 plan 방지)', () => {
    assert.match(codeAgent, /handledExternalRef/, '중복 발송 가드(handledExternalRef)가 없다');
    const eff = codeAgent.indexOf('if (!externalTurn)');
    const block = codeAgent.slice(eff, eff + 700);
    assert.match(block, /handledExternalRef\.current === externalTurn\.requestId/, '같은 requestId 재처리를 막지 않는다');
});
