/**
 * Sidebar Webview Provider (설계서 v6.4 §12)
 * 3-Mode 탭 구조: Build | Ship | Operate
 * Extension ↔ Webview 메시지 브릿지
 *
 * 2026-05-08 갱신 (P0-7 / P0-8 / P0-13):
 * - server.py 응답 형식이 PatchProposal/InfraFileProposal 통째 반환으로 통일됨에 따라
 *   webview 의 파싱을 어댑터 한 군데로 정리.
 * - infra_approved 응답에 plan 이 함께 오므로 _currentDeployPlan 자동 세팅.
 * - /api/deploy/status polling, /api/security/scan, /api/ready 결선.
 * - Sidebar 상단 Ready 카드 (Core / AI / Docker 3-칩) 추가.
 */
import * as vscode from 'vscode';
import { CoreManager } from '../core/coreManager';
import { ContextCollector } from '../collectors/contextCollector';

const DEPLOY_STATUS_POLL_MS = 1500;

export class SidebarProvider implements vscode.WebviewViewProvider {
    private _view?: vscode.WebviewView;
    private _contextCollector: ContextCollector;
    private _statusPollTimer: ReturnType<typeof setInterval> | null = null;
    private _readyPollTimer: ReturnType<typeof setInterval> | null = null;
    private _deployPollTimer: ReturnType<typeof setInterval> | null = null;

    constructor(
        private readonly context: vscode.ExtensionContext,
        private readonly coreManager: CoreManager
    ) {
        this._contextCollector = new ContextCollector();
    }

    resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken
    ): void {
        this._view = webviewView;
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this.context.extensionUri]
        };
        webviewView.webview.html = getWebviewHtml();

        webviewView.webview.onDidReceiveMessage(async (msg) => {
            await this._handleMessage(msg);
        });

        webviewView.onDidDispose(() => this._stopAllPolling());

        this._startStatusPolling();
        this._startReadyPolling();
    }

    sendMessage(msg: object): void {
        this._view?.webview.postMessage(msg);
    }

    // ── Webview → Extension ────────────────────────────────────────

    private async _handleMessage(msg: { type: string; [key: string]: any }): Promise<void> {
        switch (msg.type) {
            case 'ready':
                await this._sendInitialState();
                break;

            case 'analyze':
                await this._handleAnalyze(msg);
                break;

            case 'approve_patch':
                await this._handleApprovePatch(msg);
                break;

            case 'reject_patch':
                await this._handleRejectPatch(msg);
                break;

            case 'generate_dockerfile':
                await this._handleGenerateInfra(msg);
                break;

            case 'approve_infra':
                await this._handleApproveInfra(msg);
                break;

            case 'deploy_local':
                await this._handleDeployLocal(msg);
                break;

            case 'run_security_scan':
                await this._handleSecurityScan(msg);
                break;

            case 'paste_error_log':
                this.sendMessage({ type: 'ready_for_analyze', output: msg.log });
                break;

            case 'scan_project':
                await this._handleScanProject();
                break;
        }
    }

    private async _handleAnalyze(msg: any): Promise<void> {
        try {
            await this.coreManager.ensureRunning();
            const ctx: any = this._contextCollector.collect();
            const proposal = await this.coreManager.client.analyze({
                workspace_path: ctx.workspace_path ?? '',
                terminal_output: msg.terminal_output ?? '',
                active_file_path: ctx.active_file_path ?? '',
                selected_text: ctx.selected_text ?? '',
                command: ctx.command ?? '',
                project_files_summary: ctx.project_files_summary ?? '',
                error_text: msg.error_text ?? '',
                file_context: ctx.file_context ?? '',
                related_files: ctx.related_files ?? [],
            });
            // 서버가 PatchProposal 을 통째 반환하므로 그대로 전달
            this.sendMessage({ type: 'analyze_result', data: proposal });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: e?.message ?? String(e) });
        }
    }

    private async _handleApprovePatch(msg: any): Promise<void> {
        try {
            const result = await this.coreManager.client.approvePatch(msg.proposal_id);
            this.sendMessage({ type: 'patch_result', data: result });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `패치 적용 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleRejectPatch(msg: any): Promise<void> {
        try {
            await this.coreManager.client.rejectPatch(msg.proposal_id);
            this.sendMessage({ type: 'patch_rejected' });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: e?.message ?? String(e) });
        }
    }

    private async _handleGenerateInfra(msg: any): Promise<void> {
        try {
            await this.coreManager.ensureRunning();
            const ctx: any = this._contextCollector.collect();
            const fileType = (msg.file_type ?? 'dockerfile').toLowerCase();
            const proposal = await this.coreManager.client.generateInfra(
                msg.project_id ?? '',
                fileType,
                ctx.workspace_path ?? '',
            );
            this.sendMessage({ type: 'dockerfile_result', data: proposal });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `Dockerfile 생성 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleApproveInfra(msg: any): Promise<void> {
        try {
            const result = await this.coreManager.client.approveInfra(msg.proposal_id);
            // result.plan 이 있으면 webview 가 _currentDeployPlan 으로 잡음
            this.sendMessage({ type: 'infra_approved', data: result });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `파일 저장 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleDeployLocal(msg: any): Promise<void> {
        try {
            const result = await this.coreManager.client.deployLocal({
                plan_id: msg.plan_id ?? '',
            });
            this.sendMessage({ type: 'deploy_started', data: result });
            this._startDeployPolling();
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `배포 시작 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleSecurityScan(msg: any): Promise<void> {
        try {
            const result = await this.coreManager.client.runSecurityScan(
                msg.image ?? 'recoder-app:latest',
                msg.dockerfile_path ?? '',
            );
            this.sendMessage({ type: 'security_scan_result', data: result });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `보안 스캔 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleScanProject(): Promise<void> {
        try {
            const ctx: any = this._contextCollector.collect();
            const result = await this.coreManager.client.scanProject(ctx.workspace_path ?? '');
            this.sendMessage({ type: 'project_scanned', data: result });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `프로젝트 스캔 실패: ${e?.message ?? e}` });
        }
    }

    // ── 초기 상태 / 폴링 ───────────────────────────────────────────

    private async _sendInitialState(): Promise<void> {
        try {
            const client = await this.coreManager.ensureRunning();
            const [status, cost, ready] = await Promise.all([
                client.getStatus().catch(() => null),
                client.getCost().catch(() => null),
                client.getReady().catch(() => null),
            ]);
            this.sendMessage({ type: 'initial_state', status, cost, ready });
        } catch {
            this.sendMessage({ type: 'core_offline' });
        }
    }

    private _startStatusPolling(): void {
        this._statusPollTimer = setInterval(async () => {
            try {
                const status = await this.coreManager.client.getStatus();
                const cost = await this.coreManager.client.getCost().catch(() => null);
                this.sendMessage({ type: 'status_update', status, cost });
            } catch { /* core offline */ }
        }, 4000);
    }

    private _startReadyPolling(): void {
        this._readyPollTimer = setInterval(async () => {
            try {
                const ready = await this.coreManager.client.getReady();
                this.sendMessage({ type: 'ready_update', ready });
            } catch { /* core offline */ }
        }, 8000);
    }

    private _startDeployPolling(): void {
        if (this._deployPollTimer) return;
        this._deployPollTimer = setInterval(async () => {
            try {
                const status = await this.coreManager.client.getDeployStatus();
                this.sendMessage({ type: 'deploy_status', data: status });
                if (status.finished) {
                    this._stopDeployPolling();
                }
            } catch { /* keep polling — core may be busy */ }
        }, DEPLOY_STATUS_POLL_MS);
    }

    private _stopDeployPolling(): void {
        if (this._deployPollTimer) {
            clearInterval(this._deployPollTimer);
            this._deployPollTimer = null;
        }
    }

    private _stopAllPolling(): void {
        if (this._statusPollTimer) clearInterval(this._statusPollTimer);
        if (this._readyPollTimer) clearInterval(this._readyPollTimer);
        this._stopDeployPolling();
        this._statusPollTimer = null;
        this._readyPollTimer = null;
    }
}

function getWebviewHtml(): string {
    return /* html */`<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ReCoder</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
      color: var(--vscode-foreground);
      background: var(--vscode-sideBar-background);
      height: 100vh;
      display: flex;
      flex-direction: column;
    }
    .ready-bar {
      display: flex;
      gap: 6px;
      padding: 6px 10px;
      border-bottom: 1px solid var(--vscode-panel-border);
      font-size: 11px;
    }
    .ready-chip {
      flex: 1;
      text-align: center;
      padding: 4px 6px;
      border-radius: 12px;
      font-weight: 600;
      background: var(--vscode-editor-background);
      border: 1px solid var(--vscode-panel-border);
    }
    .ready-chip.ok { background: #1b5e20; color: #a5d6a7; border-color: #1b5e20; }
    .ready-chip.partial { background: #4e342e; color: #ffcc80; border-color: #6d4c41; }
    .ready-chip.fail { background: #b71c1c; color: #ef9a9a; border-color: #b71c1c; }

    .tabs {
      display: flex;
      border-bottom: 1px solid var(--vscode-panel-border);
      background: var(--vscode-tab-inactiveBackground);
    }
    .tab {
      flex: 1;
      padding: 8px 4px;
      text-align: center;
      cursor: pointer;
      font-size: 12px;
      font-weight: 600;
      color: var(--vscode-tab-inactiveForeground);
      border-bottom: 2px solid transparent;
      transition: all 0.15s;
    }
    .tab.active {
      color: var(--vscode-tab-activeForeground);
      border-bottom-color: var(--vscode-focusBorder);
      background: var(--vscode-tab-activeBackground);
    }
    .tab.disabled { opacity: 0.4; cursor: not-allowed; }

    .content {
      flex: 1;
      overflow-y: auto;
      padding: 12px;
      display: none;
    }
    .content.active { display: block; }

    .btn {
      display: inline-block;
      padding: 6px 14px;
      border: none;
      border-radius: 4px;
      cursor: pointer;
      font-size: 12px;
      font-weight: 600;
      transition: opacity 0.15s;
    }
    .btn:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-primary {
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
    }
    .btn-primary:hover:not(:disabled) { background: var(--vscode-button-hoverBackground); }
    .btn-secondary {
      background: var(--vscode-button-secondaryBackground);
      color: var(--vscode-button-secondaryForeground);
    }

    .card {
      background: var(--vscode-editor-background);
      border: 1px solid var(--vscode-panel-border);
      border-radius: 6px;
      padding: 12px;
      margin-bottom: 10px;
    }
    .card-title {
      font-weight: 700;
      font-size: 13px;
      margin-bottom: 6px;
      color: var(--vscode-symbolIcon-variableForeground);
    }
    .card-body { font-size: 12px; line-height: 1.6; }

    .diff-block, .pre-block {
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 11px;
      background: var(--vscode-textCodeBlock-background);
      border-radius: 4px;
      padding: 8px;
      overflow-x: auto;
      white-space: pre;
      margin-top: 8px;
    }
    .diff-add { color: #4caf50; }
    .diff-remove { color: #f44336; }
    .diff-info { color: var(--vscode-descriptionForeground); }

    .paste-area {
      width: 100%;
      min-height: 80px;
      background: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      border: 1px solid var(--vscode-input-border);
      border-radius: 4px;
      padding: 8px;
      font-family: monospace;
      font-size: 11px;
      resize: vertical;
      margin-bottom: 8px;
    }

    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 10px;
      font-size: 11px;
      font-weight: 600;
    }
    .badge-ok { background: #1b5e20; color: #a5d6a7; }
    .badge-warn { background: #e65100; color: #ffcc80; }
    .badge-fail { background: #b71c1c; color: #ef9a9a; }

    .progress-wrap {
      width: 100%;
      height: 6px;
      background: var(--vscode-panel-border);
      border-radius: 3px;
      overflow: hidden;
      margin: 8px 0;
    }
    .progress-bar {
      height: 100%;
      background: var(--vscode-focusBorder);
      width: 0%;
      transition: width 0.3s;
    }

    .cost-bar {
      display: flex;
      gap: 12px;
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      padding: 6px 12px;
      border-top: 1px solid var(--vscode-panel-border);
    }
    .cost-bar span { font-weight: 600; color: var(--vscode-foreground); }

    .spinner {
      display: inline-block;
      width: 14px; height: 14px;
      border: 2px solid var(--vscode-focusBorder);
      border-top-color: transparent;
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
      vertical-align: middle;
      margin-right: 6px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    .section-label {
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--vscode-descriptionForeground);
      margin: 12px 0 6px;
    }
    .action-row {
      display: flex;
      gap: 8px;
      margin-top: 10px;
      flex-wrap: wrap;
    }
    .hidden { display: none !important; }
    .log-tail {
      max-height: 140px;
      overflow-y: auto;
    }
  </style>
</head>
<body>

<!-- Ready 카드: Core / AI / Docker -->
<div class="ready-bar">
  <div class="ready-chip" id="chip-core" title="Local Core 상태">Core ?</div>
  <div class="ready-chip" id="chip-ai" title="AI Provider Key 상태">AI ?</div>
  <div class="ready-chip" id="chip-docker" title="Docker daemon 상태">Docker ?</div>
</div>

<!-- 탭 헤더 -->
<div class="tabs">
  <div class="tab active" data-tab="build" onclick="switchTab('build')">⚒ Build</div>
  <div class="tab" data-tab="ship" onclick="switchTab('ship')">🚢 Ship</div>
  <div class="tab disabled" data-tab="operate" title="AWS Ready + Ops Ready 필요 (2학기)">⚙ Operate</div>
</div>

<!-- ───── BUILD 탭 ───── -->
<div class="content active" id="tab-build">
  <div class="section-label">에러 로그 붙여넣기</div>
  <textarea class="paste-area" id="paste-input" placeholder="터미널 에러 로그를 여기에 붙여넣으세요..."></textarea>
  <div class="action-row">
    <button class="btn btn-primary" onclick="analyzeLog()">🔍 분석</button>
    <button class="btn btn-secondary" onclick="autoCollect()">터미널 자동 수집</button>
  </div>

  <div id="analyzing-state" class="card hidden" style="margin-top:12px;">
    <div class="card-body"><span class="spinner"></span>AI 분석 중...</div>
  </div>

  <div id="patch-card" class="card hidden" style="margin-top:12px;">
    <div class="card-title">🛠 코드 수정안 <span id="patch-risk-badge" class="badge badge-ok">LOW</span></div>
    <div class="card-body" id="patch-summary"></div>
    <div class="diff-block" id="patch-diff"></div>
    <div class="action-row">
      <button class="btn btn-primary" id="btn-approve-patch" onclick="approvePatch()">✅ 승인 (Level 1)</button>
      <button class="btn btn-secondary" onclick="rejectPatch()">❌ 거절</button>
    </div>
  </div>

  <div id="no-error-state" class="card hidden" style="margin-top:12px;">
    <div class="card-body">✅ 에러가 감지되지 않았습니다.</div>
  </div>
</div>

<!-- ───── SHIP 탭 ───── -->
<div class="content" id="tab-ship">
  <div class="action-row" style="margin-bottom:10px;">
    <button class="btn btn-primary" onclick="generateDockerfile()">📄 Dockerfile 생성</button>
    <button class="btn btn-secondary" onclick="generateCompose()">🐳 Compose 생성</button>
  </div>

  <div id="dockerfile-card" class="card hidden">
    <div class="card-title">📄 <span id="infra-target">Dockerfile</span> Preview</div>
    <div class="pre-block" id="dockerfile-content"></div>
    <div id="scan-result" style="margin-top:8px;font-size:12px;"></div>
    <div class="action-row">
      <button class="btn btn-primary" id="btn-approve-infra" onclick="approveInfra()">✅ 저장 (Level 1)</button>
      <button class="btn btn-secondary" onclick="runSecurityScan()">🔒 보안 스캔</button>
    </div>
  </div>

  <div id="deploy-section" class="card hidden" style="margin-top:10px;">
    <div class="card-title">🚀 Docker 배포 <span class="badge badge-warn">Level 2</span></div>
    <div class="card-body" id="deploy-command-preview"></div>
    <div class="action-row">
      <button class="btn btn-primary" id="btn-deploy" onclick="deployLocal()">▶ 실행 (Level 2)</button>
    </div>
  </div>

  <div id="deploy-progress-card" class="card hidden" style="margin-top:10px;">
    <div class="card-title">진행 상황: <span id="deploy-stage">building</span></div>
    <div class="progress-wrap"><div class="progress-bar" id="deploy-progress-bar"></div></div>
    <div class="pre-block log-tail" id="deploy-log-tail" style="margin-top:6px;font-size:10px;"></div>
  </div>

  <div id="health-card" class="card hidden" style="margin-top:10px;">
    <div class="card-title">Health Check</div>
    <div class="card-body" id="health-result"></div>
  </div>
</div>

<!-- ───── OPERATE 탭 ───── -->
<div class="content" id="tab-operate">
  <div class="card">
    <div class="card-body" style="text-align:center;color:var(--vscode-descriptionForeground);padding:20px 0;">
      ⚙ 2학기 구현 예정<br>
      <small>AWS Deploy Ready + Ops Ready 충족 시 활성화</small>
    </div>
  </div>
</div>

<!-- 비용 표시 -->
<div class="cost-bar">
  오늘: <span id="cost-daily">$0.000</span> &nbsp;|&nbsp;
  이번달: <span id="cost-monthly">$0.000</span>
</div>

<script>
  const vscode = acquireVsCodeApi();
  let _currentPatchProposal = null;
  let _currentInfraProposal = null;
  let _currentDeployPlan = null;


  function switchTab(tab) {
    var tabEl = document.querySelector('[data-tab="' + tab + '"]');
    if (tabEl && tabEl.classList.contains('disabled')) return;
    document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
    document.querySelectorAll('.content').forEach(function(c) { c.classList.remove('active'); });
    if (tabEl) tabEl.classList.add('active');
    var c = document.getElementById('tab-' + tab);
    if (c) c.classList.add('active');
  }

  function analyzeLog() {
    var log = document.getElementById('paste-input').value.trim();
    if (!log) { showToast('⚠ 에러 로그를 붙여넣어 주세요.'); return; }
    show('analyzing-state');
    hide('patch-card'); hide('no-error-state');
    vscode.postMessage({ type: 'analyze', terminal_output: log });
  }

  function autoCollect() {
    vscode.postMessage({ type: 'paste_error_log', log: '' });
    showToast('터미널 자동 수집 요청 (개발 중)');
  }

  function approvePatch() {
    if (!_currentPatchProposal) return;
    vscode.postMessage({ type: 'approve_patch', proposal_id: _currentPatchProposal.proposal_id });
    hide('patch-card');
    showToast('패치 적용 중...');
  }

  function rejectPatch() {
    if (!_currentPatchProposal) return;
    vscode.postMessage({ type: 'reject_patch', proposal_id: _currentPatchProposal.proposal_id });
    hide('patch-card');
  }

  function generateDockerfile() {
    vscode.postMessage({ type: 'generate_dockerfile', file_type: 'dockerfile' });
    showToast('Dockerfile 생성 중...');
  }
  function generateCompose() {
    vscode.postMessage({ type: 'generate_dockerfile', file_type: 'docker-compose' });
    showToast('docker-compose.yml 생성 중...');
  }
  function approveInfra() {
    if (!_currentInfraProposal) return;
    vscode.postMessage({ type: 'approve_infra', proposal_id: _currentInfraProposal.proposal_id });
  }
  function runSecurityScan() {
    if (!_currentInfraProposal) {
      showToast('⚠ Dockerfile 을 먼저 생성하세요.');
      return;
    }
    showToast('보안 스캔 중...');
    vscode.postMessage({
      type: 'run_security_scan',
      image: 'recoder-app:latest',
      dockerfile_path: _currentInfraProposal.target_path || ''
    });
  }
  function deployLocal() {
    if (!_currentDeployPlan) {
      showToast('⚠ 배포 플랜이 없습니다.');
      return;
    }
    vscode.postMessage({ type: 'deploy_local', plan_id: _currentDeployPlan.plan_id });
    show('deploy-progress-card');
    setProgress('building', 10);
    showToast('배포 시작...');
  }

  window.addEventListener('message', function(e) {
    var msg = e.data;
    switch (msg.type) {
      case 'initial_state':
        updateCost(msg.cost);
        updateReady(msg.ready);
        break;
      case 'status_update':
        updateCost(msg.cost);
        break;
      case 'ready_update':
        updateReady(msg.ready);
        break;
      case 'analyze_result': {
        hide('analyzing-state');
        var p = msg.data;
        if (p && p.proposal_id && p.patches && p.patches.length > 0) {
          _currentPatchProposal = p;
          renderPatchProposal(p);
        } else {
          show('no-error-state');
        }
        break;
      }
      case 'patch_result':
        showToast('패치 적용 완료');
        break;
      case 'patch_rejected':
        showToast('패치 거절됨');
        break;
      case 'dockerfile_result': {
        var ip = msg.data;
        if (!ip || !ip.proposal_id) break;
        _currentInfraProposal = ip;
        document.getElementById('infra-target').textContent = ip.target_path || ip.file_type || 'Dockerfile';
        document.getElementById('dockerfile-content').textContent = ip.content || '';
        document.getElementById('scan-result').textContent = '';
        show('dockerfile-card');
        hide('deploy-section');
        hide('deploy-progress-card');
        hide('health-card');
        break;
      }
      case 'infra_approved': {
        var r = msg.data || {};
        showToast('파일 저장: ' + (r.saved_path || ''));
        if (r.plan) {
          _currentDeployPlan = r.plan;
          show('deploy-section');
          var dp = r.plan;
          var portTxt = (dp.ports && dp.ports[0]) ? (dp.ports[0].host + ':' + dp.ports[0].container) : '8000:8000';
          document.getElementById('deploy-command-preview').innerHTML =
            '<div><b>command:</b> docker build -t ' + escHtml(dp.image) + ' .</div>' +
            '<div><b>then:</b> docker run -d -p ' + portTxt + ' --name ' + escHtml(dp.container_name) + ' ' + escHtml(dp.image) + '</div>' +
            '<div><b>health:</b> ' + escHtml(dp.health_check_path || '/health') + '</div>';
        }
        break;
      }
      case 'security_scan_result': {
        renderScanResult(msg.data || {});
        break;
      }
      case 'deploy_started':
        show('deploy-progress-card');
        setProgress('building', 15);
        break;
      case 'deploy_status': {
        var s = msg.data || {};
        renderDeployStatus(s);
        if (s.finished) {
          show('health-card');
          var okText = s.health === true
            ? 'Health Check 통과! 컨테이너 실행 중.'
            : (s.error ? ('배포 실패: ' + s.error) : 'Health Check 미확인');
          document.getElementById('health-result').textContent = okText;
        }
        break;
      }
      case 'core_offline':
        updateReady({ core_ready: 'fail', ai_ready: 'fail', docker_ready: 'fail' });
        showToast('Core가 오프라인입니다.');
        break;
      case 'error':
        hide('analyzing-state');
        showToast('❌ ' + msg.message);
        break;
    }
  });

  function renderPatchProposal(p) {
    var risk = (p.risk_level || 'low').toLowerCase();
    var badgeClass = risk === 'low' ? 'badge-ok' : (risk === 'high' || risk === 'critical' ? 'badge-fail' : 'badge-warn');
    var badge = document.getElementById('patch-risk-badge');
    badge.className = 'badge ' + badgeClass;
    badge.textContent = (p.risk_level || 'low').toUpperCase();
    document.getElementById('patch-summary').textContent = p.summary || '';
    var diff = (p.patches && p.patches[0] && p.patches[0].unified_diff) || '';
    var diffEl = document.getElementById('patch-diff');
    diffEl.innerHTML = diff.split('\\n').map(function(line) {
      if (line.charAt(0) === '+') return '<span class="diff-add">' + escHtml(line) + '</span>';
      if (line.charAt(0) === '-') return '<span class="diff-remove">' + escHtml(line) + '</span>';
      if (line.charAt(0) === '@') return '<span class="diff-info">' + escHtml(line) + '</span>';
      return escHtml(line);
    }).join('\\n');
    show('patch-card');
  }

  function renderScanResult(r) {
    var el = document.getElementById('scan-result');
    if (!el) return;
    var trivy = r.results && r.results.trivy;
    var hadolint = r.results && r.results.hadolint;
    var parts = [];
    if (trivy) {
      var cls = trivy.passed ? 'badge-ok' : 'badge-fail';
      parts.push('<span class="badge ' + cls + '">Trivy: ' + (trivy.passed ? 'PASS' : 'FAIL') + '</span> ' +
        '<small>critical=' + (trivy.critical_count || 0) + ' high=' + (trivy.high_count || 0) + '</small>');
    }
    if (hadolint) {
      var cls2 = hadolint.passed ? 'badge-ok' : 'badge-warn';
      parts.push('<span class="badge ' + cls2 + '">Hadolint: ' + (hadolint.passed ? 'PASS' : 'WARN') + '</span> ' +
        '<small>' + escHtml(hadolint.summary || '') + '</small>');
    }
    if (!parts.length) parts.push('<small>스캔 결과 없음</small>');
    el.innerHTML = parts.join('<br>');
  }

  function renderDeployStatus(s) {
    setStage(s.stage);
    var pct = 10;
    if (s.stage === 'building') pct = 35;
    else if (s.stage === 'running') pct = 65;
    else if (s.stage === 'health') pct = 85;
    else if (s.stage === 'done') pct = 100;
    else if (s.stage === 'failed') pct = 100;
    var bar = document.getElementById('deploy-progress-bar');
    if (bar) {
      bar.style.width = pct + '%';
      if (s.stage === 'failed') bar.style.background = '#f44336';
    }
    var tail = document.getElementById('deploy-log-tail');
    if (tail && Array.isArray(s.log_tail)) {
      tail.textContent = s.log_tail.slice(-30).join('\\n');
      tail.scrollTop = tail.scrollHeight;
    }
  }

  function setProgress(stage, pct) {
    setStage(stage);
    var bar = document.getElementById('deploy-progress-bar');
    if (bar) bar.style.width = (pct || 10) + '%';
  }

  function setStage(stage) {
    var el = document.getElementById('deploy-stage');
    if (el) el.textContent = stage || '...';
  }

  function updateCost(cost) {
    if (!cost) return;
    var d = (cost.daily || 0).toFixed(3);
    var m = (cost.monthly || 0).toFixed(3);
    document.getElementById('cost-daily').textContent = '$' + d;
    document.getElementById('cost-monthly').textContent = '$' + m;
  }

  function updateReady(ready) {
    if (!ready) return;
    setChip('chip-core', 'Core', ready.core_ready);
    setChip('chip-ai', 'AI', ready.ai_ready);
    setChip('chip-docker', 'Docker', ready.docker_ready);
  }

  function setChip(id, label, status) {
    var el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('ok', 'partial', 'fail');
    var icon = '?';
    if (status === 'ok') { el.classList.add('ok'); icon = '✓'; }
    else if (status === 'partial') { el.classList.add('partial'); icon = '⚠'; }
    else if (status === 'fail') { el.classList.add('fail'); icon = '✗'; }
    el.textContent = label + ' ' + icon;
  }

  function show(id) { var e = document.getElementById(id); if (e) e.classList.remove('hidden'); }
  function hide(id) { var e = document.getElementById(id); if (e) e.classList.add('hidden'); }
  function escHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  var _toastTimer = null;
  function showToast(msg) {
    var t = document.getElementById('toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'toast';
      t.style.cssText = 'position:fixed;bottom:50px;left:50%;transform:translateX(-50%);' +
        'background:var(--vscode-notifications-background);color:var(--vscode-notifications-foreground);' +
        'padding:8px 16px;border-radius:4px;font-size:12px;z-index:9999;transition:opacity 0.3s;' +
        'box-shadow:0 2px 8px rgba(0,0,0,0.3);';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.opacity = '1';
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function() { t.style.opacity = '0'; }, 3000);
  }

  vscode.postMessage({ type: 'ready' });
</script>
</body>
</html>`;
}
