/**
 * Tailwind CSS configuration for the ReCoder VSCode Webview.
 *
 * 핵심 원칙 — VSCode 테마 통합:
 *   Tailwind 의 색상 토큰을 VSCode 의 --vscode-* CSS 변수로 매핑한다.
 *   이렇게 하면 사용자가 다크/라이트/하이콘트라스트 테마를 바꾸어도
 *   webview UI 가 자동으로 따라간다. (설계서 §4.1)
 */
module.exports = {
  // JIT 모드 — 사용된 utility class만 빌드에 포함
  content: [
    './index.tsx',
    './App.tsx',
    './components/**/*.{ts,tsx}',
    './hooks/**/*.{ts,tsx}',
  ],
  // VSCode webview는 사용자 테마(dark/light/HC)를 자동 따라가므로 darkMode 토글 불필요
  darkMode: 'media',
  theme: {
    extend: {
      colors: {
        // ── Editor surfaces ──────────────────────────────────────────
        'vscode-bg':            'var(--vscode-editor-background)',
        'vscode-fg':            'var(--vscode-editor-foreground)',
        'vscode-sidebar-bg':    'var(--vscode-sideBar-background)',
        'vscode-sidebar-fg':    'var(--vscode-sideBar-foreground)',
        'vscode-panel-bg':      'var(--vscode-panel-background)',
        'vscode-panel-border':  'var(--vscode-panel-border, #444)',
        'vscode-tab-bg':        'var(--vscode-editorGroupHeader-tabsBackground, #2d2d2d)',
        'vscode-tab-active-fg': 'var(--vscode-tab-activeForeground)',
        // ── Inputs / buttons ─────────────────────────────────────────
        'vscode-input-bg':      'var(--vscode-input-background)',
        'vscode-input-fg':      'var(--vscode-input-foreground)',
        'vscode-input-border':  'var(--vscode-input-border, #3c3c3c)',
        'vscode-btn-bg':        'var(--vscode-button-background)',
        'vscode-btn-fg':        'var(--vscode-button-foreground)',
        'vscode-btn-hover':     'var(--vscode-button-hoverBackground)',
        'vscode-btn-secondary-bg': 'var(--vscode-button-secondaryBackground)',
        'vscode-btn-secondary-fg': 'var(--vscode-button-secondaryForeground)',
        // ── Semantic ─────────────────────────────────────────────────
        'vscode-error':         'var(--vscode-errorForeground)',
        'vscode-warning':       'var(--vscode-editorWarning-foreground)',
        'vscode-info':          'var(--vscode-editorInfo-foreground)',
        'vscode-success':       'var(--vscode-testing-iconPassed, #4ade80)',
        // ── Diff coloring ────────────────────────────────────────────
        'vscode-diff-add':      'var(--vscode-diffEditor-insertedTextBackground, rgba(46,160,67,0.25))',
        'vscode-diff-del':      'var(--vscode-diffEditor-removedTextBackground, rgba(229,83,75,0.25))',
        // ── Focus / link ─────────────────────────────────────────────
        'vscode-focus':         'var(--vscode-focusBorder)',
        'vscode-link':          'var(--vscode-textLink-foreground)',
        'vscode-link-active':   'var(--vscode-textLink-activeForeground)',
        // ── Description text ─────────────────────────────────────────
        'vscode-desc':          'var(--vscode-descriptionForeground)',
      },
      fontFamily: {
        vscode: 'var(--vscode-font-family)',
        mono:   'var(--vscode-editor-font-family, monospace)',
      },
      fontSize: {
        vscode: 'var(--vscode-font-size)',
      },
      borderRadius: {
        DEFAULT: '4px',
        card:    '6px',
      },
    },
  },
  // VSCode 의 inline CSSProperties 와 공존해야 하므로 preflight(베이스 reset)는 비활성.
  // 기존 인라인 스타일에 영향을 주지 않으면서 utility class 만 사용한다.
  corePlugins: {
    preflight: false,
  },
  plugins: [],
};
