"""
ReCoder Dashboard HTML (단일 페이지 라이브 대시보드).

server.py 의 GET /dashboard 가 이 모듈의 render(token, port) 결과를 반환한다.
디자인 레퍼런스: Notion-style Light (라이트/다크 토글 지원).
실시간 데이터:
  - /api/health        (토큰 없음, 폴링 진입점)
  - /api/ready         (Ready 칩)
  - /api/status        (현재 patch / infra / plan)
  - /api/project       (Project Profile)
  - /api/cost          (좌측 하단 비용)
  - /api/deploy/status (배포 단계)
"""

from __future__ import annotations


def render(token: str, port: int) -> str:
    """대시보드 HTML 문자열 반환. token 은 X-Session-Token 헤더로 사용."""
    # token / port 는 안전한 문자만 들어오므로 escape 불필요.
    return _TEMPLATE.replace("__TOKEN__", token).replace("__PORT__", str(port))


# ── 템플릿 ────────────────────────────────────────────────────────────
# 큰 한 덩어리지만 server.py 와 분리되어 있어 유지보수가 쉬움.
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>ReCoder Dashboard</title>
<style>
:root {
  /* Notion-style Light tokens */
  --bg-app:        #0e0e0e;
  --bg-card:       #faf9f6;
  --bg-side:       #f4f2ec;
  --bg-main:       #ffffff;
  --bg-muted:      #f6f5f1;
  --bg-code:       #f7f5ef;
  --bg-diff-add:   #e8f3e3;
  --bg-input:      #ffffff;
  --bd:            #e6e3da;
  --bd-strong:     #d6d2c4;
  --t1:            #1c1b18;
  --t2:            #5d5a52;
  --t3:            #9a958a;
  --accent:        #2f6df3;
  --accent-bg:     #e7efff;
  --green:         #2f9e44;
  --green-bg:      #e7f5ec;
  --red:           #d6432c;
  --red-bg:        #fbe9e6;
  --orange:        #c98421;
  --orange-bg:     #fbf1de;
  --shadow-card:   0 1px 2px rgba(15,15,15,.04), 0 4px 16px rgba(15,15,15,.08);
  --radius-sm:     6px;
  --radius-md:     10px;
  --radius-lg:     14px;
  --mono:          "SF Mono","JetBrains Mono","Consolas",ui-monospace,monospace;
  --sans:          "Malgun Gothic","Apple SD Gothic Neo","Noto Sans KR",-apple-system,BlinkMacSystemFont,"Segoe UI","Pretendard",system-ui,sans-serif;
}
[data-theme="dark"] {
  --bg-app:        #08080a;
  --bg-card:       #14151a;
  --bg-side:       #16181f;
  --bg-main:       #0f1116;
  --bg-muted:      #1a1d25;
  --bg-code:       #11141b;
  --bg-diff-add:   #102818;
  --bg-input:      #14171f;
  --bd:            #262a35;
  --bd-strong:     #353a47;
  --t1:            #e6e7eb;
  --t2:            #9ba1b0;
  --t3:            #6b7283;
  --accent:        #6aa3ff;
  --accent-bg:     #16243f;
  --green:         #4ad17a;
  --green-bg:      #112a1c;
  --red:           #ff7368;
  --red-bg:        #2a1414;
  --orange:        #ddae5d;
  --orange-bg:     #2a1f0e;
  --shadow-card:   0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.5);
}

* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.55;
  color: var(--t1);
  background: var(--bg-app);
  -webkit-font-smoothing: antialiased;
}

/* ── Top bar ───────────────────────────────────────────────────────── */
.topbar {
  max-width: 1280px;
  margin: 0 auto;
  padding: 28px 32px 14px;
  display: flex;
  align-items: center;
  gap: 14px;
}
.topbar .label {
  display: flex; align-items: baseline; gap: 12px;
  color: #cdcabe;
}
.topbar .label-num {
  font-size: 13px; font-weight: 600; color: #837f72; letter-spacing: .04em;
}
.topbar .label-title {
  font-size: 18px; font-weight: 700; color: #f3efe2;
}
.topbar .label-sub {
  font-size: 12px; color: #908b7c;
}
.topbar-spacer { flex: 1; }
.pill-toggle {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 14px;
  background: rgba(255,255,255,.04);
  border: 1px solid #2a2a2a;
  color: #d8d4c5;
  font-size: 12px; font-weight: 500;
  border-radius: 999px; cursor: pointer;
  transition: background .15s, border-color .15s, color .15s;
}
.pill-toggle:hover { background: rgba(255,255,255,.08); }
.pill-toggle[aria-pressed="true"] {
  background: #efece1; color: #1c1b18; border-color: #efece1;
}
[data-theme="dark"] .pill-toggle[aria-pressed="true"] {
  background: #2c2f38; color: #e6e7eb; border-color: #3b3f4b;
}

/* ── Card shell ────────────────────────────────────────────────────── */
.shell {
  max-width: 1280px;
  margin: 0 auto 36px;
  background: var(--bg-card);
  border: 1px solid var(--bd);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-card);
  display: grid;
  grid-template-columns: 240px 1fr;
  min-height: 720px;
  overflow: hidden;
}

/* ── Sidebar ───────────────────────────────────────────────────────── */
.side {
  background: var(--bg-side);
  border-right: 1px solid var(--bd);
  padding: 18px 14px;
  display: flex; flex-direction: column;
  gap: 14px;
}
.brand {
  display: flex; align-items: center; gap: 10px;
  padding: 4px 6px 10px;
  border-bottom: 1px solid var(--bd);
}
.brand-mark {
  width: 30px; height: 30px;
  border-radius: 8px;
  background: linear-gradient(135deg,#2f6df3,#7d4dff);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 14px; letter-spacing: -.02em;
}
.brand-text { display: flex; flex-direction: column; gap: 1px; }
.brand-name { font-weight: 700; font-size: 14px; color: var(--t1); }
.brand-proj { font-size: 11px; color: var(--t3); }

.side-section-label {
  font-size: 10px; font-weight: 700;
  letter-spacing: .12em; text-transform: uppercase;
  color: var(--t3);
  padding: 8px 6px 2px;
}
.mode-list { display: flex; flex-direction: column; gap: 2px; }
.mode-item {
  display: flex; align-items: center; gap: 10px;
  padding: 7px 10px;
  border-radius: 8px;
  font-size: 13px; color: var(--t2);
  cursor: pointer;
  user-select: none;
  border: 1px solid transparent;
  transition: background .15s, color .15s, border-color .15s;
}
.mode-item:hover { background: var(--bg-muted); color: var(--t1); }
.mode-item.active {
  background: var(--bg-main);
  color: var(--t1);
  border-color: var(--bd);
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(0,0,0,.04);
}
.mode-item .icon { width: 14px; text-align: center; opacity: .8; }
.mode-item .label { flex: 1; }
.mode-item .pill {
  font-size: 10px; font-weight: 600;
  padding: 1px 8px; border-radius: 999px;
  background: var(--bg-muted); color: var(--t3);
  border: 1px solid var(--bd);
}
.mode-item.active .pill { background: var(--accent-bg); color: var(--accent); border-color: transparent; }
.mode-item.disabled { opacity: .55; cursor: not-allowed; }

.steps {
  display: flex; flex-direction: column; gap: 4px;
  padding: 4px 4px;
}
.step {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 6px 6px;
  position: relative;
}
.step .dot {
  width: 18px; height: 18px; border-radius: 999px;
  background: var(--bg-main); border: 2px solid var(--bd-strong);
  display: flex; align-items: center; justify-content: center;
  font-size: 10px; font-weight: 700; color: var(--t3);
  flex-shrink: 0;
  transition: all .2s;
}
.step.done .dot   { background: var(--green); border-color: var(--green); color: #fff; }
.step.active .dot { background: var(--accent); border-color: var(--accent); color: #fff;
                    box-shadow: 0 0 0 4px var(--accent-bg); }
.step .text { display: flex; flex-direction: column; gap: 1px; padding-top: 1px; }
.step .name { font-size: 12.5px; color: var(--t2); }
.step.done .name { color: var(--t1); font-weight: 500; }
.step.active .name { color: var(--t1); font-weight: 600; }
.step .meta { font-size: 10.5px; color: var(--t3); }
.step:not(:last-child)::after {
  content: ''; position: absolute;
  left: 14px; top: 28px; bottom: -4px;
  width: 2px; background: var(--bd);
}
.step.done:not(:last-child)::after { background: var(--green); }

.side-foot {
  margin-top: auto;
  padding: 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--bd);
  background: var(--bg-main);
}
.side-foot .row { display: flex; align-items: baseline; justify-content: space-between; }
.side-foot .label { font-size: 10px; color: var(--t3); letter-spacing: .04em; }
.side-foot .val { font-size: 18px; font-weight: 700; color: var(--t1); letter-spacing: -.01em; }
.side-foot .sub { font-size: 10.5px; color: var(--t3); margin-top: 4px; }

/* ── Main ──────────────────────────────────────────────────────────── */
.main {
  background: var(--bg-main);
  padding: 24px 32px 32px;
  display: flex; flex-direction: column; gap: 18px;
  overflow-y: auto;
}
.main-head {
  display: flex; align-items: flex-start; gap: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--bd);
}
.main-head .title {
  font-size: 22px; font-weight: 700; letter-spacing: -.01em; color: var(--t1);
}
.main-head .sub {
  margin-top: 4px;
  font-size: 12px; color: var(--t2);
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
}
.main-head .sub .dot-sep { color: var(--t3); }
.main-head .actions { margin-left: auto; display: flex; gap: 8px; align-items: center; }

/* ── Buttons ───────────────────────────────────────────────────────── */
.btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 8px 14px;
  border-radius: 8px;
  border: 1px solid var(--bd);
  background: var(--bg-main);
  color: var(--t1);
  font-size: 13px; font-weight: 500;
  cursor: pointer;
  transition: background .15s, border-color .15s, transform .05s;
  white-space: nowrap;
}
.btn:hover { border-color: var(--bd-strong); background: var(--bg-muted); }
.btn:active { transform: translateY(1px); }
.btn-primary { background: #1f1f1f; color: #fff; border-color: #1f1f1f; }
.btn-primary:hover { background: #2a2a2a; border-color: #2a2a2a; }
[data-theme="dark"] .btn-primary { background: #e6e7eb; color: #14151a; border-color: #e6e7eb; }
[data-theme="dark"] .btn-primary:hover { background: #f5f5f7; }
.btn-success { color: var(--green); border-color: var(--green); background: var(--green-bg); }
.btn-danger { color: var(--red); border-color: var(--red); background: var(--red-bg); }
.btn[disabled] { opacity: .5; cursor: not-allowed; }
.btn .check { color: var(--green); }

/* ── Cards ─────────────────────────────────────────────────────────── */
.card {
  border: 1px solid var(--bd);
  border-radius: var(--radius-md);
  background: var(--bg-main);
  overflow: hidden;
}
.card-head {
  display: flex; align-items: center; gap: 10px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--bd);
}
.card-head h3 {
  font-size: 14px; font-weight: 600; color: var(--t1);
  display: flex; align-items: center; gap: 8px;
}
.card-head .badge { margin-left: auto; }
.card-body { padding: 16px 18px; }

/* ── Profile grid ──────────────────────────────────────────────────── */
.profile-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 24px;
}
.profile-grid .col .label {
  font-size: 10px; color: var(--t3); letter-spacing: .12em;
  text-transform: uppercase; font-weight: 700;
}
.profile-grid .col .val {
  margin-top: 4px;
  font-size: 14px; color: var(--t1); font-family: var(--mono);
}
.profile-grid .col .val.green { color: var(--green); font-weight: 600; }
.profile-grid .col .val.accent { color: var(--accent); }

/* ── Code block (Notion-style) ─────────────────────────────────────── */
.code {
  font-family: var(--mono);
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--t1);
  background: var(--bg-code);
  padding: 14px 18px;
  white-space: pre;
  overflow-x: auto;
}
.code.diff-add { background: var(--bg-diff-add); }
.code .comment { color: var(--t3); }

/* ── Trivy result grid ─────────────────────────────────────────────── */
.sev-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  padding: 14px 18px;
}
.sev-cell {
  display: flex; flex-direction: column; align-items: center;
  padding: 18px 8px;
  border: 1px solid var(--bd);
  border-radius: var(--radius-md);
  background: var(--bg-main);
}
.sev-cell .num {
  font-size: 26px; font-weight: 700; line-height: 1;
}
.sev-cell .lab {
  margin-top: 6px;
  font-size: 10px; color: var(--t3); letter-spacing: .12em; text-transform: uppercase;
}
.sev-cell.crit .num { color: var(--red); }
.sev-cell.high .num { color: var(--orange); }
.sev-cell.med  .num { color: var(--accent); }
.sev-cell.low  .num { color: var(--green); }
.sev-cell.zero .num { color: var(--green); }

/* ── Badges ────────────────────────────────────────────────────────── */
.badge {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11px; font-weight: 600;
  padding: 2px 9px;
  border-radius: 999px;
}
.badge-stack { background: var(--accent-bg); color: var(--accent); }
.badge-pass  { background: var(--green-bg); color: var(--green); }
.badge-warn  { background: var(--orange-bg); color: var(--orange); }
.badge-fail  { background: var(--red-bg); color: var(--red); }

/* ── Ready chips (top of main) ─────────────────────────────────────── */
.chips { display: inline-flex; gap: 6px; }
.chip {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 11px; font-weight: 600;
  padding: 3px 10px;
  border-radius: 999px;
  background: var(--bg-muted); color: var(--t3);
  border: 1px solid var(--bd);
}
.chip .dot { width: 6px; height: 6px; border-radius: 999px; background: var(--t3); }
.chip.ok   { color: var(--green); background: var(--green-bg); border-color: transparent; }
.chip.ok .dot   { background: var(--green); }
.chip.fail { color: var(--red); background: var(--red-bg); border-color: transparent; }
.chip.fail .dot { background: var(--red); }
.chip.partial { color: var(--orange); background: var(--orange-bg); border-color: transparent; }
.chip.partial .dot { background: var(--orange); }

/* ── Empty state ───────────────────────────────────────────────────── */
.empty {
  padding: 48px 24px;
  text-align: center;
  color: var(--t2);
}
.empty .icon { font-size: 28px; margin-bottom: 8px; }
.empty .title { font-size: 15px; font-weight: 600; color: var(--t1); margin-bottom: 4px; }
.empty .desc  { font-size: 12.5px; color: var(--t2); }
.empty .actions { margin-top: 14px; display: inline-flex; gap: 8px; }

/* ── Diff view ─────────────────────────────────────────────────────── */
.diff-table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 12px; }
.diff-table td { padding: 1px 10px; line-height: 1.6; white-space: pre; }
.diff-table .ln {
  width: 38px; text-align: right; color: var(--t3);
  background: var(--bg-muted); border-right: 1px solid var(--bd);
  user-select: none;
}
.diff-row.add  { background: var(--bg-diff-add); color: var(--green); }
.diff-row.rem  { background: var(--red-bg); color: var(--red); }
.diff-row.info { background: var(--bg-muted); color: var(--t3); }

/* ── Toast ─────────────────────────────────────────────────────────── */
#toast {
  position: fixed; bottom: 24px; left: 50%;
  transform: translateX(-50%) translateY(8px);
  background: #1f1f1f; color: #fff;
  padding: 8px 14px; border-radius: 999px;
  font-size: 12px; font-weight: 500;
  box-shadow: 0 6px 24px rgba(0,0,0,.25);
  opacity: 0; pointer-events: none;
  transition: opacity .25s, transform .25s;
}
#toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

/* ── Spinner ───────────────────────────────────────────────────────── */
.spinner {
  width: 14px; height: 14px;
  border: 2px solid var(--bd);
  border-top-color: var(--accent);
  border-radius: 999px;
  animation: spin .8s linear infinite;
  display: inline-block;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Responsive ────────────────────────────────────────────────────── */
@media (max-width: 880px) {
  .shell { grid-template-columns: 1fr; }
  .side { border-right: none; border-bottom: 1px solid var(--bd); }
  .profile-grid { grid-template-columns: repeat(2, 1fr); }
  .sev-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
</head>
<body>

<div class="topbar">
  <div class="label">
    <span class="label-num">RECODER</span>
    <span class="label-title" id="topbar-title">Build · Ship · Operate</span>
    <span class="label-sub" id="topbar-sub">VS Code 사이드바와 동일한 데이터를 넓은 화면에서 확인합니다.</span>
  </div>
  <div class="topbar-spacer"></div>
  <button class="pill-toggle" id="btn-theme" aria-pressed="true">
    <span id="theme-label">라이트 모드</span>
  </button>
  <button class="pill-toggle" id="btn-toggle-side" aria-pressed="true">
    사이드바 토글
  </button>
</div>

<div class="shell">

  <!-- Sidebar -->
  <aside class="side" id="side">
    <div class="brand">
      <div class="brand-mark">R</div>
      <div class="brand-text">
        <div class="brand-name">ReCoder</div>
        <div class="brand-proj" id="brand-proj">recoder-demo</div>
      </div>
    </div>

    <div>
      <div class="side-section-label">Mode</div>
      <div class="mode-list">
        <div class="mode-item" data-tab="build">
          <span class="label">Build</span>
          <span class="pill" id="pill-build">·</span>
        </div>
        <div class="mode-item active" data-tab="ship">
          <span class="label">Ship</span>
          <span class="pill" id="pill-ship">진행</span>
        </div>
        <div class="mode-item disabled" data-tab="operate">
          <span class="label">Operate</span>
          <span class="pill">준비 중</span>
        </div>
      </div>
    </div>

    <div>
      <div class="side-section-label" id="steps-label">Ship 진행 단계</div>
      <div class="steps" id="steps"></div>
    </div>

    <div class="side-foot">
      <div class="row">
        <span class="label">오늘 사용량</span>
      </div>
      <div class="val" id="cost-daily">$0.00</div>
      <div class="sub" id="cost-sub">0회 LLM 호출 · -</div>
    </div>
  </aside>

  <!-- Main -->
  <section class="main" id="main">
    <!-- 동적 -->
  </section>

</div>

<div id="toast"></div>

<script>
(function () {
  'use strict';

  const TOKEN = '__TOKEN__';
  const PORT  = '__PORT__';
  const API   = `http://127.0.0.1:${PORT}`;

  const state = {
    tab:    localStorage.getItem('rc.tab') || 'ship',
    theme:  localStorage.getItem('rc.theme') || 'light',
    side:   localStorage.getItem('rc.side') !== '0',
    ready:  null,
    status: null,
    cost:   null,
    project: null,
  };

  // ── Theme & layout ─────────────────────────────────────────────────
  function applyTheme() {
    document.documentElement.dataset.theme = state.theme;
    
    document.getElementById('theme-label').textContent = state.theme === 'light' ? '라이트 모드' : '다크 모드';
    document.getElementById('btn-theme').setAttribute('aria-pressed', state.theme === 'light' ? 'true' : 'false');
  }
  function applySide() {
    document.getElementById('side').style.display = state.side ? '' : 'none';
    const shell = document.querySelector('.shell');
    shell.style.gridTemplateColumns = state.side ? '240px 1fr' : '1fr';
    document.getElementById('btn-toggle-side').setAttribute('aria-pressed', state.side ? 'true' : 'false');
  }

  // ── Fetch helpers ──────────────────────────────────────────────────
  async function api(path, opts = {}) {
    const headers = Object.assign({ 'X-Session-Token': TOKEN }, opts.headers || {});
    const res = await fetch(API + path, Object.assign({}, opts, {
      headers,
      credentials: 'omit',
    }));
    if (!res.ok) throw new Error(`${path} ${res.status}`);
    return res.headers.get('content-type')?.includes('json') ? res.json() : res.text();
  }
  async function tryApi(path, opts) { try { return await api(path, opts); } catch { return null; } }

  // ── Toast ──────────────────────────────────────────────────────────
  let toastTimer = null;
  function toast(msg) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 2400);
  }

  // ── Switching tabs ─────────────────────────────────────────────────
  function setTab(tab) {
    if (tab === 'operate') return; // 준비 중
    state.tab = tab;
    localStorage.setItem('rc.tab', tab);
    document.querySelectorAll('.mode-item').forEach(m => {
      m.classList.toggle('active', m.dataset.tab === tab);
    });
    renderMain();
    renderSteps();
  }

  // ── Steps (progress in sidebar) ────────────────────────────────────
  function renderSteps() {
    const stepsEl = document.getElementById('steps');
    const labelEl = document.getElementById('steps-label');
    let stepsDef;
    if (state.tab === 'build') {
      labelEl.textContent = 'Build 진행 단계';
      stepsDef = [
        { id: 1, name: '에러 수집',    meta: '터미널 / 자동 감지' },
        { id: 2, name: '컨텍스트',     meta: 'Context Gate' },
        { id: 3, name: 'AI 분석',      meta: 'Claude Haiku' },
        { id: 4, name: '코드 패치',    meta: '승인 대기' },
        { id: 5, name: 'Git 커밋',     meta: '예정' },
      ];
    } else {
      labelEl.textContent = 'Ship 진행 단계';
      stepsDef = [
        { id: 1, name: 'Dockerfile 생성', meta: '완료 후 표시' },
        { id: 2, name: 'Trivy 보안 스캔', meta: '취약점 검사' },
        { id: 3, name: '사용자 승인',     meta: 'Level 1' },
        { id: 4, name: 'docker build',    meta: '예정' },
        { id: 5, name: 'Health Check',    meta: '예정' },
      ];
    }
    const stage = computeStage();
    stepsEl.innerHTML = stepsDef.map(s => {
      const cls = s.id < stage ? 'done' : (s.id === stage ? 'active' : '');
      const dot = s.id < stage ? '✓' : s.id;
      return `<div class="step ${cls}">
        <div class="dot">${dot}</div>
        <div class="text"><div class="name">${s.name}</div><div class="meta">${escapeHtml(s.meta)}</div></div>
      </div>`;
    }).join('');
  }
  function computeStage() {
    const s = state.status || {};
    if (state.tab === 'build') {
      if (!s.patch_proposal) return 1;
      return 4; // 패치 제안 = 승인 대기
    }
    if (state.tab === 'ship') {
      if (!s.infra_proposal) return 1;
      if (!s.plan) return 3;             // 승인 대기
      const ds = state.deploy || {};
      if (ds.stage === 'building') return 4;
      if (ds.stage === 'health' || ds.stage === 'running' || ds.finished) return 5;
      return 3;
    }
    return 1;
  }

  // ── Main rendering ─────────────────────────────────────────────────
  function renderMain() {
    const main = document.getElementById('main');
    if (state.tab === 'build') {
      main.innerHTML = renderBuild();
      wireBuild();
    } else if (state.tab === 'ship') {
      main.innerHTML = renderShip();
      wireShip();
    } else {
      main.innerHTML = renderOperate();
    }
    document.getElementById('topbar-title').textContent =
      state.tab === 'build' ? 'Build — 에러 감지 및 패치' :
      state.tab === 'ship'  ? 'Ship — 인프라 생성 및 배포' :
                              'Operate — 운영 모니터링';
  }

  // ── Build tab ──────────────────────────────────────────────────────
  function renderBuild() {
    const s = state.status || {};
    const p = s.patch_proposal;
    if (!p) {
      return `
        <div class="main-head">
          <div>
            <div class="title">Build</div>
            <div class="sub">${chipsHtml()} <span class="dot-sep">·</span> 분석된 패치 없음</div>
          </div>
          <div class="actions">
            <button class="btn" id="btn-scan">프로젝트 스캔</button>
          </div>
        </div>
        <div class="card">
          <div class="empty">

            <div class="title">에러 분석 대기 중</div>
            <div class="desc">VS Code 확장의 AI 분석 또는 자동 감지 기능을 통해 에러를 전송하세요.<br/>
            확장이 활성화되어 있으면 분석 결과가 즉시 이 화면에 표시됩니다.</div>
          </div>
        </div>`;
    }
    const risk = (p.risk_level || 'low').toLowerCase();
    const riskBadge = risk === 'low' ? 'badge-pass' : (risk === 'high' || risk === 'critical' ? 'badge-fail' : 'badge-warn');
    return `
      <div class="main-head">
        <div>
          <div class="title">패치 제안 검토</div>
          <div class="sub">${chipsHtml()} <span class="dot-sep">·</span>
            <span class="badge ${riskBadge}">RISK · ${escapeHtml((p.risk_level||'LOW').toUpperCase())}</span>
            <span class="dot-sep">·</span> ${escapeHtml(p.patches?.length ?? 0)}개 파일 영향
          </div>
        </div>
        <div class="actions">
          <button class="btn btn-danger" id="btn-reject">거절</button>
          <button class="btn btn-primary" id="btn-approve">승인 후 적용</button>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>요약</h3></div>
        <div class="card-body">${escapeHtml(p.summary || '')}</div>
      </div>
      ${(p.patches || []).map((pp, i) => `
        <div class="card">
          <div class="card-head"><h3>${escapeHtml(pp.file || `파일 ${i+1}`)}</h3></div>
          ${renderDiff(pp.unified_diff || '')}
        </div>`).join('')}
    `;
  }
  function wireBuild() {
    document.getElementById('btn-scan')?.addEventListener('click', async () => {
      toast('프로젝트 스캔 요청...');
      // workspace_path 는 알 수 없어 패스 (확장에서 호출 권장)
    });
    document.getElementById('btn-approve')?.addEventListener('click', async () => {
      const id = state.status?.patch_proposal?.proposal_id;
      if (!id) return;
      try {
        await api('/api/patch/approve', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ proposal_id: id }) });
        toast('패치 적용 요청 완료');
        await refreshAll();
      } catch (e) { toast('적용 실패: ' + e.message); }
    });
    document.getElementById('btn-reject')?.addEventListener('click', async () => {
      const id = state.status?.patch_proposal?.proposal_id;
      if (!id) return;
      try {
        await api('/api/patch/reject', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ proposal_id: id }) });
        toast('패치 거절됨');
        await refreshAll();
      } catch (e) { toast('거절 실패: ' + e.message); }
    });
  }

  // ── Ship tab ───────────────────────────────────────────────────────
  function renderShip() {
    const s = state.status || {};
    const ip = s.infra_proposal;
    if (!ip) {
      return `
        <div class="main-head">
          <div>
            <div class="title">Ship</div>
            <div class="sub">${chipsHtml()} <span class="dot-sep">·</span> 생성된 인프라 파일 없음</div>
          </div>
          <div class="actions">
            <button class="btn" id="btn-gen-dockerfile">Dockerfile</button>
            <button class="btn" id="btn-gen-compose">docker-compose</button>
            <button class="btn" id="btn-gen-gha">GitHub Actions</button>
          </div>
        </div>
        <div class="card">
          <div class="empty">

            <div class="title">인프라 파일을 한 번에 생성합니다</div>
            <div class="desc">상단 버튼으로 Dockerfile, docker-compose, GitHub Actions 워크플로를 생성합니다.<br/>
            생성된 파일은 보안 스캔(Trivy + Hadolint) 후 승인 단계로 이동합니다.</div>
          </div>
        </div>`;
    }
    const proj = state.project || {};
    const stack = proj.framework || proj.language || 'auto-detect';
    const port = proj.entry_port || ip.port || '8000';
    const health = ip.health_check_path || '/health';
    const fileLabel = ({
      'dockerfile': 'Dockerfile',
      'docker-compose': 'docker-compose.yml',
      'github-actions': '.github/workflows/deploy.yml',
    })[ip.file_type] || (ip.target_path || 'Dockerfile');
    return `
      <div class="main-head">
        <div>
          <div class="title">${escapeHtml(fileLabel.split('/').pop())} 생성 완료</div>
          <div class="sub">
            ${chipsHtml()} <span class="dot-sep">·</span>
            <span class="badge badge-stack">${escapeHtml(stack)}</span>
            <span class="dot-sep">·</span> 보안 스캔 대기 / 승인 대기
          </div>
        </div>
        <div class="actions">
          <button class="btn" id="btn-gen-dockerfile">Dockerfile</button>
          <button class="btn" id="btn-gen-compose">Compose</button>
          <button class="btn" id="btn-gen-gha">GitHub Actions</button>
          <button class="btn" id="btn-scan">보안 스캔</button>
          <button class="btn btn-primary" id="btn-approve-infra">승인 후 빌드</button>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3>Project Profile · 자동 감지</h3>
          <span class="badge badge-stack">${escapeHtml(stack)}</span>
        </div>
        <div class="card-body">
          <div class="profile-grid">
            <div class="col"><div class="label">Stack</div><div class="val accent">${escapeHtml(stack)}</div></div>
            <div class="col"><div class="label">Port</div><div class="val">${escapeHtml(port)}</div></div>
            <div class="col"><div class="label">Health Check</div><div class="val">GET ${escapeHtml(health)}</div></div>
            <div class="col"><div class="label">Approval</div><div class="val green">Level 1</div></div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3>생성된 ${escapeHtml(fileLabel.split('/').pop())}</h3>
          <span class="badge badge-pass" id="lines-badge">${(ip.content||'').split('\n').length} lines</span>
        </div>
        <pre class="code diff-add">${escapeHtml(ip.content || '')}</pre>
      </div>

      <div class="card">
        <div class="card-head">
          <h3>Trivy 보안 스캔 결과</h3>
          <span class="badge badge-pass" id="scan-badge">스캔 대기</span>
        </div>
        <div class="sev-grid" id="sev-grid">
          ${sevCell('crit',  0, 'CRITICAL')}
          ${sevCell('high',  0, 'HIGH')}
          ${sevCell('med',   0, 'MEDIUM')}
          ${sevCell('low',   0, 'LOW')}
        </div>
      </div>
    `;
  }
  function sevCell(cls, n, label) {
    return `<div class="sev-cell ${cls} ${n===0?'zero':''}"><div class="num">${n}</div><div class="lab">${label}</div></div>`;
  }
  function wireShip() {
    document.getElementById('btn-gen-dockerfile')?.addEventListener('click', () => generateInfra('dockerfile'));
    document.getElementById('btn-gen-compose')?.addEventListener('click', () => generateInfra('docker-compose'));
    document.getElementById('btn-gen-gha')?.addEventListener('click', () => generateInfra('github-actions'));
    document.getElementById('btn-approve-infra')?.addEventListener('click', approveInfra);
    document.getElementById('btn-scan')?.addEventListener('click', securityScan);
  }
  async function generateInfra(fileType) {
    toast(`${fileType} 생성 중...`);
    try {
      const proj = state.project || {};
      const body = { file_type: fileType, project_id: proj.project_id || '', workspace_path: proj.workspace_path || '' };
      await api('/api/infra/generate', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
      await refreshAll();
      toast('생성 완료');
    } catch (e) { toast('생성 실패: ' + e.message); }
  }
  async function approveInfra() {
    const id = state.status?.infra_proposal?.proposal_id;
    if (!id) return toast('인프라 제안이 없습니다');
    try {
      await api('/api/infra/approve', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ proposal_id: id }) });
      toast('승인 완료');
      await refreshAll();
    } catch (e) { toast('승인 실패: ' + e.message); }
  }
  async function securityScan() {
    const ip = state.status?.infra_proposal;
    if (!ip) return toast('Dockerfile 을 먼저 생성하세요');
    toast('보안 스캔 중...');
    try {
      const body = { image: 'recoder-app:latest', dockerfile_path: ip.target_path || '' };
      const r = await api('/api/security/scan', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
      applyScanResult(r || {});
      toast(r?.passed ? '스캔 통과' : '취약점 발견');
    } catch (e) { toast('스캔 실패: ' + e.message); }
  }
  function applyScanResult(r) {
    const trivy = r.results?.trivy || {};
    const c = trivy.critical_count ?? 0, h = trivy.high_count ?? 0, m = trivy.medium_count ?? 0, l = trivy.low_count ?? 5;
    const grid = document.getElementById('sev-grid');
    if (grid) grid.innerHTML =
      sevCell('crit', c, 'CRITICAL') + sevCell('high', h, 'HIGH') +
      sevCell('med',  m, 'MEDIUM')   + sevCell('low',  l, 'LOW');
    const badge = document.getElementById('scan-badge');
    if (badge) {
      badge.textContent = r.passed ? `PASS · ${(r.duration_ms||800)/1000}s` : 'FAIL';
      badge.className = 'badge ' + (r.passed ? 'badge-pass' : 'badge-fail');
    }
  }

  // ── Operate (placeholder) ──────────────────────────────────────────
  function renderOperate() {
    return `
      <div class="main-head"><div><div class="title">Operate</div>
        <div class="sub">AWS Deploy Ready 및 Ops Ready 설정 후 활성화됩니다</div></div></div>
      <div class="card"><div class="empty">

        <div class="title">아직 활성화되지 않았습니다</div>
        <div class="desc">AWS 자격증명을 ~/.aws/credentials 에 등록하면 Level 3/4 기능이 활성화됩니다.</div>
      </div></div>`;
  }

  // ── Diff render ────────────────────────────────────────────────────
  function renderDiff(diff) {
    if (!diff) return '<div class="card-body"><div class="empty"><div class="desc">diff 없음</div></div></div>';
    const lines = diff.split('\n');
    let addLn = 0, remLn = 0, ctxLn = 1;
    const m = diff.match(/@@ -(\d+)/); if (m) ctxLn = parseInt(m[1], 10);
    const rows = lines.map(line => {
      const c = line.charAt(0);
      if (c === '+') { addLn++; return `<tr class="diff-row add"><td class="ln">+${addLn}</td><td>${escapeHtml(line)}</td></tr>`; }
      if (c === '-') { remLn++; return `<tr class="diff-row rem"><td class="ln">-${remLn}</td><td>${escapeHtml(line)}</td></tr>`; }
      if (c === '@') { return `<tr class="diff-row info"><td class="ln"></td><td>${escapeHtml(line)}</td></tr>`; }
      return `<tr><td class="ln">${ctxLn++}</td><td>${escapeHtml(line)}</td></tr>`;
    }).join('');
    return `<table class="diff-table">${rows}</table>`;
  }

  // ── Chips & utility ───────────────────────────────────────────────
  function chipsHtml() {
    const r = state.ready || {};
    function chip(label, status) {
      const cls = status === 'ok' ? 'ok' : (status === 'partial' ? 'partial' : status === 'fail' ? 'fail' : '');
      const tick = status === 'ok' ? '✓' : (status === 'fail' ? '✗' : status === 'partial' ? '~' : '·');
      return `<span class="chip ${cls}"><span class="dot"></span>${label} ${tick}</span>`;
    }
    return `<span class="chips">${chip('Core', r.core_ready)}${chip('AI', r.ai_ready)}${chip('Docker', r.docker_ready)}</span>`;
  }
  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // ── Polling ────────────────────────────────────────────────────────
  async function refreshReady() { state.ready = await tryApi('/api/ready') || state.ready; }
  async function refreshStatus() { state.status = await tryApi('/api/status') || state.status; }
  async function refreshCost()   { state.cost   = await tryApi('/api/cost')   || state.cost; }
  async function refreshProject(){ state.project= await tryApi('/api/project')|| state.project; }
  async function refreshDeploy() { state.deploy = await tryApi('/api/deploy/status') || state.deploy; }

  function applyCost() {
    const c = state.cost || {};
    document.getElementById('cost-daily').textContent = `$${(c.daily||0).toFixed(3)}`;
    document.getElementById('cost-sub').textContent = `LLM 호출 ${c.calls||0}회 · ${(state.ready?.provider_type)||'Bedrock'}`;
  }
  function applyBrand() {
    const p = state.project || {};
    document.getElementById('brand-proj').textContent = p.project_id || p.name || 'recoder-demo';
  }

  async function refreshAll() {
    await Promise.all([refreshReady(), refreshStatus(), refreshCost(), refreshProject(), refreshDeploy()]);
    applyCost(); applyBrand();
    renderMain(); renderSteps();
  }

  // ── Wiring ─────────────────────────────────────────────────────────
  document.getElementById('btn-theme').addEventListener('click', () => {
    state.theme = state.theme === 'light' ? 'dark' : 'light';
    localStorage.setItem('rc.theme', state.theme);
    applyTheme();
  });
  document.getElementById('btn-toggle-side').addEventListener('click', () => {
    state.side = !state.side;
    localStorage.setItem('rc.side', state.side ? '1' : '0');
    applySide();
  });
  document.querySelectorAll('.mode-item').forEach(m => {
    m.addEventListener('click', () => {
      if (m.classList.contains('disabled')) return;
      setTab(m.dataset.tab);
    });
  });

  // ── Init ───────────────────────────────────────────────────────────
  applyTheme();
  applySide();
  setTab(state.tab);

  refreshAll();
  setInterval(refreshReady, 8000);
  setInterval(refreshStatus, 4000);
  setInterval(refreshCost, 6000);
  setInterval(refreshProject, 12000);
  setInterval(refreshDeploy, 4000);
  // 폴링 후 화면 갱신
  setInterval(() => { applyCost(); applyBrand(); renderMain(); renderSteps(); }, 4000);

})();
</script>
</body>
</html>
"""
