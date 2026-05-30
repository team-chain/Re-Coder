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
  /* Background hierarchy — Linear/Vercel 영감, 깊이감 강화 */
  --bg0:#0a0d12;  /* 가장 깊은 배경 */
  --bg1:#11151c;  /* 카드 배경 */
  --bg2:#161b25;  /* 입력칸 배경 */
  --bg3:#1d2330;  /* hover/active */
  --bg4:#252c3b;
  /* Border tiers */
  --bd:#1f2530; --bd2:#2a3140; --bd3:#384151;
  /* Text hierarchy */
  --t1:#f0f3f8; --t2:#9aa3b2; --t3:#5e6776; --t4:#3d4456;
  /* Brand accent — refined blue */
  --blue:#5b8eff; --blue-2:#3d6ee5; --blue-bg:rgba(91,142,255,.08); --blue-glow:rgba(91,142,255,.20);
  /* Semantic colors — slightly softer */
  --green:#4ade80; --green-bg:rgba(74,222,128,.08); --green-2:#22c55e;
  --red:#f87171; --red-bg:rgba(248,113,113,.08); --red-2:#ef4444;
  --yellow:#fbbf24; --yellow-bg:rgba(251,191,36,.08); --yellow-2:#f59e0b;
  --purple:#a78bfa; --purple-bg:rgba(167,139,250,.08);
  /* Radius scale */
  --r-xs:3px; --r-sm:6px; --r-md:8px; --r-lg:12px; --r-xl:16px;
  /* Shadow tokens */
  --shadow-sm: 0 1px 2px rgba(0,0,0,.18);
  --shadow-md: 0 4px 12px rgba(0,0,0,.28), 0 1px 3px rgba(0,0,0,.16);
  --shadow-lg: 0 12px 32px rgba(0,0,0,.32), 0 4px 12px rgba(0,0,0,.20);
  --shadow-glow: 0 0 0 1px var(--blue-bg), 0 0 24px var(--blue-glow);
  /* Easing */
  --ease: cubic-bezier(.4,0,.2,1);
  --ease-spring: cubic-bezier(.34,1.56,.64,1);
}
*{box-sizing:border-box; margin:0; padding:0}
body{
  background:var(--bg0);
  color:var(--t1);
  font-family:'Inter',-apple-system,'Apple SD Gothic Neo','Malgun Gothic','Noto Sans KR',BlinkMacSystemFont,'Segoe UI',sans-serif;
  font-size:13px;
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
  letter-spacing:-0.005em;
  padding:24px 28px 28px;
  min-height:100vh;
}
::-webkit-scrollbar{ width:10px; height:10px }
::-webkit-scrollbar-track{ background:transparent }
::-webkit-scrollbar-thumb{ background:var(--bd2); border-radius:5px; border:2px solid var(--bg0) }
::-webkit-scrollbar-thumb:hover{ background:var(--bd3) }
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

/* ── Brand header — 단순/명료 ── */
.brand{display:flex; align-items:center; gap:12px; padding:0 0 24px}
.brand .logo{ width:28px; height:28px; color:var(--blue); flex-shrink:0 }
.brand .name{
  font-size:17px; font-weight:700; color:var(--t1);
  letter-spacing:-0.02em; line-height:1;
}
.brand .tag{
  font-size:11px; font-weight:500; color:var(--t3);
  margin-top:4px;
}
html[data-mode="sidebar"] .brand{padding:2px 0 8px; gap:8px}
html[data-mode="sidebar"] .brand .logo{width:24px; height:24px}
html[data-mode="sidebar"] .brand .name{font-size:14px}
html[data-mode="sidebar"] .brand .tag{font-size:9px}

.tabs{
  display:inline-flex; gap:3px; padding:3px;
  background:var(--bg2); border-radius:var(--r-md);
  margin-bottom:24px; position:relative;
}
.tab{
  display:flex; align-items:center; gap:7px;
  padding:7px 18px; border-radius:var(--r-sm);
  cursor:pointer; color:var(--t2); font-weight:500; font-size:13px;
  border:none; background:transparent;
  transition:color .15s var(--ease), background .15s var(--ease);
  letter-spacing:-0.005em;
}
.tab:hover{color:var(--t1)}
.tab.active{ color:var(--t1); font-weight:600; background:var(--bg1); box-shadow:0 1px 2px rgba(0,0,0,.18) }
.tab .ic{width:14px;height:14px;flex-shrink:0; opacity:.85}
.icon-svg{width:14px;height:14px;flex-shrink:0;stroke-width:1.7;fill:none;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round}
.right-chips{margin-left:auto; display:flex; align-items:center; gap:6px}
.chip{
  display:inline-flex; align-items:center; gap:6px;
  padding:4px 10px; border-radius:999px; border:1px solid var(--bd);
  font-size:11px; font-weight:500; color:var(--t3); background:transparent;
  transition:color .15s var(--ease);
}
.chip .dot{ width:6px; height:6px; border-radius:50%; background:var(--t4) }
.chip.ok{color:var(--green); border-color:rgba(74,222,128,.25)}
.chip.ok .dot{background:var(--green)}
.chip.warn{color:var(--yellow); border-color:rgba(251,191,36,.25)}
.chip.warn .dot{background:var(--yellow)}
.chip.fail{color:var(--red); border-color:rgba(248,113,113,.25)}
.chip.fail .dot{background:var(--red)}
.cost{margin-left:8px; color:var(--t2); font-size:12px}
.cost b{color:var(--t1); font-weight:600}

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

.row{display:grid; grid-template-columns:1.4fr 1fr; gap:16px; margin-bottom:20px}
.panel{
  background:var(--bg1);
  border:1px solid var(--bd);
  border-radius:var(--r-lg);
  padding:20px 22px; margin-bottom:14px;
}
.panel h4{
  font-size:14px; font-weight:600; margin-bottom:14px;
  display:flex; align-items:center; gap:8px;
  letter-spacing:-0.005em;
}
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
.wb-btn .ic{width:13px;height:13px;flex-shrink:0;stroke-width:1.8;fill:none;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round;vertical-align:-1px;margin-right:4px}
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

/* ── 폼 컨트롤 — 단순/일관 ── */
.wb-btn{
  display:inline-flex; align-items:center; justify-content:center; gap:7px;
  padding:8px 14px; border-radius:var(--r-md);
  border:1px solid var(--bd2);
  background:var(--bg2); color:var(--t1);
  font-size:13px; font-weight:500; font-family:inherit;
  cursor:pointer;
  transition:background .12s var(--ease), border-color .12s var(--ease);
  line-height:1;
  letter-spacing:-0.005em;
  white-space:nowrap;
}
.wb-btn:hover{ background:var(--bg3); border-color:var(--bd3) }
.wb-btn:disabled{ opacity:.40; cursor:not-allowed }
.wb-btn:disabled:hover{ background:var(--bg2); border-color:var(--bd2) }
.wb-btn-primary{ background:var(--blue); border-color:var(--blue); color:#fff; font-weight:600 }
.wb-btn-primary:hover{ background:var(--blue-2); border-color:var(--blue-2) }
.wb-btn-danger{ background:transparent; border-color:rgba(248,113,113,.35); color:var(--red) }
.wb-btn-danger:hover{ background:rgba(248,113,113,.08); border-color:var(--red) }
.wb-btn-ghost{ background:transparent; border-color:transparent; color:var(--t2) }
.wb-btn-ghost:hover{ background:var(--bg2); color:var(--t1) }
.wb-btn-sm{ padding:6px 11px; font-size:12px }

.wb-input{
  width:100%; padding:10px 14px; border-radius:var(--r-md);
  border:1px solid var(--bd2); background:var(--bg2); color:var(--t1);
  font-size:13px; font-family:inherit; line-height:1.4;
  transition:all .15s var(--ease);
}
.wb-input:hover{ border-color:var(--bd3) }
.wb-input:focus{
  outline:none; border-color:var(--blue);
  background:var(--bg1);
  box-shadow:0 0 0 3px var(--blue-bg);
}
.wb-input::placeholder{ color:var(--t4) }
.wb-input:disabled{ opacity:.5; cursor:not-allowed }
textarea.wb-input{ line-height:1.55; padding:12px 14px }
select.wb-input{ cursor:pointer; appearance:none;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%239aa3b2' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><polyline points='6 9 12 15 18 9'/></svg>");
  background-repeat:no-repeat; background-position:right 12px center; padding-right:32px;
}
/* 체크박스 → iOS 토글 스위치 (모든 탭 공통) */
input[type="checkbox"]{
  appearance:none; -webkit-appearance:none; margin:0;
  width:34px; height:20px; border-radius:999px; flex-shrink:0;
  background:var(--bd2); position:relative; cursor:pointer; vertical-align:-5px;
  transition:background .15s var(--ease);
}
input[type="checkbox"]::after{
  content:""; position:absolute; top:2px; left:2px;
  width:16px; height:16px; border-radius:50%; background:#fff;
  transition:left .15s var(--ease);
}
input[type="checkbox"]:checked{ background:var(--blue) }
input[type="checkbox"]:checked::after{ left:16px }
input[type="checkbox"]:focus-visible{ outline:none; box-shadow:0 0 0 3px var(--blue-bg) }
/* label 안의 input 위 텍스트 (필드 이름) — 폰트 살짝 키우고 여백 */
.deploy-grid label,
#page-github label{
  font-size:12px; color:var(--t2); display:flex; flex-direction:column; gap:6px;
}

.deploy-tabs{
  display:flex; gap:4px; padding:4px;
  background:var(--bg2); border-radius:var(--r-md);
}
.deploy-tab{
  flex:1; padding:7px 12px; border-radius:var(--r-sm);
  background:transparent; border:none;
  color:var(--t2); font-size:12.5px; font-weight:500; cursor:pointer;
  transition:background .12s var(--ease), color .12s var(--ease);
  white-space:nowrap;
}
.deploy-tab:hover{ color:var(--t1) }
.deploy-tab.active{
  background:var(--bg0);
  color:var(--t1);
  font-weight:600;
}

.deploy-pane{ display:none }
.deploy-pane.active{ display:block }

.deploy-grid{
  /* 1-column 으로 변경: 시원한 라인 + 답답함 해소 */
  display:flex; flex-direction:column; gap:12px;
}
html[data-mode="sidebar"] .deploy-grid{ }

/* ── 폼 그룹 구분선 — 단순 ── */
.subgroup-title{
  font-size:11px; font-weight:600; color:var(--t3);
  text-transform:uppercase; letter-spacing:.06em;
  margin:18px 0 10px;
  padding-bottom:6px; border-bottom:1px solid var(--bd);
}
.subgroup-title:first-child{ margin-top:0 }

/* ── 섹션 제목 ── */
.section-title{
  display:flex; align-items:center; gap:8px;
  font-size:14px; font-weight:600; color:var(--t1); margin-bottom:14px;
  letter-spacing:-0.005em;
}

/* ── GitHub runs list ── */
.gh-run-item{
  display:flex; align-items:center; gap:8px;
  padding:6px 8px; border-bottom:1px solid var(--bd);
}
.gh-run-item:last-child{ border-bottom:none }
.gh-run-item a{ color:var(--blue); text-decoration:none; font-size:11px }
.gh-run-item a:hover{ text-decoration:underline }
.gh-run-status{ font-size:10px; padding:2px 6px; border-radius:999px }
.gh-run-status.success{ background:var(--green-bg); color:var(--green) }
.gh-run-status.failure{ background:var(--red-bg); color:var(--red) }
.gh-run-status.in_progress{ background:var(--blue-bg); color:var(--blue) }
.gh-run-status.queued{ background:var(--yellow-bg); color:var(--yellow) }

/* ── UX 컴포넌트 — 미니멀 v2 ────────────────────────────────────
   원칙: 텍스트 최소화, 1-column, 큰 input/button, 충분한 여백 */
.section-title{
  display:flex; align-items:center; gap:8px;
  font-size:14px; font-weight:700; color:var(--t1); margin-bottom:12px;
}
.section-desc{
  /* 설명 텍스트는 시각적 noise 가 됨. 기본 숨김. */
  display:none;
}

/* ── 사전점검 — chip strip (한 줄, 클릭으로 펼침) ── */
.precheck-strip{
  display:flex; gap:6px; flex-wrap:wrap;
  padding:0 0 16px;
  align-items:center;
  font-size:11.5px;
}
.precheck-pill{
  display:inline-flex; align-items:center; gap:6px;
  padding:4px 10px 4px 8px; border-radius:999px;
  background:var(--bg1); border:1px solid var(--bd);
  color:var(--t2); font-weight:500;
  cursor:default;
}
.precheck-pill.ok   { color:var(--green); border-color:rgba(74,222,128,.25) }
.precheck-pill.warn { color:var(--yellow); border-color:rgba(251,191,36,.25) }
.precheck-pill.fail { color:var(--red); border-color:rgba(248,113,113,.25); cursor:pointer }
.precheck-pill.fail:hover{ background:rgba(248,113,113,.08) }
.precheck-pill .dot{ width:6px; height:6px; border-radius:50%; background:currentColor; opacity:.85 }
/* 호환: 옛 precheck-grid 형식도 동작 (필요 시) */
.precheck-grid{ display:flex; flex-direction:column; gap:0; margin-bottom:14px; background:var(--bg2); border-radius:var(--r-md); overflow:hidden; border:1px solid var(--bd) }
.precheck-item{
  display:flex; align-items:center; gap:12px;
  padding:11px 14px; background:transparent;
  font-size:13px;
  border-bottom:1px solid var(--bd);
  transition:background .15s var(--ease);
}
.precheck-item:last-child{ border-bottom:none }
.precheck-item:hover{ background:var(--bg3) }
.precheck-icon{
  flex-shrink:0; width:22px; height:22px; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  font-size:12px; font-weight:700;
}
.precheck-icon.ok   { background:var(--green); color:#fff }
.precheck-icon.fail { background:var(--red);   color:#fff }
.precheck-icon.warn { background:var(--yellow);color:#fff }
.precheck-icon.pending{ background:var(--bg3); color:var(--t3) }
.precheck-text{ flex:1; min-width:0; display:flex; align-items:center; gap:10px; flex-wrap:wrap }
.precheck-name{ font-weight:500; color:var(--t1) }
.precheck-msg{ color:var(--t3); font-size:12px }
.precheck-action{
  font-size:12px; color:var(--blue); cursor:pointer;
  background:transparent; border:1px solid var(--bd2); padding:4px 10px;
  border-radius:var(--r-sm); margin-left:auto; font-weight:500;
  transition:background .12s var(--ease), border-color .12s var(--ease);
}
.precheck-action:hover{ background:var(--blue-bg); border-color:var(--blue) }

/* ── Progress Stepper — 단순/명확 ── */
.stepper{
  display:flex; align-items:flex-start;
  padding:4px 0 20px; margin-bottom:8px;
  overflow-x:auto;
}
.stepper-item{
  flex:1; display:flex; flex-direction:column; align-items:center; gap:8px;
  position:relative; min-width:80px;
}
.stepper-item::after{
  content:''; position:absolute;
  top:12px; left:calc(50% + 16px); right:calc(-50% + 16px);
  height:1px; background:var(--bd); z-index:0;
  transition:background .2s var(--ease);
}
.stepper-item:last-child::after{ display:none }
.stepper-item.done::after{ background:var(--green) }
.stepper-item.active::after{ background:var(--bd2) }
.stepper-dot{
  position:relative; z-index:1;
  width:24px; height:24px; border-radius:50%;
  background:var(--bg2); border:1px solid var(--bd2);
  display:flex; align-items:center; justify-content:center;
  font-size:11px; font-weight:600; color:var(--t3);
  transition:background .15s var(--ease), border-color .15s var(--ease);
}
.stepper-item.active .stepper-dot{ background:var(--blue); border-color:var(--blue); color:#fff }
.stepper-item.done .stepper-dot{ background:var(--green); border-color:var(--green); color:#fff }
.stepper-item.fail .stepper-dot{ background:var(--red); border-color:var(--red); color:#fff }
.stepper-label{
  font-size:11px; color:var(--t3); text-align:center;
  white-space:nowrap; font-weight:400;
}
.stepper-item.active .stepper-label{ color:var(--t1); font-weight:500 }
.stepper-item.done .stepper-label{ color:var(--t2) }
.stepper-item.fail .stepper-label{ color:var(--red) }

/* 필드 helper text — 미니멀 모드: 기본 숨김. placeholder 로 충분. */
.field-hint{
  display:none;
}
.field-required::after{
  content:' *'; color:var(--red); font-weight:700;
}

/* ── 인라인 알림 — 깔끔한 콜아웃 박스 ── */
.alert{
  display:flex; align-items:center; gap:10px;
  padding:10px 14px; border-radius:var(--r-md); margin:8px 0;
  font-size:12.5px; line-height:1.5;
  background:var(--bg2);
  border:1px solid var(--bd);
  border-left:3px solid var(--bd2);
}
.alert.info { border-color:var(--bd); border-left-color:var(--blue);   background:linear-gradient(90deg, var(--blue-bg), transparent 20%) }
.alert.ok   { border-color:var(--bd); border-left-color:var(--green);  background:linear-gradient(90deg, var(--green-bg), transparent 20%) }
.alert.warn { border-color:var(--bd); border-left-color:var(--yellow); background:linear-gradient(90deg, var(--yellow-bg), transparent 20%) }
.alert.fail { border-color:var(--bd); border-left-color:var(--red);    background:linear-gradient(90deg, var(--red-bg), transparent 20%) }
.alert b{ color:var(--t1); font-weight:700 }
.alert code{ background:var(--bg0); padding:2px 6px; border-radius:var(--r-xs); font-size:11.5px; color:var(--blue) }

/* 폼 그룹 — 라벨 + input + hint 한 묶음 */
.deploy-grid label .field-hint{ margin-top:2px }

/* ── 인라인 로그 — 터미널 스타일 ── */
.inline-log{
  margin-top:14px;
  background:var(--bg0);
  border:1px solid var(--bd);
  border-radius:var(--r-md);
  font-family:'JetBrains Mono','Cascadia Code','Consolas','SF Mono',monospace;
  font-size:11.5px;
  max-height:260px; overflow:hidden;
  box-shadow:inset 0 1px 2px rgba(0,0,0,.18);
}
.inline-log-header{
  display:flex; align-items:center; gap:10px;
  padding:9px 14px; background:var(--bg1);
  border-bottom:1px solid var(--bd);
  font-size:11px; color:var(--t2); font-family:inherit;
  cursor:pointer; user-select:none;
  font-weight:600; letter-spacing:.02em; text-transform:uppercase;
  transition:background .15s var(--ease);
}
.inline-log-header:hover{ background:var(--bg2); color:var(--t1) }
.inline-log-header .chev{ transition:transform .2s var(--ease); font-size:9px; color:var(--t3) }
.inline-log.collapsed .chev{ transform:rotate(-90deg) }
.inline-log.collapsed .inline-log-body{ display:none }
.inline-log-body{
  padding:10px 14px;
  max-height:200px; overflow-y:auto;
  line-height:1.65;
}
.inline-log-line{ color:var(--t1); padding:2px 0 }
.inline-log-line.cmd{ color:var(--blue) }
.inline-log-line.ok { color:var(--green) }
.inline-log-line.err{ color:var(--red) }
.inline-log-line.dim{ color:var(--t3) }
.inline-log-empty{ color:var(--t4); padding:4px 0; font-style:italic }

/* ── 단계별 카드 — 단순/명확 ── */
.step-card{
  background:var(--bg1);
  border:1px solid var(--bd); border-radius:var(--r-lg);
  padding:20px 22px; margin-bottom:12px;
  transition:border-color .15s var(--ease);
}
.step-card.disabled{ opacity:.50; pointer-events:none }
.step-card.done{ border-color:rgba(74,222,128,.25) }
.step-card.active{ border-color:var(--blue) }
.step-card-header{ display:flex; align-items:center; gap:12px; margin-bottom:14px }
.step-num{
  flex-shrink:0; width:26px; height:26px; border-radius:50%;
  background:var(--bg2); border:1px solid var(--bd2);
  display:flex; align-items:center; justify-content:center;
  font-size:12px; font-weight:700; color:var(--t2);
}
.step-card.done .step-num{ background:var(--green); border-color:var(--green); color:#fff }
.step-card.active .step-num{ background:var(--blue); border-color:var(--blue); color:#fff }
.step-title{ font-size:14px; font-weight:600; flex:1; letter-spacing:-0.005em }
.step-desc{ font-size:12px; color:var(--t3); line-height:1.55; margin:-6px 0 16px 38px }
.step-status{
  font-size:11px; padding:3px 9px; border-radius:999px;
  background:var(--bg2); color:var(--t3); font-weight:500;
}
.step-card.done .step-status{ color:var(--green) }
.step-card.active .step-status{ color:var(--blue) }
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
    <symbol id="i-dc" viewBox="0 0 24 24"><path d="M18.93 5.42 a16 16 0 0 0 -4.07 -1.27 l-.2 .36 a14.8 14.8 0 0 1 3.62 1.16 c-3.85 -1.81 -8.4 -1.81 -12.4 0 a14.8 14.8 0 0 1 3.62 -1.16 l-.2 -.36 a16 16 0 0 0 -4.07 1.27 C2.6 9.04 1.9 12.54 2.22 16 a16.1 16.1 0 0 0 4.9 2.48 l.36 -.49 a10.3 10.3 0 0 1 -1.62 -.78 c.13 -.1 .27 -.2 .39 -.31 a11.4 11.4 0 0 0 9.7 0 c .13 .1 .26 .21 .39 .31 a10.3 10.3 0 0 1 -1.62 .78 l .36 .49 a16.1 16.1 0 0 0 4.9 -2.48 c.4 -3.93 -.47 -7.4 -2.05 -10.58 Z M8.68 13.86 c -.97 0 -1.77 -.9 -1.77 -2 0 -1.1 .79 -2 1.77 -2 .98 0 1.78 .9 1.77 2 0 1.1 -.79 2 -1.77 2 Z m6.65 0 c -.97 0 -1.77 -.9 -1.77 -2 0 -1.1 .79 -2 1.77 -2 .98 0 1.78 .9 1.77 2 0 1.1 -.79 2 -1.77 2 Z"/></symbol>
    <symbol id="i-link" viewBox="0 0 24 24"><path d="M10 13 a5 5 0 0 0 7.07 0 l3 -3 a5 5 0 0 0 -7.07 -7.07 l-1.5 1.5"/><path d="M14 11 a5 5 0 0 0 -7.07 0 l-3 3 a5 5 0 0 0 7.07 7.07 l1.5 -1.5"/></symbol>
    <symbol id="i-save" viewBox="0 0 24 24"><path d="M19 21 H5 a2 2 0 0 1 -2 -2 V5 a2 2 0 0 1 2 -2 h11 l5 5 v11 a2 2 0 0 1 -2 2 Z"/><polyline points="17,21 17,13 7,13 7,21"/><polyline points="7,3 7,8 15,8"/></symbol>
  </defs>
</svg>

<!-- Sync Banner / Brand 제거 — 작업에 필요 없는 정보 -->
<div style="display:none" id="sync-banner">
  <span id="sync-mode"></span><span id="sync-last"></span><span id="sync-meta"></span>
</div>

<!-- ── 탭 + 상태 chip 한 줄 ── -->
<div class="tabs">
  <div class="tab" data-page="github"><svg class="ic"><use href="#i-gh"/></svg>GitHub</div>
  <div class="tab active" data-page="deploy"><svg class="ic"><use href="#i-up"/></svg>배포</div>
  <div class="tab" data-page="discord"><svg class="ic"><use href="#i-dc"/></svg>Discord</div>
  <div class="right-chips">
    <span class="chip" id="chip-core"><span class="dot"></span>Core</span>
    <span class="chip" id="chip-ai"><span class="dot"></span>AI</span>
    <span class="chip" id="chip-docker"><span class="dot"></span>Docker</span>
    <span class="chip" id="chip-github"><span class="dot"></span>GitHub</span>
    <span class="cost"><b id="cost-today">$0.0000</b></span>
    <span style="display:none" id="cost-month"></span>
  </div>
</div>

<!-- Command Center 제거. ID 만 남겨 JS 참조 안전 -->
<div class="page" id="page-command" style="display:none">
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

<!-- Error Center 제거 — 사이드바로 위임 -->
<div class="page" id="page-error" style="display:none"></div>

<!-- ── GitHub ── -->
<div class="page" id="page-github">
  <!-- Step 1: 로그인 -->
  <div class="step-card" id="gh-step-1">
    <div class="step-card-header">
      <div class="step-num" id="gh-step-1-num">1</div>
      <div class="step-title">GitHub 로그인</div>
      <span class="step-status" id="gh-step-1-status">대기</span>
    </div>
    <div class="step-desc">GitHub 계정을 연결합니다. 연결하면 저장소를 불러오고 코드를 올릴 수 있습니다.</div>
    <div id="gh-status-text" class="alert info" style="margin:0 0 12px">상태 확인 중…</div>
    <div style="display:flex; gap:8px; flex-wrap:wrap">
      <button class="wb-btn wb-btn-primary" id="gh-login-btn">GitHub 계정 연결</button>
      <button class="wb-btn wb-btn-ghost" id="gh-status-refresh">상태 새로고침</button>
      <button class="wb-btn wb-btn-danger" id="gh-logout-btn">로그아웃</button>
    </div>
  </div>

  <!-- Step 2: 레포 -->
  <div class="step-card" id="gh-step-2">
    <div class="step-card-header">
      <div class="step-num" id="gh-step-2-num">2</div>
      <div class="step-title">저장소 연결</div>
      <span class="step-status" id="gh-step-2-status">대기</span>
    </div>
    <div class="step-desc">코드를 올릴 GitHub 저장소를 선택하거나, 새 저장소를 만듭니다.</div>
    <div style="display:flex; gap:8px; margin-bottom:12px; flex-wrap:wrap">
      <button class="wb-btn" id="gh-list-repos-btn">내 저장소 불러오기</button>
      <select id="gh-repo-select" class="wb-input" style="flex:1; min-width:220px">
        <option value="">저장소를 선택하세요</option>
      </select>
    </div>
    <details>
      <summary style="cursor:pointer; color:var(--t2); font-size:12px; padding:4px 0">새 저장소 만들기</summary>
      <div style="display:grid; grid-template-columns:1fr auto; gap:8px; margin-top:10px">
        <input id="gh-new-name" class="wb-input" placeholder="저장소 이름 (예: my-app)">
        <label style="display:flex; align-items:center; gap:6px; color:var(--t2); font-size:12px; padding:0 6px">
          <input type="checkbox" id="gh-new-private" checked> Private
        </label>
        <input id="gh-new-desc" class="wb-input" style="grid-column:1/-1" placeholder="저장소 설명 (선택 사항)">
        <button class="wb-btn wb-btn-primary" id="gh-create-btn" style="grid-column:1/-1">저장소 만들고 코드 올리기</button>
      </div>
    </details>
    <div class="inline-log collapsed" id="gh-step-2-log">
      <div class="inline-log-header"><span class="chev">▼</span> 로그</div>
      <div class="inline-log-body"><div class="inline-log-empty">결과 없음</div></div>
    </div>
  </div>

  <!-- Step 3: Push -->
  <div class="step-card" id="gh-step-3">
    <div class="step-card-header">
      <div class="step-num" id="gh-step-3-num">3</div>
      <div class="step-title">코드 올리기</div>
      <span class="step-status" id="gh-step-3-status">대기</span>
    </div>
    <div class="step-desc">현재 작업 폴더의 코드를 선택한 저장소로 푸시합니다.</div>
    <div style="display:grid; grid-template-columns:1fr auto auto; gap:8px; align-items:center">
      <input id="gh-push-branch" class="wb-input" placeholder="브랜치 — 비워두면 현재 브랜치">
      <label style="display:flex; align-items:center; gap:6px; color:var(--t2); font-size:12px; white-space:nowrap; padding:0 4px">
        <input type="checkbox" id="gh-push-force"> 강제 푸시
      </label>
      <button class="wb-btn wb-btn-primary" id="gh-push-btn">코드 올리기</button>
    </div>
    <span id="gh-ws-name" style="display:none"></span>
    <div class="inline-log collapsed" id="gh-step-3-log">
      <div class="inline-log-header"><span class="chev">▼</span> 로그</div>
      <div class="inline-log-body"><div class="inline-log-empty">결과 없음</div></div>
    </div>
  </div>

  <!-- Step 4: Secret -->
  <div class="step-card" id="gh-step-4">
    <div class="step-card-header">
      <div class="step-num" id="gh-step-4-num">4</div>
      <div class="step-title">배포 시크릿 등록</div>
      <span class="step-status" id="gh-step-4-status">선택</span>
    </div>
    <div class="step-desc">자동 배포(GitHub Actions)에 쓸 AWS 키 등을 저장소 비밀값으로 안전하게 등록합니다. 값은 가려진 채 저장됩니다.</div>
    <div style="display:flex; gap:4px; flex-wrap:wrap; margin-bottom:10px">
      <button class="wb-btn wb-btn-sm" data-preset="AWS_ACCESS_KEY_ID">AWS_KEY_ID</button>
      <button class="wb-btn wb-btn-sm" data-preset="AWS_SECRET_ACCESS_KEY">AWS_SECRET</button>
      <button class="wb-btn wb-btn-sm" data-preset="ECR_REGISTRY">ECR</button>
      <button class="wb-btn wb-btn-sm" data-preset="EC2_HOST">EC2_HOST</button>
      <button class="wb-btn wb-btn-sm" data-preset="EC2_SSH_KEY">EC2_KEY</button>
      <button class="wb-btn wb-btn-sm" data-preset="ECS_CLUSTER">ECS_CLUSTER</button>
      <button class="wb-btn wb-btn-sm" data-preset="ECS_SERVICE">ECS_SERVICE</button>
    </div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px">
      <input id="gh-secret-repo" class="wb-input" placeholder="소유자/저장소 (예: my-id/my-app)">
      <input id="gh-secret-name" class="wb-input" placeholder="시크릿 이름">
      <input id="gh-secret-value" class="wb-input" type="password" placeholder="시크릿 값 (가려져 저장됨)" style="grid-column:1/-1">
      <button class="wb-btn wb-btn-primary" id="gh-secret-btn" style="grid-column:1/-1">시크릿 등록</button>
    </div>
    <div class="inline-log collapsed" id="gh-step-4-log">
      <div class="inline-log-header"><span class="chev">▼</span> 로그</div>
      <div class="inline-log-body"><div class="inline-log-empty">결과 없음</div></div>
    </div>
  </div>

  <!-- Actions 실행 이력 -->
  <details class="step-card" style="padding:14px 18px">
    <summary style="cursor:pointer; font-weight:600; color:var(--t1); list-style:none; display:flex; align-items:center; gap:10px">
      <svg class="icon-svg" style="color:var(--t2); width:14px; height:14px"><use href="#i-clock"/></svg>
      <span>워크플로 실행 이력</span>
    </summary>
    <div style="display:flex; gap:8px; margin:12px 0 8px">
      <input id="gh-runs-repo" class="wb-input" placeholder="소유자/저장소" style="flex:1">
      <button class="wb-btn" id="gh-runs-btn">불러오기</button>
    </div>
    <div id="gh-runs-list" style="font-size:11px; color:var(--t3)"></div>
  </details>
</div>

<!-- ── Deploy Center ── -->
<div class="page active" id="page-deploy">
  <!-- 사전 점검: chip strip (자동, 클릭 시 펼침) -->
  <div class="precheck-strip" id="deploy-precheck"></div>

  <!-- 배포 방식 -->
  <div class="deploy-tabs">
    <button class="deploy-tab active" data-deploy="local">Local Docker</button>
    <button class="deploy-tab" data-deploy="ec2">EC2</button>
    <button class="deploy-tab" data-deploy="ecs">ECS Fargate</button>
    <button class="deploy-tab" data-deploy="actions">GitHub Actions</button>
  </div>

  <!-- Local Docker -->
  <div class="panel deploy-pane active" id="deploy-local">
    <div class="step-desc" style="margin-top:0">내 컴퓨터의 Docker로 이미지를 빌드·실행해 바로 테스트합니다. AWS 없이 로컬에서 확인할 때 사용하세요.</div>
    <div class="stepper" id="local-stepper">
      <div class="stepper-item" data-step="generate"><div class="stepper-dot">1</div><div class="stepper-label">생성</div></div>
      <div class="stepper-item" data-step="approve"><div class="stepper-dot">2</div><div class="stepper-label">승인</div></div>
      <div class="stepper-item" data-step="scan"><div class="stepper-dot">3</div><div class="stepper-label">스캔</div></div>
      <div class="stepper-item" data-step="build"><div class="stepper-dot">4</div><div class="stepper-label">빌드</div></div>
      <div class="stepper-item" data-step="run"><div class="stepper-dot">5</div><div class="stepper-label">실행</div></div>
      <div class="stepper-item" data-step="health"><div class="stepper-dot">6</div><div class="stepper-label">헬스</div></div>
    </div>

    <div style="display:grid; grid-template-columns:2fr 1fr 1fr; gap:8px; margin-bottom:14px">
      <input id="local-image" class="wb-input" value="recoder-app" placeholder="이미지 이름">
      <input id="local-host-port" class="wb-input" type="number" value="8000" placeholder="호스트 포트">
      <input id="local-container-port" class="wb-input" type="number" value="8000" placeholder="컨테이너 포트">
    </div>

    <textarea id="local-dockerfile-preview" class="wb-input" rows="9"
      style="font-family:'JetBrains Mono','Cascadia Code','Consolas',monospace; font-size:11.5px; resize:vertical; min-height:140px"
      placeholder="Dockerfile (생성 후 표시)"></textarea>

    <div id="local-scan-result" style="display:none; gap:10px; flex-wrap:wrap; padding:10px 0 0"></div>

    <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:14px">
      <button class="wb-btn wb-btn-primary" id="local-btn-generate">Dockerfile 생성</button>
      <button class="wb-btn" id="local-btn-approve" disabled>승인</button>
      <button class="wb-btn" id="local-btn-scan" disabled>스캔</button>
      <button class="wb-btn wb-btn-primary" id="local-btn-deploy" disabled>배포 실행</button>
      <span style="flex:1"></span>
      <button class="wb-btn wb-btn-ghost" id="local-btn-reset">초기화</button>
    </div>

    <div class="inline-log collapsed" id="local-inline-log">
      <div class="inline-log-header"><span class="chev">▼</span> 로그</div>
      <div class="inline-log-body"><div class="inline-log-empty">결과 없음</div></div>
    </div>
  </div>

  <!-- EC2 -->
  <div class="panel deploy-pane" id="deploy-ec2">
    <div class="step-desc" style="margin-top:0">이미지를 ECR에 올리고 EC2 서버에 SSH로 접속해 배포합니다. 단일 서버 운영에 적합합니다.</div>
    <div class="stepper" id="ec2-stepper">
      <div class="stepper-item" data-step="building"><div class="stepper-dot">1</div><div class="stepper-label">빌드</div></div>
      <div class="stepper-item" data-step="ecr_login"><div class="stepper-dot">2</div><div class="stepper-label">ECR 로그인</div></div>
      <div class="stepper-item" data-step="ecr_push"><div class="stepper-dot">3</div><div class="stepper-label">push</div></div>
      <div class="stepper-item" data-step="ec2_deploy"><div class="stepper-dot">4</div><div class="stepper-label">SSH</div></div>
      <div class="stepper-item" data-step="done"><div class="stepper-dot">5</div><div class="stepper-label">완료</div></div>
    </div>

    <div style="display:grid; grid-template-columns:2fr 1fr 1fr 1fr; gap:8px; margin-bottom:8px">
      <input id="ec2-image-name" class="wb-input" value="recoder-app" placeholder="이미지">
      <input id="ec2-tag" class="wb-input" value="latest" placeholder="태그">
      <input id="ec2-host-port" class="wb-input" type="number" value="8000" placeholder="호스트 포트">
      <input id="ec2-container-port" class="wb-input" type="number" value="8000" placeholder="컨테이너 포트">
    </div>
    <input id="ec2-health-path" class="wb-input" value="/health" placeholder="헬스체크 경로" style="margin-bottom:8px">

    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:8px">
      <input id="ec2-region" class="wb-input" placeholder="AWS Region (env: AWS_REGION)">
      <input id="ec2-ecr" class="wb-input" placeholder="ECR Registry (env: ECR_REGISTRY)">
      <input id="ec2-host" class="wb-input" placeholder="EC2 Host (env: EC2_HOST)">
      <input id="ec2-ssh-key" class="wb-input" placeholder="SSH 키 (env: EC2_SSH_KEY)">
      <input id="ec2-user" class="wb-input" value="ec2-user" placeholder="EC2 사용자">
    </div>

    <div style="display:flex; gap:6px; margin-top:6px; align-items:center">
      <button class="wb-btn wb-btn-primary" id="ec2-deploy-btn">배포 실행</button>
      <button class="wb-btn wb-btn-ghost" id="ec2-ready-btn" style="display:none">점검</button>
      <span style="flex:1"></span>
      <span id="ec2-status-line" style="font-size:11px; color:var(--t2)"></span>
    </div>

    <div class="inline-log collapsed" id="ec2-inline-log">
      <div class="inline-log-header"><span class="chev">▼</span> 로그</div>
      <div class="inline-log-body"><div class="inline-log-empty">결과 없음</div></div>
    </div>
  </div>

  <!-- ECS -->
  <div class="panel deploy-pane" id="deploy-ecs">
    <div class="step-desc" style="margin-top:0">ECS Fargate에 무중단(롤링) 방식으로 배포합니다. 서버 관리 없이 컨테이너를 운영할 때 사용하세요.</div>
    <div class="stepper" id="ecs-stepper">
      <div class="stepper-item" data-step="building"><div class="stepper-dot">1</div><div class="stepper-label">빌드</div></div>
      <div class="stepper-item" data-step="ecr_push"><div class="stepper-dot">2</div><div class="stepper-label">push</div></div>
      <div class="stepper-item" data-step="task_def"><div class="stepper-dot">3</div><div class="stepper-label">Task Def</div></div>
      <div class="stepper-item" data-step="svc_update"><div class="stepper-dot">4</div><div class="stepper-label">서비스</div></div>
      <div class="stepper-item" data-step="deploying"><div class="stepper-dot">5</div><div class="stepper-label">Rolling</div></div>
      <div class="stepper-item" data-step="done"><div class="stepper-dot">6</div><div class="stepper-label">완료</div></div>
    </div>

    <div style="display:grid; grid-template-columns:2fr 1fr; gap:8px; margin-bottom:8px">
      <input id="ecs-image-name" class="wb-input" value="recoder-app" placeholder="이미지">
      <input id="ecs-tag" class="wb-input" value="latest" placeholder="태그">
    </div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:8px">
      <input id="ecs-region" class="wb-input" placeholder="AWS Region (env: AWS_REGION)">
      <input id="ecs-ecr" class="wb-input" placeholder="ECR Registry (env: ECR_REGISTRY)">
      <input id="ecs-cluster" class="wb-input" placeholder="ECS Cluster (env: ECS_CLUSTER)">
      <input id="ecs-service" class="wb-input" placeholder="ECS Service (env: ECS_SERVICE)">
    </div>
    <div style="display:grid; grid-template-columns:2fr 1fr 1fr 1fr; gap:8px; margin-bottom:8px">
      <input id="ecs-task-family" class="wb-input" value="recoder-task" placeholder="Task Family">
      <input id="ecs-container-port" class="wb-input" type="number" value="8000" placeholder="포트">
      <input id="ecs-cpu" class="wb-input" value="256" placeholder="CPU">
      <input id="ecs-memory" class="wb-input" value="512" placeholder="Memory">
    </div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px">
      <select id="ecs-env" class="wb-input">
        <option value="staging">staging</option>
        <option value="production">production</option>
      </select>
      <input id="ecs-branch" class="wb-input" placeholder="브랜치 (production용)">
    </div>

    <div style="display:flex; gap:12px; margin-top:14px; align-items:center; flex-wrap:wrap">
      <label style="color:var(--t3); font-size:11px; display:flex; align-items:center; gap:5px">
        <input type="checkbox" id="ecs-skip-sbom"> SBOM 생성 생략
      </label>
      <label style="color:var(--t3); font-size:11px; display:flex; align-items:center; gap:5px">
        <input type="checkbox" id="ecs-skip-opa"> 정책 검사(OPA) 생략
      </label>
      <span style="flex:1"></span>
      <button class="wb-btn wb-btn-primary" id="ecs-deploy-btn">배포 실행</button>
      <button class="wb-btn wb-btn-ghost" id="ecs-ready-btn" style="display:none">점검</button>
    </div>
    <div id="ecs-status-line" style="margin-top:10px; font-size:11px; color:var(--t2)"></div>

    <div class="inline-log collapsed" id="ecs-inline-log">
      <div class="inline-log-header"><span class="chev">▼</span> 로그</div>
      <div class="inline-log-body"><div class="inline-log-empty">결과 없음</div></div>
    </div>
  </div>

  <!-- GitHub Actions -->
  <div class="panel deploy-pane" id="deploy-actions">
    <div class="step-desc" style="margin-top:0">배포 워크플로(YAML)를 만들어 저장소에 넣습니다. 이후 코드를 푸시하면 GitHub가 자동으로 배포합니다.</div>
    <div class="stepper" id="actions-stepper">
      <div class="stepper-item" data-step="generate"><div class="stepper-dot">1</div><div class="stepper-label">생성</div></div>
      <div class="stepper-item" data-step="approve"><div class="stepper-dot">2</div><div class="stepper-label">저장</div></div>
      <div class="stepper-item" data-step="secret"><div class="stepper-dot">3</div><div class="stepper-label">Secret</div></div>
      <div class="stepper-item" data-step="push"><div class="stepper-dot">4</div><div class="stepper-label">push</div></div>
      <div class="stepper-item" data-step="run"><div class="stepper-dot">5</div><div class="stepper-label">실행</div></div>
    </div>

    <textarea id="actions-yaml-preview" class="wb-input" rows="12"
      style="font-family:'JetBrains Mono','Cascadia Code','Consolas',monospace; font-size:11.5px; resize:vertical; min-height:180px"
      placeholder="워크플로 YAML (생성 후 표시)"></textarea>

    <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:14px">
      <button class="wb-btn wb-btn-primary" id="actions-generate-btn">워크플로 생성</button>
      <button class="wb-btn" id="actions-approve-btn" disabled>저장</button>
      <button class="wb-btn" id="actions-push-btn" disabled>푸시</button>
      <span style="flex:1"></span>
      <button class="wb-btn wb-btn-ghost" id="actions-reset-btn">초기화</button>
    </div>

    <div class="inline-log collapsed" id="actions-inline-log">
      <div class="inline-log-header"><span class="chev">▼</span> 로그</div>
      <div class="inline-log-body"><div class="inline-log-empty">결과 없음</div></div>
    </div>
  </div>
</div>

<!-- ── Discord Bridge ── -->
<div class="page" id="page-discord">

  <!-- Step 1: 봇 상태 -->
  <div class="step-card" id="dc-step-1">
    <div class="step-card-header">
      <div class="step-num" id="dc-step-1-num">1</div>
      <div class="step-title">봇 상태</div>
      <span class="step-status" id="dc-step-1-status">대기</span>
    </div>
    <div class="step-desc">Discord 봇이 켜져 있고 내 VSCode와 연결됐는지 확인합니다.</div>
    <div id="dc-status-text" class="alert info" style="margin:0 0 12px">봇 HTTP API 연결 확인 중…</div>
    <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center">
      <span style="display:inline-flex; align-items:center; gap:8px">
        <img id="dc-bot-avatar" alt="" style="width:24px; height:24px; border-radius:50%; display:none; object-fit:cover; border:1px solid var(--bd2)">
        <span id="dc-bot-name" style="font-size:12px; color:var(--t2)">봇이 감지되지 않음</span>
      </span>
      <span class="cost" style="margin-left:auto"><b id="dc-bridge-clients">0</b>&nbsp;<span style="color:var(--t3); font-weight:400">VSCode 브리지 연결</span></span>
    </div>
    <div style="display:flex; gap:8px; margin-top:12px; flex-wrap:wrap">
      <button class="wb-btn wb-btn-ghost" id="dc-refresh-btn">상태 새로고침</button>
    </div>
    <div style="margin-top:10px; font-size:11px; color:var(--t3); line-height:1.5">
      봇 HTTP API: <code id="dc-http-endpoint" style="color:var(--t2)">http://127.0.0.1:8765</code>
      <span style="margin-left:8px">— recoder.bridge.httpPort 설정으로 변경 가능</span>
    </div>
  </div>

  <!-- Step 2: 봇 초대 (Discord 서버에 추가) -->
  <div class="step-card" id="dc-step-2">
    <div class="step-card-header">
      <div class="step-num" id="dc-step-2-num">2</div>
      <div class="step-title">봇 초대</div>
      <span class="step-status" id="dc-step-2-status">선택</span>
    </div>
    <div class="step-desc">내 Discord 서버에 봇을 추가합니다. 추가해야 채널을 선택하고 명령을 쓸 수 있습니다.</div>
    <p style="margin:0 0 12px; color:var(--t2); font-size:12px; line-height:1.5">
      Discord 서버에 봇을 추가해야 채널 선택이 가능합니다. 아래 버튼을 누르면 OAuth 초대 페이지가 브라우저에서 열립니다.
    </p>
    <div style="display:flex; gap:8px; flex-wrap:wrap">
      <button class="wb-btn wb-btn-primary" id="dc-invite-btn">
        <svg class="ic"><use href="#i-link"/></svg> 서버에 봇 초대
      </button>
      <button class="wb-btn wb-btn-ghost" id="dc-copy-invite-btn">URL 복사</button>
    </div>
    <div style="margin-top:10px; font-size:11px; color:var(--t3); word-break:break-all">
      <span style="color:var(--t2)">초대 URL:</span> <span id="dc-invite-url" style="font-family:var(--vscode-editor-font-family, monospace); color:var(--t3)">—</span>
    </div>
  </div>

  <!-- Step 3: 서버 + 채널 선택 -->
  <div class="step-card" id="dc-step-3">
    <div class="step-card-header">
      <div class="step-num" id="dc-step-3-num">3</div>
      <div class="step-title">코드 생성 채널</div>
      <span class="step-status" id="dc-step-3-status">대기</span>
    </div>
    <p style="margin:0 0 12px; color:var(--t2); font-size:12px; line-height:1.5">
      봇이 자유 대화에 응답하고 코드를 생성할 채널을 지정합니다. 슬래시 명령(<code>/recoder</code> 등)은 모든 채널에서 동작하지만, <b>자연어 → 코드 변환</b>은 여기서 선택한 채널에서만 일어납니다.
    </p>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:12px">
      <label style="display:flex; flex-direction:column; gap:4px">
        <span style="font-size:11px; color:var(--t3); font-weight:600; text-transform:uppercase; letter-spacing:0.04em">서버</span>
        <select id="dc-guild-select" class="wb-input">
          <option value="">— 봇을 초대한 서버 선택 —</option>
        </select>
      </label>
      <label style="display:flex; flex-direction:column; gap:4px">
        <span style="font-size:11px; color:var(--t3); font-weight:600; text-transform:uppercase; letter-spacing:0.04em">채널</span>
        <select id="dc-channel-select" class="wb-input" disabled>
          <option value="">— 먼저 서버 선택 —</option>
        </select>
      </label>
    </div>
    <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center">
      <button class="wb-btn wb-btn-primary" id="dc-save-channel-btn" disabled>
        <svg class="ic"><use href="#i-save"/></svg> 저장
      </button>
      <button class="wb-btn wb-btn-danger" id="dc-clear-channel-btn">해제</button>
      <span id="dc-save-hint" style="font-size:11px; color:var(--t3); margin-left:auto">채널을 고르면 활성화됩니다</span>
    </div>
    <details style="margin-top:14px">
      <summary style="cursor:pointer; color:var(--t2); font-size:12px; padding:4px 0">수동 입력 (채널 ID 직접)</summary>
      <div style="display:grid; grid-template-columns:1fr auto; gap:8px; margin-top:10px">
        <input id="dc-manual-channel" class="wb-input" placeholder="채널 우클릭 → ID 복사 (예: 1234567890123456789)">
        <button class="wb-btn" id="dc-manual-save-btn">저장</button>
      </div>
    </details>
    <div class="inline-log collapsed" id="dc-step-3-log">
      <div class="inline-log-header"><span class="chev">▼</span> 로그</div>
      <div class="inline-log-body"><div class="inline-log-empty">결과 없음</div></div>
    </div>
  </div>

  <!-- Step 4: 현재 설정 -->
  <div class="step-card" id="dc-step-4">
    <div class="step-card-header">
      <div class="step-num" id="dc-step-4-num">4</div>
      <div class="step-title">현재 활성 채널</div>
      <span class="step-status" id="dc-step-4-status">없음</span>
    </div>
    <div id="dc-current-channel" style="font-size:13px; color:var(--t2); line-height:1.7">
      활성 채널이 설정되지 않았습니다. 위에서 채널을 저장하세요.
    </div>
    <details style="margin-top:14px">
      <summary style="cursor:pointer; color:var(--t2); font-size:12px; padding:4px 0">고급 — 모든 봇 설정 보기</summary>
      <pre id="dc-settings-snapshot" style="margin-top:10px; padding:10px; background:var(--bg2); border:1px solid var(--bd); border-radius:4px; font-size:11px; color:var(--t3); overflow-x:auto; max-height:200px">—</pre>
    </details>
  </div>

</div>

<!-- 하단 풀 로그 패널 제거됨 — 각 탭의 인라인 로그가 그 역할을 함. 시각 중복 정리. -->
<div style="display:none">
  <div id="log-ai"></div><div id="log-docker"></div><div id="log-github"></div>
  <div id="log-deploy"></div><div id="log-health"></div>
  <button id="log-clear"></button>
</div>

<script nonce="${nonce}">
(function(){
  const vscode = acquireVsCodeApi();
  let currentTab = 'deploy';
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
      case 'actions':    switchTab('github'); vscode.postMessage({ type:'wb.generateGithubActions' }); break;
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

  // ─── UX 헬퍼 ──────────────────────────────────────────────────
  function appendInlineLog(logId, line, klass){
    const wrap = $(logId);
    if (!wrap) return;
    const body = wrap.querySelector('.inline-log-body');
    if (!body) return;
    const empty = body.querySelector('.inline-log-empty');
    if (empty) empty.remove();
    const el = document.createElement('div');
    el.className = 'inline-log-line ' + (klass || '');
    el.textContent = '[' + now() + '] ' + line;
    body.appendChild(el);
    body.scrollTop = body.scrollHeight;
    wrap.classList.remove('collapsed');
  }
  function setStepCard(stepNum, status){
    const card = $('gh-step-' + stepNum);
    if (!card) return;
    card.classList.remove('disabled','active','done');
    if (status === 'done') card.classList.add('done');
    else if (status === 'active') card.classList.add('active');
    else if (status === 'disabled') card.classList.add('disabled');
    const badge = $('gh-step-' + stepNum + '-status');
    if (badge){
      badge.textContent = status === 'done' ? '완료 ✓'
        : status === 'active' ? '진행 중'
        : status === 'disabled' ? '잠금'
        : (stepNum === 4 ? '선택' : '대기');
    }
    const numEl = $('gh-step-' + stepNum + '-num');
    if (numEl) numEl.textContent = status === 'done' ? '✓' : String(stepNum);
  }
  function updateStepper(stepperId, currentStage, finalStage){
    const stepper = $(stepperId);
    if (!stepper) return;
    const items = stepper.querySelectorAll('.stepper-item');
    let passed = true;
    items.forEach(item => {
      const step = item.dataset.step;
      item.classList.remove('active','done','fail');
      if (step === currentStage){
        if (finalStage === 'failed') item.classList.add('fail');
        else if (finalStage === 'done') item.classList.add('done');
        else item.classList.add('active');
        passed = false;
      } else if (passed){
        item.classList.add('done');
      }
    });
  }

  // 인라인 로그 collapse 토글
  document.querySelectorAll('.inline-log-header').forEach(h => {
    h.addEventListener('click', () => {
      const wrap = h.closest('.inline-log');
      if (wrap) wrap.classList.toggle('collapsed');
    });
  });

  // Secret 프리셋 클릭
  document.querySelectorAll('[data-preset]').forEach(b => {
    b.addEventListener('click', () => {
      if ($('gh-secret-name')) $('gh-secret-name').value = b.dataset.preset;
      if ($('gh-secret-value')) $('gh-secret-value').focus();
    });
  });

  window.addEventListener('message', (e)=>{
    const m = e.data || {};
    switch(m.type){
      case 'wb.healthUpdate': { setChip('chip-core', healthToChip((m.payload||{}).status)); break; }
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
      case 'wb.workbenchState': {
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
          banner.classList.add('flash');
          setTimeout(()=> banner.classList.remove('flash'), 1500);
        }
        break;
      }
      case 'wb.gh.statusResult': {
        const s = m.payload || {};
        const el = $('gh-status-text');
        const connected = (s.status === 'connected' || s.user);
        if (el){
          el.className = 'alert ' + (connected ? 'ok' : 'info');
          el.innerHTML = connected
            ? '<span><b>✓ 연결됨</b> — ' + (s.user || '') + (s.message ? ' · ' + s.message : '') + '</span>'
            : '<span>아직 로그인하지 않았습니다. 위 "VS Code 로 로그인" 버튼을 누르세요.' + (s.message ? ' — ' + s.message : '') + '</span>';
        }
        setStepCard(1, connected ? 'done' : 'active');
        setStepCard(2, connected ? 'active' : 'disabled');
        setStepCard(3, connected ? 'active' : 'disabled');
        setStepCard(4, connected ? 'active' : 'disabled');
        setChip('chip-github', connected ? 'ok' : '');
        break;
      }
      case 'wb.gh.reposResult': {
        const sel = $('gh-repo-select');
        if (sel){
          const repos = (m.payload && m.payload.repos) || [];
          sel.innerHTML = '<option value="">— 레포 선택 —</option>' +
            repos.map(r => '<option value="' + r.name + '">' + (r.private ? '🔒 ' : '') + r.name + '</option>').join('');
          sel.onchange = () => {
            const v = sel.value || '';
            ['gh-secret-repo','gh-runs-repo'].forEach(id => { if ($(id) && !$(id).value) $(id).value = v; });
            if (v) setStepCard(2, 'done');
          };
          if (repos.length) setStepCard(2, 'active');
        }
        break;
      }
      case 'wb.gh.createRepoResult': {
        const p = m.payload || {};
        if (p.ok){ appendInlineLog('gh-step-2-log', '✓ ' + (p.message || ('레포 생성: ' + p.url)), 'ok'); setStepCard(2, 'done'); }
        else { appendInlineLog('gh-step-2-log', '✗ ' + (p.error || '실패'), 'err'); }
        break;
      }
      case 'wb.gh.pushResult': {
        const p = m.payload || {};
        if (p.ok){ appendInlineLog('gh-step-3-log', '✓ ' + (p.message || 'push 완료'), 'ok'); setStepCard(3, 'done'); }
        else { appendInlineLog('gh-step-3-log', '✗ ' + (p.error || 'push 실패'), 'err'); }
        break;
      }
      case 'wb.gh.secretResult': {
        const p = m.payload || {};
        if (p.ok){ appendInlineLog('gh-step-4-log', '✓ ' + (p.message || ('Secret 등록: ' + p.name)), 'ok'); setStepCard(4, 'done'); }
        else { appendInlineLog('gh-step-4-log', '✗ ' + (p.error || 'Secret 등록 실패'), 'err'); }
        break;
      }
      case 'wb.gh.workspaceInfo': {
        const p = m.payload || {};
        if ($('gh-ws-name') && p.workspace) $('gh-ws-name').textContent = p.workspace;
        break;
      }
      case 'wb.gh.runsResult': {
        const el = $('gh-runs-list');
        if (el){
          const runs = (m.payload && m.payload.runs) || [];
          if (!runs.length){
            el.innerHTML = '<div style="color:var(--t3); padding:8px">실행 이력 없음</div>';
          } else {
            el.innerHTML = runs.slice(0, 20).map(r => {
              const status = r.conclusion || r.status || 'queued';
              return '<div class="gh-run-item"><span class="gh-run-status ' + status + '">' + status + '</span>'
                + '<a href="' + r.html_url + '" target="_blank">' + r.name + ' (#' + r.id + ')</a></div>';
            }).join('');
          }
        }
        break;
      }
      case 'wb.deploy.ec2.statusResult': {
        const s = m.payload || {};
        const el = $('ec2-status-line');
        if (el){
          const tone = s.stage === 'failed' ? 'color:var(--red)' : s.stage === 'done' ? 'color:var(--green)' : 'color:var(--blue)';
          el.innerHTML = '<span style="' + tone + '">' + s.stage + '</span>'
            + ' · running=' + s.running
            + (s.image_uri ? ' · image=' + s.image_uri : '')
            + (s.error ? ' · error=' + s.error : '');
        }
        const finalStage = s.stage === 'done' ? 'done' : s.stage === 'failed' ? 'failed' : '';
        updateStepper('ec2-stepper', s.stage, finalStage);
        const tail = (s.log_tail || []).slice(-3);
        tail.forEach(line => appendInlineLog('ec2-inline-log', line, s.stage === 'failed' ? 'err' : ''));
        break;
      }
      case 'wb.deploy.ecs.statusResult': {
        const s = m.payload || {};
        const el = $('ecs-status-line');
        if (el){
          const tone = s.stage === 'failed' ? 'color:var(--red)' : s.stage === 'done' ? 'color:var(--green)' : 'color:var(--blue)';
          el.innerHTML = '<span style="' + tone + '">' + s.stage + '</span>'
            + ' · running=' + s.running
            + (s.task_def_arn ? ' · task=' + s.task_def_arn : '')
            + (s.error ? ' · error=' + s.error : '');
        }
        const finalStage = s.stage === 'done' ? 'done' : s.stage === 'failed' ? 'failed' : '';
        updateStepper('ecs-stepper', s.stage, finalStage);
        const tail = (s.log_tail || []).slice(-3);
        tail.forEach(line => appendInlineLog('ecs-inline-log', line, s.stage === 'failed' ? 'err' : ''));
        break;
      }
      case 'wb.deploy.actionsResult': {
        const p = m.payload || {};
        if (p.ok) appendInlineLog('actions-inline-log', '✓ ' + (p.message || '워크플로 YAML 생성 완료'), 'ok');
        else appendInlineLog('actions-inline-log', '✗ ' + (p.error || '워크플로 생성 실패'), 'err');
        break;
      }
      case 'wb.deploy.precheckResult': {
        renderPrechecks(m.payload && m.payload.items);
        break;
      }
      // ── Local Docker wizard results ──────────────────────────
      case 'wb.local.generateResult': {
        const p = m.payload || {};
        if (p.ok){
          localProposalId = p.proposal_id || '';
          if ($('local-dockerfile-preview')) $('local-dockerfile-preview').value = p.content || '';
          appendInlineLog('local-inline-log', '✓ Dockerfile 생성 완료', 'ok');
          updateStepper('local-stepper', 'approve', '');
          setLocalBtns({ generate:true, approve:true, scan:false, deploy:false });
        } else {
          appendInlineLog('local-inline-log', '✗ ' + (p.error || '생성 실패'), 'err');
        }
        break;
      }
      case 'wb.local.approveResult': {
        const p = m.payload || {};
        if (p.ok){
          appendInlineLog('local-inline-log', '✓ Dockerfile 저장됨: ' + (p.path || ''), 'ok');
          updateStepper('local-stepper', 'scan', '');
          setLocalBtns({ generate:true, approve:false, scan:true, deploy:false });
        } else {
          appendInlineLog('local-inline-log', '✗ ' + (p.error || '승인 실패'), 'err');
        }
        break;
      }
      case 'wb.local.scanResult': {
        const p = m.payload || {};
        if (p.ok){
          const r = p.result || {};
          const c = Number(r.critical_count || 0);
          const h = Number(r.high_count || 0);
          const med = Number(r.medium_count || 0);
          const el = $('local-scan-result');
          if (el){
            el.innerHTML =
              '<div style="display:flex; gap:10px; flex-wrap:wrap">' +
              '<span class="precheck-icon ' + (c>0?'fail':'ok') + '">' + c + '</span><span style="font-size:12px; color:var(--t2)">Critical</span>' +
              '<span class="precheck-icon ' + (h>0?'warn':'ok') + '">' + h + '</span><span style="font-size:12px; color:var(--t2)">High</span>' +
              '<span class="precheck-icon ' + (med>0?'warn':'ok') + '">' + med + '</span><span style="font-size:12px; color:var(--t2)">Medium</span>' +
              '</div>';
          }
          appendInlineLog('local-inline-log', '✓ 스캔 완료 (critical=' + c + ', high=' + h + ')', c>0 || h>0 ? 'err' : 'ok');
          updateStepper('local-stepper', 'build', '');
          setLocalBtns({ generate:true, approve:false, scan:false, deploy:true });
        } else {
          appendInlineLog('local-inline-log', '✗ 스캔 실패: ' + (p.error || ''), 'err');
        }
        break;
      }
      case 'wb.local.deployProgress': {
        const p = m.payload || {};
        if (p.stage) updateStepper('local-stepper', p.stage, p.finished ? (p.error ? 'failed' : 'done') : '');
        if (p.line) appendInlineLog('local-inline-log', p.line, p.error ? 'err' : '');
        if (p.finished && !p.error){
          appendInlineLog('local-inline-log', '✓ 배포 완료 + 헬스체크 통과', 'ok');
          updateStepper('local-stepper', 'health', 'done');
          setLocalBtns({ generate:true, approve:false, scan:false, deploy:false });
        }
        break;
      }
      // ── GitHub Actions wizard results ────────────────────────
      case 'wb.actions.generateResult': {
        const p = m.payload || {};
        if (p.ok){
          actionsProposalId = p.proposal_id || '';
          if ($('actions-yaml-preview')) $('actions-yaml-preview').value = p.content || '';
          appendInlineLog('actions-inline-log', '✓ 워크플로 YAML 생성 완료', 'ok');
          updateStepper('actions-stepper', 'approve', '');
          setActionsBtns({ generate:true, approve:true, push:false });
        } else {
          appendInlineLog('actions-inline-log', '✗ ' + (p.error || '생성 실패'), 'err');
        }
        break;
      }
      case 'wb.actions.approveResult': {
        const p = m.payload || {};
        if (p.ok){
          appendInlineLog('actions-inline-log', '✓ 저장됨: ' + (p.path || '.github/workflows/ci-cd.yml'), 'ok');
          updateStepper('actions-stepper', 'secret', '');
          setActionsBtns({ generate:true, approve:false, push:true });
        } else {
          appendInlineLog('actions-inline-log', '✗ ' + (p.error || '저장 실패'), 'err');
        }
        break;
      }
      // ── Discord Bridge ──────────────────────────────────────
      case 'wb.discord.statusResult': {
        const p = m.payload || {};
        const txt = $('dc-status-text');
        if (!p.ok){
          if (txt){
            txt.className = 'alert warn';
            txt.innerHTML = '<span>봇 HTTP API에 연결할 수 없습니다 — 봇이 켜져있는지 확인하세요. <br><span style="color:var(--t3); font-size:11px">' + (p.error || '') + '</span></span>';
          }
          setDcStepCard(1, 'active');
          setDcStepCard(2, 'disabled');
          setDcStepCard(3, 'disabled');
          setDcStepCard(4, 'disabled');
          if ($('dc-step-1-status')) $('dc-step-1-status').textContent = '오프라인';
          break;
        }
        if (txt){
          txt.className = 'alert ok';
          txt.innerHTML = '<span><b>✓ 봇 온라인</b> · ' + (p.connected_clients || 0) + '개 VSCode 클라이언트 연결</span>';
        }
        if ($('dc-bridge-clients')) $('dc-bridge-clients').textContent = String(p.connected_clients || 0);
        setDcStepCard(1, 'done');
        setDcStepCard(2, 'active');
        setDcStepCard(3, 'active');
        if ($('dc-step-1-status')) $('dc-step-1-status').textContent = '온라인';

        // Step 4 — 현재 활성 채널
        const ch = $('dc-current-channel');
        if (p.active_channel_id){
          setDcStepCard(4, 'done');
          if ($('dc-step-4-status')) $('dc-step-4-status').textContent = '설정됨';
          if (ch){
            ch.innerHTML =
              '<div style="display:flex; align-items:center; gap:10px; padding:10px; background:var(--green-bg); border:1px solid var(--bd); border-radius:6px">'
              + '<svg class="icon-svg" style="color:var(--green); width:18px; height:18px"><use href="#i-dc"/></svg>'
              + '<div>'
              + '<div style="color:var(--t1); font-weight:600">#' + (p.channel_name || '(이름 조회 실패)') + '</div>'
              + '<div style="font-size:11px; color:var(--t3)">' + (p.guild_name || '(서버 이름 조회 실패)') + ' · ID ' + p.active_channel_id + '</div>'
              + '</div>'
              + '</div>';
          }
        } else {
          setDcStepCard(4, 'active');
          if ($('dc-step-4-status')) $('dc-step-4-status').textContent = '없음';
          if (ch) ch.textContent = '활성 채널이 설정되지 않았습니다. 위에서 채널을 저장하세요.';
        }
        if ($('dc-settings-snapshot')){
          try { $('dc-settings-snapshot').textContent = JSON.stringify(p.settings || {}, null, 2); }
          catch(_) { $('dc-settings-snapshot').textContent = '—'; }
        }
        // 자동 후속 로드 (1회만 — overwrite 방지)
        if (!window.__dcLoadedExtras){
          window.__dcLoadedExtras = true;
          vscode.postMessage({ type:'wb.discord.fetchInviteUrl' });
          vscode.postMessage({ type:'wb.discord.fetchGuilds' });
        }
        break;
      }
      case 'wb.discord.inviteUrlResult': {
        const p = m.payload || {};
        if (!p.ok){
          if ($('dc-invite-url')) $('dc-invite-url').textContent = '(생성 불가 — ' + (p.error || 'DISCORD_CLIENT_ID 미설정') + ')';
          break;
        }
        if ($('dc-invite-url')) $('dc-invite-url').textContent = p.invite_url || '—';
        if ($('dc-bot-name')) $('dc-bot-name').textContent = p.bot_name || '봇 이름 미감지';
        if ($('dc-bot-avatar') && p.bot_avatar){
          $('dc-bot-avatar').src = p.bot_avatar;
          $('dc-bot-avatar').style.display = 'inline-block';
        }
        window.__dcInviteUrl = p.invite_url || '';
        break;
      }
      case 'wb.discord.guildsResult': {
        const p = m.payload || {};
        const sel = $('dc-guild-select');
        if (!sel) break;
        if (!p.ok){
          sel.innerHTML = '<option value="">— 로딩 실패: ' + (p.error || '') + ' —</option>';
          break;
        }
        const guilds = p.guilds || [];
        if (!guilds.length){
          sel.innerHTML = '<option value="">— 봇을 초대한 서버가 없습니다 —</option>';
        } else {
          sel.innerHTML = '<option value="">— 서버 선택 (' + guilds.length + '개) —</option>' +
            guilds.map(g => '<option value="' + g.id + '">' + (g.name || '(이름 없음)') + ' · 채널 ' + (g.text_channel_count || 0) + '개</option>').join('');
        }
        break;
      }
      case 'wb.discord.channelsResult': {
        const p = m.payload || {};
        const sel = $('dc-channel-select');
        if (!sel) break;
        if (!p.ok){
          sel.innerHTML = '<option value="">— 로딩 실패 —</option>';
          sel.disabled = true;
          break;
        }
        const channels = p.channels || [];
        if (!channels.length){
          sel.innerHTML = '<option value="">— 텍스트 채널 없음 —</option>';
          sel.disabled = true;
          break;
        }
        // category 별로 묶기
        const byCategory = {};
        channels.forEach(c => {
          const k = c.category || '(카테고리 없음)';
          if (!byCategory[k]) byCategory[k] = [];
          byCategory[k].push(c);
        });
        let html = '<option value="">— 채널 선택 (' + channels.length + '개) —</option>';
        Object.keys(byCategory).forEach(cat => {
          html += '<optgroup label="' + cat.replace(/"/g, '&quot;') + '">';
          byCategory[cat].forEach(c => {
            html += '<option value="' + c.id + '">#' + (c.name || '(이름 없음)') + '</option>';
          });
          html += '</optgroup>';
        });
        sel.innerHTML = html;
        sel.disabled = false;
        break;
      }
      case 'wb.discord.setChannelResult': {
        const p = m.payload || {};
        if (p.ok){
          appendInlineLog('dc-step-3-log', '✓ 저장됨: ' + (p.channel_name ? ('#' + p.channel_name) : (p.active_channel_id || '(해제)')), 'ok');
          if ($('dc-save-hint')) $('dc-save-hint').textContent = '저장 완료';
        } else {
          appendInlineLog('dc-step-3-log', '✗ ' + (p.error || '저장 실패'), 'err');
        }
        break;
      }
      case 'wb.discord.error': {
        const p = m.payload || {};
        appendInlineLog('dc-step-3-log', '✗ ' + (p.context || '') + ': ' + (p.message || ''), 'err');
        break;
      }
    }
  });

  // ─── Discord Bridge 핸들러 ──────────────────────────────────────
  function setDcStepCard(stepNum, status){
    const card = $('dc-step-' + stepNum);
    if (!card) return;
    card.classList.remove('disabled','active','done');
    if (status === 'done') card.classList.add('done');
    else if (status === 'active') card.classList.add('active');
    else if (status === 'disabled') card.classList.add('disabled');
    const badge = $('dc-step-' + stepNum + '-status');
    if (badge){
      badge.textContent = status === 'done' ? '완료 ✓'
        : status === 'active' ? '진행 중'
        : status === 'disabled' ? '잠금'
        : '대기';
    }
    const numEl = $('dc-step-' + stepNum + '-num');
    if (numEl) numEl.textContent = status === 'done' ? '✓' : String(stepNum);
  }
  function dcBindClick(id, fn){
    const el = $(id);
    if (el) el.addEventListener('click', fn);
  }
  dcBindClick('dc-refresh-btn',       () => vscode.postMessage({ type:'wb.discord.fetchStatus' }));
  dcBindClick('dc-invite-btn',        () => vscode.postMessage({ type:'wb.discord.openInvite' }));
  dcBindClick('dc-copy-invite-btn',   () => {
    const url = window.__dcInviteUrl || '';
    if (!url){ appendInlineLog('dc-step-3-log', '✗ 초대 URL이 아직 로드되지 않았습니다', 'err'); return; }
    try {
      navigator.clipboard.writeText(url).then(
        () => appendInlineLog('dc-step-3-log', '✓ 초대 URL을 클립보드에 복사', 'ok'),
        () => appendInlineLog('dc-step-3-log', '✗ 복사 실패 — 수동으로 선택해 복사하세요', 'err')
      );
    } catch (e) {
      appendInlineLog('dc-step-3-log', '✗ 복사 불가: ' + e, 'err');
    }
  });

  // Guild 변경 → 채널 목록 다시 로드
  const dcGuildSel = $('dc-guild-select');
  if (dcGuildSel) dcGuildSel.addEventListener('change', () => {
    const gid = dcGuildSel.value || '';
    const chSel = $('dc-channel-select');
    if (chSel){
      chSel.innerHTML = '<option value="">— 로딩 중… —</option>';
      chSel.disabled = true;
    }
    if (!gid){
      if (chSel){
        chSel.innerHTML = '<option value="">— 먼저 서버 선택 —</option>';
        chSel.disabled = true;
      }
      return;
    }
    vscode.postMessage({ type:'wb.discord.fetchChannels', payload:{ guild_id: gid } });
  });

  // Channel 선택 → 저장 버튼 활성화
  const dcChannelSel = $('dc-channel-select');
  if (dcChannelSel) dcChannelSel.addEventListener('change', () => {
    const v = dcChannelSel.value || '';
    const btn = $('dc-save-channel-btn');
    if (btn) btn.disabled = !v;
    if ($('dc-save-hint')) $('dc-save-hint').textContent = v ? '저장 준비됨' : '채널을 고르면 활성화됩니다';
  });

  dcBindClick('dc-save-channel-btn', () => {
    const v = ($('dc-channel-select') || {}).value || '';
    if (!v) return;
    vscode.postMessage({ type:'wb.discord.setChannel', payload:{ channel_id: v } });
  });
  dcBindClick('dc-clear-channel-btn', () => {
    vscode.postMessage({ type:'wb.discord.setChannel', payload:{ channel_id: '' } });
  });
  dcBindClick('dc-manual-save-btn', () => {
    const v = (($('dc-manual-channel') || {}).value || '').trim();
    if (!v){ appendInlineLog('dc-step-3-log', '✗ 채널 ID를 입력하세요', 'err'); return; }
    if (!/^[0-9]{15,22}$/.test(v)){
      appendInlineLog('dc-step-3-log', '✗ 채널 ID는 15~22자리 숫자여야 합니다', 'err');
      return;
    }
    vscode.postMessage({ type:'wb.discord.setChannel', payload:{ channel_id: v } });
  });

  // Discord 탭에 처음 진입할 때 상태 로드 (lazy). 스크립트가 이미 DOM 끝에 있으므로
  // querySelectorAll로 즉시 listener 부착. 이미 일반 .tab 클릭 핸들러가 따로 있으므로
  // 여기는 "최초 1회 fetch" 트리거만 담당.
  document.querySelectorAll('.tab[data-page="discord"]').forEach(t => {
    t.addEventListener('click', () => {
      if (!window.__dcLoadedOnce){
        window.__dcLoadedOnce = true;
        vscode.postMessage({ type:'wb.discord.fetchStatus' });
      }
    });
  });

  // ─── GitHub Hub 핸들러 ──────────────────────────────────────────
  function ghBindClick(id, fn){
    const el = $(id);
    if (el) el.addEventListener('click', fn);
  }
  ghBindClick('gh-login-btn',       () => vscode.postMessage({ type:'wb.gh.login' }));
  ghBindClick('gh-logout-btn',      () => vscode.postMessage({ type:'wb.gh.logout' }));
  ghBindClick('gh-status-refresh',  () => vscode.postMessage({ type:'wb.gh.status' }));
  ghBindClick('gh-list-repos-btn',  () => vscode.postMessage({ type:'wb.gh.listRepos' }));
  ghBindClick('gh-create-btn', () => {
    const name = ($('gh-new-name')||{}).value || '';
    const isPrivate = !!($('gh-new-private')||{}).checked;
    const desc = ($('gh-new-desc')||{}).value || '';
    if (!name.trim()){ alert('레포 이름을 입력하세요'); return; }
    vscode.postMessage({ type:'wb.gh.createRepo', payload:{ name: name.trim(), private:isPrivate, description:desc } });
  });
  ghBindClick('gh-push-btn', () => {
    vscode.postMessage({
      type:'wb.gh.push',
      payload:{
        branch: (($('gh-push-branch')||{}).value || '').trim(),
        force:  !!($('gh-push-force')||{}).checked,
      },
    });
  });
  ghBindClick('gh-secret-btn', () => {
    const repo  = (($('gh-secret-repo')||{}).value || '').trim();
    const name  = (($('gh-secret-name')||{}).value || '').trim();
    const value = (($('gh-secret-value')||{}).value || '');
    if (!repo || !name || !value){ alert('repo / name / value 모두 입력하세요'); return; }
    vscode.postMessage({ type:'wb.gh.setSecret', payload:{ repo, name, value } });
    if ($('gh-secret-value')) $('gh-secret-value').value = '';
  });
  ghBindClick('gh-runs-btn', () => {
    const repo = (($('gh-runs-repo')||{}).value || '').trim();
    if (!repo){ alert('레포 (owner/name) 를 입력하세요'); return; }
    vscode.postMessage({ type:'wb.gh.listRuns', payload:{ repo } });
  });

  // ─── Deploy Center 탭 전환 ─────────────────────────────────────
  document.querySelectorAll('.deploy-tab').forEach(t => {
    t.addEventListener('click', () => {
      const name = t.dataset.deploy;
      document.querySelectorAll('.deploy-tab').forEach(x => x.classList.toggle('active', x === t));
      document.querySelectorAll('.deploy-pane').forEach(p => p.classList.toggle('active', p.id === 'deploy-' + name));
    });
  });

  // ── Local Docker 6단계 wizard ──────────────────────────────────
  let localProposalId = '';
  function setLocalStepperStage(stage){
    updateStepper('local-stepper', stage, '');
  }
  function setLocalBtns(states){
    ['generate','approve','scan','deploy'].forEach(k => {
      const b = $('local-btn-' + k);
      if (b) b.disabled = !states[k];
    });
  }
  setLocalBtns({ generate:true, approve:false, scan:false, deploy:false });
  ghBindClick('local-btn-generate', () => {
    appendInlineLog('local-inline-log', '[…] Dockerfile 생성 요청', 'cmd');
    setLocalStepperStage('generate');
    vscode.postMessage({ type:'wb.local.generate' });
  });
  ghBindClick('local-btn-approve', () => {
    if (!localProposalId){ alert('먼저 Dockerfile 을 생성하세요'); return; }
    appendInlineLog('local-inline-log', '[…] Dockerfile 승인 + 저장', 'cmd');
    setLocalStepperStage('approve');
    const content = ($('local-dockerfile-preview')||{}).value || '';
    vscode.postMessage({ type:'wb.local.approve', payload:{ proposal_id: localProposalId, content } });
  });
  ghBindClick('local-btn-scan', () => {
    appendInlineLog('local-inline-log', '[…] Trivy 스캔 실행', 'cmd');
    setLocalStepperStage('scan');
    vscode.postMessage({ type:'wb.local.scan' });
  });
  ghBindClick('local-btn-deploy', () => {
    appendInlineLog('local-inline-log', '[…] docker build + run + 헬스체크', 'cmd');
    setLocalStepperStage('build');
    vscode.postMessage({
      type:'wb.local.deploy',
      payload:{
        image: ($('local-image')||{}).value || 'recoder-app',
        host_port: Number(($('local-host-port')||{}).value || 8000),
        container_port: Number(($('local-container-port')||{}).value || 8000),
      },
    });
  });
  ghBindClick('local-btn-reset', () => {
    localProposalId = '';
    if ($('local-dockerfile-preview')) $('local-dockerfile-preview').value = '';
    if ($('local-scan-result')) $('local-scan-result').innerHTML = '<div style="color:var(--t3); font-size:12px">아직 스캔하지 않음</div>';
    setLocalBtns({ generate:true, approve:false, scan:false, deploy:false });
    const wrap = $('local-inline-log'); if (wrap){
      const body = wrap.querySelector('.inline-log-body');
      if (body) body.innerHTML = '<div class="inline-log-empty">초기화됨</div>';
    }
    updateStepper('local-stepper', 'generate', '');
  });

  ghBindClick('ec2-ready-btn', () => {
    const el = $('ec2-status-line'); if (el) el.textContent = '[…] 사전 점검은 배포 버튼 클릭 시 자동 실행됩니다.';
  });
  ghBindClick('ec2-deploy-btn', () => {
    const p = {
      image_name: ($('ec2-image-name')||{}).value,
      tag:        ($('ec2-tag')||{}).value,
      host_port:  Number(($('ec2-host-port')||{}).value || 8000),
      container_port: Number(($('ec2-container-port')||{}).value || 8000),
      health_check_path: ($('ec2-health-path')||{}).value,
      ecr_registry: ($('ec2-ecr')||{}).value,
      ec2_host:     ($('ec2-host')||{}).value,
      ec2_ssh_key:  ($('ec2-ssh-key')||{}).value,
      aws_region:   ($('ec2-region')||{}).value,
      ec2_user:     ($('ec2-user')||{}).value || 'ec2-user',
    };
    vscode.postMessage({ type:'wb.deploy.ec2', payload:p });
    const el = $('ec2-status-line'); if (el) el.textContent = '[…] EC2 배포 요청 전송';
  });
  ghBindClick('ecs-ready-btn', () => {
    const el = $('ecs-status-line'); if (el) el.textContent = '[…] 사전 점검은 배포 버튼 클릭 시 자동 실행됩니다.';
  });
  ghBindClick('ecs-deploy-btn', () => {
    const p = {
      image_name: ($('ecs-image-name')||{}).value,
      tag:        ($('ecs-tag')||{}).value,
      aws_region: ($('ecs-region')||{}).value,
      ecr_registry: ($('ecs-ecr')||{}).value,
      ecs_cluster: ($('ecs-cluster')||{}).value,
      ecs_service: ($('ecs-service')||{}).value,
      container_port: Number(($('ecs-container-port')||{}).value || 8000),
      cpu: ($('ecs-cpu')||{}).value,
      memory: ($('ecs-memory')||{}).value,
      task_family: ($('ecs-task-family')||{}).value,
      environment: ($('ecs-env')||{}).value,
      branch: ($('ecs-branch')||{}).value,
      skip_sbom: !!($('ecs-skip-sbom')||{}).checked,
      skip_opa:  !!($('ecs-skip-opa')||{}).checked,
    };
    vscode.postMessage({ type:'wb.deploy.ecs', payload:p });
    const el = $('ecs-status-line'); if (el) el.textContent = '[…] ECS 배포 요청 전송';
  });

  let actionsProposalId = '';
  function setActionsBtns(states){
    ['generate','approve','push'].forEach(k => {
      const b = $('actions-' + k + '-btn');
      if (b) b.disabled = !states[k];
    });
  }
  setActionsBtns({ generate:true, approve:false, push:false });
  ghBindClick('actions-generate-btn', () => {
    appendInlineLog('actions-inline-log', '[…] 워크플로 YAML 생성', 'cmd');
    updateStepper('actions-stepper', 'generate', '');
    vscode.postMessage({ type:'wb.actions.generate' });
  });
  ghBindClick('actions-approve-btn', () => {
    if (!actionsProposalId){ alert('먼저 YAML 을 생성하세요'); return; }
    appendInlineLog('actions-inline-log', '[…] 승인 + .github/workflows/ 저장', 'cmd');
    updateStepper('actions-stepper', 'approve', '');
    const content = ($('actions-yaml-preview')||{}).value || '';
    vscode.postMessage({ type:'wb.actions.approve', payload:{ proposal_id: actionsProposalId, content } });
  });
  ghBindClick('actions-push-btn', () => {
    appendInlineLog('actions-inline-log', '[…] .github/ commit + push', 'cmd');
    updateStepper('actions-stepper', 'push', '');
    vscode.postMessage({ type:'wb.gh.push', payload:{ branch:'' } });
  });
  ghBindClick('actions-reset-btn', () => {
    actionsProposalId = '';
    if ($('actions-yaml-preview')) $('actions-yaml-preview').value = '';
    setActionsBtns({ generate:true, approve:false, push:false });
    const wrap = $('actions-inline-log'); if (wrap){
      const body = wrap.querySelector('.inline-log-body');
      if (body) body.innerHTML = '<div class="inline-log-empty">초기화됨</div>';
    }
    updateStepper('actions-stepper', 'generate', '');
  });

  function renderPrechecks(items){
    const strip = $('deploy-precheck');
    if (!strip) return;
    if (!items){
      strip.innerHTML = '<span class="precheck-pill"><span class="dot"></span>점검 중</span>';
      return;
    }
    // chip strip 형태 — 핵심 상태만 (이름 + dot + 해결 액션 inline)
    strip.innerHTML = items.map(it => {
      const action = it.action ? ' data-precheck-action="' + it.action + '" title="클릭으로 해결"' : '';
      const label = it.name + (it.action ? '  →' : '');
      return '<span class="precheck-pill ' + it.status + '"' + action + '><span class="dot"></span>' + label + '</span>';
    }).join('');
    strip.querySelectorAll('[data-precheck-action]').forEach(el => {
      el.addEventListener('click', () => {
        const a = el.dataset.precheckAction;
        if (a === 'aws_configure') vscode.postMessage({ type:'wb.cmd', payload:{ cmd:'recoder.awsConfigure' } });
        else if (a === 'github_login') vscode.postMessage({ type:'wb.gh.login' });
        else if (a === 'core_diagnostics') vscode.postMessage({ type:'wb.runDiagnostics' });
      });
    });
  }
  function runDeployPrecheck(){
    renderPrechecks(null);
    vscode.postMessage({ type:'wb.deploy.precheck' });
  }
  if ($('deploy-precheck-refresh')) $('deploy-precheck-refresh').addEventListener('click', runDeployPrecheck);
  document.querySelectorAll('.tab').forEach(t => {
    if (t.dataset.page === 'deploy'){
      t.addEventListener('click', runDeployPrecheck);
    }
  });

  setTimeout(() => vscode.postMessage({ type:'wb.gh.status' }), 1200);

  vscode.postMessage({ type:'wb.ready' });
  setInterval(()=> vscode.postMessage({ type:'wb.poll.health' }), 5000);
})();
</script>
</body>
</html>`;
}
