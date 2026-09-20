/**
 * Workbench 메시지 배선 — 보드 이슈 「Workbench 뷰 버튼 대부분이 무반응」.
 *
 * 무엇이 사고였나
 *   같은 workbenchHtml 을 쓰는 화면이 둘(에디터 패널 / 사이드바 뷰)인데
 *   핸들러는 패널에만 37종이 있었고 사이드바에는 9종뿐이었다. 게다가 패널은
 *   아무도 인스턴스화하지 않는 고아라, **실제로 렌더되는 사이드바에서
 *   GitHub 탭·배포 서브탭·Discord 탭 버튼이 전부 무반응**이었다.
 *
 * 여기서 고정하는 것
 *   1. 핸들러 구현이 한 곳(workbenchHost)에만 있다 — 화면이 둘이어도 갈라지지 않는다.
 *   2. HTML 이 보내는 메시지를 호스트가 **빠짐없이** 받는다. 이게 핵심 회귀 검사다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

const HOST = read('../src/sidebar/workbenchHost.ts');
const HTML = read('../src/sidebar/workbenchHtml.ts');
const PANEL = read('../src/sidebar/WorkbenchPanel.ts');
const SIDEBAR = read('../src/sidebar/WorkbenchSidebarProvider.ts');

/** 호스트가 switch 로 처리하는 메시지 타입. */
function handledTypes(source) {
  return new Set(
    [...source.matchAll(/case '(wb\.[a-zA-Z0-9.]+)':/g)].map((m) => m[1]),
  );
}

/** 웹뷰 HTML 이 postMessage 로 **보내는** 메시지 타입.
 *  (수신용 문자열과 섞이지 않도록 post 호출 형태만 고른다.) */
function sentTypes(source) {
  const out = new Set();
  for (const m of source.matchAll(/post\(\s*['"](wb\.[a-zA-Z0-9.]+)['"]/g)) { out.add(m[1]); }
  for (const m of source.matchAll(/postMessage\(\s*\{\s*type:\s*['"](wb\.[a-zA-Z0-9.]+)['"]/g)) { out.add(m[1]); }
  return out;
}

test('두 화면 모두 공통 호스트를 상속한다 (구현이 한 곳)', () => {
  assert.match(PANEL, /extends WorkbenchHost/, '패널이 공통 호스트를 안 쓴다');
  assert.match(SIDEBAR, /extends WorkbenchHost/, '사이드바가 공통 호스트를 안 쓴다');
  //: 핸들러가 다시 갈라지면 이 검사가 깨진다.
  assert.ok(!/case 'wb\./.test(PANEL), '패널에 별도 핸들러가 되살아났다');
  assert.ok(!/case 'wb\./.test(SIDEBAR), '사이드바에 별도 핸들러가 되살아났다');
});

test('호스트가 GitHub·배포·Discord 핸들러를 모두 갖는다', () => {
  const handled = handledTypes(HOST);
  const must = [
    'wb.gh.login', 'wb.gh.logout', 'wb.gh.status', 'wb.gh.listRepos',
    'wb.gh.createRepo', 'wb.gh.setSecret', 'wb.gh.push', 'wb.gh.listRuns',
    'wb.local.generate', 'wb.local.approve', 'wb.local.scan', 'wb.local.deploy',
    'wb.actions.generate', 'wb.actions.approve',
    'wb.deploy.ecs', 'wb.deploy.precheck',
    'wb.discord.fetchStatus', 'wb.discord.setChannel', 'wb.discord.openInvite',
  ];
  const missing = must.filter((t) => !handled.has(t));
  assert.deepStrictEqual(missing, [], `핸들러 누락: ${missing.join(', ')}`);
});

test('[핵심] 화면이 보내는 메시지를 호스트가 빠짐없이 받는다', () => {
  const handled = handledTypes(HOST);
  const sent = sentTypes(HTML);
  assert.ok(sent.size > 20, `보내는 메시지를 제대로 못 찾았다 (${sent.size}종) — 검사 자체를 확인할 것`);
  const unhandled = [...sent].filter((t) => !handled.has(t));
  assert.deepStrictEqual(unhandled, [],
    `화면은 보내는데 받는 곳이 없다(= 눌러도 무반응): ${unhandled.join(', ')}`);
});

test('사이드바도 양방향 sync 를 시작하고 정리한다', () => {
  //: 패널에만 있던 것. 없으면 Core/Discord 이벤트가 화면에 영영 안 뜬다.
  assert.match(SIDEBAR, /_startWorkbenchPolling\(\)/, '사이드바가 sync 를 시작하지 않는다');
  //: 시작했으면 반드시 멈춰야 한다 — 뷰가 사라진 뒤의 폴링은 순수 낭비다.
  const dispose = SIDEBAR.slice(SIDEBAR.indexOf('onDidDispose'), SIDEBAR.indexOf('onDidChangeVisibility'));
  assert.match(dispose, /_workbenchPollTimer/, 'sync 타이머를 정리하지 않는다');
});
