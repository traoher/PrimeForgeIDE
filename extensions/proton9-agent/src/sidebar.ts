import * as vscode from 'vscode';
import { ForgeWebSocket } from './websocket';

export class SidebarProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'Proton9.chat';
    private view?: vscode.WebviewView;
    private disposables: vscode.Disposable[] = [];
    // Cache the last active text editor (persists when sidebar gets focus)
    public static lastActiveEditor: vscode.TextEditor | undefined;

    constructor(
        private readonly extensionUri: vscode.Uri,
        private readonly forge: ForgeWebSocket
    ) {}

    resolveWebviewView(webviewView: vscode.WebviewView): void {
        this.view = webviewView;
        const webview = webviewView.webview;
        webview.options = { enableScripts: true, enableCommandUris: true };

        console.log('[Proton9] resolveWebviewView called, forge.connected:', this.forge.connected);

        // Register message handler ONCE (survives html re-sets)
        webview.onDidReceiveMessage((msg) => {
            console.log('[Proton9] Webview message received:', msg.type);
            switch (msg.type) {
                case 'run_task': {
                    const workDir =
                        vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '';
                    const maxIter = vscode.workspace
                        .getConfiguration('Proton9')
                        .get<number>('maxIterations', 50);

                    // Gather active editor context (fall back to cached editor)
                    const editor = vscode.window.activeTextEditor || SidebarProvider.lastActiveEditor;
                    let activeFile: {
                        path: string;
                        language: string;
                        cursorLine: number;
                        selection: string;
                        surroundingLines: string;
                    } | undefined;

                    if (editor) {
                        const doc = editor.document;
                        const sel = editor.selection;
                        const selectedText = doc.getText(sel);
                        // Get ~20 lines around cursor for context
                        const cursorLine = sel.active.line;
                        const startLine = Math.max(0, cursorLine - 10);
                        const endLine = Math.min(doc.lineCount - 1, cursorLine + 10);
                        const range = new vscode.Range(startLine, 0, endLine, doc.lineAt(endLine).text.length);
                        const surroundingLines = doc.getText(range);

                        activeFile = {
                            path: doc.uri.fsPath,
                            language: doc.languageId,
                            cursorLine: cursorLine + 1, // 1-indexed
                            selection: selectedText || '',
                            surroundingLines,
                        };
                        console.log('[Proton9] Active file context:', activeFile.path, 'line', activeFile.cursorLine);
                    } else {
                        console.log('[Proton9] No active editor found (activeTextEditor and cache both undefined)');
                    }

                    this.forge.runTask(msg.task, workDir, maxIter, activeFile, msg.sessionId);
                    break;
                }
                case 'stop':
                    this.forge.stopTask();
                    break;
                case 'set_model':
                    this.forge.setModel(msg.provider || '', msg.model || '');
                    break;
                case 'query_models':
                    this.forge.queryModels(msg.provider || '');
                    break;
                case 'save_chat': {
                    // Persist chat session to file in .proton9/chats/
                    const wsFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                    if (wsFolder && msg.sessionId) {
                        const fs = require('fs');
                        const path = require('path');
                        const chatsDir = path.join(wsFolder, '.proton9', 'chats');
                        try {
                            fs.mkdirSync(chatsDir, { recursive: true });
                            const slug = (msg.title || 'chat').replace(/[^a-zA-Z0-9]+/g, '_').slice(0, 40).toLowerCase();
                            const filename = `${msg.sessionId}_${slug}.json`;
                            const filepath = path.join(chatsDir, filename);
                            fs.writeFileSync(filepath, JSON.stringify({
                                id: msg.sessionId,
                                title: msg.title || '',
                                messages: msg.messages || [],
                                savedAt: new Date().toISOString(),
                            }, null, 2), 'utf-8');
                            console.log('[Proton9] Chat saved:', filepath);
                        } catch (err) {
                            console.error('[Proton9] Chat save error:', err);
                        }
                    }
                    break;
                }
                case 'switch_session':
                    // Tell server to recontext to a different session/ensemble
                    this.forge.send({ type: 'switch_session', session_id: msg.sessionId || '' });
                    break;
                case 'revert_file':
                    // Send revert to server with snapshot data
                    this.forge.send({ type: 'revert_file', path: msg.path || '', snapshot: msg.snapshot });
                    break;
                case 'open_file': {
                    // Open a file in the editor
                    const uri = vscode.Uri.file(msg.path);
                    vscode.workspace.openTextDocument(uri).then(doc => {
                        vscode.window.showTextDocument(doc, { preview: false, preserveFocus: true });
                    }, () => {});
                    break;
                }
                case 'webview_ready':
                case 'get_state':
                    // Webview just loaded — re-send current connection state
                    if (this.forge.connected) {
                        try { webview.postMessage({ type: 'connected', version: this.forge.serverVersion || '' }); } catch {}
                    }
                    break;
            }
        });

        // Helper: set HTML only
        const renderHtml = (connected: boolean, version: string) => {
            webview.html = this.getHtml(connected, version);
        };

        // Initial render with current state
        renderHtml(this.forge.connected, this.forge.serverVersion || '');

        // Forward events to the webview — do NOT re-render HTML (that destroys event listeners)
        this.disposables.push(
            this.forge.onEvent((msg) => {
                console.log('[Proton9] Event for webview:', msg.type);
                try { webview.postMessage(msg); } catch { /* ignore */ }
            })
        );
    }

    postMessage(msg: any): void {
        this.view?.webview.postMessage(msg);
    }

    getHtml(initialConnected = false, initialVersion = ''): string {
        const nonce = this.getNonce();
        // Build </script> at runtime so esbuild can't embed a literal </script> in the output
        // (a literal </script> inside a <script> block breaks the HTML parser)
        const scriptEnd = ['<', '/', 'script', '>'].join('');

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
        #gear-btn { margin-left:auto; background:none; border:none; color:var(--vscode-descriptionForeground); cursor:pointer; font-size:14px; padding:2px 4px; line-height:1; opacity:0.7; }
        #gear-btn:hover { opacity:1; color:var(--vscode-foreground); }
        #model-label { font-size:10px; font-weight:400; color:var(--vscode-descriptionForeground); text-transform:none; letter-spacing:0; margin-left:4px; max-width:100px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        #settings-panel { display:none; padding:10px 12px; background:var(--vscode-editor-background); border-bottom:1px solid var(--vscode-widget-border, #444); }
        #settings-panel.open { display:block; }
        #settings-panel label { display:block; font-size:11px; font-weight:600; margin-bottom:4px; color:var(--vscode-foreground); text-transform:uppercase; letter-spacing:0.3px; }
        #settings-panel select, #settings-panel input[type=text] { width:100%; padding:5px 8px; border:1px solid var(--vscode-input-border, #444); background:var(--vscode-input-background); color:var(--vscode-input-foreground); font-family:var(--vscode-font-family); font-size:12px; border-radius:3px; outline:none; margin-bottom:8px; }
        #settings-panel select:focus, #settings-panel input[type=text]:focus { border-color:var(--vscode-focusBorder); }
        #save-model-btn { width:100%; padding:5px 10px; background:var(--vscode-button-background); color:var(--vscode-button-foreground); border:none; border-radius:3px; font-size:11px; font-weight:600; cursor:pointer; }
        #save-model-btn:hover { background:var(--vscode-button-hoverBackground); }
        .settings-row { margin-bottom:2px; }
        #chat-container { flex:1; overflow-y:auto; padding:8px; }
        #messages { display:flex; flex-direction:column; gap:8px; }
        .message { padding:8px 12px; border-radius:6px; font-size:13px; line-height:1.5; word-wrap:break-word; }
        .message.user { background:var(--vscode-button-background); color:var(--vscode-button-foreground); align-self:flex-end; max-width:90%; border-radius:6px 6px 2px 6px; }
        .message.system { background:var(--vscode-editor-background); border:1px solid var(--vscode-widget-border, #444); font-size:12px; }
        .message.error { background:rgba(244,71,71,0.15); border:1px solid #f44747; color:#f44747; }
        .message.complete { background:rgba(78,201,176,0.15); border:1px solid #4ec9b0; }
        .message.assistant { background:var(--vscode-editor-background); border:1px solid var(--vscode-widget-border, #444); white-space:pre-wrap; font-size:12px; line-height:1.6; max-height:300px; overflow-y:auto; }
        .diff-card { background:var(--vscode-editor-background); border:1px solid var(--vscode-widget-border, #444); border-radius:6px; margin:4px 0; font-size:12px; overflow:hidden; }
        .diff-card .diff-header { display:flex; align-items:center; justify-content:space-between; padding:6px 10px; background:rgba(78,201,176,0.1); border-bottom:1px solid var(--vscode-widget-border, #333); cursor:pointer; }
        .diff-card .diff-header .diff-file { font-weight:bold; color:#4ec9b0; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .diff-card .diff-header .diff-actions { display:flex; gap:6px; }
        .diff-card .diff-header .diff-actions button { background:none; border:1px solid var(--vscode-widget-border, #555); color:var(--vscode-foreground); padding:2px 8px; border-radius:3px; font-size:11px; cursor:pointer; }
        .diff-card .diff-header .diff-actions button:hover { background:rgba(255,255,255,0.1); }
        .diff-card .diff-header .diff-actions .revert-btn { border-color:#f44747; color:#f44747; }
        .diff-card .diff-header .diff-actions .revert-btn:hover { background:rgba(244,71,71,0.15); }
        .diff-card .diff-body { max-height:200px; overflow-y:auto; padding:4px 0; display:none; }
        .diff-card .diff-body.open { display:block; }
        .diff-card .diff-line { padding:1px 10px; font-family:var(--vscode-editor-font-family, monospace); font-size:11px; white-space:pre; }
        .diff-card .diff-line.add { background:rgba(78,201,176,0.15); color:#4ec9b0; }
        .diff-card .diff-line.del { background:rgba(244,71,71,0.12); color:#f44747; }
        .diff-card .diff-line.hunk { color:#569cd6; font-style:italic; }
        .diff-card.reverted { opacity:0.5; }
        .diff-card.reverted .diff-header { background:rgba(244,71,71,0.1); }
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
        #new-chat-btn { background:transparent; border:1px solid var(--vscode-button-background); color:var(--vscode-button-background); padding:2px 8px; font-size:11px; cursor:pointer; border-radius:3px; margin-left:4px; min-width:unset; }
        #new-chat-btn:hover { background:var(--vscode-button-background); color:var(--vscode-button-foreground); }
        #history-btn { background:transparent; border:1px solid var(--vscode-widget-border, #444); color:var(--vscode-foreground); padding:2px 8px; font-size:11px; cursor:pointer; border-radius:3px; margin-left:2px; min-width:unset; }
        #history-btn:hover { background:var(--vscode-list-hoverBackground, #2a2d2e); }
        #history-panel { display:none; position:absolute; top:36px; left:8px; right:8px; background:var(--vscode-editorWidget-background, #252526); border:1px solid var(--vscode-widget-border, #444); border-radius:4px; z-index:100; max-height:200px; overflow-y:auto; box-shadow:0 4px 12px rgba(0,0,0,0.4); }
        #history-panel.open { display:block; }
        .history-item { padding:6px 10px; cursor:pointer; font-size:12px; border-bottom:1px solid var(--vscode-widget-border, #333); display:flex; justify-content:space-between; }
        .history-item:hover { background:var(--vscode-list-hoverBackground, #2a2d2e); }
        .history-item.active { background:var(--vscode-list-activeSelectionBackground, #094771); color:var(--vscode-list-activeSelectionForeground, #fff); }
        .history-item .title { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .history-item .count { font-size:10px; opacity:0.6; margin-left:8px; white-space:nowrap; }
    </style>
</head>
<body>
    <div id="app">
        <div id="status-bar"><span id="status-dot" class="dot ${initialConnected ? 'connected' : 'disconnected'}"></span><span id="status-text">${initialConnected ? 'Ready' : 'Connecting...'}</span><span id="model-label"></span><button id="new-chat-btn" title="New Chat">+ New</button><button id="history-btn" title="Chat History">☰</button><button id="gear-btn" title="Settings">⚙</button></div>
        <div id="history-panel"></div>
        <div id="settings-panel">
            <div class="settings-row">
                <label for="provider-select">Provider</label>
                <select id="provider-select"><option value="deepseek">DeepSeek</option><option value="gemini">Gemini</option><option value="openai">OpenAI</option><option value="anthropic">Anthropic</option></select>
            </div>
            <div class="settings-row">
                <label for="model-select">Model</label>
                <select id="model-select"><option value="">Loading...</option></select>
            </div>
            <button id="save-model-btn">💾 Save &amp; Apply</button>
        </div>
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
            const modelLabel = document.getElementById('model-label');
            const gearBtn = document.getElementById('gear-btn');
            const settingsPanel = document.getElementById('settings-panel');
            const providerSelect = document.getElementById('provider-select');
            const modelSelect = document.getElementById('model-select');
            const saveModelBtn = document.getElementById('save-model-btn');
            let isRunning = false;
            let streamingEl = null;

            // Sync state on load — request current state from extension host
            vscode.postMessage({ type: 'webview_ready' });
            let currentFeedEl = null;      // current Action Feed wrapper
            let currentFeedBody = null;    // its body (contains steps)
            let currentPrompt = '';        // current task prompt
            let currentAnswer = '';        // task answer/summary
            let stepCount = 0;
            let cachedModels = [];         // [{id, context_window}, ...]
            let streamBuffer = '';          // accumulate LLM tokens for saving

            // ─── Multi-session chat history ───
            var sessions = [];             // [{id, title, messages: [{type, text, ts}]}]
            var activeSessionId = '';

            function generateSessionId() { return 's-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2,6); }

            function deriveTitle(messages) {
                var first = messages.find(function(m) { return m.type === 'user'; });
                if (!first) return 'New Chat';
                var t = first.text.slice(0, 40);
                return t.length < first.text.length ? t + '...' : t;
            }

            function saveState() {
                // Update active session's messages and title
                var active = sessions.find(function(s) { return s.id === activeSessionId; });
                if (active) {
                    active.messages = chatMessages;
                    active.title = deriveTitle(chatMessages);
                }
                vscode.setState({ sessions: sessions, activeSessionId: activeSessionId });
            }

            function loadSession(sessionId) {
                var session = sessions.find(function(s) { return s.id === sessionId; });
                if (!session) return;
                activeSessionId = sessionId;
                chatMessages = session.messages || [];
                messagesEl.innerHTML = '';
                chatMessages.forEach(function(m) { addMessage(m.text, m.type, true); });
                currentPrompt = '';
                currentAnswer = '';
                stepCount = 0;
                streamBuffer = '';
                currentFeedEl = null;
                currentFeedBody = null;
                saveState();
                renderHistory();
                // Tell server to recontext to this session's ensemble
                vscode.postMessage({ type: 'switch_session', sessionId: sessionId });
            }

            function createNewSession() {
                // Save current session first
                var active = sessions.find(function(s) { return s.id === activeSessionId; });
                if (active) {
                    active.messages = chatMessages;
                    active.title = deriveTitle(chatMessages);
                }
                // Create new
                var newId = generateSessionId();
                sessions.unshift({ id: newId, title: 'New Chat', messages: [] });
                activeSessionId = newId;
                chatMessages = [];
                messagesEl.innerHTML = '';
                currentPrompt = '';
                currentAnswer = '';
                stepCount = 0;
                streamBuffer = '';
                currentFeedEl = null;
                currentFeedBody = null;
                // Keep max 20 sessions
                if (sessions.length > 20) sessions = sessions.slice(0, 20);
                saveState();
                renderHistory();
                vscode.postMessage({ type: 'run_task', task: '/clear' });
            }

            function renderHistory() {
                var panel = document.getElementById('history-panel');
                if (!panel) return;
                panel.innerHTML = '';
                sessions.forEach(function(s) {
                    var el = document.createElement('div');
                    el.className = 'history-item' + (s.id === activeSessionId ? ' active' : '');
                    var count = s.messages ? s.messages.filter(function(m) { return m.type === 'user'; }).length : 0;
                    el.innerHTML = '<span class="title">' + esc(s.title) + '</span><span class="count">' + count + ' msg</span>';
                    el.addEventListener('click', function() {
                        loadSession(s.id);
                        panel.classList.remove('open');
                    });
                    panel.appendChild(el);
                });
            }

            // Restore saved sessions on load
            var savedState = vscode.getState();
            if (savedState && savedState.sessions && savedState.sessions.length) {
                sessions = savedState.sessions;
                activeSessionId = savedState.activeSessionId || sessions[0].id;
                var activeSession = sessions.find(function(s) { return s.id === activeSessionId; });
                if (activeSession) {
                    chatMessages = activeSession.messages || [];
                    chatMessages.forEach(function(m) { addMessage(m.text, m.type, true); });
                }
            } else {
                // First time: create initial session
                activeSessionId = generateSessionId();
                sessions = [{ id: activeSessionId, title: 'New Chat', messages: [] }];
                chatMessages = [];
                saveState();
            }
            renderHistory();

            function populateModels(models, currentModel) {
                if (!modelSelect) return;
                modelSelect.innerHTML = '';
                var list = models || [];
                if (list.length === 0) {
                    var opt = document.createElement('option');
                    opt.value = currentModel || '';
                    opt.textContent = currentModel || '(none)';
                    modelSelect.appendChild(opt);
                    return;
                }
                for (var i = 0; i < list.length; i++) {
                    var opt = document.createElement('option');
                    var mid = list[i].id || list[i];
                    opt.value = mid;
                    opt.textContent = mid;
                    if (mid === currentModel) { opt.selected = true; }
                    modelSelect.appendChild(opt);
                }
                if (currentModel && !modelSelect.value) {
                    var extra = document.createElement('option');
                    extra.value = currentModel;
                    extra.textContent = currentModel + ' (current)';
                    extra.selected = true;
                    modelSelect.insertBefore(extra, modelSelect.firstChild);
                }
            }

            // ─── New Chat ───
            var newChatBtn = document.getElementById('new-chat-btn');
            if (newChatBtn) {
                newChatBtn.addEventListener('click', function() {
                    createNewSession();
                    addMessage('New chat started', 'system');
                });
            }

            // ─── History toggle ───
            var historyBtn = document.getElementById('history-btn');
            var historyPanel = document.getElementById('history-panel');
            if (historyBtn && historyPanel) {
                historyBtn.addEventListener('click', function() {
                    renderHistory();
                    historyPanel.classList.toggle('open');
                });
            }

            // ─── Settings Panel ───
            gearBtn.addEventListener('click', function() {
                settingsPanel.classList.toggle('open');
            });

            // Default models per provider (instant, no API call needed)
            var defaultModels = {
                deepseek: ['deepseek-chat', 'deepseek-reasoner'],
                gemini: ['gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-2.0-flash'],
                openai: ['gpt-4o', 'gpt-4o-mini', 'o3-mini'],
                anthropic: ['claude-3-7-sonnet-20250219', 'claude-3-5-sonnet-20241022', 'claude-3-5-haiku-20241022']
            };

            providerSelect.addEventListener('change', function() {
                var prov = providerSelect.value;
                var defs = defaultModels[prov] || [];
                var defList = defs.map(function(id) { return { id: id }; });
                populateModels(defList, defs[0] || '');
                vscode.postMessage({ type: 'query_models', provider: prov });
            });
            saveModelBtn.addEventListener('click', function() {
                var provider = providerSelect.value;
                var model = modelSelect ? modelSelect.value : '';
                if (!model) { addMessage('Please select a model.', 'error'); return; }
                vscode.postMessage({ type: 'set_model', provider: provider, model: model });
                settingsPanel.classList.remove('open');
                addMessage('Switching to ' + provider + '/' + model + '...', 'system');
            });

            function sendTask() {
                const task = inputEl.value.trim();
                if (!task || isRunning) return;
                currentPrompt = task;
                currentAnswer = '';
                stepCount = 0;
                streamBuffer = '';
                addMessage(task, 'user');
                inputEl.value = '';
                vscode.postMessage({ type: 'run_task', task: task, sessionId: activeSessionId });
            }

            sendBtn.addEventListener('click', sendTask);
            inputEl.addEventListener('keydown', function(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendTask(); } });
            stopBtn.addEventListener('click', function() {
                vscode.postMessage({ type: 'stop' });
                addMessage('Stop requested...', 'system');
            });

            window.addEventListener('message', function(event) {
                const msg = event.data;
                if (!msg || !msg.type) return;
                switch (msg.type) {
                    case 'connected':
                        setStatus('connected', 'Ready');
                        var cProv = msg.llm_provider || '';
                        var cModel = msg.llm_model || '';
                        if (cProv && providerSelect) { providerSelect.value = cProv; }
                        cachedModels = msg.available_models || [];
                        populateModels(cachedModels, cModel);
                        if (modelLabel) { modelLabel.textContent = cProv && cModel ? cProv + '/' + cModel : cModel || cProv || ''; }
                        break;
                    case 'disconnected': setStatus('disconnected', 'Disconnected'); setRunning(false); break;
                    case 'task_started': setRunning(true); setStatus('running', 'Running...'); createActionFeed(); break;
                    case 'action': endStreaming(); addAction(msg.step, msg.tool, msg.args); break;
                    case 'result': updateLastAction(msg.success, msg.output, msg.error); break;
                    case 'llm_token': appendStream(msg.text || ''); streamBuffer += (msg.text || ''); break;
                    case 'task_complete':
                        endStreaming();
                        setRunning(false);
                        setStatus('connected', 'Ready');
                        finalizeActionFeed(msg.result || {});
                        // Save the full agent response as a message
                        var summary = (msg.result || {}).summary || '';
                        var responseText = streamBuffer || summary || '(no response)';
                        addMessage(responseText, 'assistant');
                        streamBuffer = '';
                        addCompletionCard(msg.result || {});
                        // Persist full session to file
                        vscode.postMessage({ type: 'save_chat', sessionId: activeSessionId, title: deriveTitle(chatMessages), messages: chatMessages });
                        break;
                    case 'task_error': endStreaming(); setRunning(false); setStatus('connected', 'Ready'); addMessage('Error: ' + (msg.error || 'Unknown'), 'error'); streamBuffer = ''; break;
                    case 'error': addMessage('Server: ' + (msg.message || msg.error || 'Unknown error'), 'error'); break;
                    case 'model_changed':
                        var mProv = msg.provider || '';
                        var mModel = msg.model || '';
                        var mLabel = mProv && mModel ? mProv + '/' + mModel : mModel || mProv || '?';
                        addMessage('\u2705 Model switched to ' + mLabel, 'system');
                        if (modelLabel) { modelLabel.textContent = mLabel; }
                        if (mProv && providerSelect) { providerSelect.value = mProv; }
                        if (mModel && modelSelect) { modelSelect.value = mModel; }
                        break;
                    case 'models_list':
                        if (msg.provider === providerSelect.value) {
                            var curVal = modelSelect ? modelSelect.value : '';
                            populateModels(msg.models || [], curVal);
                        }
                        break;
                    case 'file_changed':
                        addFileChangeCard(msg.path, msg.diff, msg.tool, msg.snapshot, msg.is_new);
                        // Also open the file in the editor
                        vscode.postMessage({ type: 'open_file', path: msg.path });
                        break;
                    case 'file_reverted':
                        // Mark the diff card as reverted
                        var cards = document.querySelectorAll('.diff-card[data-path="' + (msg.path || '') + '"]');
                        cards.forEach(function(c) { c.classList.add('reverted'); });
                        addMessage('↩ ' + (msg.message || 'File reverted'), 'system');
                        // Re-open the reverted file to refresh editor
                        vscode.postMessage({ type: 'open_file', path: msg.path });
                        break;
                }
            });

            function setStatus(state, text) { statusDot.className = 'dot ' + state; statusText.textContent = text; }
            function setRunning(running) { isRunning = running; sendBtn.classList.toggle('hidden', running); stopBtn.classList.toggle('hidden', !running); inputEl.disabled = running; }

            // Diff Card for File Changes
            var fileSnapshots = {};

            function addFileChangeCard(filePath, diff, tool, snapshot, isNew) {
                if (filePath) fileSnapshots[filePath] = snapshot;
                var card = document.createElement('div');
                card.className = 'diff-card';
                card.setAttribute('data-path', filePath || '');
                var basename = (filePath || '').replace(/.*[\\/\\\\]/, '') || 'unknown';
                var toolLabel = isNew ? 'new file' : (tool === 'file_write' ? 'created' : 'edited');
                var header = document.createElement('div');
                header.className = 'diff-header';
                var fileSpan = document.createElement('span');
                fileSpan.className = 'diff-file';
                fileSpan.textContent = basename + ' (' + toolLabel + ')';
                var actionsDiv = document.createElement('div');
                actionsDiv.className = 'diff-actions';
                var toggleBtn = document.createElement('button');
                toggleBtn.className = 'toggle-btn';
                toggleBtn.title = 'Toggle diff';
                toggleBtn.textContent = '>';
                var revertBtn = document.createElement('button');
                revertBtn.className = 'revert-btn';
                revertBtn.title = 'Revert to original';
                revertBtn.textContent = 'Revert';
                actionsDiv.appendChild(toggleBtn);
                actionsDiv.appendChild(revertBtn);
                header.appendChild(fileSpan);
                header.appendChild(actionsDiv);
                var body = document.createElement('div');
                body.className = 'diff-body';
                if (diff) {
                    var dlines = diff.split('\\n');
                    for (var i = 0; i < dlines.length; i++) {
                        var ln = document.createElement('div');
                        ln.className = 'diff-line';
                        var dl = dlines[i];
                        if (dl.charAt(0) === '+' && dl.substring(0,3) !== '+++') { ln.className += ' add'; }
                        else if (dl.charAt(0) === '-' && dl.substring(0,3) !== '---') { ln.className += ' del'; }
                        else if (dl.substring(0,2) === '@@') { ln.className += ' hunk'; }
                        ln.textContent = dl;
                        body.appendChild(ln);
                    }
                } else {
                    var nd = document.createElement('div');
                    nd.className = 'diff-line';
                    nd.style.opacity = '0.5';
                    nd.textContent = 'No diff available';
                    body.appendChild(nd);
                }
                card.appendChild(header);
                card.appendChild(body);
                messagesEl.appendChild(card);
                scrollToBottom();
                toggleBtn.addEventListener('click', function(e) {
                    e.stopPropagation();
                    body.classList.toggle('open');
                    toggleBtn.textContent = body.classList.contains('open') ? 'v' : '>';
                });
                header.addEventListener('click', function() {
                    body.classList.toggle('open');
                    toggleBtn.textContent = body.classList.contains('open') ? 'v' : '>';
                });
                revertBtn.addEventListener('click', function(e) {
                    e.stopPropagation();
                    var snap = fileSnapshots[filePath];
                    vscode.postMessage({ type: 'revert_file', path: filePath, snapshot: snap === undefined ? null : snap });
                });
            }
            function addMessage(text, type, skipSave) {
                var el = document.createElement('div');
                el.className = 'message ' + type;
                el.textContent = text;
                messagesEl.appendChild(el);
                scrollToBottom();
                if (!skipSave && (type === 'user' || type === 'complete' || type === 'error' || type === 'assistant')) {
                    chatMessages.push({ type: type, text: text, ts: Date.now() });
                    if (chatMessages.length > 100) chatMessages = chatMessages.slice(-100);
                    saveState();
                }
            }
            var chatMessages = [];
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
                if (currentPrompt) parts.push('Prompt:\\n' + currentPrompt);
                if (currentAnswer) parts.push('Answer:\\n' + currentAnswer);
                // Gather all step text
                var steps = currentFeedBody ? currentFeedBody.querySelectorAll('.action-step') : [];
                if (steps.length) {
                    var stepLines = [];
                    steps.forEach(function(s) {
                        var header = s.querySelector('.action-header');
                        var body = s.querySelector('.action-body');
                        var headerText = header ? header.textContent.replace(/[+\-]$/, '').trim() : '';
                        var bodyText = body ? body.textContent : '';
                        stepLines.push(headerText + '\\n' + bodyText);
                    });
                    parts.push('Action Feed (' + steps.length + ' steps):\\n' + stepLines.join('\\n\\n' + '-'.repeat(60) + '\\n\\n'));
                }
                var text = parts.join('\\n\\n' + '='.repeat(60) + '\\n\\n');
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
                el.textContent = lines.join('\\n');
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
    ${scriptEnd}
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
