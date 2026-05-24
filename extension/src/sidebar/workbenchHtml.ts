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

/* ── Discord 페이지 ── */
.dc-header{display:flex;align-items:center;gap:12px;margin-bottom:18px}
.dc-icon{width:42px;height:42px;border-radius:10px;background:rgba(88,101,242,.18);color:#5865f2;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.dc-icon svg{width:22px;height:22px;fill:currentColor}
.dc-title{font-size:15px;font-weight:700}
.dc-sub{font-size:11px;color:var(--t2);margin-top:2px}
.dc-card{background:var(--bg1);border:1px solid var(--bd);border-radius:var(--r-lg);padding:16px 18px;margin-bottom:14px}
.dc-card h4{font-size:13px;font-weight:700;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.dc-label{font-size:10px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px;display:block}
.dc-input{width:100%;padding:7px 10px;border-radius:var(--r-sm);border:1px solid var(--bd2);background:var(--bg2);color:var(--t1);font-size:12px;font-family:inherit;outline:none;box-sizing:border-box}
.dc-input:focus{border-color:#5865f2}
.dc-hint{font-size:10px;color:var(--t3);margin-top:3px;line-height:1.5}
.dc-row{display:flex;gap:8px;align-items:flex-start}
.dc-btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:var(--r-sm);border:none;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap}
.dc-btn.invite{background:#5865f2;color:#fff}
.dc-btn.invite:hover{background:#4752c4}
.dc-btn.secondary{background:var(--bg3);color:var(--t1);border:1px solid var(--bd2)}
.dc-btn.secondary:hover{background:var(--bd)}
.dc-btn:disabled{opacity:.45;cursor:not-allowed}
.dc-server-card{display:flex;align-items:center;gap:10px;padding:10px 14px;border-radius:var(--r-md);background:rgba(88,101,242,.07);border:1px solid rgba(88,101,242,.22);margin-bottom:12px}
.dc-server-icon{width:38px;height:38px;border-radius:8px;background:rgba(88,101,242,.2);color:#5865f2;display:flex;align-items:center;justify-content:center;overflow:hidden;flex-shrink:0}
.dc-server-icon img{width:100%;height:100%;object-fit:cover}
.dc-server-name{font-size:13px;font-weight:700}
.dc-server-meta{font-size:10px;color:var(--t2);margin-top:2px}
.dc-badge-ok{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;background:rgba(63,185,80,.12);color:var(--green);font-size:10px;font-weight:600;border:1px solid rgba(63,185,80,.3);margin-left:auto}
.dc-badge-warn{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;background:rgba(210,153,34,.12);color:var(--yellow);font-size:10px;font-weight:600;border:1px solid rgba(210,153,34,.3);margin-left:auto}
.dc-status{display:flex;align-items:center;gap:8px;padding:9px 12px;border-radius:var(--r-md);margin-bottom:12px}
.dc-status.connected{background:rgba(63,185,80,.08);border:1px solid rgba(63,185,80,.25)}
.dc-status.disconnected{background:rgba(107,114,128,.08);border:1px solid rgba(107,114,128,.2)}
.dc-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.dc-dot.on{background:var(--green)}
.dc-dot.off{background:var(--t3)}
.dc-status-text{font-size:12px;font-weight:600}
.dc-status-text.on{color:var(--green)}
.dc-status-text.off{color:var(--t2)}
.dc-usage{padding:10px 14px;border-radius:var(--r-md);background:rgba(88,101,242,.07);border:1px solid rgba(88,101,242,.18);font-size:11px;color:var(--t2);line-height:1.6;margin-top:4px}
.dc-usage code{color:#5865f2;font-family:monospace}
.dc-err{padding:8px 10px;border-radius:var(--r-sm);background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);color:var(--red);font-size:11px;margin-top:6px}
html[data-mode="sidebar"] .dc-card{padding:12px 14px}
html[data-mode="sidebar"] .dc-title{font-size:13px}
html[data-mode="sidebar"] .dc-icon{width:34px;height:34px}

/* Discord 스텝 인디케이터 */
.dc-steps{display:flex;align-items:center;margin-bottom:16px}
.dc-step{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--t3)}
.dc-step-num{width:20px;height:20px;border-radius:50%;border:1.5px solid var(--bd2);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;flex-shrink:0}
.dc-step.active .dc-step-num{background:#5865f2;color:#fff;border-color:#5865f2}
.dc-step.done .dc-step-num{background:var(--green);color:#fff;border-color:var(--green)}
.dc-step.active .dc-step-label{color:var(--t1);font-weight:600}
.dc-step.done .dc-step-label{color:var(--t2)}
.dc-step-line{flex:1;height:1px;background:var(--bd2);margin:0 8px;min-width:18px}

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
    <symbol id="i-discord" viewBox="0 0 24 24"><path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z"/></symbol>
    <symbol id="i-link" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></symbol>
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

<!-- ── 탭 ── -->
<div class="tabs">
  <div class="tab active" data-page="command"><svg class="ic"><use href="#i-cmd"/></svg>Command Center</div>
  <div class="tab" data-page="error"><svg class="ic"><use href="#i-err"/></svg>Error Center</div>
  <div class="tab" data-page="github"><svg class="ic"><use href="#i-gh"/></svg>GitHub Hub</div>
  <div class="tab" data-page="deploy"><svg class="ic"><use href="#i-up"/></svg>Deploy Center</div>
  <div class="tab" data-page="discord"><svg class="ic" style="fill:currentColor;stroke:none"><use href="#i-discord"/></svg>Discord 연동</div>
  <div class="right-chips">
    <span class="chip" id="chip-core"><span class="dot"></span>Core</span>
    <span class="chip" id="chip-ai"><span class="dot"></span>AI</span>
    <span class="chip" id="chip-docker"><span class="dot"></span>Docker</span>
    <span class="chip" id="chip-github"><span class="dot"></span>GitHub</span>
    <span class="chip" id="chip-discord"><span class="dot"></span>Discord</span>
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

<!-- ── Discord 연동 페이지 ── -->
<div class="page" id="page-discord">

  <div class="dc-header">
    <div class="dc-icon">
      <svg style="width:22px;height:22px;fill:currentColor"><use href="#i-discord"/></svg>
    </div>
    <div>
      <div class="dc-title">Discord 연동</div>
      <div class="dc-sub">Re-Coder 봇을 Discord 서버에 추가하고 알림을 설정하세요</div>
    </div>
  </div>

  <!-- 연결 상태 표시 -->
  <div id="dc-status-bar" class="dc-status" style="display:none">
    <div class="dc-dot" id="dc-dot"></div>
    <span class="dc-status-text" id="dc-status-text">연결 안 됨</span>
    <span id="dc-status-server" style="font-size:11px;color:var(--t2);margin-left:4px"></span>
    <button class="dc-btn secondary" id="dc-reset-btn" style="margin-left:auto;padding:4px 10px;font-size:11px">재설정</button>
  </div>

  <!-- 설정 위자드 -->
  <div id="dc-wizard">
    <!-- 스텝 인디케이터 -->
    <div class="dc-steps">
      <div class="dc-step active" id="dcs-1"><span class="dc-step-num">1</span><span class="dc-step-label">서버 선택</span></div>
      <div class="dc-step-line"></div>
      <div class="dc-step" id="dcs-2"><span class="dc-step-num">2</span><span class="dc-step-label">채널 설정</span></div>
      <div class="dc-step-line"></div>
      <div class="dc-step" id="dcs-3"><span class="dc-step-num">3</span><span class="dc-step-label">완료</span></div>
    </div>

    <!-- STEP 1: 서버 선택 -->
    <div id="dc-step-1" class="dc-card">
      <h4><svg class="icon-svg" style="stroke:none;fill:#5865f2;width:14px;height:14px"><use href="#i-discord"/></svg>Discord 서버 연결</h4>
      <label class="dc-label" for="dc-invite-input">서버 초대 링크</label>
      <div class="dc-row" style="margin-bottom:4px">
        <input class="dc-input" id="dc-invite-input" type="text" placeholder="discord.gg/xxxxxx 또는 https://discord.gg/xxxxxx">
        <button class="dc-btn secondary" id="dc-resolve-btn">서버 확인</button>
      </div>
      <div class="dc-hint">서버의 초대 링크를 붙여넣으세요. 만료되지 않은 링크여야 합니다.</div>
      <div class="dc-err" id="dc-resolve-err" style="display:none"></div>

      <!-- 서버 카드 (resolve 성공 시) -->
      <div id="dc-server-card" class="dc-server-card" style="display:none;margin-top:14px">
        <div class="dc-server-icon" id="dc-server-icon">
          <svg style="width:20px;height:20px;fill:#5865f2"><use href="#i-discord"/></svg>
        </div>
        <div style="min-width:0">
          <div class="dc-server-name" id="dc-server-name-text"></div>
          <div class="dc-server-meta" id="dc-server-meta-text"></div>
        </div>
        <span class="dc-badge-ok">확인됨</span>
      </div>

      <!-- 봇 초대 영역 (resolve 성공 시) -->
      <div id="dc-invite-area" style="display:none;margin-top:12px">
        <p style="font-size:11px;color:var(--t2);margin-bottom:8px">봇을 서버에 아직 추가하지 않았다면 아래 버튼으로 초대하세요. 서버 관리자 권한이 필요합니다.</p>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <button class="dc-btn invite" id="dc-open-invite-btn">
            <svg style="width:14px;height:14px;fill:currentColor;stroke:none"><use href="#i-discord"/></svg>봇 서버에 추가
          </button>
          <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--t2);cursor:pointer">
            <input type="checkbox" id="dc-added-check">봇이 이미 서버에 있습니다
          </label>
        </div>
      </div>

      <div style="display:flex;justify-content:flex-end;margin-top:14px">
        <button class="dc-btn invite" id="dc-step1-next" disabled>다음 →</button>
      </div>
    </div>

    <!-- STEP 2: 채널 설정 -->
    <div id="dc-step-2" class="dc-card" style="display:none">
      <h4>채널 설정 <span style="font-size:11px;font-weight:400;color:var(--t2)">(선택 사항)</span></h4>

      <label class="dc-label" for="dc-ch-deploy">배포 알림 채널 ID</label>
      <input class="dc-input" id="dc-ch-deploy" type="text" placeholder="채널 ID (숫자)" style="margin-bottom:10px">

      <label class="dc-label" for="dc-ch-incident">인시던트 알림 채널 ID</label>
      <input class="dc-input" id="dc-ch-incident" type="text" placeholder="채널 ID (숫자)" style="margin-bottom:10px">

      <label class="dc-label" for="dc-ch-standup">데일리 스탠드업 채널 ID</label>
      <input class="dc-input" id="dc-ch-standup" type="text" placeholder="채널 ID (숫자)" style="margin-bottom:10px">

      <label class="dc-label" for="dc-standup-cron">스탠드업 스케줄 (cron)</label>
      <input class="dc-input" id="dc-standup-cron" type="text" placeholder="0 9 * * 1-5  (평일 오전 9시)">
      <div class="dc-hint" style="margin-bottom:14px">채널 ID: 채널 우클릭 → "채널 ID 복사" (개발자 모드 필요) | 나중에 <code>/recoder setup</code>으로도 설정 가능</div>

      <div style="display:flex;gap:8px;justify-content:space-between">
        <button class="dc-btn secondary" id="dc-step2-back">← 이전</button>
        <button class="dc-btn invite" id="dc-step2-next">다음 →</button>
      </div>
    </div>

    <!-- STEP 3: 완료 확인 -->
    <div id="dc-step-3" class="dc-card" style="display:none">
      <h4 style="color:var(--green)">✓ 설정 확인</h4>
      <div id="dc-summary" style="font-size:12px;color:var(--t2);line-height:1.8;margin-bottom:16px"></div>
      <div class="dc-usage">
        Discord에서도 <code>/recoder setup</code> 커맨드로 채널·역할을 추가 설정할 수 있습니다.
      </div>
      <div style="display:flex;gap:8px;justify-content:space-between;margin-top:14px">
        <button class="dc-btn secondary" id="dc-step3-back">← 이전</button>
        <button class="dc-btn invite" id="dc-save-btn">저장</button>
      </div>
    </div>
  </div>

  <!-- 설정 완료 후 커맨드 안내 -->
  <div id="dc-done-area" style="display:none">
    <div class="dc-usage">
      <b style="color:#5865f2;display:block;margin-bottom:6px">Discord 커맨드 안내</b>
      <code>/recoder status</code> — 현재 상태 확인<br>
      <code>/recoder deploy list</code> — 최근 배포 이력<br>
      <code>/recoder setup status</code> — 설정 현황 보기<br>
      <code>/recoder standup</code> — 데일리 스탠드업 시작
    </div>
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
    if (name === 'discord') vscode.postMessage({ type:'wb.discord.loadConfig' });
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

  // ── Discord 연동 상태 ──
  let dcGuildId = null;
  let dcGuildName = '';
  let dcStep = 1;

  function dcSetStep(n){
    dcStep = n;
    for(let i=1;i<=3;i++){
      const sp = $('dc-step-'+i); if(sp) sp.style.display = i===n ? 'block' : 'none';
      const ind = $('dcs-'+i); if(!ind) continue;
      ind.className = 'dc-step' + (i < n ? ' done' : i === n ? ' active' : '');
    }
  }

  function dcSetStatus(connected, guildName){
    const bar = $('dc-status-bar'); if(!bar) return;
    const wiz = $('dc-wizard');
    const done = $('dc-done-area');
    if(connected){
      bar.style.display = 'flex';
      bar.className = 'dc-status connected';
      const dot = $('dc-dot'); if(dot){ dot.className = 'dc-dot on'; }
      const txt = $('dc-status-text'); if(txt){ txt.className = 'dc-status-text on'; txt.textContent = '연결됨'; }
      const srv = $('dc-status-server'); if(srv) srv.textContent = guildName ? '• ' + guildName : '';
      if(wiz) wiz.style.display = 'none';
      if(done) done.style.display = 'block';
      setChip('chip-discord', 'ok');
    } else {
      bar.style.display = 'none';
      if(wiz) wiz.style.display = 'block';
      if(done) done.style.display = 'none';
      setChip('chip-discord', '');
    }
  }

  // Resolve invite
  const dcResolveBtn = $('dc-resolve-btn');
  const dcInviteInput = $('dc-invite-input');
  if(dcResolveBtn && dcInviteInput){
    dcResolveBtn.addEventListener('click', ()=>{
      const val = dcInviteInput.value.trim(); if(!val) return;
      dcResolveBtn.disabled = true; dcResolveBtn.textContent = '확인 중...';
      $('dc-resolve-err').style.display = 'none';
      $('dc-server-card').style.display = 'none';
      $('dc-invite-area').style.display = 'none';
      vscode.postMessage({ type:'wb.discord.resolveInvite', payload:{ url: val } });
    });
    dcInviteInput.addEventListener('keydown', (ev)=>{ if(ev.key==='Enter') dcResolveBtn.click(); });
  }

  // Added check checkbox → enable Next button
  const dcAddedCheck = $('dc-added-check');
  const dcStep1Next = $('dc-step1-next');
  if(dcAddedCheck && dcStep1Next){
    dcAddedCheck.addEventListener('change', ()=>{ dcStep1Next.disabled = !dcAddedCheck.checked; });
  }

  // Open invite button
  const dcOpenInviteBtn = $('dc-open-invite-btn');
  if(dcOpenInviteBtn){
    dcOpenInviteBtn.addEventListener('click', ()=>{
      vscode.postMessage({ type:'wb.discord.openInvite', payload:{ guildId: dcGuildId } });
    });
  }

  // Step 1 → 2
  if(dcStep1Next) dcStep1Next.addEventListener('click', ()=>{ if(dcGuildId) dcSetStep(2); });

  // Step 2 nav
  const dcStep2Back = $('dc-step2-back'); const dcStep2Next = $('dc-step2-next');
  if(dcStep2Back) dcStep2Back.addEventListener('click', ()=> dcSetStep(1));
  if(dcStep2Next) dcStep2Next.addEventListener('click', ()=>{
    const cDeploy   = $('dc-ch-deploy').value.trim();
    const cIncident = $('dc-ch-incident').value.trim();
    const cStandup  = $('dc-ch-standup').value.trim();
    const cron      = $('dc-standup-cron').value.trim() || '0 9 * * 1-5';
    let sum = '<b>서버:</b> ' + (dcGuildName || dcGuildId) + '<br>';
    if(cDeploy)   sum += '<b>배포 채널:</b> ' + cDeploy + '<br>';
    if(cIncident) sum += '<b>인시던트 채널:</b> ' + cIncident + '<br>';
    if(cStandup)  sum += '<b>스탠드업 채널:</b> ' + cStandup + '<br>';
    sum += '<b>스탠드업 스케줄:</b> ' + cron;
    $('dc-summary').innerHTML = sum;
    dcSetStep(3);
  });

  // Step 3 nav
  const dcStep3Back = $('dc-step3-back'); const dcSaveBtn = $('dc-save-btn');
  if(dcStep3Back) dcStep3Back.addEventListener('click', ()=> dcSetStep(2));
  if(dcSaveBtn){
    dcSaveBtn.addEventListener('click', ()=>{
      const config = {
        guildId:           dcGuildId,
        guildName:         dcGuildName,
        deployChannelId:   $('dc-ch-deploy').value.trim()   || '',
        incidentChannelId: $('dc-ch-incident').value.trim() || '',
        standupChannelId:  $('dc-ch-standup').value.trim()  || '',
        standupCron:       $('dc-standup-cron').value.trim() || '0 9 * * 1-5',
      };
      vscode.postMessage({ type:'wb.discord.saveConfig', payload: config });
    });
  }

  // Reset button
  const dcResetBtn = $('dc-reset-btn');
  if(dcResetBtn){
    dcResetBtn.addEventListener('click', ()=>{
      dcGuildId = null; dcGuildName = '';
      if(dcInviteInput) dcInviteInput.value = '';
      if($('dc-server-card')) $('dc-server-card').style.display = 'none';
      if($('dc-invite-area')) $('dc-invite-area').style.display = 'none';
      if(dcAddedCheck) dcAddedCheck.checked = false;
      if(dcStep1Next) dcStep1Next.disabled = true;
      dcSetStep(1);
      dcSetStatus(false, '');
      vscode.postMessage({ type:'wb.discord.saveConfig', payload: null });
    });
  }

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
      case 'wb.discord.inviteResolved': {
        const rb = $('dc-resolve-btn');
        if(rb){ rb.disabled = false; rb.textContent = '서버 확인'; }
        const p = m.payload || {};
        if(p.ok){
          dcGuildId = p.guildId;
          dcGuildName = p.guildName || '';
          const nameEl = $('dc-server-name-text'); if(nameEl) nameEl.textContent = p.guildName || '알 수 없는 서버';
          const metaEl = $('dc-server-meta-text'); if(metaEl) metaEl.textContent = p.memberCount ? p.memberCount.toLocaleString() + '명' : '';
          if(p.guildIcon){
            const iconEl = $('dc-server-icon');
            if(iconEl) iconEl.innerHTML = '<img src="https://cdn.discordapp.com/icons/' + p.guildId + '/' + p.guildIcon + '.png?size=64" alt="">';
          }
          const sc = $('dc-server-card'); if(sc) sc.style.display = 'flex';
          const ia = $('dc-invite-area'); if(ia) ia.style.display = 'block';
          const errEl = $('dc-resolve-err'); if(errEl) errEl.style.display = 'none';
        } else {
          const errEl = $('dc-resolve-err');
          if(errEl){ errEl.textContent = p.error || '초대 링크를 확인할 수 없습니다.'; errEl.style.display = 'block'; }
        }
        break;
      }
      case 'wb.discord.configLoaded': {
        const p = m.payload || {};
        if(p.connected && p.guildId){
          dcGuildId = p.guildId;
          dcGuildName = p.guildName || '';
          dcSetStatus(true, dcGuildName);
          if($('dc-ch-deploy'))    $('dc-ch-deploy').value    = p.deployChannelId   || '';
          if($('dc-ch-incident'))  $('dc-ch-incident').value  = p.incidentChannelId || '';
          if($('dc-ch-standup'))   $('dc-ch-standup').value   = p.standupChannelId  || '';
          if($('dc-standup-cron')) $('dc-standup-cron').value = p.standupCron       || '0 9 * * 1-5';
        } else {
          dcSetStatus(false, '');
        }
        break;
      }
      case 'wb.discord.botStatus': {
        const p = m.payload || {};
        if(p.guildId){
          dcGuildId = p.guildId;
          dcGuildName = p.guildName || '';
          dcSetStatus(true, dcGuildName);
        } else {
          dcSetStatus(false, '');
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
