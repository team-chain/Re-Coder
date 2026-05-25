/**
 * ReCoder Workbench Panel
 *
 * 별도 VSCode WebviewPanel 로 열리는 대시보드.
 * 사이드바와는 다른 큰 화면 (탭 + 최근 활동 + 로그 + 빠른 작업).
 *
 * 사용:
 *   await vscode.commands.executeCommand('recoder.openWorkbench');
 *
 * 메시지 프로토콜:
 *   webview → extension:  { type, payload? }
 *     - 'wb.ready'            웹뷰 준비 완료, 초기 상태 요청
 *     - 'wb.analyze'          새 에러 분석 시작
 *     - 'wb.openSidebar'      사이드바 포커스
 *     - 'wb.poll.health'      Core 헬스 + 비용 즉시 갱신 요청
 *     - 'wb.tab'              { tab: 'command'|'error'|'github'|'deploy' }
 *   extension → webview:
 *     - 'wb.healthUpdate'    { ...CoreHealth }
 *     - 'wb.costUpdate'      { ...CostSummary }
 *     - 'wb.activity'        { items: ActivityItem[] }
 *     - 'wb.log'             { pane: 'ai'|'docker'|'github'|'deploy'|'health', line: string }
 */
import * as vscode from 'vscode';
import { CoreManager } from '../core/CoreManager';
import { ApiClient } from '../core/ApiClient';
import { PollingService } from '../core/PollingService';
import { renderWorkbenchHtml } from './workbenchHtml';

export class WorkbenchPanel {
    public static readonly viewType = 'recoder.workbench';
    private static _current: WorkbenchPanel | undefined;

    private readonly _panel: vscode.WebviewPanel;
    private _activity: { dot: string; text: string; time: string }[] = [];
    private _pollTimer: ReturnType<typeof setInterval> | null = null;
    private _diagnosticsInflight: boolean = false;
    private _diagnosticsLoaded: boolean = false;

    // ── Workbench bidirectional sync (Discord ↔ Core ↔ VSCode) ──────────
    /** /workbench/events cursor — index offset into Core's in-memory event buffer
     *  (NOT a timestamp). Each /events response carries next_offset which we use
     *  for the next call. Starts at 0 = "give me everything from the beginning". */
    private _workbenchEventsCursor: number = 0;
    private _workbenchPollTimer: ReturnType<typeof setInterval> | null = null;
    /** Current workbench mode (cached, last seen from Core). */
    private _workbenchMode: 'home' | 'build' | 'ship' | 'operate' | 'recover' = 'home';

    static createOrShow(
        extensionUri: vscode.Uri,
        apiClient: ApiClient,
        coreManager: CoreManager,
        polling: PollingService,
    ): WorkbenchPanel {
        const column = vscode.window.activeTextEditor?.viewColumn ?? vscode.ViewColumn.One;
        if (WorkbenchPanel._current) {
            WorkbenchPanel._current._panel.reveal(column);
            return WorkbenchPanel._current;
        }

        const panel = vscode.window.createWebviewPanel(
            WorkbenchPanel.viewType,
            'ReCoder Workbench',
            column,
            {
                enableScripts: true,
                retainContextWhenHidden: true,
                localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
            }
        );

        WorkbenchPanel._current = new WorkbenchPanel(panel, extensionUri, apiClient, coreManager, polling);
        return WorkbenchPanel._current;
    }

    private constructor(
        panel: vscode.WebviewPanel,
        private readonly _extensionUri: vscode.Uri,
        private readonly _apiClient: ApiClient,
        private readonly _coreManager: CoreManager,
        private readonly _polling: PollingService,
    ) {
        this._panel = panel;
        this._panel.webview.html = this._renderHtml(panel.webview);

        this._panel.onDidDispose(() => this._dispose(), null, []);
        this._panel.webview.onDidReceiveMessage(async (msg: { type: string; payload?: any }) => {
            await this._handleMessage(msg);
        });

        // 1초 후 자동으로 헬스/비용 1회 요청
        setTimeout(() => this._pushHealthAndCost().catch(() => {}), 800);
        this._startPolling();

        // Workbench 양방향 sync 폴링 — Discord 에서 한 액션을 3초 안에 VSCode 에 반영.
        setTimeout(() => this._pushWorkbenchState().catch(() => {}), 1200);
        this._startWorkbenchPolling();
    }

    public addActivity(dotClass: 'ok' | 'warn' | 'fail' | 'info', text: string): void {
        const item = { dot: dotClass, text, time: this._now() };
        this._activity.unshift(item);
        if (this._activity.length > 30) this._activity.pop();
        this._panel.webview.postMessage({ type: 'wb.activity', payload: { items: this._activity } });
    }

    public pushLog(pane: 'ai' | 'docker' | 'github' | 'deploy' | 'health', line: string): void {
        this._panel.webview.postMessage({ type: 'wb.log', payload: { pane, line } });
    }

    public pushDiagnostics(diag: import('../types').DiagnosticsResult): void {
        this._panel.webview.postMessage({ type: 'wb.diagnosticsUpdate', payload: diag });
    }

    // ──────────────────────────────────────────────────────────────────

    private async _handleMessage(msg: { type: string; payload?: any }): Promise<void> {
        switch (msg.type) {
            case 'wb.ready':
                await this._pushHealthAndCost();
                this._panel.webview.postMessage({ type: 'wb.activity', payload: { items: this._activity } });
                break;
            case 'wb.analyze':
                await vscode.commands.executeCommand('recoder.analyzeError');
                this.addActivity('info', '에러 분석 시작');
                break;
            case 'wb.openSidebar':
                await vscode.commands.executeCommand('recoder.sidebarView.focus');
                break;
            case 'wb.poll.health':
                await this._pushHealthAndCost();
                break;
            case 'wb.tab':
                // 클라이언트 측에서 탭 전환만 — 별도 처리 불필요
                break;
            case 'wb.generateDockerfile':
                await vscode.commands.executeCommand('recoder.generateDockerfile');
                this.addActivity('info', 'Dockerfile 생성 요청');
                break;
            case 'wb.runDiagnostics':
                await vscode.commands.executeCommand('recoder.runDiagnostics');
                this.addActivity('info', '진단 재실행');
                break;
            case 'wb.restartCore':
                await vscode.commands.executeCommand('recoder.restartCore');
                this.addActivity('warn', 'Core 재시작');
                break;
            // ── Workbench bidirectional sync (VSCode → Core → Discord) ───
            case 'wb.changeMode': {
                const mode = (msg.payload?.mode as string) || 'build';
                try {
                    await this._apiClient.workbenchChangeMode(
                        mode as 'build' | 'ship' | 'operate' | 'recover',
                        'vscode',
                    );
                    // 즉시 자기 자신도 갱신 (Core 응답 후 다음 polling tick 까지 안 기다림)
                    void this._pollWorkbenchEvents();
                } catch (err) {
                    this.addActivity('fail', `모드 전환 실패: ${err}`);
                }
                break;
            }
            default:
                console.warn('[WorkbenchPanel] Unknown message:', msg.type);
        }
    }

    private async _pushHealthAndCost(): Promise<void> {
        // 토큰이 빈 상태로 API 호출되어 401 누적되는 것을 막기 위해, 호출 직전
        // runtime.json 에서 토큰을 한 번 더 확인한다. (ApiClient 안에도 같은 가드가
        // 있지만, Core 가 막 부팅된 직후엔 in-memory 토큰이 비어있을 수 있어 이중 안전망.)
        try {
            if (!this._coreManager.getSessionToken()) {
                await this._coreManager.refreshToken();
            }
        } catch { /* ignore */ }

        try {
            const last = this._polling.getLastHealth();
            if (last) {
                this._panel.webview.postMessage({ type: 'wb.healthUpdate', payload: last });
            } else {
                void this._polling.poll();
            }
        } catch { /* ignore */ }
        try {
            const cost = await this._apiClient.getCostSummary();
            this._panel.webview.postMessage({ type: 'wb.costUpdate', payload: cost });
        } catch { /* ignore */ }

        // Diagnostics — chip-ai / chip-docker 색상은 DiagnosticsResult 의 ReadyState 에서 결정.
        // (1) 첫 호출엔 캐시 확인 → 없으면 runDiagnostics 1회 실행 → 캐싱.
        // (2) 이후 polling tick 에선 GET 만 호출 (저렴). runDiagnostics 는 중복 실행 방지 플래그로 차단.
        try {
            let diag = await this._apiClient.getDiagnostics();
            if (!diag && !this._diagnosticsInflight && !this._diagnosticsLoaded) {
                this._diagnosticsInflight = true;
                try {
                    diag = await this._apiClient.runDiagnostics();
                    this._diagnosticsLoaded = true;
                } finally {
                    this._diagnosticsInflight = false;
                }
            }
            if (diag) {
                this._panel.webview.postMessage({ type: 'wb.diagnosticsUpdate', payload: diag });
            }
        } catch { /* ignore */ }
    }

    private _startPolling(): void {
        if (this._pollTimer) return;
        this._pollTimer = setInterval(() => {
            void this._pushHealthAndCost();
        }, 5000);
    }

    // ─────────── Workbench bidirectional sync (Discord ↔ Core ↔ VSCode) ──
    //
    // 흐름:
    //   1) 패널 오픈 시 /workbench/state 1회 호출 → 현재 모드 + 최근 배포 webview 로 push.
    //   2) 3초 간격으로 /workbench/events?since=<cursor> 폴링.
    //   3) 새 이벤트마다 webview 에 'wb.workbenchEvent' postMessage → JS 가 배너 갱신 + 활동 추가.
    //   4) 이벤트 source='discord' 인 경우 활동 점이 파란색(info), 'vscode' 면 회색.
    //
    // 양방향성 (비대칭 design):
    //   - Discord → VSCode  : 본 polling 회로로 3초 안에 반영 (auto).
    //   - VSCode  → Core    : webview 액션이 workbenchChangeMode('vscode') 호출 → 즉시.
    //   - Core    → Discord : Discord embed 는 push 가 안되므로, 사용자가 다음
    //                        /recoder workbench 또는 embed 의 Refresh 버튼을
    //                        클릭할 때 fresh state 가 표시됨 (pull-on-demand).
    //
    private _startWorkbenchPolling(): void {
        if (this._workbenchPollTimer) return;
        this._workbenchPollTimer = setInterval(() => {
            void this._pollWorkbenchEvents();
        }, 3000);
    }

    /** /workbench/state 1회 호출 → 현재 모드 + 최근 배포 목록 push. */
    private async _pushWorkbenchState(): Promise<void> {
        try {
            const state = await this._apiClient.workbenchState();
            if (state && state.active_mode) {
                this._workbenchMode = state.active_mode;
                this._panel.webview.postMessage({
                    type: 'wb.workbenchState',
                    payload: state,
                });
            }
        } catch {
            // Core 가 /workbench/* 를 지원하지 않거나 토큰 미설정 — 조용히 무시
        }
    }

    /** /workbench/events 폴링 — Discord 또는 VSCode 가 일으킨 액션 반영.
     *  Core 의 _LAST_EVENTS 는 index 기반 cursor — `since` 는 timestamp 가 아님. */
    private async _pollWorkbenchEvents(): Promise<void> {
        try {
            const result = await this._apiClient.workbenchEvents(this._workbenchEventsCursor);
            // ApiClient 는 payload 를 `object` 로 정의하지만, 내부에서 안전하게
            // dict-like 으로 다루기 위해 한번 unknown 을 거쳐 Record 로 narrow.
            const rawEvents = (result && Array.isArray(result.events)) ? result.events : [];
            const events = rawEvents as unknown as Array<{
                at: string;
                kind: string;
                source: string;
                payload?: Record<string, unknown>;
            }>;

            // cursor 갱신: 응답의 next_offset 사용 (없으면 현재 + 받은 개수)
            const nextOffset = result?.next_offset;
            if (typeof nextOffset === 'number') {
                this._workbenchEventsCursor = nextOffset;
            } else {
                this._workbenchEventsCursor += events.length;
            }

            for (const ev of events) {
                this._renderWorkbenchEvent(ev);
            }
        } catch {
            // 무시 (Core 미가동/토큰 없음 등)
        }
    }

    /** 단일 workbench event → 활동 패널 + 배너 갱신.
     *  kind 는 Core route 의 _record_event 가 쏘는 값:
     *    'mode_change' / 'preflight' / 'deploy' / 'rollback' */
    private _renderWorkbenchEvent(ev: {
        at: string;
        kind: string;
        source: string;
        payload?: Record<string, unknown>;
    }): void {
        const sourceLabel = ev.source === 'discord' ? 'Discord' : ev.source === 'vscode' ? 'VSCode' : ev.source;
        let dot: 'ok' | 'warn' | 'fail' | 'info' = 'info';
        let text = '';

        switch (ev.kind) {
            case 'mode_change': {
                const mode = (ev.payload?.mode as string) || 'unknown';
                this._workbenchMode = mode as typeof this._workbenchMode;
                text = `${sourceLabel} → Workbench 모드 전환: ${mode.toUpperCase()}`;
                dot = 'info';
                break;
            }
            case 'preflight': {
                const status = (ev.payload?.status as string) || '';
                const ok = status === 'pass' || status === 'PASS' || ev.payload?.ok === true;
                text = `${sourceLabel} → Preflight 실행 (${ok ? 'PASS' : (status || 'BLOCKED')})`;
                dot = ok ? 'ok' : 'warn';
                break;
            }
            case 'deploy': {
                const id = (ev.payload?.deployment_id as string) || '?';
                text = `${sourceLabel} → 배포 시작 (${id.slice(0, 8)})`;
                dot = 'info';
                break;
            }
            case 'rollback': {
                const id = (ev.payload?.deployment_id as string) || '?';
                text = `${sourceLabel} → Rollback 트리거 (${id.slice(0, 8)})`;
                dot = 'warn';
                break;
            }
            default:
                text = `${sourceLabel} → ${ev.kind}`;
                dot = 'info';
        }

        // 활동 패널에 push (기존 addActivity 재사용)
        this.addActivity(dot, text);

        // 배너 갱신용 별도 메시지 — webview 가 상단 배너에 표시 가능
        this._panel.webview.postMessage({
            type: 'wb.workbenchEvent',
            payload: {
                at: ev.at,
                kind: ev.kind,
                source: ev.source,
                mode: this._workbenchMode,
                text,
            },
        });
    }

    private _now(): string {
        return new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }

    private _dispose(): void {
        if (this._pollTimer) clearInterval(this._pollTimer);
        this._pollTimer = null;
        if (this._workbenchPollTimer) clearInterval(this._workbenchPollTimer);
        this._workbenchPollTimer = null;
        WorkbenchPanel._current = undefined;
        this._panel.dispose();
    }

    private _renderHtml(webview: vscode.Webview): string {
        return renderWorkbenchHtml(webview, 'panel');
    }

    // Legacy inline HTML below is kept temporarily but no longer called.
    // Will be removed in a follow-up cleanup commit.
    private _renderHtmlLegacy(webview: vscode.Webview): string {
        const nonce = Array.from({ length: 24 }, () =>
            'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'[Math.floor(Math.random() * 62)],
        ).join('');
        const cspConnect = Array.from({ length: 17 }, (_, i) => `http://127.0.0.1:${17894 + i}`).join(' ');

        return `<!DOCTYPE html>
<html lang="ko">
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
/* GitHub silhouette path is meant to be filled, not stroked */
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
.log-tabs{display:flex; align-items:center; padding:10px 14px; border-bottom:1px solid var(--bd); gap:14px}
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

<!-- ── 인라인 SVG 아이콘 심볼 (한 번 정의 → 어디서나 <use> 로 참조) ── -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <defs>
    <!-- Command Center: lightning -->
    <symbol id="i-cmd" viewBox="0 0 24 24"><polygon points="13,2 4,14 11,14 9,22 20,10 13,10" /></symbol>
    <!-- Error Center: alert triangle -->
    <symbol id="i-err" viewBox="0 0 24 24"><path d="M12 3 L22 20 L2 20 Z"/><line x1="12" y1="10" x2="12" y2="14"/><circle cx="12" cy="17" r="0.8" fill="currentColor" stroke="none"/></symbol>
    <!-- GitHub: cat silhouette simplified -->
    <symbol id="i-gh" viewBox="0 0 24 24"><path d="M12 2 a10 10 0 0 0 -3.16 19.49 c.5 .09 .68 -.22 .68 -.48 v-1.7 c-2.78 .6 -3.37 -1.34 -3.37 -1.34 -.45 -1.15 -1.11 -1.46 -1.11 -1.46 -.91 -.62 .07 -.61 .07 -.61 1 .07 1.53 1.03 1.53 1.03 .89 1.53 2.34 1.09 2.91 .83 .09 -.65 .35 -1.09 .63 -1.34 -2.22 -.25 -4.55 -1.11 -4.55 -4.94 0 -1.09 .39 -1.98 1.03 -2.68 -.1 -.25 -.45 -1.27 .1 -2.64 0 0 .84 -.27 2.75 1.02 a9.5 9.5 0 0 1 5 0 c1.91 -1.29 2.75 -1.02 2.75 -1.02 .55 1.37 .2 2.39 .1 2.64 .64 .7 1.03 1.59 1.03 2.68 0 3.84 -2.34 4.69 -4.57 4.93 .36 .31 .68 .92 .68 1.85 v2.74 c0 .27 .18 .58 .69 .48 A10 10 0 0 0 12 2 Z" /></symbol>
    <!-- Deploy: upload cloud -->
    <symbol id="i-up" viewBox="0 0 24 24"><path d="M4 14 a4 4 0 1 1 1.5 -7.78 a5.5 5.5 0 0 1 10.6 1.78 a4 4 0 0 1 -.6 7.95"/><polyline points="9,15 12,12 15,15"/><line x1="12" y1="12" x2="12" y2="21"/></symbol>
    <!-- Clock (최근 활동) -->
    <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><polyline points="12,7 12,12 16,14"/></symbol>
    <!-- Bolt (빠른 작업) -->
    <symbol id="i-bolt" viewBox="0 0 24 24"><polygon points="13,2 4,14 11,14 9,22 20,10 13,10"/></symbol>
    <!-- Search (새 에러 분석) -->
    <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="20" y1="20" x2="16.5" y2="16.5"/></symbol>
    <!-- Package (Dockerfile) -->
    <symbol id="i-pkg" viewBox="0 0 24 24"><path d="M21 8.5 L12 13 L3 8.5 L12 4 Z"/><polyline points="3,8.5 3,16 12,20.5 21,16 21,8.5"/><line x1="12" y1="13" x2="12" y2="20.5"/></symbol>
    <!-- Cog (Actions) -->
    <symbol id="i-cog" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15 a1.65 1.65 0 0 0 .33 1.82 l.06 .06 a2 2 0 1 1 -2.83 2.83 l-.06 -.06 a1.65 1.65 0 0 0 -1.82 -.33 1.65 1.65 0 0 0 -1 1.51 V21 a2 2 0 0 1 -4 0 v-.09 A1.65 1.65 0 0 0 9 19.4 a1.65 1.65 0 0 0 -1.82 .33 l-.06 .06 a2 2 0 1 1 -2.83 -2.83 l.06 -.06 A1.65 1.65 0 0 0 4.6 15 a1.65 1.65 0 0 0 -1.51 -1 H3 a2 2 0 0 1 0 -4 h.09 A1.65 1.65 0 0 0 4.6 9 a1.65 1.65 0 0 0 -.33 -1.82 l-.06 -.06 a2 2 0 1 1 2.83 -2.83 l.06 .06 A1.65 1.65 0 0 0 9 4.6 a1.65 1.65 0 0 0 1 -1.51 V3 a2 2 0 0 1 4 0 v.09 A1.65 1.65 0 0 0 15 4.6 a1.65 1.65 0 0 0 1.82 -.33 l.06 -.06 a2 2 0 1 1 2.83 2.83 l-.06 .06 A1.65 1.65 0 0 0 19.4 9 a1.65 1.65 0 0 0 1.51 1 H21 a2 2 0 0 1 0 4 h-.09 a1.65 1.65 0 0 0 -1.51 1 Z"/></symbol>
    <!-- Heart (health check) -->
    <symbol id="i-heart" viewBox="0 0 24 24"><path d="M20.84 4.61 a5.5 5.5 0 0 0 -7.78 0 L12 5.67 l-1.06 -1.06 a5.5 5.5 0 0 0 -7.78 7.78 l1.06 1.06 L12 21.23 l7.78 -7.78 1.06 -1.06 a5.5 5.5 0 0 0 0 -7.78 Z"/></symbol>
    <!-- Dashboard grid -->
    <symbol id="i-dash" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></symbol>
    <!-- Logs (lines) -->
    <symbol id="i-log" viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></symbol>
  </defs>
</svg>

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
      <h2 id="greet-h">안녕하세요!</h2>
      <p>ReCoder가 개발부터 배포까지 여기에서 도와드립니다.</p>
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
  // CoreHealth.status ('ok'|'degraded'|'down') → chip 상태
  function healthToChip(status){
    if (status === 'ok')       return 'ok';
    if (status === 'degraded') return 'partial';
    if (status === 'down')     return 'fail';
    return '';
  }
  // DiagnosticsResult.<x>_ready ('ready'|'partial'|'not_ready'|'error') → chip 상태
  function readyToChip(state){
    if (state === 'ready')   return 'ok';
    if (state === 'partial') return 'partial';
    if (state === 'not_ready' || state === 'error') return 'fail';
    return '';
  }
  function now(){ return new Date().toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit'}); }

  // 로컬 탭 전환 (DOM 만 갱신, 메시지는 보내지 않음)
  function switchTab(name){
    if (!name) return;
    currentTab = name;
    document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active', x.dataset.page===currentTab));
    document.querySelectorAll('.page').forEach(p=>p.classList.toggle('active', p.id==='page-'+currentTab));
  }
  // 탭 헤더 클릭 → 탭 전환 + extension 알림
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

  // 카드 / 빠른 작업 클릭
  // 카드(에러/깃허브/배포)는 클릭 시 해당 탭으로 즉시 이동.
  function dispatchAction(name){
    switch(name){
      case 'analyze':    switchTab('error');  vscode.postMessage({ type:'wb.analyze' }); break;
      case 'error':      switchTab('error');  break;
      case 'dockerfile': switchTab('deploy'); vscode.postMessage({ type:'wb.generateDockerfile' }); break;
      case 'github':     switchTab('github'); vscode.postMessage({ type:'wb.tab', payload:{tab:'github'} }); break;
      case 'deploy':     switchTab('deploy'); vscode.postMessage({ type:'wb.tab', payload:{tab:'deploy'} }); break;
      case 'actions':    switchTab('github'); vscode.postMessage({ type:'wb.generateDockerfile' }); break; // alias
      case 'health':     vscode.postMessage({ type:'wb.runDiagnostics' }); break;
      case 'dashboard':  switchTab('command'); break;
      case 'logs':       /* no-op, 로그 패널은 항상 보임 */ break;
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

  // Extension -> Webview messages
  window.addEventListener('message', (e)=>{
    const m = e.data || {};
    switch(m.type){
      case 'wb.healthUpdate': {
        // CoreHealth.status -> chip-core
        const h = m.payload || {};
        setChip('chip-core', healthToChip(h.status));
        break;
      }
      case 'wb.diagnosticsUpdate': {
        // DiagnosticsResult -> chip-ai / chip-docker
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
    }
  });

  // ready signal
  vscode.postMessage({ type:'wb.ready' });
  setInterval(()=> vscode.postMessage({ type:'wb.poll.health' }), 5000);
})();
</script>
</body>
</html>`;
    }
}
