/**
 * 회귀: Workspace 창에서 설계 결정(AI-DLC) 경로가 살아있는가
 *
 * 배경 — 이 테스트가 왜 있는가
 *   `BuildMode` 에 `showCodeAgent` 플래그가 있었고, 큰 작업 화면(Workspace)
 *   에서는 오른쪽 대화 패널이 그 역할을 대신한다는 전제로 `false` 를 넘겼다.
 *   그런데 그 대화 패널은 /api/chat 만 호출해서 /api/code/plan 단계를 타지
 *   않는다. 결과적으로 Workspace 창에서는 **결정 카드가 뜨는 경로가 아예
 *   사라졌고**, 데모에서 "AI가 선택지를 제공하지 못함" 으로 드러났다.
 *   (ADR D5 항상 선택지 · D6 사람 승인이 UI 에서 소실된 것)
 *
 * 검사 방법 — 소스 문자열이 아니라 **실제 렌더 결과**를 본다
 *   정규식으로 `showCodeAgent={false}` 가 없는지 보는 검사는, 나중에 다른
 *   방식으로 숨겨지면(조건부 렌더, CSS display:none 등) 통과해 버린다.
 *   그래서 react-dom/server 로 실제로 그려서 CodeAgent 의 UI 가 결과물에
 *   있는지 확인한다.
 *
 * 음성 대조(negative control)
 *   "있다" 만 검사하면 검사식이 항상 참인 문자열을 잡고 있어도 통과한다.
 *   그래서 CodeAgent 가 없어야 하는 화면(view="ship")에서는 **없다**는 것을
 *   함께 검사한다. 이 대조가 깨지면 검사식 자체가 잘못된 것이다.
 */
const test = require('node:test');
const assert = require('node:assert');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

const { WorkspaceLayout } = require('../out/webview-test/App.js');
const { BuildMode } = require('../out/webview-test/components/BuildMode.js');

//: CodeAgent 안에만 있는 문구. 이게 렌더 결과에 있으면 설계 결정 →
//: 코드 생성 경로가 화면에 붙어 있다는 뜻이다.
const CODE_AGENT_MARKER = '코드 작성 및 수정';

function renderWorkspace(view, isAiReady = true) {
  return renderToStaticMarkup(
    React.createElement(WorkspaceLayout, {
      view,
      diagnostics: null,
      coreStatus: 'ok',
      showDiagnostics: false,
      isAiReady,
      isDockerReady: true,
      isOpsReady: true,
      costSummary: null,
      onSelectMode: () => {},
      onToggleDiagnostics: () => {},
      postMessage: () => {},
    })
  );
}

test('Workspace 창의 build 화면에 CodeAgent(설계 결정 경로)가 렌더된다', () => {
  const html = renderWorkspace('build');
  assert.ok(
    html.includes(CODE_AGENT_MARKER),
    'Workspace build 화면에 CodeAgent 가 없다 — 결정 카드가 뜰 경로가 사라졌다'
  );
});

test('[음성 대조] CodeAgent 가 없어야 하는 화면에서는 렌더되지 않는다', () => {
  const html = renderWorkspace('ship');
  assert.ok(
    !html.includes(CODE_AGENT_MARKER),
    '검사식이 화면과 무관하게 항상 참이다 — 위 테스트는 아무것도 증명하지 못한다'
  );
});

test('사이드바 경로(BuildMode 단독)에도 CodeAgent 가 그대로 있다', () => {
  const html = renderToStaticMarkup(
    React.createElement(BuildMode, { isActive: true })
  );
  assert.ok(html.includes(CODE_AGENT_MARKER), '사이드바 레이아웃에서 CodeAgent 가 사라졌다');
});

test('BuildMode 에 숨김 플래그를 넘겨도 CodeAgent 는 숨겨지지 않는다', () => {
  // 예전 플래그 이름을 그대로 넘겨 본다. prop 이 되살아나 있으면 여기서 잡힌다.
  const html = renderToStaticMarkup(
    React.createElement(BuildMode, { isActive: true, showCodeAgent: false })
  );
  assert.ok(
    html.includes(CODE_AGENT_MARKER),
    'showCodeAgent 같은 숨김 스위치가 되살아났다 — 같은 버그가 재발할 수 있다'
  );
});

test('AI가 준비되지 않아도 홈에서 Deploy에 진입할 수 있다', () => {
  const html = renderWorkspace('home', false);
  const labelIndex = html.indexOf('>Deploy<');
  assert.ok(labelIndex >= 0, 'Deploy 항목이 렌더되지 않았다');
  const buttonStart = html.lastIndexOf('<button', labelIndex);
  const buttonEnd = html.indexOf('>', buttonStart);
  const openingTag = html.slice(buttonStart, buttonEnd + 1);
  assert.ok(!openingTag.includes('disabled'), 'AI 미설정 상태에서 Deploy가 비활성화됐다');
});

test('AI가 준비되지 않아도 Ship Mode가 템플릿 폴백 UI를 렌더한다', () => {
  const html = renderWorkspace('ship', false);
  assert.ok(html.includes('Dockerfile'), 'Dockerfile 생성 탭이 사라졌다');
  assert.ok(html.includes('Compose'), 'Compose 생성 탭이 사라졌다');
  assert.ok(
    html.includes('로컬 템플릿 폴백으로 생성됩니다'),
    'AI 미설정 시 폴백 동작을 안내하지 않는다'
  );
  assert.ok(
    !html.includes('Ship Mode는 AI Ready가 필요합니다'),
    'AI Ready 전체 차단 화면이 되살아났다'
  );
});
