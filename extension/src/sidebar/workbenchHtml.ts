/**
 * ReCoder Workbench — Shared HTML Renderer
 *
 * 같은 UI를 두 곳에서 띄울 수 있게 HTML을 공유 함수로 추출.
 *
 * 사용처:
 *   - WorkbenchPanel        — Editor Area의 WebviewPanel (큰 화면)
 *   - WorkbenchSidebarProvider — Primary/Secondary Sidebar의 WebviewView (좁은 화면, Kiro 스타일)
 *
 * 두 위치 모두 같은 메시지 프로토콜(wb.*)을 사용한다. mode 인자로 narrow
 * 레이아웃을 활성화할 수 있다.
 */
import * as vscode from 'vscode';

export type WorkbenchMode = 'panel' | 'sidebar';

/**
 * Workbench HTML 생성.
 *
 * @param webview - VSCode webview 인스턴스 (CSP source 추출용)
 * @param mode    - 'panel' (editor area, 넓음) 또는 'sidebar' (좁음)
 */
export function renderWorkbenchHtml(webview: vscode.Webview, mode: WorkbenchMode = 'panel'): string {
    const nonce = Array.from({ length: 24 }, () =>
        'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'[Math.floor(Math.random() * 62)],
    ).join('');
    const cspConnect = Array.from({ length: 17 }, (_, i) => `http://127.0.0.1:${17894 + i}`).join(' ');

    // sidebar 모드는 폭이 좁으므로 단일 컬럼 + 카드 세로 배치로 자동 전환
    const isSidebar = mode === 'sidebar';

    return `<!DOCTYPE html>
<html lang="ko" data-mode="${mode}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="Content-Security-Policy"
  content="default-src 'none';
           style-src 'unsafe-inline';
           script-src 'nonce-${nonce}';
           img-src ${webview.cspSource} https: data:;
           connect-src ${cspConnect};">
<title>ReCoder Workbench</title>
<style>
:root{
  --bg0:#0d1117; --bg1:#161b22; --bg2:#21262d; --bg3:#30363d;
  --bd:#30363d; --bd2:#484f58;
  --t1:#e6edf3; --t2:#8b949e; --t3:#6e7681;
  --blue:#58a6ff; --blue-bg:rgba(88,166,255,.08);
  --green:#3fb950; --green-bg:rgba(63,185,80,.10);
  --red:#f85149; --red-bg:rgba(248,81,73,.10);
  --yellow:#d29922; --yellow-bg:rgba(210,153,34,.10);
  --r-sm:4px; --r-md:6px; --r-lg:10px;
}
*{box-sizing:border-box; margin:0; padding:0}
body{
  background:var(--bg0); color:var(--t1);
  font-family:'Malgun Gothic','Apple SD Gothic Neo','Noto Sans KR',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  font-size:13px; padding:14px 18px 18px;
}
/* ── Sidebar 모드 자동 분기 ── */
html[data-mode="sidebar"] body{ padding:10px 12px 14px; font-size:12px; }
html[data-mode="sidebar"] .tabs{ flex-wrap:wrap; gap:4px; padding:6px 0 10px; margin-bottom:12px; }
html[data-mode="sidebar"] .tab{ padding:5px 9px; font-size:11px; }
html[data-mode="sidebar"] .right-chips{ width:100%; flex-wrap:wrap; margin-left:0; margin-top:6px; gap:4px; }
html[data-mode="sidebar"] .chip{ padding:2px 7px; font-size:10px; }
html[data-mode="sidebar"] .cost{ width:100%; margin-left:0; margin-top:4px; text-align:right; }
html[data-mode="sidebar"] .greet{ flex-direction:column; align-items:flex-start; gap:8px; margin-bottom:12px; }
html[data-mode="sidebar"] .greet h2{ font-size:16px; }
html[data-mode="sidebar"] .greet .cost-large{ text-align:left; }
html[data-mode="sidebar"] .greet .cost-large b{ font-size:18px; }
html[data-mode="sidebar"] .cards{ grid-template-columns:1fr; gap:10px; margin-bottom:14px; }
html[data-mode="sidebar"] .card{ padding:14px 14px 12px; }
html[data-mode="sidebar"] .row{ grid-template-columns:1fr; gap:10px; margin-bottom:14px; }
html[data-mode="sidebar"] .quick-grid{ grid-template-columns:1fr; }
html[data-mode="sidebar"] .log-body{ max-height:140px; font-size:10px; }

/* Brand header */
.brand{display:flex; align-items:center; gap:10px; padding:4px 0 10px}
.brand .logo{width:30px; height:30px; color:var(--blue); flex-shrink:0}
.brand .name{font-size:16px; font-weight:700; color:var(--t1); letter-spacing:-0.01em; line-height:1}
.brand .tag{font-size:10px; font-weight:500; color:var(--t3); letter-spacing:0.05em; margin-top:3px}
html[data-mode="sidebar"] .brand{padding:2px 0 8px; gap:8px}
html[data-mode="sidebar"] .brand .logo{width:24px; height:24px}
html[data-mode="sidebar"] .brand .name{font-size:14px}
html[data-mode="sidebar"] .brand .tag{font-size:9px}

.tabs{display:flex; gap:6px; padding:8px 0 14px; border-bottom:1px solid var(--bd); margin-bottom:18px}
.tab{
  display:flex; align-items:center; gap:6px;
  padding:6px 14px; border-radius:var(--r-md);
  cursor:pointer; color:var(--t2); font-weight:600; font-size:12px;
  border:1px solid transparent;
}
.tab:hover{color:var(--t1); background:var(--bg1)}
.tab.active{color:var(--blue); border-color:var(--blue); background:var(--blue-bg)}
.tab .ic{width:14px;height:14px;flex-shrink:0}
.icon-svg{width:14px;height:14px;flex-shrink:0;stroke-width:1.7;fill:none;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round}
.right-chips{margin-left:auto; display:flex; align-items:center; gap:6px}
.chip{
  display:inline-flex; align-items:center; gap:5px;
  padding:3px 9px; border-radius:999px; border:1px solid var(--bd);
  font-size:11px; font-weight:600; color:var(--t3); background:var(--bg1);
}
.chip .dot{width:6px; height:6px; border-radius:50%; background:var(--t3)}
.chip.ok{color:var(--green); border-color:rgba(63,185,80,.35)} .chip.ok .dot{background:var(--green)}
.chip.warn{color:var(--yellow); border-color:rgba(210,153,34,.35)} .chip.warn .dot{background:var(--yellow)}
.chip.fail{color:var(--red); border-color:rgba(248,81,73,.35)} .chip.fail .dot{background:var(--red)}
.cost{margin-left:8px; color:var(--t2); font-size:11px}
.cost b{color:var(--green); font-weight:700}

.page{display:none}
.page.active{display:block}

/* Command Center */
.greet{display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:18px}
.greet h2{font-size:20px; font-weight:700}
.greet p{color:var(--t2); margin-top:6px; font-size:13px}
.greet .cost-large{text-align:right}
.greet .cost-large b{font-size:24px; color:var(--green)}
.greet .cost-large span{color:var(--t3); font-size:11px}

.cards{display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-bottom:18px}
.card{
  background:var(--bg1); border:1px solid var(--bd); border-radius:var(--r-lg);
  padding:18px 18px 16px; position:relative; cursor:pointer; transition:border-color .15s;
}
.card:hover{border-color:var(--bd2)}
.card .icon{width:38px; height:38px; border-radius:50%; display:flex; align-items:center; justify-content:center; margin-bottom:12px}
.card .icon svg{width:18px;height:18px;stroke:currentColor;stroke-width:1.8;fill:none;stroke-linecap:round;stroke-linejoin:round}
.card .icon.blue svg{stroke:none; fill:currentColor}
.card .icon.red{background:var(--red-bg); color:var(--red)}
.card .icon.blue{background:var(--blue-bg); color:var(--blue)}
.card .icon.green{background:var(--green-bg); color:var(--green)}
.card h3{font-size:14px; font-weight:700; margin-bottom:6px}
.card p{font-size:12px; color:var(--t2); margin-bottom:4px; line-height:1.5}
.card .meta{font-size:11px; color:var(--t3); margin-bottom:12px}
.card .badge{position:absolute; top:14px; right:14px; padding:2px 7px; border-radius:999px; background:var(--red); color:white; font-size:10px; font-weight:700}
.card .cta{font-size:11px; font-weight:600; padding:5px 10px; border-radius:var(--r-sm); border:none; cursor:pointer}
.card .cta.red{background:var(--red); color:white}
.card .cta.blue{background:var(--blue); color:white}
.card .cta.green{background:var(--green); color:white}

.row{display:grid; grid-template-columns:1.4fr 1fr; gap:14px; margin-bottom:18px}
.panel{background:var(--bg1); border:1px solid var(--bd); border-radius:var(--r-lg); padding:14px 16px}
.panel h4{font-size:13px; font-weight:700; margin-bottom:10px; display:flex; align-items:center; gap:7px}
.panel h4 .icon-svg{width:15px;height:15px}
.act-list{display:flex; flex-direction:column; gap:8px; max-height:300px; overflow-y:auto}
.act-item{display:flex; align-items:center; gap:8px; font-size:12px; color:var(--t1)}
.act-dot{width:6px; height:6px; border-radius:50%; background:var(--t3); flex-shrink:0}
.act-dot.ok{background:var(--green)} .act-dot.warn{background:var(--yellow)}
.act-dot.fail{background:var(--red)} .act-dot.info{background:var(--blue)}
.act-time{margin-left:auto; color:var(--t3); font-size:11px}
.quick-grid{display:grid; grid-template-columns:1fr 1fr; gap:8px}
.quick-btn{
  display:flex; align-items:center; gap:6px;
  padding:9px 12px; background:var(--bg2); border:1px solid var(--bd);
  border-radius:var(--r-md); color:var(--t1); font-size:12px;
  cursor:pointer; font-weight:500;
}
.quick-btn:hover{background:var(--bg3); border-color:var(--bd2)}
.quick-btn .ic{color:var(--blue); width:14px;height:14px;flex-shrink:0;stroke-width:1.8;fill:none;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round}
.quick-toggle{display:flex; align-items:center; gap:6px; font-size:11px; color:var(--t2); margin-top:10px}

/* Log panel (bottom) */
.log-panel{
  background:var(--bg1); border:1px solid var(--bd); border-radius:var(--r-lg);
  margin-top:18px;
}
.log-tabs{display:flex; align-items:center; padding:10px 14px; border-bottom:1px solid var(--bd); gap:14px; flex-wrap:wrap}
.log-tab{
  font-size:12px; color:var(--t2); cursor:pointer; padding:4px 0;
  border-bottom:2px solid transparent; font-weight:600;
}
.log-tab.active{color:var(--blue); border-bottom-color:var(--blue)}
.log-clear{margin-left:auto; font-size:11px; color:var(--t3); cursor:pointer; border:none; background:none}
.log-clear:hover{color:var(--t1)}
.log-body{padding:10px 14px; max-height:200px; overflow-y:auto; font-family:'Consolas','SF Mono',monospace; font-size:11px; line-height:1.6}
.log-pane{display:none}
.log-pane.active{display:block}
.log-line{color:var(--t1)}
.log-line.cmd{color:var(--blue)}
.log-line.ok{color:var(--green)}
.log-line.err{color:var(--red)}

/* ─── Workbench Sync Banner ─── */
.sync-banner{
  display:flex; align-items:center; gap:10px;
  padding:7px 13px; margin-bottom:10px;
  background:var(--bg1); border:1px solid var(--bd); border-radius:var(--r-md);
  font-size:11px; transition: box-shadow .35s, border-color .35s;
}
.sync-banner.flash{
  border-color: var(--blue);
  box-shadow: 0 0 0 2px var(--blue-bg);
}
.sync-label{
  font-weight:700; color:var(--t2); letter-spacing:.3px;
  text-transform:uppercase; font-size:10px;
}
.sync-mode{
  padding:2px 8px; border-radius:999px; font-weight:700; font-size:10px;
  border:1px solid var(--bd2); color:var(--t1);
}
.sync-mode.mode-home   { background:var(--bg2); color:var(--t2); border-color:var(--bd) }
.sync-mode.mode-build  { background:rgba(248,81,73,.12);  color:var(--red);    border-color:rgba(248,81,73,.40) }
.sync-mode.mode-ship   { background:rgba(88,166,255,.12); color:var(--blue);   border-color:rgba(88,166,255,.40) }
.sync-mode.mode-operate{ background:rgba(63,185,80,.12);  color:var(--green);  border-color:rgba(63,185,80,.40) }
.sync-mode.mode-recover{ background:rgba(210,153,34,.12); color:var(--yellow); border-color:rgba(210,153,34,.40) }
.sync-last{ color:var(--t1); flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
.sync-meta{ color:var(--t3); font-size:10px }
</style>
</head>
<body>

<!-- ── 인라인 SVG 아이콘 심볼 ── -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <defs>
    <!-- Re-Coder brand logo (사이드바/Workbench 헤더용) -->
    <symbol id="i-logo" viewBox="0 0 64 64">
      <g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round">
        <path d="M 51 16 A 22 22 0 0 0 13 22" stroke-width="3" />
        <polyline points="51,7 51,16 42,16" stroke-width="3" />
        <path d="M 13 48 A 22 22 0 0 0 51 42" stroke-width="3" />
        <polyline points="13,57 13,48 22,48" stroke-width="3" />
        <line x1="22" y1="20" x2="22" y2="46" stroke-width="3.5" />
        <path d="M 22 20 L 30 20 A 6 6 0 0 1 30 32 L 22 32" stroke-width="3.5" />
        <line x1="27" y1="32" x2="35" y2="46" stroke-width="3.5" />
        <polyline points="41,26 46,31 41,36" stroke-width="2.5" />
      </g>
    </symbol>
    <symbol id="i-cmd" viewBox="0 0 24 24"><polygon points="13,2 4,14 11,14 9,22 20,10 13,10" /></symbol>
    <symbol id="i-err" viewBox="0 0 24 24"><path d="M12 3 L22 20 L2 20 Z"/><line x1="12" y1="10" x2="12" y2="14"/><circle cx="12" cy="17" r="0.8" fill="currentColor" stroke="none"/></symbol>
    <symbol id="i-gh" viewBox="0 0 24 24"><path d="M12 2 a10 10 0 0 0 -3.16 19.49 c.5 .09 .68 -.22 .68 -.48 v-1.7 c-2.78 .6 -3.37 -1.34 -3.37 -1.34 -.45 -1.15 -1.11 -1.46 -1.11 -1.46 -.91 -.62 .07 -.61 .07 -.61 1 .07 1.53 1.03 1.53 1.03 .89 1.53 2.34 1.09 2.91 .83 .09 -.65 .35 -1.09 .63 -1.34 -2.22 -.25 -4.55 -1.11 -4.55 -4.94 0 -1.09 .39 -1.98 1.03 -2.68 -.1 -.25 -.45 -1.27 .1 -2.64 0 0 .84 -.27 2.75 1.02 a9.5 9.5 0 0 1 5 0 c1.91 -1.29 2.75 -1.02 2.75 -1.02 .55 1.37 .2 2.39 .1 2.64 .64 .7 1.03 1.59 1.03 2.68 0 3.84 -2.34 4.69 -4.57 4.93 .36 .31 .68 .92 .68 1.85 v2.74 c0 .27 .18 .58 .69 .48 A10 10 0 0 0 12 2 Z" /></symbol>
    <symbol id="i-up" viewBox="0 0 24 24"><path d="M4 14 a4 4 0 1 1 1.5 -7.78 a5.5 5.5 0 0 1 10.6 1.78 a4 4 0 0 1 -.6 7.95"/><polyline points="9,15 12,12 15,15"/><line x1="12" y1="12" x2="12" y2="21"/></symbol>
    <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><polyline points="12,7 12,12 16,14"/></symbol>
    <symbol id="i-bolt" viewBox="0 0 24 24"><polygon points="13,2 4,14 11,14 9,22 20,10 13,10"/></symbol>
    <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="20" y1="20" x2="16.5" y2="16.5"/></symbol>
    <symbol id="i-pkg" viewBox="0 0 24 24"><path d="M21 8.5 L12 13 L3 8.5 L12 4 Z"/><polyline points="3,8.5 3,16 12,20.5 21,16 21,8.5"/><line x1="12" y1="13" x2="12" y2="20.5"/></symbol>
    <symbol id="i-cog" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15 a1.65 1.65 0 0 0 .33 1.82 l.06 .06 a2 2 0 1 1 -2.83 2.83 l-.06 -.06 a1.65 1.65 0 0 0 -1.82 -.33 1.65 1.65 0 0 0 -1 1.51 V21 a2 2 0 0 1 -4 0 v-.09 A1.65 1.65 0 0 0 9 19.4 a1.65 1.65 0 0 0 -1.82 .33 l-.06 .06 a2 2 0 1 1 -2.83 -2.83 l.06 -.06 A1.65 1.65 0 0 0 4.6 15 a1.65 1.65 0 0 0 -1.51 -1 H3 a2 2 0 0 1 0 -4 h.09 A1.65 1.65 0 0 0 4.6 9 a1.65 1.65 0 0 0 -.33 -1.82 l-.06 -.06 a2 2 0 1 1 2.83 -2.83 l.06 .06 A1.65 1.65 0 0 0 9 4.6 a1.65 1.65 0 0 0 1 -1.51 V3 a2 2 0 0 1 4 0 v.09 A1.65 1.65 0 0 0 15 4.6 a1.65 1.65 0 0 0 1.82 -.33 l.06 -.06 a2 2 0 1 1 2.83 2.83 l-.06 .06 A1.65 1.65 0 0 0 19.4 9 a1.65 1.65 0 0 0 1.51 1 H21 a2 2 0 0 1 0 4 h-.09 a1.65 1.65 0 0 0 -1.51 1 Z"/></symbol>
    <symbol id="i-heart" viewBox="0 0 24 24"><path d="M20.84 4.61 a5.5 5.5 0 0 0 -7.78 0 L12 5.67 l-1.06 -1.06 a5.5 5.5 0 0 0 -7.78 7.78 l1.06 1.06 L12 21.23 l7.78 -7.78 1.06 -1.06 a5.5 5.5 0 0 0 0 -7.78 Z"/></symbol>
    <symbol id="i-dash" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></symbol>
    <symbol id="i-log" viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></symbol>
  </defs>
</svg>

<!-- ── Brand header ── -->
<div class="brand">
  <svg class="logo" viewBox="0 0 64 64" aria-label="Re-Coder"><use href="#i-logo"/></svg>
  <div>
    <div class="name">Re-Coder</div>
    <div class="tag">Remember. Return. Re-Code.</div>
  </div>
</div>

<!-- ── Workbench Sync Banner (Discord ↔ Core ↔ VSCode) ──
     숨김 상태로 시작; /workbench/state 가 응답하면 표시되고, 새 이벤트마다 깜빡임. -->
<div class="sync-banner" id="sync-banner" style="display:none">
  <span class="sync-label">Workbench Sync</span>
  <span class="sync-mode mode-home" id="sync-mode">HOME</span>
  <span class="sync-last" id="sync-last">대기 중…</span>
  <span class="sync-meta" id="sync-meta"></span>
</div>

<!-- ── 탭 ── -->
<div class="tabs">
  <div class="tab active" data-page="command"><svg class="ic"><use href="#i-cmd"/></svg>Command Center</div>
  <div class="tab" data-page="error"><svg class="ic"><use href="#i-err"/></svg>Error Center</div>
  <div class="tab" data-page="github"><svg class="ic"><use href="#i-gh"/></svg>GitHub Hub</div>
  <div class="tab" data-page="deploy"><svg class="ic"><use href="#i-up"/></svg>Deploy Center</div>
  <div class="right-chips">
    <span class="chip" id="chip-core"><span class="dot"></span>Core</span>
    <span class="chip" id="chip-ai"><span class="dot"></span>AI</span>
    <span class="chip" id="chip-docker"><span class="dot"></span>Docker</span>
    <span class="chip" id="chip-github"><span class="dot"></span>GitHub</span>
    <span class="cost">오늘 사용 <b id="cost-today">$0.0000</b></span>
  </div>
</div>

<!-- ── Command Center 페이지 ── -->
<div class="page active" id="page-command">
  <div class="greet">
    <div>
      <h2 id="greet-h">무엇을 도와드릴까요?</h2>
      <p>에러 분석부터 배포·운영 대응까지, 한곳에서 진행하세요.</p>
    </div>
    <div class="cost-large">
      <b id="cost-month">$0.00</b><br>
      <span>/ $3.00 한도</span>
    </div>
  </div>

  <div class="cards">
    <div class="card" data-action="error">
      <span class="badge" id="card-error-badge" style="display:none">1</span>
      <div class="icon red"><svg viewBox="0 0 24 24"><use href="#i-err"/></svg></div>
      <h3 id="card-error-title">에러 감지</h3>
      <p id="card-error-desc">감지된 이슈 없음</p>
      <div class="meta" id="card-error-meta"></div>
      <button class="cta red">에러 센터 열기 →</button>
    </div>
    <div class="card" data-action="github">
      <div class="icon blue"><svg viewBox="0 0 24 24"><use href="#i-gh"/></svg></div>
      <h3>GitHub Hub</h3>
      <p id="card-gh-desc">연결 안 됨</p>
      <div class="meta" id="card-gh-meta"></div>
      <button class="cta blue">GitHub Hub 열기 →</button>
    </div>
    <div class="card" data-action="deploy">
      <div class="icon green"><svg viewBox="0 0 24 24"><use href="#i-up"/></svg></div>
      <h3>배포 센터</h3>
      <p id="card-deploy-desc">배포 현황 없음</p>
      <div class="meta" id="card-deploy-meta"></div>
      <button class="cta green">배포 센터 열기 →</button>
    </div>
  </div>

  <div class="row">
    <div class="panel">
      <h4><svg class="icon-svg" style="color:var(--t2)"><use href="#i-clock"/></svg>최근 활동</h4>
      <div class="act-list" id="act-list">
        <div class="act-item"><div class="act-dot"></div><span style="color:var(--t3)">활동 이력 없음</span></div>
      </div>
    </div>
    <div class="panel">
      <h4><svg class="icon-svg" style="color:var(--yellow)"><use href="#i-bolt"/></svg>빠른 작업</h4>
      <div class="quick-grid">
        <button class="quick-btn" data-q="analyze"><svg class="ic"><use href="#i-search"/></svg>새 에러 분석</button>
        <button class="quick-btn" data-q="dockerfile"><svg class="ic"><use href="#i-pkg"/></svg>Dockerfile 생성</button>
        <button class="quick-btn" data-q="actions"><svg class="ic"><use href="#i-cog"/></svg>GitHub Actions 생성</button>
        <button class="quick-btn" data-q="health"><svg class="ic"><use href="#i-heart"/></svg>헬스 체크</button>
        <button class="quick-btn" data-q="dashboard"><svg class="ic"><use href="#i-dash"/></svg>대시보드</button>
        <button class="quick-btn" data-q="logs"><svg class="ic"><use href="#i-log"/></svg>로그 분리</button>
      </div>
      <label class="quick-toggle">
        <input type="checkbox" id="auto-detect"> 자동 오류 감지 활성화
      </label>
    </div>
  </div>
</div>

<!-- ── Error Center 페이지 ── -->
<div class="page" id="page-error">
  <div class="panel">
    <h4><svg class="icon-svg" style="color:var(--red)"><use href="#i-err"/></svg>Error Center</h4>
    <p style="color:var(--t2);margin-bottom:14px">사이드바의 "Paste Error Log" 텍스트박스에 에러 메시지를 붙여넣고 Analyze Error 버튼을 누르세요.</p>
    <button class="quick-btn" data-q="analyze"><svg class="ic"><use href="#i-search"/></svg>사이드바 열기</button>
  </div>
</div>

<!-- ── GitHub Hub 페이지 ── -->
<div class="page" id="page-github">
  <div class="panel">
    <h4><svg class="icon-svg" style="color:var(--blue)"><use href="#i-gh"/></svg>GitHub Hub</h4>
    <p style="color:var(--t2)">GitHub 통합 기능은 Local Core 의 /api/gh/* 엔드포인트를 통해 동작합니다.</p>
  </div>
</div>

<!-- ── Deploy Center 페이지 ── -->
<div class="page" id="page-deploy">
  <div class="panel">
    <h4><svg class="icon-svg" style="color:var(--green)"><use href="#i-up"/></svg>Deploy Center</h4>
    <p style="color:var(--t2)">Local Docker / EC2 SSH / ECS Fargate 배포 흐름. 사이드바의 Ship 탭에서 진행하세요.</p>
  </div>
</div>

<!-- ── 하단 로그 패널 ── -->
<div class="log-panel">
  <div class="log-tabs">
    <div class="log-tab active" data-log="ai">AI 분석 로그</div>
    <div class="log-tab" data-log="docker">Docker 빌드 로그</div>
    <div class="log-tab" data-log="github">GitHub Actions 로그</div>
    <div class="log-tab" data-log="deploy">배포 로그</div>
    <div class="log-tab" data-log="health">헬스체크 로그</div>
    <button class="log-clear" id="log-clear">Clear</button>
  </div>
  <div class="log-body">
    <div class="log-pane active" id="log-ai"></div>
    <div class="log-pane" id="log-docker"></div>
    <div class="log-pane" id="log-github"></div>
    <div class="log-pane" id="log-deploy"></div>
    <div class="log-pane" id="log-health"></div>
  </div>
</div>

<script nonce="${nonce}">
(function(){
  const vscode = acquireVsCodeApi();
  let currentTab = 'command';
  let currentLog = 'ai';

  function $(id){ return document.getElementById(id); }
  function setChip(id, state){
    const el = $(id); if(!el) return;
    el.className = 'chip' + (state==='ok' ? ' ok' : state==='partial' ? ' warn' : state==='fail' ? ' fail' : '');
  }
  function healthToChip(status){
    if (status === 'ok')       return 'ok';
    if (status === 'degraded') return 'partial';
    if (status === 'down')     return 'fail';
    return '';
  }
  function readyToChip(state){
    if (state === 'ready')   return 'ok';
    if (state === 'partial') return 'partial';
    if (state === 'not_ready' || state === 'error') return 'fail';
    return '';
  }
  function now(){ return new Date().toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit'}); }

  function switchTab(name){
    if (!name) return;
    currentTab = name;
    document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active', x.dataset.page===currentTab));
    document.querySelectorAll('.page').forEach(p=>p.classList.toggle('active', p.id==='page-'+currentTab));
  }
  document.querySelectorAll('.tab').forEach(t=>{
    t.addEventListener('click', ()=>{
      switchTab(t.dataset.page);
      vscode.postMessage({ type:'wb.tab', payload:{ tab: currentTab } });
    });
  });
  document.querySelectorAll('.log-tab').forEach(t=>{
    t.addEventListener('click', ()=>{
      currentLog = t.dataset.log;
      document.querySelectorAll('.log-tab').forEach(x=>x.classList.toggle('active', x.dataset.log===currentLog));
      document.querySelectorAll('.log-pane').forEach(p=>p.classList.toggle('active', p.id==='log-'+currentLog));
    });
  });
  $('log-clear').addEventListener('click', ()=>{
    const el = $('log-'+currentLog); if(el) el.innerHTML='';
  });

  function dispatchAction(name){
    switch(name){
      case 'analyze':    switchTab('error');  vscode.postMessage({ type:'wb.analyze' }); break;
      case 'error':      switchTab('error');  break;
      case 'dockerfile': switchTab('deploy'); vscode.postMessage({ type:'wb.generateDockerfile' }); break;
      case 'github':     switchTab('github'); vscode.postMessage({ type:'wb.tab', payload:{tab:'github'} }); break;
      case 'deploy':     switchTab('deploy'); vscode.postMessage({ type:'wb.tab', payload:{tab:'deploy'} }); break;
      case 'actions':    switchTab('github'); vscode.postMessage({ type:'wb.generateDockerfile' }); break;
      case 'health':     vscode.postMessage({ type:'wb.runDiagnostics' }); break;
      case 'dashboard':  switchTab('command'); break;
      case 'logs':       break;
      default: console.log('unknown action', name);
    }
  }
  document.querySelectorAll('.card').forEach(c=>{
    c.addEventListener('click', ()=> dispatchAction(c.dataset.action));
  });
  document.querySelectorAll('.quick-btn').forEach(b=>{
    b.addEventListener('click', (ev)=>{
      ev.stopPropagation();
      dispatchAction(b.dataset.q);
    });
  });

  window.addEventListener('message', (e)=>{
    const m = e.data || {};
    switch(m.type){
      case 'wb.healthUpdate': {
        const h = m.payload || {};
        setChip('chip-core', healthToChip(h.status));
        break;
      }
      case 'wb.diagnosticsUpdate': {
        const d = m.payload || {};
        setChip('chip-ai', readyToChip(d.ai_ready));
        setChip('chip-docker', readyToChip(d.docker_ready));
        break;
      }
      case 'wb.costUpdate': {
        const c = m.payload || {};
        if (c.daily_usd != null) $('cost-today').textContent = '$' + Number(c.daily_usd).toFixed(4);
        if (c.monthly_usd != null) $('cost-month').textContent = '$' + Number(c.monthly_usd).toFixed(2);
        break;
      }
      case 'wb.activity': {
        const items = (m.payload && m.payload.items) || [];
        const el = $('act-list');
        if (!items.length){
          el.innerHTML = '<div class="act-item"><div class="act-dot"></div><span style="color:var(--t3)">활동 이력 없음</span></div>';
        } else {
          el.innerHTML = items.map(a =>
            '<div class="act-item"><div class="act-dot '+a.dot+'"></div><span>'+a.text+'</span><span class="act-time">'+a.time+'</span></div>'
          ).join('');
        }
        break;
      }
      case 'wb.log': {
        const p = $('log-'+m.payload.pane);
        if (p){
          const line = document.createElement('div');
          line.className = 'log-line';
          line.textContent = '['+now()+'] '+m.payload.line;
          p.appendChild(line);
          p.scrollTop = p.scrollHeight;
        }
        break;
      }
      // ─── Workbench bidirectional sync (Discord ↔ Core ↔ VSCode) ──────
      case 'wb.workbenchState': {
        // 초기 1회 호출. 현재 모드 표시.
        const s = m.payload || {};
        const banner = $('sync-banner');
        const modeChip = $('sync-mode');
        if (banner && modeChip){
          modeChip.textContent = (s.active_mode || 'home').toUpperCase();
          modeChip.className = 'sync-mode mode-' + (s.active_mode || 'home');
          banner.style.display = 'flex';
          $('sync-meta').textContent = '동기화 활성 · ' + (s.deployments_24h ?? 0) + '건 배포 (24h)';
        }
        break;
      }
      case 'wb.workbenchEvent': {
        // 새 이벤트 도착. 배너 깜빡 + 마지막 액션 표시.
        const p = m.payload || {};
        const banner = $('sync-banner');
        if (banner){
          banner.style.display = 'flex';
          const modeChip = $('sync-mode');
          if (modeChip && p.mode){
            modeChip.textContent = String(p.mode).toUpperCase();
            modeChip.className = 'sync-mode mode-' + p.mode;
          }
          $('sync-last').textContent = p.text || '';
          // 깜빡 효과 — 1.5초 outline
          banner.classList.add('flash');
          setTimeout(()=> banner.classList.remove('flash'), 1500);
        }
        break;
      }
    }
  });

  vscode.postMessage({ type:'wb.ready' });
  setInterval(()=> vscode.postMessage({ type:'wb.poll.health' }), 5000);
})();
</script>
</body>
</html>`;
}
