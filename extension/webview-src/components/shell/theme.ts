/**
 * 3.0.2 화면 테마 — 로고 색(청록 → 스틸 블루) 하나로 모든 화면을 맞춘다.
 *
 *  · 토큰(--rc-*)은 body 에 둔다. 기존 화면 수십 개가 var(--vscode-*) 로 색을 쓰므로, 같은 이름의 변수를
 *    body 에서 다시 정의해 한 번에 같은 톤이 되게 한다(고대비 테마는 접근성 때문에 VS Code 값을 그대로 둔다).
 *  · 글꼴은 확장에 들어 있는 Pretendard(OFL). 외부 요청 없음.
 *  · 레이아웃 규칙(위 막대 · 타일 · 아래 요청창)은 여기 한 곳에만 있다.
 */
export const THEME_CSS = `
body{
  --rc-teal:#35A9D1;--rc-steel:#7891B4;--rc-ok:#3DD68C;--rc-warn:#F2B544;--rc-bad:#F27272;
  --rc-grad:linear-gradient(135deg,#36B1D9 0%,#5E92C4 55%,#7B8FB6 100%);
  --rc-mono:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
}
body.vscode-dark,body:not(.vscode-light):not(.vscode-high-contrast):not(.vscode-high-contrast-light){
  --rc-bg:#070A0E;--rc-solid:#0D131A;--rc-tile:rgba(148,178,210,.035);--rc-tile2:rgba(148,178,210,.065);
  --rc-line:rgba(160,190,220,.09);--rc-line2:rgba(160,190,220,.16);
  --rc-fg:#EEF3F8;--rc-fg2:#9AA8B8;--rc-fg3:#67768A;--rc-shadow:0 24px 60px -24px rgba(0,0,0,.85);
  --rc-glow1:rgba(53,169,209,.18);--rc-glow2:rgba(120,145,180,.14);
  --vscode-editor-background:var(--rc-bg);--vscode-sideBar-background:var(--rc-bg);--vscode-panel-background:var(--rc-bg);
  --vscode-foreground:var(--rc-fg);--vscode-editor-foreground:var(--rc-fg);--vscode-descriptionForeground:var(--rc-fg2);
  --vscode-panel-border:var(--rc-line2);--vscode-widget-border:var(--rc-line2);
  --vscode-editorWidget-background:var(--rc-solid);--vscode-sideBarSectionHeader-background:var(--rc-solid);
  --vscode-input-background:#0A1017;--vscode-input-border:var(--rc-line2);--vscode-input-foreground:var(--rc-fg);
  --vscode-dropdown-background:#0A1017;--vscode-dropdown-border:var(--rc-line2);
  --vscode-button-background:#1E86B2;--vscode-button-hoverBackground:#2596C4;--vscode-button-foreground:#FFFFFF;
  --vscode-button-secondaryBackground:rgba(148,178,210,.1);--vscode-button-secondaryForeground:var(--rc-fg);
  --vscode-textLink-foreground:#5CC3E6;--vscode-textLink-activeForeground:#8AD6F0;--vscode-focusBorder:#35A9D1;
  --vscode-list-activeSelectionBackground:rgba(53,169,209,.16);--vscode-list-activeSelectionForeground:#FFFFFF;
  --vscode-list-hoverBackground:rgba(148,178,210,.06);--vscode-badge-background:#1E86B2;--vscode-badge-foreground:#FFFFFF;
  --vscode-charts-green:var(--rc-ok);--vscode-testing-iconPassed:var(--rc-ok);--vscode-charts-blue:var(--rc-teal);
  --vscode-editorWarning-foreground:var(--rc-warn);--vscode-charts-yellow:var(--rc-warn);
  --vscode-editorError-foreground:var(--rc-bad);--vscode-errorForeground:var(--rc-bad);--vscode-charts-red:var(--rc-bad);
  --vscode-textBlockQuote-background:rgba(148,178,210,.05);--vscode-textCodeBlock-background:rgba(4,7,10,.6);
}
body.vscode-light{
  --rc-bg:#F4F7FA;--rc-solid:#FFFFFF;--rc-tile:#FFFFFF;--rc-tile2:#F0F4F8;
  --rc-line:rgba(20,45,75,.08);--rc-line2:rgba(20,45,75,.14);
  --rc-fg:#0E1A26;--rc-fg2:#4E5D6E;--rc-fg3:#8494A6;--rc-shadow:0 18px 40px -22px rgba(20,45,75,.35);
  --rc-teal:#1F8EB8;--rc-ok:#14945C;--rc-warn:#B97F0C;--rc-bad:#CF4343;
  --rc-glow1:rgba(31,142,184,.12);--rc-glow2:rgba(94,119,153,.10);
  --vscode-editor-background:var(--rc-bg);--vscode-sideBar-background:var(--rc-bg);
  --vscode-foreground:var(--rc-fg);--vscode-editor-foreground:var(--rc-fg);--vscode-descriptionForeground:var(--rc-fg2);
  --vscode-panel-border:var(--rc-line2);--vscode-editorWidget-background:#FFFFFF;
  --vscode-input-background:#FFFFFF;--vscode-input-border:var(--rc-line2);
  --vscode-button-background:#1A7FA8;--vscode-button-hoverBackground:#1F8EB8;--vscode-button-foreground:#FFFFFF;
  --vscode-textLink-foreground:#1A7FA8;--vscode-focusBorder:#1F8EB8;
  --vscode-list-activeSelectionBackground:rgba(31,142,184,.12);--vscode-list-activeSelectionForeground:var(--rc-fg);
}
body.vscode-high-contrast,body.vscode-high-contrast-light{
  --rc-bg:var(--vscode-editor-background,#000);--rc-solid:var(--vscode-editor-background,#000);--rc-tile:transparent;--rc-tile2:transparent;
  --rc-line:var(--vscode-contrastBorder,#6FC3DF);--rc-line2:var(--vscode-contrastBorder,#6FC3DF);
  --rc-fg:var(--vscode-foreground,#fff);--rc-fg2:var(--vscode-foreground,#fff);--rc-fg3:var(--vscode-descriptionForeground,#ddd);
  --rc-shadow:none;--rc-glow1:transparent;--rc-glow2:transparent;
}
body{background:var(--rc-bg);color:var(--rc-fg);font-family:'Pretendard',var(--vscode-font-family,system-ui),sans-serif;letter-spacing:-.006em;-webkit-font-smoothing:antialiased;font-feature-settings:"tnum"}
html[data-recoder-layout=workspace] body:before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;background:
  radial-gradient(640px 420px at 6% -4%,var(--rc-glow1),transparent 62%),
  radial-gradient(560px 420px at 98% 2%,var(--rc-glow2),transparent 62%)}
#root{position:relative;z-index:1}
.rc-app button,.rc-app input,.rc-app select,.rc-app textarea{font-family:inherit}
.rc-mono{font-family:var(--rc-mono);letter-spacing:0}

/* ── 위 막대 ── */
.rc-top{position:relative;z-index:5;display:flex;align-items:center;gap:12px;height:60px;padding:0 22px;flex:none}
.rc-top .rc-brand{all:unset;cursor:pointer;display:flex;align-items:center;gap:10px;font-weight:700;font-size:15.5px;letter-spacing:-.02em;color:var(--rc-fg)}
.rc-top .rc-brand img{width:28px;height:28px;display:block}
.rc-top .rc-brand:focus-visible{outline:2px solid var(--rc-teal);outline-offset:4px;border-radius:8px}
.rc-proj{display:flex;align-items:center;gap:8px;height:30px;padding:0 11px;border-radius:9px;border:1px solid var(--rc-line);background:var(--rc-tile);font-size:12.5px;color:var(--rc-fg2);max-width:220px;min-width:0}
.rc-proj b{color:var(--rc-fg);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-proj span{font-family:var(--rc-mono);font-size:11px;white-space:nowrap}
.rc-pills{display:flex;gap:2px;padding:3px;border-radius:12px;background:var(--rc-tile);border:1px solid var(--rc-line)}
.rc-pills button{all:unset;cursor:pointer;display:flex;align-items:center;padding:6px 13px;border-radius:9px;color:var(--rc-fg2);font-size:13.5px;font-weight:500;white-space:nowrap}
.rc-pills button:hover{color:var(--rc-fg)}
.rc-pills button[aria-current=page]{background:linear-gradient(180deg,rgba(53,169,209,.24),rgba(53,169,209,.10));color:var(--rc-fg);box-shadow:inset 0 0 0 1px rgba(53,169,209,.38)}
.rc-pills button:focus-visible{outline:2px solid var(--rc-teal);outline-offset:1px}
.rc-pills .rc-nav-badge{display:inline-grid;place-items:center;min-width:17px;height:17px;padding:0 4px;margin-left:6px;border-radius:9px;background:var(--rc-warn);color:#1A1300;font-size:10.5px;font-weight:700}
.rc-top-right{margin-left:auto;display:flex;align-items:center;gap:6px;min-width:0}
.rc-top-right>*{flex:none}
.rc-top-right>button,.rc-top-right .rc-connection-button,.rc-top-right .rc-status-toggle,.rc-top-right .rc-quick-button{all:unset;cursor:pointer;height:30px;display:flex!important;align-items:center;gap:7px;padding:0 11px;border-radius:999px;background:var(--rc-tile);border:1px solid var(--rc-line);font-size:12px;color:var(--rc-fg2);white-space:nowrap}
.rc-top-right button:hover{color:var(--rc-fg);border-color:var(--rc-line2)}
.rc-top-right button:focus-visible{outline:2px solid var(--rc-teal);outline-offset:2px}
.rc-top-right kbd{font-family:var(--rc-mono);font-size:10.5px;border:1px solid var(--rc-line2);border-radius:5px;padding:0 5px;color:var(--rc-fg2)}
.rc-top-right .rc-connection-dot{width:6px;height:6px;border-radius:50%;background:var(--rc-warn)}
.rc-top-right .rc-connection-dot[data-ready=true]{background:var(--rc-ok);box-shadow:0 0 8px var(--rc-ok)}

/* ── 본문 · 아래 요청창 ── */
.rc-main{position:relative;z-index:1;flex:1;min-height:0;overflow:auto;padding:6px 22px 108px}
.rc-main.rc-no-dock{padding-bottom:28px}
.rc-dock{position:fixed;z-index:6;left:50%;bottom:18px;transform:translateX(-50%);width:min(660px,calc(100% - 32px));height:56px;border-radius:18px;
  background:color-mix(in srgb,var(--rc-solid) 88%,transparent);border:1px solid var(--rc-line2);box-shadow:0 30px 80px -12px rgba(0,0,0,.8);
  backdrop-filter:blur(22px);display:flex;align-items:center;gap:10px;padding:0 8px 0 14px}
body.vscode-light .rc-dock{box-shadow:0 20px 50px -18px rgba(20,45,75,.35)}
.rc-dock .rc-dock-ai{width:28px;height:28px;border-radius:9px;display:grid;place-items:center;background:rgba(53,169,209,.14);color:var(--rc-teal);flex:none}
.rc-dock input{flex:1;min-width:0;height:40px;border:0;outline:0;background:transparent;color:var(--rc-fg);font-size:14.5px}
.rc-dock input::placeholder{color:var(--rc-fg3)}
.rc-dock kbd{font-family:var(--rc-mono);font-size:10.5px;color:var(--rc-fg3);border:1px solid var(--rc-line2);border-radius:5px;padding:2px 6px}
.rc-dock .rc-dock-go{all:unset;cursor:pointer;width:40px;height:40px;border-radius:12px;background:var(--rc-grad);display:grid;place-items:center;color:#04121A;flex:none}
.rc-dock .rc-dock-go[aria-disabled=true]{opacity:.45;cursor:default}
.rc-dock:focus-within{border-color:rgba(53,169,209,.55);box-shadow:0 0 0 4px rgba(53,169,209,.12),0 30px 80px -12px rgba(0,0,0,.8)}

/* ── 타일(벤토) ── */
.rc-tile{position:relative;border-radius:18px;background:var(--rc-tile);border:1px solid var(--rc-line);overflow:hidden;box-shadow:inset 0 1px 0 rgba(255,255,255,.035)}
body.vscode-light .rc-tile{box-shadow:0 1px 2px rgba(20,45,75,.05)}
.rc-tile-hd{display:flex;align-items:center;gap:8px;padding:13px 16px 0 18px;font-size:12.5px;color:var(--rc-fg2);font-weight:500;position:relative;z-index:2}
.rc-tile-hd .rc-more{all:unset;cursor:pointer;margin-left:auto;display:flex;align-items:center;gap:3px;color:var(--rc-fg3);font-size:12px}
.rc-tile-hd .rc-more:hover{color:var(--rc-fg)}.rc-tile-hd .rc-more:focus-visible{outline:2px solid var(--rc-teal);border-radius:6px}
.rc-chip{height:22px;display:inline-flex;align-items:center;gap:6px;padding:0 8px;border-radius:999px;font-size:11.5px;background:var(--rc-tile2);border:1px solid var(--rc-line);color:var(--rc-fg2);white-space:nowrap}
.rc-chip i{width:6px;height:6px;border-radius:50%;background:var(--rc-ok);display:inline-block}
.rc-chip.warn{color:var(--rc-warn)}.rc-chip.warn i{background:var(--rc-warn)}.rc-chip.bad{color:var(--rc-bad)}.rc-chip.bad i{background:var(--rc-bad)}.rc-chip.off i{background:var(--rc-fg3)}
.rc-btn{all:unset;box-sizing:border-box;cursor:pointer;height:34px;display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:0 14px;border-radius:10px;font-weight:600;font-size:13px;border:1px solid var(--rc-line2);background:var(--rc-tile2);color:var(--rc-fg);white-space:nowrap}
.rc-btn:hover{border-color:rgba(53,169,209,.45)}
.rc-btn.p{background:var(--rc-grad);border-color:transparent;color:#04121A;box-shadow:0 8px 22px -10px rgba(53,169,209,.7)}
.rc-btn.sm{height:28px;padding:0 10px;font-size:12px;border-radius:8px}
.rc-btn:focus-visible{outline:2px solid var(--rc-teal);outline-offset:2px}
.rc-btn[aria-disabled=true]{opacity:.5;cursor:default}
.rc-empty{color:var(--rc-fg3);font-size:12.5px}

/* ── 허브 안 탭 ── */
.rc-subtabs{display:flex;gap:2px;margin:2px 0 18px;border-bottom:1px solid var(--rc-line);overflow-x:auto}
.rc-subtabs button{all:unset;cursor:pointer;display:flex;align-items:center;gap:8px;padding:10px 12px 12px;color:var(--rc-fg2);font-size:13.5px;border-bottom:2px solid transparent;margin-bottom:-1px;white-space:nowrap}
.rc-subtabs button:hover{color:var(--rc-fg)}
.rc-subtabs button[aria-current=page]{color:var(--rc-fg);border-color:var(--rc-teal)}
.rc-subtabs button:focus-visible{outline:2px solid var(--rc-teal);outline-offset:-2px;border-radius:6px}
.rc-subtabs button[aria-disabled=true]{opacity:.45}

/* ── 예전 화면들의 인라인 스타일을 같은 톤으로(버튼 · 입력칸 · 모서리) ── */
.rc-app button[style*="vscode-button-background"]:not(:disabled){background:var(--rc-grad)!important;color:#04121A!important;border-color:transparent!important;box-shadow:0 8px 20px -12px rgba(53,169,209,.8)}
.rc-app button[style*="vscode-button-background"]{font-weight:600}
.rc-app button:is([style*="#2563eb"],[style*="#3794ff"],[style*="#0e639c"],[style*="#16a34a"],[style*="#22c55e"],[style*="#2ea043"]):not(:disabled){background:var(--rc-grad)!important;color:#04121A!important;border-color:transparent!important}
.rc-app :is(div,section,pre)[style*="#252526"],.rc-app :is(div,section,pre)[style*="#1e1e1e"],.rc-app :is(div,section,pre)[style*="#1c1c1c"],.rc-app :is(div,section,pre)[style*="#1a1a1a"]{background:var(--rc-solid)!important}
:where(.rc-app) :where(input:not([type=checkbox]):not([type=radio]):not([type=range]),select,textarea){background-color:var(--vscode-input-background);color:var(--rc-fg);border:1px solid var(--rc-line2)}
.rc-dock input{border:0;background:transparent}
.rc-app button[style*="border-radius: 4px"],.rc-app button[style*="border-radius: 5px"],.rc-app button[style*="border-radius: 6px"],.rc-app button[style*="border-radius: 7px"]{border-radius:9px!important}
:where(.rc-app) :where(input:not([type=checkbox]):not([type=radio]):not([type=range]),select,textarea){border-radius:10px}
.rc-app input:focus-visible,.rc-app select:focus-visible,.rc-app textarea:focus-visible{outline:2px solid rgba(53,169,209,.6);outline-offset:1px}
.rc-feature-body [style*="border-radius: 6px"]:not(button):not(span),.rc-feature-body [style*="border-radius: 8px"]:not(button):not(span){border-radius:12px!important}
.rc-app ::selection{background:rgba(53,169,209,.35)}
.rc-app ::-webkit-scrollbar{width:10px;height:10px}.rc-app ::-webkit-scrollbar-thumb{background:var(--rc-line2);border-radius:10px;border:2px solid transparent;background-clip:content-box}

@media (prefers-reduced-motion:reduce){.rc-app *{animation:none!important;transition:none!important}}
@media (max-width:1100px){.rc-proj{display:none}}
@media (max-width:1000px){.rc-top{gap:8px;padding:0 14px}.rc-top-right .rc-quick-button{display:none!important}}
@media (max-width:700px){.rc-top{height:auto;flex-wrap:wrap;padding:10px 12px;row-gap:8px}.rc-pills{order:5;width:100%;justify-content:space-between}.rc-pills button{padding:6px 8px;font-size:12.5px}
  .rc-top-right .rc-status-toggle{display:none!important}.rc-main{padding:4px 12px 100px}}
`;
