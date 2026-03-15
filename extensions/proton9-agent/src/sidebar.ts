import * as vscode from 'vscode';
import { ForgeWebSocket } from './websocket';

export class SidebarProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'Proton9.chat';
    private view?: vscode.WebviewView;
    private disposables: vscode.Disposable[] = [];

    constructor(
        private readonly extensionUri: vscode.Uri,
        private readonly forge: ForgeWebSocket
    ) {
        this.disposables.push(
            forge.onEvent((msg) => {
                this.postMessage(msg);
            })
        );
    }

    resolveWebviewView(webviewView: vscode.WebviewView): void {
        this.view = webviewView;
        const webview = webviewView.webview;
        webview.options = { enableScripts: true, enableCommandUris: true };

        console.log('[Proton9] resolveWebviewView called, forge.connected:', this.forge.connected);

        // Register message handler ONCE (survives html re-sets)
        webview.onDidReceiveMessage((msg) => {
            console.log('[Proton9] Webview message received:', msg.type, JSON.stringify(msg));
            vscode.window.showInformationMessage('[P9] Webview sent: ' + msg.type);
            switch (msg.type) {
                case 'run_task': {
                    const workDir =
                        vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ||
                        'c:\\DATA\\PrimeNexus\\Proton9';
                    const maxIter = vscode.workspace
                        .getConfiguration('Proton9')
                        .get<number>('maxIterations', 50);
                    this.forge.runTask(msg.task, workDir, maxIter);
                    break;
                }
                case 'stop':
                    this.forge.stopTask();
                    break;
            }
        });

        // Helper: set HTML only
        const renderHtml = (connected: boolean, version: string) => {
            webview.html = this.getHtml(connected, version);
        };

        // Initial render with current state
        renderHtml(this.forge.connected, this.forge.serverVersion || '');

        // Re-render on connection status changes
        this.disposables.push(
            this.forge.onEvent((msg) => {
                console.log('[Proton9] Event for webview:', msg.type);
                try { webview.postMessage(msg); } catch { /* ignore */ }
                if (msg.type === 'connected' || msg.type === 'disconnected') {
                    const isConn = msg.type === 'connected';
                    renderHtml(isConn, isConn ? (msg.version || '') : '');
                }
            })
        );
    }

    postMessage(msg: any): void {
        this.view?.webview.postMessage(msg);
    }

    getHtml(initialConnected = false, initialVersion = ''): string {
        const nonce = this.getNonce();

        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:var(--vscode-font-family); font-size:var(--vscode-font-size); color:var(--vscode-foreground); background:var(--vscode-sideBar-background); height:100vh; display:flex; flex-direction:column; }
        #app { display:flex; flex-direction:column; height:100vh; }
        #status-bar { display:flex; align-items:center; gap:6px; padding:8px 12px; background:var(--vscode-sideBarSectionHeader-background); border-bottom:1px solid var(--vscode-sideBarSectionHeader-border, #333); font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; }
        .dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
        .dot.connected { background:#4ec9b0; }
        .dot.disconnected { background:#f44747; }
        .dot.running { background:#dcdcaa; animation:pulse 1s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
        #chat-container { flex:1; overflow-y:auto; padding:8px; }
        #messages { display:flex; flex-direction:column; gap:8px; }
        .message { padding:8px 12px; border-radius:6px; font-size:13px; line-height:1.5; word-wrap:break-word; }
        .message.user { background:var(--vscode-button-background); color:var(--vscode-button-foreground); align-self:flex-end; max-width:90%; border-radius:6px 6px 2px 6px; }
        .message.system { background:var(--vscode-editor-background); border:1px solid var(--vscode-widget-border, #444); font-size:12px; }
        .message.error { background:rgba(244,71,71,0.15); border:1px solid #f44747; color:#f44747; }
        .message.complete { background:rgba(78,201,176,0.15); border:1px solid #4ec9b0; }
        .message.streaming { border-left:3px solid var(--vscode-button-background); background:var(--vscode-editor-background); }
        .message.streaming .cursor { display:inline-block; width:6px; height:14px; background:var(--vscode-button-background); animation:blink 0.8s infinite; vertical-align:text-bottom; margin-left:2px; }
        @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
        .action-step { margin:2px 0; border:1px solid var(--vscode-widget-border, #444); border-radius:4px; overflow:hidden; }
        .action-header { display:flex; align-items:center; gap:6px; padding:6px 10px; background:var(--vscode-editor-background); cursor:pointer; font-size:12px; font-family:var(--vscode-editor-font-family); }
        .action-header:hover { background:var(--vscode-list-hoverBackground); }
        .action-header .step-num { color:var(--vscode-descriptionForeground); font-size:10px; min-width:24px; }
        .action-header .tool-name { font-weight:600; color:#dcdcaa; }
        .action-header .tool-name.success { color:#4ec9b0; }
        .action-header .tool-name.error { color:#f44747; }
        .action-body { display:none; padding:8px 10px; background:var(--vscode-editor-background); border-top:1px solid var(--vscode-widget-border, #444); font-family:var(--vscode-editor-font-family); font-size:12px; white-space:pre-wrap; max-height:200px; overflow-y:auto; color:var(--vscode-descriptionForeground); }
        .action-step.expanded .action-body { display:block; }
        .action-feed-wrapper { margin:4px 0; border:1px solid var(--vscode-widget-border, #444); border-radius:4px; overflow:hidden; }
        .action-feed-header { display:flex; align-items:center; gap:6px; padding:8px 10px; background:var(--vscode-sideBarSectionHeader-background); cursor:pointer; font-size:12px; font-weight:600; }
        .action-feed-header:hover { background:var(--vscode-list-hoverBackground); }
        .action-feed-body { display:none; padding:4px 6px; }
        .action-feed-wrapper.expanded .action-feed-body { display:block; }
        .action-feed-prompt { padding:6px 10px; margin:4px 0; border-left:3px solid var(--vscode-button-background); background:var(--vscode-editor-background); font-size:12px; line-height:1.5; }
        .action-feed-prompt-label { color:var(--vscode-button-background); font-weight:600; margin-right:6px; }
        .copy-btn { background:var(--vscode-button-secondaryBackground, #333); color:var(--vscode-button-secondaryForeground, #ccc); border:1px solid var(--vscode-widget-border, #444); padding:2px 8px; font-size:10px; cursor:pointer; border-radius:3px; margin-left:auto; }
        .copy-btn:hover { background:var(--vscode-button-secondaryHoverBackground, #444); }
        #input-area { padding:8px; border-top:1px solid var(--vscode-sideBarSectionHeader-border, #333); }
        #task-input { width:100%; padding:8px 10px; border:1px solid var(--vscode-input-border, #444); background:var(--vscode-input-background); color:var(--vscode-input-foreground); font-family:var(--vscode-font-family); font-size:13px; border-radius:4px; resize:vertical; min-height:50px; outline:none; }
        #task-input:focus { border-color:var(--vscode-focusBorder); }
        #task-input::placeholder { color:var(--vscode-input-placeholderForeground); }
        #input-controls { display:flex; gap:6px; margin-top:6px; }
        button { padding:6px 14px; border:none; border-radius:3px; font-family:var(--vscode-font-family); font-size:12px; cursor:pointer; font-weight:600; }
        #send-btn { background:var(--vscode-button-background); color:var(--vscode-button-foreground); flex:1; }
        #send-btn:hover { background:var(--vscode-button-hoverBackground); }
        #stop-btn { background:#f44747; color:white; flex:1; }
        .hidden { display:none !important; }
    </style>
</head>
<body>
    <div id="app">
        <div id="status-bar"><span id="status-dot" class="dot ${initialConnected ? 'connected' : 'disconnected'}"></span><span id="status-text">${initialConnected ? 'Connected (v' + initialVersion + ')' : 'Connecting...'}</span></div>
        <div id="chat-container"><div id="messages"></div></div>
        <div id="input-area">
            <textarea id="task-input" placeholder="Describe your task..." rows="3"></textarea>
            <div id="input-controls">
                <button id="send-btn">▶ Run</button>
                <button id="stop-btn" class="hidden">■ Stop</button>
            </div>
        </div>
    </div>
    <script nonce="${nonce}">
        (function () {
            const vscode = acquireVsCodeApi();
            const messagesEl = document.getElementById('messages');
            const inputEl = document.getElementById('task-input');
            const sendBtn = document.getElementById('send-btn');
            const stopBtn = document.getElementById('stop-btn');
            const statusDot = document.getElementById('status-dot');
            const statusText = document.getElementById('status-text');
            let isRunning = false;
            let streamingEl = null;
            let currentFeedEl = null;      // current Action Feed wrapper
            let currentFeedBody = null;    // its body (contains steps)
            let currentPrompt = '';        // current task prompt
            let currentAnswer = '';        // task answer/summary
            let stepCount = 0;

            function sendTask() {
                const task = inputEl.value.trim();
                if (!task || isRunning) return;
                currentPrompt = task;
                currentAnswer = '';
                stepCount = 0;
                addMessage(task, 'user');
                inputEl.value = '';
                // Use command URI to bypass broken postMessage
                var encoded = encodeURIComponent(JSON.stringify(task));
                var a = document.createElement('a');
                a.href = 'command:Proton9.runTaskDirect?' + encoded;
                a.style.display = 'none';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            }

            sendBtn.addEventListener('click', sendTask);
            inputEl.addEventListener('keydown', function(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendTask(); } });
            stopBtn.addEventListener('click', function() {
                var a = document.createElement('a');
                a.href = 'command:Proton9.stopTask';
                a.style.display = 'none';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                addMessage('Stop requested...', 'system');
            });

            window.addEventListener('message', function(event) {
                const msg = event.data;
                if (!msg || !msg.type) return;
                switch (msg.type) {
                    case 'connected': setStatus('connected', 'Connected (v' + (msg.version || '?') + ')'); break;
                    case 'disconnected': setStatus('disconnected', 'Disconnected'); setRunning(false); break;
                    case 'task_started': setRunning(true); setStatus('running', 'Running...'); createActionFeed(); break;
                    case 'action': endStreaming(); addAction(msg.step, msg.tool, msg.args); break;
                    case 'result': updateLastAction(msg.success, msg.output, msg.error); break;
                    case 'llm_token': appendStream(msg.text || ''); break;
                    case 'task_complete': endStreaming(); setRunning(false); setStatus('connected', 'Done'); finalizeActionFeed(msg.result || {}); addCompletionCard(msg.result || {}); break;
                    case 'task_error': endStreaming(); setRunning(false); setStatus('connected', 'Error'); addMessage('Error: ' + (msg.error || 'Unknown'), 'error'); break;
                    case 'error': addMessage('Server: ' + (msg.message || msg.error || 'Unknown error'), 'error'); break;
                }
            });

            function setStatus(state, text) { statusDot.className = 'dot ' + state; statusText.textContent = text; }
            function setRunning(running) { isRunning = running; sendBtn.classList.toggle('hidden', running); stopBtn.classList.toggle('hidden', !running); inputEl.disabled = running; }
            function addMessage(text, type) { var el = document.createElement('div'); el.className = 'message ' + type; el.textContent = text; messagesEl.appendChild(el); scrollToBottom(); }

            // ─── Action Feed ───
            function createActionFeed() {
                currentFeedEl = document.createElement('div');
                currentFeedEl.className = 'action-feed-wrapper';
                
                var header = document.createElement('div');
                header.className = 'action-feed-header';
                header.innerHTML = '<span class="chevron">▶</span> <span class="feed-title">Action Feed (0 steps)</span>';
                
                var copyBtn = document.createElement('button');
                copyBtn.className = 'copy-btn';
                copyBtn.textContent = '📋 Copy';
                copyBtn.addEventListener('click', function(e) {
                    e.stopPropagation();
                    copyActionFeed(copyBtn);
                });
                header.appendChild(copyBtn);
                
                header.addEventListener('click', function() {
                    currentFeedEl.classList.toggle('expanded');
                    header.querySelector('.chevron').textContent = currentFeedEl.classList.contains('expanded') ? '▼' : '▶';
                });
                
                currentFeedBody = document.createElement('div');
                currentFeedBody.className = 'action-feed-body';
                
                // Prompt inside feed
                if (currentPrompt) {
                    var promptEl = document.createElement('div');
                    promptEl.className = 'action-feed-prompt';
                    promptEl.innerHTML = '<span class="action-feed-prompt-label">Prompt:</span>' + esc(currentPrompt);
                    currentFeedBody.appendChild(promptEl);
                }
                
                currentFeedEl.appendChild(header);
                currentFeedEl.appendChild(currentFeedBody);
                messagesEl.appendChild(currentFeedEl);
                scrollToBottom();
            }

            function finalizeActionFeed(result) {
                if (!currentFeedEl) return;
                currentAnswer = result.summary || '';
                var titleEl = currentFeedEl.querySelector('.feed-title');
                if (titleEl) titleEl.textContent = 'Action Feed (' + stepCount + ' steps)';
            }

            function copyActionFeed(btn) {
                var parts = [];
                if (currentPrompt) parts.push('Prompt:\n' + currentPrompt);
                if (currentAnswer) parts.push('Answer:\n' + currentAnswer);
                // Gather all step text
                var steps = currentFeedBody ? currentFeedBody.querySelectorAll('.action-step') : [];
                if (steps.length) {
                    var stepLines = [];
                    steps.forEach(function(s) {
                        var header = s.querySelector('.action-header');
                        var body = s.querySelector('.action-body');
                        var headerText = header ? header.textContent.replace(/[+\-]$/, '').trim() : '';
                        var bodyText = body ? body.textContent : '';
                        stepLines.push(headerText + '\n' + bodyText);
                    });
                    parts.push('Action Feed (' + steps.length + ' steps):\n' + stepLines.join('\n\n' + '-'.repeat(60) + '\n\n'));
                }
                var text = parts.join('\n\n' + '='.repeat(60) + '\n\n');
                navigator.clipboard.writeText(text).then(function() {
                    btn.textContent = '✅ Copied';
                    setTimeout(function() { btn.textContent = '📋 Copy'; }, 1500);
                });
            }

            function addAction(step, tool, args) {
                stepCount++;
                if (!currentFeedBody) createActionFeed();
                
                // Update step count in header
                var titleEl = currentFeedEl.querySelector('.feed-title');
                if (titleEl) titleEl.textContent = 'Action Feed (' + stepCount + ' steps)';
                
                var el = document.createElement('div');
                el.className = 'action-step';
                var argsText = args ? Object.keys(args).map(function(k) { return k + ': ' + String(args[k]).slice(0, 80); }).join(', ') : '';
                el.innerHTML = '<div class="action-header"><span class="step-num">[' + step + ']</span> <span class="tool-name">' + esc(tool || 'thinking') + '</span><span style="flex:1"></span><span class="chevron">+</span></div><div class="action-body">' + esc(argsText) + '</div>';
                el.querySelector('.action-header').addEventListener('click', function() { el.classList.toggle('expanded'); el.querySelector('.chevron').textContent = el.classList.contains('expanded') ? '-' : '+'; });
                currentFeedBody.appendChild(el);
                scrollToBottom();
            }

            function updateLastAction(success, output, error) {
                var container = currentFeedBody || messagesEl;
                var steps = container.querySelectorAll('.action-step');
                if (!steps.length) return;
                var last = steps[steps.length - 1];
                var tn = last.querySelector('.tool-name');
                if (tn) tn.classList.add(success ? 'success' : 'error');
                var body = last.querySelector('.action-body');
                if (body) body.textContent = success ? (output || '(no output)').slice(0, 500) : 'ERROR: ' + (error || 'unknown');
            }

            function addCompletionCard(result) {
                var el = document.createElement('div');
                var stopped = result.task_complete === false;
                el.className = 'message ' + (stopped ? 'error' : 'complete');
                var lines = [];
                lines.push(stopped ? '⚠ Task Stopped' : '✅ Task Complete');
                lines.push('━━━━━━━━━━━━━━━━━━━━━━━━━');
                if (result.summary) lines.push(result.summary);
                var files = result.files_changed || [];
                if (files.length) {
                    lines.push('');
                    lines.push('📁 Files Changed (' + files.length + '):');
                    files.forEach(function(f) { var name = f.replace(/[\\\\/]/g, '/').split('/').pop(); lines.push('  • ' + name); });
                }
                var actions = result.actions || [];
                if (actions.length) {
                    var successes = actions.filter(function(a) { return a.success; }).length;
                    lines.push('');
                    lines.push('⚡ Steps: ' + actions.length + ' | ✅ ' + successes + '/' + actions.length + ' succeeded');
                }
                var usage = result.usage || {};
                var inTok = usage.total_input_tokens || 0;
                var outTok = usage.total_output_tokens || 0;
                if (inTok || outTok) {
                    var fmt = function(n) { return n >= 1000 ? (n/1000).toFixed(1) + 'K' : n; };
                    lines.push('🔢 Tokens: ' + fmt(inTok) + ' in / ' + fmt(outTok) + ' out');
                }
                var findings = result.critic_findings || [];
                if (findings.length) {
                    var crit = findings.filter(function(f) { return f.severity === 'critical'; }).length;
                    var rec = findings.filter(function(f) { return f.severity === 'recommended'; }).length;
                    var sug = findings.length - crit - rec;
                    lines.push('');
                    lines.push('🔍 Critic: ' + findings.length + ' finding(s)');
                    if (crit) lines.push('  🔴 ' + crit + ' critical');
                    if (rec) lines.push('  🟡 ' + rec + ' recommended');
                    if (sug) lines.push('  🔵 ' + sug + ' suggestion(s)');
                    findings.forEach(function(f) {
                        var icon = f.severity === 'critical' ? '🔴' : f.severity === 'recommended' ? '🟡' : '🔵';
                        lines.push('  ' + icon + ' ' + (f.title || 'Unknown'));
                    });
                } else if (files.length) {
                    lines.push('');
                    lines.push('🔍 Critic: No issues found ✓');
                }
                if (result.status) { lines.push(''); lines.push(result.status); }
                el.textContent = lines.join('\n');
                el.style.whiteSpace = 'pre-wrap';
                el.style.fontFamily = 'var(--vscode-editor-font-family)';
                el.style.fontSize = '12px';
                el.style.lineHeight = '1.6';
                messagesEl.appendChild(el);
                scrollToBottom();
            }

            function appendStream(text) {
                if (!streamingEl) { streamingEl = document.createElement('div'); streamingEl.className = 'message streaming'; streamingEl.innerHTML = '<span class="stream-text"></span><span class="cursor"></span>'; messagesEl.appendChild(streamingEl); }
                streamingEl.querySelector('.stream-text').textContent += text;
                scrollToBottom();
            }

            function endStreaming() { if (streamingEl) { streamingEl.classList.remove('streaming'); var c = streamingEl.querySelector('.cursor'); if (c) c.remove(); streamingEl = null; } }
            function scrollToBottom() { var c = document.getElementById('chat-container'); c.scrollTop = c.scrollHeight; }
            function esc(s) { var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
        })();
    </script>
</body>
</html>`;
    }

    private getNonce(): string {
        const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
        let nonce = '';
        for (let i = 0; i < 32; i++) {
            nonce += chars.charAt(Math.floor(Math.random() * chars.length));
        }
        return nonce;
    }

    dispose(): void {
        this.disposables.forEach(d => d.dispose());
    }
}
