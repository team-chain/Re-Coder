/**
 * ReCoder Workbench Panel (v2 - dashboard layout)
 * Command Center / Error Center / GitHub Hub / Deploy Center
 * + bottom log panel tabs
 */
import * as vscode from 'vscode';
import * as crypto from 'crypto';

function getNonce(): string {
    return crypto.randomBytes(16).toString('base64');
}

export class WorkbenchPanel {
    public static currentPanel: WorkbenchPanel | undefined;
    private readonly _panel: vscode.WebviewPanel;
    private readonly _disposables: vscode.Disposable[] = [];
    private _onMessageFromWebview: ((msg: any) => Promise<void>) | null = null;

    static createOrShow(
        context: vscode.ExtensionContext,
        onMessageFromWebview: (msg: any) => Promise<void>
    ): WorkbenchPanel {
        const column = vscode.ViewColumn.One;

        if (WorkbenchPanel.currentPanel) {
            WorkbenchPanel.currentPanel._panel.reveal(column);
            WorkbenchPanel.currentPanel._onMessageFromWebview = onMessageFromWebview;
            return WorkbenchPanel.currentPanel;
        }

        const panel = vscode.window.createWebviewPanel(
            'recoderWorkbench',
            'ReCoder Workbench',
            column,
            {
                enableScripts: true,
                retainContextWhenHidden: true,
                localResourceRoots: [context.extensionUri],
            }
        );

        const instance = new WorkbenchPanel(panel, context, onMessageFromWebview);
        WorkbenchPanel.currentPanel = instance;
        return instance;
    }

    private constructor(
        panel: vscode.WebviewPanel,
        private readonly context: vscode.ExtensionContext,
        onMessageFromWebview: (msg: any) => Promise<void>
    ) {
        this._panel = panel;
        this._onMessageFromWebview = onMessageFromWebview;
        this._panel.webview.options = {
            enableScripts: true,
            localResourceRoots: [context.extensionUri],
        };
        this._panel.webview.html = this._getHtml(this._panel.webview);
        this._panel.webview.onDidReceiveMessage(
            async (msg) => { await this._onMessageFromWebview?.(msg); },
            null, this._disposables
        );
        this._panel.onDidDispose(() => this._dispose(), null, this._disposables);
    }

    sendMessage(msg: object): void {
        try {
            if (this._panel.visible) this._panel.webview.postMessage(msg);
        } catch (_) { /* panel disposed — 무시 */ }
    }

    private _dispose(): void {
        WorkbenchPanel.currentPanel = undefined;
        this._panel.dispose();
        while (this._disposables.length) this._disposables.pop()?.dispose();
    }

    private _getHtml(webview: vscode.Webview): string {
        const nonce = getNonce();
        const scriptUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this.context.extensionUri, 'media', 'workbench.js')
        );
        const csp = `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';`;
        return `<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReCoder Workbench</title>
<style>
:root{
  --bg0:#0d1117;--bg1:#161b22;--bg2:#21262d;--bg3:#30363d;
  --bd:#30363d;--bd2:#484f58;
  --t1:#e6edf3;--t2:#8b949e;--t3:#6e7681;
  --blue:#58a6ff;--blue-bg:rgba(88,166,255,.08);
  --green:#3fb950;--green-bg:rgba(63,185,80,.08);
  --red:#f85149;--red-bg:rgba(248,81,73,.08);
  --yellow:#d29922;--yellow-bg:rgba(210,153,34,.08);
  --purple:#bc8cff;
  --radius-sm:4px;--radius-md:6px;--radius-lg:10px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg0);color:var(--t1);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  font-size:13px;height:100vh;display:flex;flex-direction:column;overflow:hidden;
}

/* ── Top navigation ── */
.wb-header{
  display:flex;align-items:center;
  background:var(--bg1);border-bottom:1px solid var(--bd);
  padding:0;flex-shrink:0;
}
.wb-logo{
  display:flex;align-items:center;gap:8px;
  padding:0 16px;border-right:1px solid var(--bd);
  font-weight:700;font-size:14px;color:var(--blue);
  white-space:nowrap;flex-shrink:0;height:42px;
}
.wb-tabs{display:flex;flex:1;overflow-x:auto;height:42px}
.wb-tab{
  display:flex;align-items:center;gap:6px;
  padding:0 18px;cursor:pointer;border:none;background:transparent;
  color:var(--t2);font-size:13px;font-weight:500;
  border-bottom:2px solid transparent;white-space:nowrap;
  transition:color .15s,border-color .15s;height:42px;
}
.wb-tab:hover{color:var(--t1);background:rgba(255,255,255,.04)}
.wb-tab.active{color:var(--blue);border-bottom-color:var(--blue)}

/* ── Chip bar (header right) ── */
.wb-chips{
  display:flex;align-items:center;gap:5px;padding:0 14px;flex-shrink:0;
}
.wc{
  display:flex;align-items:center;gap:3px;padding:2px 7px;
  border-radius:var(--radius-lg);border:1px solid var(--bd);
  font-size:10px;font-weight:600;color:var(--t3);white-space:nowrap;
}
.wc .dot{width:5px;height:5px;border-radius:50%;background:var(--t3);flex-shrink:0}
.wc.ok{color:var(--green);border-color:rgba(63,185,80,.3)}.wc.ok .dot{background:var(--green)}
.wc.warn{color:var(--yellow);border-color:rgba(210,153,34,.3)}.wc.warn .dot{background:var(--yellow)}
.wc.fail{color:var(--red);border-color:rgba(248,81,73,.3)}.wc.fail .dot{background:var(--red)}

/* ── Cost display ── */
.wb-cost{
  padding:0 14px;border-left:1px solid var(--bd);height:42px;
  display:flex;align-items:center;gap:8px;flex-shrink:0;font-size:11px;color:var(--t2);
}
.wb-cost-val{color:var(--blue);font-weight:700;font-size:13px}

/* ── Main body ── */
.wb-body{flex:1;overflow:hidden;display:flex;flex-direction:column}
.wb-page{display:none;flex:1;overflow:hidden;flex-direction:column}
.wb-page.active{display:flex}
.page-scroll{flex:1;overflow-y:auto;padding:20px}

/* ── Bottom log panel ── */
.log-panel{
  flex-shrink:0;height:180px;background:var(--bg1);
  border-top:1px solid var(--bd);display:flex;flex-direction:column;
}
.log-tabs{
  display:flex;align-items:center;gap:0;
  background:var(--bg1);border-bottom:1px solid var(--bd);flex-shrink:0;
}
.log-tab{
  padding:5px 14px;font-size:11px;color:var(--t3);cursor:pointer;
  border-bottom:2px solid transparent;white-space:nowrap;
  transition:color .15s,border-color .15s;
}
.log-tab:hover{color:var(--t2)}
.log-tab.active{color:var(--blue);border-bottom-color:var(--blue)}
.log-clear{margin-left:auto;padding:3px 10px;font-size:10px;color:var(--t3);cursor:pointer;background:none;border:none}
.log-clear:hover{color:var(--t1)}
.log-content{flex:1;overflow-y:auto;padding:8px 14px;font-family:'Cascadia Code','SF Mono','Consolas',monospace;font-size:11px;line-height:1.7}
.log-pane{display:none}
.log-pane.active{display:block}
.log-line{color:var(--t2);padding:1px 0}
.log-line.ai{color:#79c0ff}
.log-line.ok{color:var(--green)}
.log-line.err{color:var(--red)}
.log-line.warn{color:var(--yellow)}

/* ── Cards ── */
.card{
  background:var(--bg1);border:1px solid var(--bd);border-radius:var(--radius-lg);
  padding:14px 16px;
}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.card-title{display:flex;align-items:center;gap:7px;font-weight:600;font-size:13px}
.card-title .icon{font-size:16px}

/* ── Command Center welcome ── */
.wb-welcome{
  background:linear-gradient(135deg,rgba(88,166,255,.07) 0%,rgba(63,185,80,.04) 100%);
  border:1px solid rgba(88,166,255,.2);border-radius:var(--radius-lg);
  padding:16px 20px;display:flex;align-items:center;justify-content:space-between;
}
.wb-welcome-text h2{font-size:16px;font-weight:700;color:var(--t1);margin-bottom:4px}
.wb-welcome-text p{font-size:12px;color:var(--t2)}
.wb-cost-summary{text-align:right;font-size:11px;color:var(--t2)}
.wb-cost-summary .big{font-size:20px;font-weight:700;color:var(--blue);display:block;margin-bottom:2px}

/* ── Status chips row ── */
.chips-row{display:flex;gap:8px;flex-wrap:wrap}
.status-chip{
  display:flex;align-items:center;gap:5px;padding:4px 10px;
  border-radius:var(--radius-lg);font-size:11px;font-weight:600;
  border:1px solid var(--bd);color:var(--t3);
}
.status-chip .dot{width:6px;height:6px;border-radius:50%;background:var(--t3)}
.status-chip.ok{color:var(--green);border-color:rgba(63,185,80,.3);background:var(--green-bg)}.status-chip.ok .dot{background:var(--green)}
.status-chip.warn{color:var(--yellow);border-color:rgba(210,153,34,.3)}.status-chip.warn .dot{background:var(--yellow)}
.status-chip.fail{color:var(--red);border-color:rgba(248,81,73,.3)}.status-chip.fail .dot{background:var(--red)}

/* ── Feature nav cards ── */
.feature-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
@media(max-width:700px){.feature-cards{grid-template-columns:1fr}}
.feature-card{
  border-radius:var(--radius-lg);padding:16px;cursor:pointer;
  border:1px solid var(--bd);background:var(--bg1);
  transition:all .18s;position:relative;overflow:hidden;
}
.feature-card:hover{border-color:var(--bd2);background:var(--bg2);transform:translateY(-2px)}
.feature-card.red{border-color:rgba(248,81,73,.25);background:rgba(248,81,73,.04)}
.feature-card.red:hover{border-color:rgba(248,81,73,.5)}
.feature-card.blue{border-color:rgba(88,166,255,.25);background:rgba(88,166,255,.04)}
.feature-card.blue:hover{border-color:rgba(88,166,255,.5)}
.feature-card.green{border-color:rgba(63,185,80,.25);background:rgba(63,185,80,.04)}
.feature-card.green:hover{border-color:rgba(63,185,80,.5)}
.fc-icon{font-size:24px;margin-bottom:10px}
.fc-title{font-size:14px;font-weight:700;margin-bottom:6px}
.fc-desc{font-size:11px;color:var(--t2);line-height:1.5;margin-bottom:12px}
.fc-meta{font-size:11px;color:var(--t3);margin-bottom:10px;min-height:16px}
.fc-btn{
  display:inline-flex;align-items:center;gap:5px;
  padding:5px 12px;border-radius:var(--radius-sm);
  border:none;font-size:11px;font-weight:600;cursor:pointer;transition:all .15s;
}
.fc-btn.red{background:rgba(248,81,73,.15);color:var(--red)}
.fc-btn.blue{background:rgba(88,166,255,.15);color:var(--blue)}
.fc-btn.green{background:rgba(63,185,80,.15);color:var(--green)}
.fc-btn:hover{opacity:.85}
.fc-badge{
  position:absolute;top:12px;right:12px;
  padding:2px 7px;border-radius:var(--radius-sm);
  font-size:10px;font-weight:700;
}
.fc-badge.red{background:var(--red-bg);color:var(--red)}

/* ── 2-col activity+actions ── */
.cmd-bottom{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:600px){.cmd-bottom{grid-template-columns:1fr}}

.activity-item{
  display:flex;align-items:center;gap:8px;
  padding:6px 0;border-bottom:1px solid var(--bd);font-size:12px;color:var(--t2);
}
.activity-item:last-child{border-bottom:none}
.activity-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.activity-dot.ok{background:var(--green)}
.activity-dot.err{background:var(--red)}
.activity-dot.blue{background:var(--blue)}
.activity-time{margin-left:auto;font-size:10px;color:var(--t3);white-space:nowrap}

.quick-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.qbtn{
  display:flex;align-items:center;gap:6px;
  padding:8px 10px;border-radius:var(--radius-md);
  border:1px solid var(--bd);background:var(--bg2);color:var(--t1);
  font-size:11px;cursor:pointer;transition:all .15s;
}
.qbtn:hover{background:var(--bg3);border-color:var(--bd2)}

/* ── Reusable ── */
.sec-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--t3);margin-bottom:8px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:5px;padding:7px 14px;border-radius:var(--radius-md);border:1px solid var(--bd);background:var(--bg2);color:var(--t1);font-size:12px;font-weight:500;cursor:pointer;transition:all .15s;white-space:nowrap}
.btn:hover{background:var(--bg3)}.btn:active{transform:scale(.97)}.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-primary{background:var(--blue-bg);border-color:rgba(88,166,255,.4);color:var(--blue)}.btn-primary:hover{background:rgba(88,166,255,.15)}
.btn-green{background:var(--green-bg);border-color:rgba(63,185,80,.4);color:var(--green)}.btn-green:hover{background:rgba(63,185,80,.2)}
.btn-danger{background:var(--red-bg);border-color:rgba(248,81,73,.3);color:var(--red)}.btn-danger:hover{background:rgba(248,81,73,.15)}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.badge{padding:2px 7px;border-radius:var(--radius-sm);font-size:10px;font-weight:600}
.badge-ok{background:var(--green-bg);color:var(--green)}
.badge-warn{background:var(--yellow-bg);color:var(--yellow)}
.badge-info{background:var(--blue-bg);color:var(--blue)}
.badge-err{background:var(--red-bg);color:var(--red)}
.pre-block{background:var(--bg0);border:1px solid var(--bd);border-radius:var(--radius-md);padding:10px 12px;font-family:'Cascadia Code','Consolas',monospace;font-size:11px;line-height:1.6;white-space:pre-wrap;word-break:break-all;max-height:260px;overflow-y:auto;color:var(--t2)}
input[type=text],input[type=password],textarea{background:var(--bg2);color:var(--t1);border:1px solid var(--bd);border-radius:var(--radius-sm);padding:7px 10px;font-size:12px;outline:none;transition:border-color .15s;font-family:inherit}
input:focus,textarea:focus{border-color:var(--blue)}
input::placeholder,textarea::placeholder{color:var(--t3)}
textarea{resize:vertical;line-height:1.5}
label{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--t2);cursor:pointer}
.spinner{width:12px;height:12px;border:2px solid var(--bd2);border-top-color:var(--blue);border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.stage-bar{display:flex;align-items:center;gap:0;background:var(--bg1);border:1px solid var(--bd);border-radius:var(--radius-lg);padding:8px 14px;overflow:hidden}
.stage-item{display:flex;align-items:center;gap:5px;flex:1;font-size:10px;color:var(--t3);font-weight:500;transition:color .2s}
.stage-item+.stage-item::before{content:'>';color:var(--t3);margin-right:6px;font-size:13px}
.stage-dot{width:7px;height:7px;border-radius:50%;background:var(--bg3);border:1px solid var(--bd2);transition:all .2s}
.stage-item.done .stage-dot{background:var(--green);border-color:var(--green)}.stage-item.done{color:var(--green)}
.stage-item.active .stage-dot{background:var(--blue);border-color:var(--blue);box-shadow:0 0 0 2px var(--blue-bg)}.stage-item.active{color:var(--blue)}
.file-tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}
.file-tab{display:flex;align-items:center;gap:4px;padding:3px 8px;border-radius:var(--radius-sm);border:1px solid var(--bd);background:var(--bg2);font-size:10px;cursor:pointer;color:var(--t2)}
.file-tab.active{border-color:var(--blue);background:var(--blue-bg);color:var(--blue)}
.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 20px;text-align:center;gap:12px;color:var(--t3)}
.empty-icon{font-size:36px;opacity:.6}
.git-card{background:var(--bg1);border:1px solid var(--bd);border-radius:var(--radius-lg);overflow:hidden}
.git-main{display:flex;align-items:center;gap:10px;padding:12px 14px;flex-wrap:wrap}
.git-acct{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--t3);padding:3px 8px;border-radius:var(--radius-lg);border:1px solid var(--bd);background:var(--bg2)}
.git-acct.ok{color:var(--green);border-color:rgba(63,185,80,.3);background:var(--green-bg)}
.git-branch-pill{display:flex;align-items:center;gap:5px;font-size:11px;padding:4px 10px;border-radius:var(--radius-lg);border:1px solid rgba(88,166,255,.35);background:var(--blue-bg);color:var(--blue);cursor:pointer;transition:all .15s}
.git-branch-pill:hover{background:rgba(88,166,255,.15)}
.git-chip{padding:2px 7px;border-radius:var(--radius-sm);font-size:10px;font-weight:600}
.git-chip-changed{background:var(--yellow-bg);color:var(--yellow)}
.git-chip-ahead{background:var(--blue-bg);color:var(--blue)}
.git-actions{display:flex;gap:8px;padding:0 14px 12px}
.git-actions button{flex:1}
.git-dropdown{display:none;border-top:1px solid var(--bd);padding:8px 10px}
.git-dropdown.open{display:block}
.gd-section{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--t3);margin-bottom:4px;padding:0 4px}
.gd-branch{display:flex;align-items:center;gap:6px;padding:5px 8px;border-radius:var(--radius-sm);font-size:11px;cursor:pointer;transition:background .1s}
.gd-branch:hover{background:var(--bg2)}.gd-branch.current{color:var(--green);font-weight:600}
.gd-dot{width:6px;height:6px;border-radius:50%;background:var(--bd2);flex-shrink:0}.gd-dot.current{background:var(--green)}
.gd-remote-tag{padding:1px 5px;border-radius:3px;font-size:9px;background:var(--bg3);color:var(--t3);margin-left:auto}
.gd-new-row{display:flex;gap:6px;margin-top:8px;padding:0 2px}
.gd-new-input{flex:1;padding:4px 8px;font-size:11px}
.gd-new-btn{padding:4px 10px;font-size:11px;border-radius:var(--radius-sm);border:1px solid rgba(88,166,255,.4);color:var(--blue);background:var(--blue-bg);cursor:pointer;white-space:nowrap}
.gd-new-btn:hover{background:rgba(88,166,255,.2)}
.git-commit-panel{display:none;border-top:1px solid var(--bd);padding:12px 14px}
.git-commit-panel.open{display:block}
.git-commit-input{width:100%;margin-bottom:8px;padding:7px 10px;min-height:50px}
.git-commit-btns{display:flex;gap:8px}.git-commit-btns button{flex:1}
.git-status-line{margin-top:6px;font-size:10px;color:var(--t3);min-height:14px}
.wb-cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:700px){.wb-cols{grid-template-columns:1fr}}
.deploy-steps{display:flex;flex-direction:column;gap:6px;margin-top:10px}
.deploy-step{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--t2)}
.ds-dot{width:8px;height:8px;border-radius:50%;background:var(--bg3);border:1px solid var(--bd2)}
.deploy-step.done .ds-dot{background:var(--green)}.deploy-step.running .ds-dot{background:var(--blue);animation:pulse .8s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.progress-wrap{height:3px;background:var(--bg3);border-radius:2px;overflow:hidden;margin:8px 0}
.progress-bar{height:100%;background:var(--blue);border-radius:2px;transition:width .4s}
.ship-step{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--bd);font-size:11px;color:var(--t2)}
.ship-step:last-child{border-bottom:none}
.gh-section{display:flex;flex-direction:column;gap:10px}
#wb-toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(8px);background:var(--bg2);color:var(--t1);padding:8px 16px;border-radius:var(--radius-lg);font-size:12px;font-weight:500;z-index:9999;opacity:0;transition:opacity .25s,transform .25s;border:1px solid var(--bd2);box-shadow:0 4px 16px rgba(0,0,0,.5);pointer-events:none;white-space:nowrap}
#wb-toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.hidden{display:none!important}

.ic{display:inline-block;width:13px;height:13px;vertical-align:middle;flex-shrink:0;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.ic-fill{fill:currentColor;stroke:none}
.ic-sm{width:11px;height:11px}
.ic-md{width:15px;height:15px}
.ic-lg{width:18px;height:18px}
.ic-fc{width:28px;height:28px;stroke-width:1.4}
</style>
</head>
<body>
<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">
  <symbol id="ic-zap" viewBox="0 0 16 16"><path d="M9 2L4 9h4l-1 5 5-7H8z"/></symbol>
  <symbol id="ic-home" viewBox="0 0 16 16"><path d="M2 8L8 2l6 6"/><path d="M4 8v6h3v-3h2v3h3V8"/></symbol>
  <symbol id="ic-alert" viewBox="0 0 16 16"><path d="M8 2L1 14h14L8 2z"/><path d="M8 7v3M8 11.5v.5"/></symbol>
  <symbol id="ic-github" viewBox="0 0 16 16"><path d="M8 1a7 7 0 0 0-2.21 13.63c.35.06.48-.15.48-.34v-1.2c-1.94.42-2.35-.94-2.35-.94-.32-.81-.78-1.03-.78-1.03-.64-.43.05-.42.05-.42.7.05 1.07.72 1.07.72.62 1.07 1.63.76 2.03.58.06-.45.24-.76.44-.93-1.55-.18-3.18-.77-3.18-3.44 0-.76.27-1.38.72-1.87-.07-.18-.31-.88.07-1.84 0 0 .58-.19 1.9.71A6.6 6.6 0 0 1 8 4.8c.59 0 1.18.08 1.73.23 1.32-.9 1.9-.71 1.9-.71.38.96.14 1.66.07 1.84.45.49.72 1.11.72 1.87 0 2.68-1.63 3.26-3.19 3.44.25.22.47.64.47 1.29v1.91c0 .19.13.41.48.34A7 7 0 0 0 8 1z"/></symbol>
  <symbol id="ic-deploy" viewBox="0 0 16 16"><path d="M8 2v8M5 7l3-3 3 3"/><path d="M3 12h10"/></symbol>
  <symbol id="ic-clock" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M8 5v3l2 2"/></symbol>
  <symbol id="ic-search" viewBox="0 0 16 16"><circle cx="7" cy="7" r="4"/><path d="M10 10l3 3"/></symbol>
  <symbol id="ic-file" viewBox="0 0 16 16"><path d="M4 2h6l3 3v9H4V2z"/><path d="M10 2v3h3"/></symbol>
  <symbol id="ic-heart" viewBox="0 0 16 16"><path d="M8 13C8 13 2 9 2 5.5A3.5 3.5 0 0 1 8 4a3.5 3.5 0 0 1 6 1.5C14 9 8 13 8 13z"/></symbol>
  <symbol id="ic-chart" viewBox="0 0 16 16"><rect x="2" y="10" width="3" height="4"/><rect x="6.5" y="6" width="3" height="8"/><rect x="11" y="3" width="3" height="11"/></symbol>
  <symbol id="ic-log" viewBox="0 0 16 16"><path d="M4 2h8a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z"/><path d="M5 6h6M5 9h4"/></symbol>
  <symbol id="ic-person" viewBox="0 0 16 16"><circle cx="8" cy="5" r="3"/><path d="M2 14c0-3.3 2.7-6 6-6s6 2.7 6 6"/></symbol>
  <symbol id="ic-branch" viewBox="0 0 16 16"><circle cx="5" cy="4" r="1.5"/><circle cx="5" cy="12" r="1.5"/><circle cx="11" cy="6" r="1.5"/><path d="M5 5.5v5M5 5.5C5 8 11 8 11 7.5"/></symbol>
  <symbol id="ic-commit" viewBox="0 0 16 16"><circle cx="8" cy="8" r="2.5"/><path d="M2 8h3.5M10.5 8H14"/></symbol>
  <symbol id="ic-push" viewBox="0 0 16 16"><path d="M8 11V3M5 6l3-3 3 3"/><path d="M3 13h10"/></symbol>
  <symbol id="ic-refresh" viewBox="0 0 16 16"><path d="M13 8A5 5 0 1 1 8 3"/><path d="M8 1v4h4"/></symbol>
  <symbol id="ic-link" viewBox="0 0 16 16"><path d="M7 4H4a2 2 0 0 0 0 4h3M9 4h3a2 2 0 0 1 0 4H9M6 8h4"/></symbol>
  <symbol id="ic-clipboard" viewBox="0 0 16 16"><path d="M6 2H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V3a1 1 0 0 0-1-1h-2"/><rect x="5" y="1" width="6" height="2" rx="1"/></symbol>
  <symbol id="ic-lock" viewBox="0 0 16 16"><rect x="3" y="7" width="10" height="7" rx="1"/><path d="M5 7V5a3 3 0 0 1 6 0v2"/></symbol>
  <symbol id="ic-docker" viewBox="0 0 16 16"><rect x="2" y="8" width="3" height="3" rx=".5"/><rect x="6" y="8" width="3" height="3" rx=".5"/><rect x="10" y="8" width="3" height="3" rx=".5"/><rect x="6" y="4" width="3" height="3" rx=".5"/><rect x="10" y="4" width="3" height="3" rx=".5"/><path d="M2 11c0 1.5 1 2.5 2.5 2.5h7"/></symbol>
  <symbol id="ic-patch" viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10" rx="2"/><path d="M8 6v4M6 8h4"/></symbol>
  <symbol id="ic-trash" viewBox="0 0 16 16"><path d="M3 5h10M6 5V3h4v2M5 5v8a1 1 0 0 0 1 1h4a1 1 0 0 0 1-1V5"/></symbol>
  <symbol id="ic-check" viewBox="0 0 16 16"><path d="M3 8l3.5 3.5L13 5"/></symbol>
  <symbol id="ic-x" viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8"/></symbol>
</svg>

<!-- Navigation header -->
<div class="wb-header">
  <div class="wb-logo"><svg class="ic ic-md"><use href="#ic-zap"/></svg> ReCoder</div>
  <div class="wb-tabs">
    <button class="wb-tab active" data-page="command"><svg class="ic ic-sm"><use href="#ic-home"/></svg> Command Center</button>
    <button class="wb-tab" data-page="error"><svg class="ic ic-sm"><use href="#ic-alert"/></svg> Error Center</button>
    <button class="wb-tab" data-page="github"><svg class="ic ic-sm"><use href="#ic-github"/></svg> GitHub Hub</button>
    <button class="wb-tab" data-page="deploy"><svg class="ic ic-sm"><use href="#ic-deploy"/></svg> Deploy Center</button>
  </div>
  <div class="wb-chips">
    <div class="wc" id="wb-chip-core"><span class="dot"></span>Core</div>
    <div class="wc" id="wb-chip-ai"><span class="dot"></span>AI</div>
    <div class="wc" id="wb-chip-docker"><span class="dot"></span>Docker</div>
    <div class="wc" id="wb-chip-github"><span class="dot"></span>GitHub</div>
  </div>
  <div class="wb-cost">
    <span id="wb-cost-label" style="font-size:10px">오늘 사용</span>
    <span class="wb-cost-val" id="wb-cost-val">$—</span>
  </div>
</div>

<!-- ══════════════════════════════════════════════
     PAGE: COMMAND CENTER
════════════════════════════════════════════════ -->
<div class="wb-page active" id="page-command">
  <div class="page-scroll" style="display:flex;flex-direction:column;gap:16px">

    <!-- Welcome card -->
    <div class="wb-welcome">
      <div class="wb-welcome-text">
        <h2 id="cmd-greeting">안녕하세요!</h2>
        <p>ReCoder가 개발부터 배포까지 여기에서 도와드립니다.</p>
      </div>
      <div class="wb-cost-summary">
        <span class="big" id="cmd-cost-big">$—</span>
        <span id="cmd-cost-sub" style="font-size:10px">/ $3.00 한도</span>
      </div>
    </div>

    <!-- Status chips -->
    <div class="chips-row" id="cmd-chips-row">
      <div class="status-chip" id="sc-core"><span class="dot"></span>Core</div>
      <div class="status-chip" id="sc-ai"><span class="dot"></span>AI</div>
      <div class="status-chip" id="sc-docker"><span class="dot"></span>Docker</div>
      <div class="status-chip" id="sc-github"><span class="dot"></span>GitHub</div>
      <div class="status-chip" id="sc-aws"><span class="dot"></span>AWS</div>
    </div>

    <!-- Feature navigation cards -->
    <div class="feature-cards">
      <!-- Error Center card -->
      <div class="feature-card red" id="fc-card-error">
        <span class="fc-badge red hidden" id="fc-error-badge">1</span>
        <div class="fc-icon"><svg class="ic ic-fc"><use href="#ic-alert"/></svg></div>
        <div class="fc-title" style="color:var(--red)">에러 감지</div>
        <div class="fc-desc" id="fc-error-desc">감지된 오류 없음</div>
        <div class="fc-meta" id="fc-error-meta"></div>
        <button class="fc-btn red" id="fc-btn-error">분석 시작 →</button>
      </div>
      <!-- GitHub Hub card -->
      <div class="feature-card blue" id="fc-card-github">
        <div class="fc-icon"><svg class="ic ic-fc"><use href="#ic-github"/></svg></div>
        <div class="fc-title" style="color:var(--blue)">GitHub Hub</div>
        <div class="fc-desc" id="fc-github-desc">연결된 저장소 없음</div>
        <div class="fc-meta" id="fc-github-meta"></div>
        <button class="fc-btn blue" id="fc-btn-github">열기 →</button>
      </div>
      <!-- Deploy Center card -->
      <div class="feature-card green" id="fc-card-deploy">
        <div class="fc-icon"><svg class="ic ic-fc"><use href="#ic-deploy"/></svg></div>
        <div class="fc-title" style="color:var(--green)">배포 센터</div>
        <div class="fc-desc" id="fc-deploy-desc">배포 현황 없음</div>
        <div class="fc-meta" id="fc-deploy-meta"></div>
        <button class="fc-btn green" id="fc-btn-deploy">배포 센터 열기 →</button>
      </div>
    </div>

    <!-- Recent activity + Quick actions -->
    <div class="cmd-bottom">
      <!-- Recent activity -->
      <div class="card">
        <div class="card-header">
          <div class="card-title"><span class="icon"><svg class="ic ic-sm"><use href="#ic-clock"/></svg></span> 최근 활동</div>
        </div>
        <div id="activity-list">
          <div class="activity-item">
            <div class="activity-dot" style="background:var(--t3)"></div>
            <span style="color:var(--t3)">활동 이력 없음</span>
          </div>
        </div>
      </div>
      <!-- Quick actions -->
      <div class="card">
        <div class="card-header">
          <div class="card-title"><span class="icon"><svg class="ic ic-sm"><use href="#ic-zap"/></svg></span> 빠른 작업</div>
        </div>
        <div class="quick-grid">
          <button class="qbtn" id="cmd-qbtn-error"><svg class="ic ic-sm"><use href="#ic-search"/></svg> 새 에러 분석</button>
          <button class="qbtn" id="cmd-qbtn-dockerfile"><svg class="ic ic-sm"><use href="#ic-file"/></svg> Dockerfile 생성</button>
          <button class="qbtn" id="cmd-qbtn-gha"><svg class="ic ic-sm"><use href="#ic-github"/></svg> GitHub Actions 생성</button>
          <button class="qbtn" id="cmd-health-btn"><svg class="ic ic-sm"><use href="#ic-heart"/></svg> 헬스 체크</button>
          <button class="qbtn" id="cmd-qbtn-dashboard"><svg class="ic ic-sm"><use href="#ic-chart"/></svg> 대시보드</button>
          <button class="qbtn" id="cmd-log-btn"><svg class="ic ic-sm"><use href="#ic-log"/></svg> 로그 분리</button>
        </div>
        <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
          <label style="font-size:11px">
            <input type="checkbox" id="cmd-auto-detect">
            자동 오류 감지 활성화
          </label>
        </div>
      </div>
    </div>

  </div>

  <!-- Bottom log panel -->
  <div class="log-panel">
    <div class="log-tabs">
      <div class="log-tab active" data-log="ai">AI 분석 로그</div>
      <div class="log-tab" data-log="docker">Docker 빌드 로그</div>
      <div class="log-tab" data-log="gha">GitHub Actions 로그</div>
      <div class="log-tab" data-log="deploy">배포 로그</div>
      <div class="log-tab" data-log="health">헬스체크 로그</div>
      <button class="log-clear" id="btn-log-clear">Clear</button>
    </div>
    <div class="log-content">
      <div class="log-pane active" id="log-ai"></div>
      <div class="log-pane" id="log-docker"></div>
      <div class="log-pane" id="log-gha"></div>
      <div class="log-pane" id="log-deploy"></div>
      <div class="log-pane" id="log-health"></div>
    </div>
  </div>
</div>

<!-- ══════════════════════════════════════════════
     PAGE: ERROR CENTER
════════════════════════════════════════════════ -->
<div class="wb-page" id="page-error">
  <div class="page-scroll" style="display:flex;flex-direction:column;gap:14px">

    <div class="stage-bar">
      <div class="stage-item" id="stage-collect"><div class="stage-dot"></div><div>에러 수집</div></div>
      <div class="stage-item" id="stage-patch"><div class="stage-dot"></div><div>코드 패치</div></div>
      <div class="stage-item" id="stage-infra"><div class="stage-dot"></div><div>Dockerfile</div></div>
      <div class="stage-item" id="stage-deploy"><div class="stage-dot"></div><div>배포</div></div>
    </div>

    <div class="card">
      <div class="card-header">
        <div class="card-title"><span class="icon"><svg class="ic ic-sm"><use href="#ic-clipboard"/></svg></span> 에러 로그 붙여넣기</div>
      </div>
      <textarea id="paste-input" placeholder="터미널 에러 메시지나 스택 트레이스를 여기에 붙여넣으세요..." rows="6" style="width:100%;margin-bottom:10px"></textarea>
      <div class="btn-row" style="margin-top:0">
        <button class="btn btn-primary" id="btn-analyze"><svg class="ic ic-sm"><use href="#ic-search"/></svg> 에러 분석</button>
        <button class="btn" onclick="document.getElementById('paste-input').value=''"><svg class="ic ic-sm"><use href="#ic-trash"/></svg> 지우기</button>
      </div>
    </div>

    <div id="analyzing-state" class="card hidden">
      <div style="display:flex;align-items:center;gap:10px">
        <span class="spinner"></span>
        <span style="color:var(--t2)">에러를 분석하는 중입니다... (LLM 호출 중)</span>
      </div>
    </div>

    <div id="no-error-state" class="card hidden">
      <div class="empty-state">
        <div class="empty-icon"><svg class="ic ic-fc"><use href="#ic-check"/></svg></div>
        <div style="color:var(--green);font-weight:600">에러가 감지되지 않았습니다</div>
        <div>코드가 정상 상태입니다</div>
      </div>
    </div>

    <div id="patch-card" class="card hidden">
      <div class="card-header">
        <div class="card-title"><span class="icon"><svg class="ic ic-sm"><use href="#ic-patch"/></svg></span> 패치 제안</div>
        <span id="patch-risk-badge" class="badge badge-warn">분석 중</span>
      </div>
      <div id="patch-summary" style="font-size:12px;color:var(--t2);margin-bottom:10px"></div>
      <div id="file-tabs-container" class="file-tabs"></div>
      <div class="pre-block" id="diff-content" style="max-height:280px"></div>
      <div class="btn-row">
        <button class="btn btn-green" id="btn-approve-patch"><svg class="ic ic-sm"><use href="#ic-check"/></svg> 패치 적용</button>
        <button class="btn btn-danger" id="btn-reject-patch"><svg class="ic ic-sm"><use href="#ic-x"/></svg> 거절</button>
        <button class="btn" id="btn-git-commit"><svg class="ic ic-sm"><use href="#ic-commit"/></svg> 커밋</button>
      </div>
      <div id="patch-approved-result" class="hidden" style="margin-top:10px;padding:8px 10px;border-radius:var(--radius-md);background:var(--green-bg);border:1px solid rgba(63,185,80,.3);font-size:12px"></div>
    </div>

  </div>
</div>

<!-- ══════════════════════════════════════════════
     PAGE: GITHUB HUB
════════════════════════════════════════════════ -->
<div class="wb-page" id="page-github">
  <div class="page-scroll">
    <div class="wb-cols">
      <div>
        <div class="sec-label">Git 저장소</div>
        <div class="git-card" style="margin-top:6px">
          <div class="git-main">
            <div class="git-acct" id="git-account"><span><svg class="ic ic-sm"><use href="#ic-person"/></svg></span><span id="git-account-name">—</span></div>
            <button class="git-branch-pill" id="git-branch-btn"><span><svg class="ic ic-sm"><use href="#ic-branch"/></svg></span><span id="git-branch-name">로딩...</span><span style="color:var(--t3);font-size:9px">▼</span></button>
            <span class="git-chip git-chip-changed hidden" id="git-uncommitted-badge">●0</span>
            <span class="git-chip git-chip-ahead hidden" id="git-ahead-badge">↑0</span>
          </div>
          <div class="git-actions">
            <button class="btn" id="git-btn-commit"><svg class="ic ic-sm"><use href="#ic-commit"/></svg> 커밋</button>
            <button class="btn btn-primary" id="git-btn-push">↑ Push</button>
          </div>
          <div class="git-dropdown" id="git-dropdown">
            <div class="gd-section">로컬 브랜치</div>
            <div id="git-local-branches"><div class="gd-branch" style="color:var(--t3)">로딩 중...</div></div>
            <div class="gd-section" style="margin-top:6px">원격 브랜치</div>
            <div id="git-remote-branches"><div class="gd-branch" style="color:var(--t3)">로딩 중...</div></div>
            <div class="gd-new-row">
              <input class="gd-new-input" id="git-new-branch-input" placeholder="새 브랜치 이름" type="text">
              <button class="gd-new-btn" id="git-btn-branch-create">+ 생성</button>
            </div>
          </div>
          <div class="git-commit-panel" id="git-commit-panel">
            <textarea class="git-commit-input" id="git-commit-msg" placeholder="커밋 메시지..." rows="2" style="width:100%;min-height:50px"></textarea>
            <div class="git-commit-btns">
              <button class="btn btn-green" id="git-btn-do-commit"><svg class="ic ic-sm"><use href="#ic-commit"/></svg> Commit</button>
              <button class="btn btn-primary" id="git-btn-commit-push"><svg class="ic ic-sm"><use href="#ic-push"/></svg> Commit + Push</button>
            </div>
            <div class="git-status-line" id="git-commit-status"></div>
          </div>
        </div>
      </div>
      <div class="gh-section">
        <div class="sec-label">GitHub 자동화</div>
        <div id="gh-loading-card" class="card" style="margin-top:6px"><div style="display:flex;align-items:center;gap:8px;color:var(--t2);font-size:11px"><span class="spinner"></span><span>GitHub 상태 확인 중...</span></div></div>
        <div id="gh-login-card" class="card hidden" style="margin-top:6px">
          <div class="card-header"><div class="card-title"><span class="icon"><svg class="ic ic-sm"><use href="#ic-github"/></svg></span> GitHub 연결</div><span class="badge badge-info" id="gh-login-badge">미연결</span></div>
          <div style="font-size:11px;color:var(--t2);line-height:1.6;margin-top:6px">VS Code GitHub 계정으로 안전하게 연결합니다.<br>브라우저 인증 창이 열립니다.</div>
          <div class="btn-row" style="margin-top:10px">
            <button class="btn btn-primary" id="btn-gh-login" style="flex:1"><svg class="ic ic-sm"><use href="#ic-github"/></svg> GitHub 연결</button>
          </div>
          <div id="gh-login-progress" style="margin-top:6px;font-size:10px;color:var(--t3);min-height:14px;text-align:center"></div>
        </div>
        <div id="gh-ship-card" class="card hidden" style="margin-top:6px">
          <div class="card-header"><div class="card-title"><span class="icon"><svg class="ic ic-sm"><use href="#ic-deploy"/></svg></span> GitHub 한 번에 배포</div><span class="badge badge-ok" id="gh-user-badge">연결됨</span></div>
          <div style="display:flex;align-items:center;justify-content:space-between;margin-top:8px;padding:8px 10px;background:var(--bg2);border:1px solid var(--bd);border-radius:var(--radius-sm)">
            <div style="display:flex;align-items:center;gap:6px;font-size:12px"><span><svg class="ic ic-sm"><use href="#ic-person"/></svg></span><span id="gh-account-name" style="font-weight:600">—</span></div>
            <button class="btn btn-danger" id="btn-gh-logout" style="padding:3px 10px;font-size:10px">로그아웃</button>
          </div>
          <div class="btn-row" style="margin-top:10px">
            <button class="btn" id="btn-project-scan"><svg class="ic ic-sm"><use href="#ic-search"/></svg> 프로젝트 스캔</button>
            <span id="gh-project-status" style="font-size:10px;color:var(--t3);align-self:center">Ship 전에 자동 스캔됩니다</span>
          </div>
          <div style="margin-top:10px">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px"><span class="sec-label">브랜치</span><button class="btn" id="btn-gh-refresh-branches" style="padding:2px 8px;font-size:10px"><svg class="ic ic-sm"><use href="#ic-refresh"/></svg> 새로고침</button></div>
            <div id="gh-branch-list" style="background:var(--bg0);border:1px solid var(--bd);border-radius:var(--radius-sm);max-height:100px;overflow-y:auto;font-size:11px;padding:4px 0"><div style="padding:4px 10px;color:var(--t3)">로딩 중...</div></div>
          </div>
          <div style="margin-top:10px;display:flex;flex-direction:column;gap:6px">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:2px">
              <span class="sec-label">기존 레포지토리</span>
              <button class="btn" id="btn-gh-refresh-repos" style="padding:2px 8px;font-size:10px"><svg class="ic ic-sm"><use href="#ic-refresh"/></svg> 새로고침</button>
            </div>
            <div id="gh-repo-list" style="background:var(--bg0);border:1px solid var(--bd);border-radius:var(--radius-sm);max-height:110px;overflow-y:auto;font-size:11px;padding:4px 0"><div style="padding:4px 10px;color:var(--t3)">로딩 중...</div></div>
            <input id="gh-repo-name" type="text" placeholder="새 repo 이름 (예: my-app)" style="width:100%">
            <label><input type="checkbox" id="gh-private" checked> 비공개 (Private)</label>
            <label style="color:var(--t3)"><input type="checkbox" id="gh-include-infra"> AI 인프라 파일 생성 (Dockerfile · Compose · Actions)</label>
          </div>
          <div class="btn-row"><button class="btn btn-primary" id="btn-ship-go"><svg class="ic ic-sm"><use href="#ic-deploy"/></svg> 한 번에 배포</button></div>
        </div>
        <div id="gh-ship-progress" class="card hidden" style="margin-top:6px">
          <div class="card-header"><div class="card-title" id="gh-ship-title"><span class="spinner" id="gh-ship-spinner"></span> <span id="gh-ship-title-text">GitHub 배포 진행 중</span></div><span id="gh-ship-current" style="font-size:10px;color:var(--t3)">init</span></div>
          <div id="gh-ship-steps" style="display:flex;flex-direction:column;gap:2px;margin-top:8px"></div>
          <div id="gh-ship-result" class="hidden" style="margin-top:10px;padding:8px 10px;border-radius:var(--radius-md);font-size:12px"></div>
          <button id="btn-ship-back" class="btn" style="margin-top:10px;width:100%;display:none">← 배포 설정으로 돌아가기</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ══════════════════════════════════════════════
     PAGE: DEPLOY CENTER
════════════════════════════════════════════════ -->
<div class="wb-page" id="page-deploy">
  <div class="page-scroll">
    <div class="wb-cols">
      <div>
        <div class="sec-label">인프라 파일 생성</div>
        <div class="btn-row" style="margin-top:6px">
          <button class="btn btn-primary" id="btn-gen-dockerfile"><svg class="ic ic-sm"><use href="#ic-file"/></svg> Dockerfile</button>
          <button class="btn" id="btn-gen-compose"><svg class="ic ic-sm"><use href="#ic-docker"/></svg> Compose</button>
          <button class="btn" id="btn-gen-gha"><svg class="ic ic-sm"><use href="#ic-github"/></svg> GH Actions</button>
        </div>
        <div id="dockerfile-card" class="card hidden" style="margin-top:14px">
          <div class="card-header"><div class="card-title"><span class="icon"><svg class="ic ic-sm"><use href="#ic-file"/></svg></span> <span id="infra-target">Dockerfile</span></div><span class="badge badge-info">Preview</span></div>
          <div class="pre-block" id="dockerfile-content"></div>
          <div id="scan-result" style="margin-top:8px"></div>
          <div class="btn-row">
            <button class="btn btn-green" id="btn-approve-infra">✓ 저장 <span style="opacity:.7;font-size:10px">Level 1</span></button>
            <button class="btn" id="btn-security-scan"><svg class="ic ic-sm"><use href="#ic-lock"/></svg> 보안 스캔</button>
          </div>
        </div>
      </div>
      <div>
        <div class="sec-label">Docker 로컬 배포</div>
        <div id="deploy-idle-card" class="card" style="margin-top:6px"><div class="empty-state" style="padding:20px"><div class="empty-icon"><svg class="ic ic-fc"><use href="#ic-docker"/></svg></div><div style="font-size:11px">Dockerfile을 먼저 생성하세요</div></div></div>
        <div id="deploy-section" class="card hidden" style="margin-top:6px">
          <div class="card-header"><div class="card-title"><span class="icon"><svg class="ic ic-sm"><use href="#ic-deploy"/></svg></span> Docker 배포</div><span class="badge badge-warn">Level 2</span></div>
          <div class="pre-block" id="deploy-command-preview" style="font-size:10px;color:var(--t2)"></div>
          <div class="btn-row"><button class="btn btn-primary" id="btn-deploy-local"><svg class="ic ic-sm"><use href="#ic-deploy"/></svg> 배포 실행</button></div>
        </div>
        <div id="deploy-progress-card" class="card hidden" style="margin-top:6px">
          <div class="card-header"><div class="card-title"><span class="spinner"></span> 배포 진행 중</div><span id="deploy-stage" style="font-size:10px;color:var(--t3)">building</span></div>
          <div class="progress-wrap"><div class="progress-bar" id="deploy-progress-bar"></div></div>
          <div class="deploy-steps">
            <div class="deploy-step" id="dstep-build"><div class="ds-dot"></div><span>이미지 빌드</span></div>
            <div class="deploy-step" id="dstep-run"><div class="ds-dot"></div><span>컨테이너 실행</span></div>
            <div class="deploy-step" id="dstep-health"><div class="ds-dot"></div><span>Health Check</span></div>
          </div>
          <div style="font-family:monospace;font-size:10px;color:var(--t2);margin-top:8px;max-height:100px;overflow-y:auto" id="deploy-log-tail"></div>
        </div>
        <div id="health-card" class="card hidden" style="margin-top:6px">
          <div class="card-header"><div class="card-title"><span class="icon"><svg class="ic ic-sm"><use href="#ic-heart"/></svg></span> Health Check</div></div>
          <div id="health-result" style="font-size:12px;margin-top:6px"></div>
          <div class="btn-row"><button class="btn btn-danger hidden" id="btn-rollback"><svg class="ic ic-sm"><use href="#ic-refresh"/></svg> 롤백</button></div>
      </div>
    </div>
  </div>
</div>

<!-- 레포 변경 확인 모달 -->
<div id="repo-confirm-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:200;align-items:center;justify-content:center">
  <div style="background:var(--bg1);border:1px solid var(--bd2);border-radius:var(--radius-lg);padding:22px 24px;max-width:420px;width:92%;box-shadow:0 8px 32px rgba(0,0,0,.5)">
    <div style="font-weight:700;font-size:14px;margin-bottom:10px">⚠️ 원격 저장소 변경</div>
    <div id="repo-confirm-msg" style="font-size:12px;color:var(--t2);line-height:1.7;margin-bottom:18px"></div>
    <div style="font-size:11px;color:var(--yellow);margin-bottom:18px">현재 push 설정이 새 레포지토리로 변경됩니다. 기존 연결은 해제됩니다.</div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="btn" id="btn-repo-confirm-cancel" style="min-width:72px">취소</button>
      <button class="btn btn-primary" id="btn-repo-confirm-ok" style="min-width:72px">변경</button>
    </div>
  </div>
</div>

<div id="wb-toast"></div>
<script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
    }
}
