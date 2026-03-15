import * as vscode from 'vscode';
import { ForgeWebSocket } from './websocket';

function getWorkspaceDir(): string {
    return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '';
}

/**
 * Chat panel that opens as an editor tab.
 * The webview connects DIRECTLY to the P9 WebSocket server,
 * bypassing the broken postMessage bridge entirely.
 */
export class ChatPanel {
    public static readonly viewType = 'proton9.chatPanel';
    private static instance: ChatPanel | undefined;

    private constructor(
        private panel: vscode.WebviewPanel,
        private forge: ForgeWebSocket,
        private outputChannel: vscode.OutputChannel
    ) {
        const serverUrl = vscode.workspace.getConfiguration('Proton9')
            .get<string>('serverUrl', 'ws://localhost:9321');
        const workspaceDir = getWorkspaceDir();

        panel.webview.html = this.getHtml(serverUrl, workspaceDir);

        panel.onDidDispose(() => {
            ChatPanel.instance = undefined;
        });
    }

    public static createOrShow(
        extensionUri: vscode.Uri,
        forge: ForgeWebSocket,
        outputChannel: vscode.OutputChannel
    ): void {
        if (ChatPanel.instance) {
            ChatPanel.instance.panel.reveal(vscode.ViewColumn.Beside);
            return;
        }

        const panel = vscode.window.createWebviewPanel(
            ChatPanel.viewType,
            'Proton9 Chat',
            vscode.ViewColumn.Beside,
            {
                enableScripts: true,
                retainContextWhenHidden: true,
            }
        );

        ChatPanel.instance = new ChatPanel(panel, forge, outputChannel);
    }

    private getHtml(serverUrl: string, workspaceDir: string): string {
        // Convert ws:// URL for CSP
        const wsHost = serverUrl.replace('ws://', '').replace('wss://', '').split('/')[0];

        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src ws://${wsHost};">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: var(--vscode-font-family, 'Segoe UI', sans-serif);
            color: var(--vscode-foreground);
            background: var(--vscode-editor-background);
            height: 100vh;
            display: flex;
            flex-direction: column;
        }
        #status-bar {
            padding: 8px 12px;
            background: var(--vscode-sideBar-background);
            border-bottom: 1px solid var(--vscode-panel-border);
            font-size: 12px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .dot {
            width: 8px; height: 8px;
            border-radius: 50%;
            display: inline-block;
        }
        .dot.connected { background: #4ec9b0; }
        .dot.disconnected { background: #f44747; }
        .dot.connecting { background: #dcdcaa; }
        #messages {
            flex: 1;
            overflow-y: auto;
            padding: 12px;
        }
        .msg {
            margin-bottom: 12px;
            padding: 10px 14px;
            border-radius: 8px;
            font-size: 13px;
            line-height: 1.5;
            max-width: 90%;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .msg.user {
            background: var(--vscode-button-background);
            color: var(--vscode-button-foreground);
            margin-left: auto;
            border-radius: 8px 8px 2px 8px;
        }
        .msg.assistant {
            background: var(--vscode-editorWidget-background);
            border: 1px solid var(--vscode-panel-border);
            border-radius: 8px 8px 8px 2px;
        }
        .msg.system {
            background: transparent;
            color: var(--vscode-descriptionForeground);
            font-style: italic;
            font-size: 11px;
            text-align: center;
            max-width: 100%;
        }
        .msg.action {
            background: var(--vscode-textBlockQuote-background);
            border-left: 3px solid var(--vscode-textLink-foreground);
            font-size: 12px;
            font-family: var(--vscode-editor-font-family, monospace);
        }
        #input-area {
            padding: 12px;
            background: var(--vscode-sideBar-background);
            border-top: 1px solid var(--vscode-panel-border);
        }
        #task-input {
            width: 100%;
            background: var(--vscode-input-background);
            color: var(--vscode-input-foreground);
            border: 1px solid var(--vscode-input-border, #333);
            border-radius: 6px;
            padding: 10px 12px;
            font-family: inherit;
            font-size: 13px;
            resize: vertical;
            min-height: 60px;
        }
        #task-input:focus { outline: 1px solid var(--vscode-focusBorder); }
        #btn-row { display: flex; gap: 8px; margin-top: 8px; }
        button {
            flex: 1;
            padding: 8px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
        }
        #send-btn {
            background: var(--vscode-button-background);
            color: var(--vscode-button-foreground);
        }
        #send-btn:hover { background: var(--vscode-button-hoverBackground); }
        #stop-btn {
            background: var(--vscode-errorForeground);
            color: white;
            display: none;
        }
    </style>
</head>
<body>
    <div id="status-bar">
        <span id="status-dot" class="dot connecting"></span>
        <span id="status-text">Connecting to P9...</span>
    </div>
    <div id="messages"></div>
    <div id="input-area">
        <textarea id="task-input" placeholder="Describe your task..." rows="3"></textarea>
        <div id="btn-row">
            <button id="send-btn">▶ Run</button>
            <button id="stop-btn">■ Stop</button>
        </div>
    </div>
    <script>
        // Direct WebSocket connection to P9 server — NO postMessage needed!
        var WS_URL = '${serverUrl}';
        var WORKSPACE_DIR = ${JSON.stringify(workspaceDir)};
        var ws = null;
        var isRunning = false;
        var currentAnswer = '';
        var answerEl = null;

        var messagesEl = document.getElementById('messages');
        var inputEl = document.getElementById('task-input');
        var sendBtn = document.getElementById('send-btn');
        var stopBtn = document.getElementById('stop-btn');
        var statusDot = document.getElementById('status-dot');
        var statusText = document.getElementById('status-text');

        function addMessage(text, type) {
            var div = document.createElement('div');
            div.className = 'msg ' + type;
            div.textContent = text;
            messagesEl.appendChild(div);
            messagesEl.scrollTop = messagesEl.scrollHeight;
            return div;
        }

        function setRunning(running) {
            isRunning = running;
            sendBtn.style.display = running ? 'none' : 'block';
            stopBtn.style.display = running ? 'block' : 'none';
        }

        function connectWS() {
            statusDot.className = 'dot connecting';
            statusText.textContent = 'Connecting to P9...';

            ws = new WebSocket(WS_URL);

            ws.onopen = function() {
                statusDot.className = 'dot connected';
                statusText.textContent = 'Connected to P9';
            };

            ws.onclose = function() {
                statusDot.className = 'dot disconnected';
                statusText.textContent = 'Disconnected';
                setRunning(false);
                // Reconnect after 3s
                setTimeout(connectWS, 3000);
            };

            ws.onerror = function() {
                statusDot.className = 'dot disconnected';
                statusText.textContent = 'Connection error';
            };

            ws.onmessage = function(event) {
                var msg;
                try { msg = JSON.parse(event.data); } catch { return; }

                switch (msg.type) {
                    case 'connected':
                        statusDot.className = 'dot connected';
                        statusText.textContent = 'Connected (v' + (msg.version || '') + ')';
                        break;
                    case 'task_started':
                        setRunning(true);
                        addMessage('Agent is working...', 'system');
                        break;
                    case 'action':
                        var tool = msg.tool || 'thinking';
                        var step = msg.step || '?';
                        var detail = '';
                        if (msg.args) {
                            var keys = Object.keys(msg.args);
                            detail = keys.map(function(k) {
                                return k + ': ' + String(msg.args[k]).substring(0, 80);
                            }).join(', ');
                        }
                        addMessage('[Step ' + step + '] ' + tool + (detail ? '\\n' + detail : ''), 'action');
                        break;
                    case 'result':
                        if (msg.success) {
                            addMessage('OK: ' + (msg.output || '').substring(0, 200), 'action');
                        } else {
                            addMessage('Error: ' + (msg.error || ''), 'action');
                        }
                        break;
                    case 'llm_token':
                        if (!answerEl) {
                            answerEl = addMessage('', 'assistant');
                        }
                        currentAnswer += (msg.text || '');
                        answerEl.textContent = currentAnswer;
                        messagesEl.scrollTop = messagesEl.scrollHeight;
                        break;
                    case 'task_complete':
                        setRunning(false);
                        var summary = (msg.result && msg.result.summary) ? msg.result.summary : 'Done';
                        // Show the agent's response as a proper assistant message
                        if (!answerEl) {
                            // No streaming tokens were received — display summary as the response
                            addMessage(summary, 'assistant');
                        } else {
                            // Streaming was active — just add a completion note
                            addMessage('✅ Task complete', 'system');
                        }
                        answerEl = null;
                        currentAnswer = '';
                        break;
                    case 'task_error':
                        setRunning(false);
                        addMessage('Error: ' + (msg.error || 'Unknown'), 'system');
                        answerEl = null;
                        currentAnswer = '';
                        break;
                }
            };
        }

        // Send task directly to P9 via WebSocket
        sendBtn.addEventListener('click', function() {
            var task = inputEl.value.trim();
            if (!task || isRunning || !ws || ws.readyState !== 1) return;
            currentAnswer = '';
            answerEl = null;
            addMessage(task, 'user');
            inputEl.value = '';
            ws.send(JSON.stringify({
                type: 'run_task',
                task: task,
                working_dir: WORKSPACE_DIR || undefined,
                max_iterations: 50
            }));
        });

        inputEl.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendBtn.click();
            }
        });

        stopBtn.addEventListener('click', function() {
            if (ws && ws.readyState === 1) {
                ws.send(JSON.stringify({ type: 'stop' }));
            }
            addMessage('Stop requested...', 'system');
        });

        // Start connection
        connectWS();
    </script>
</body>
</html>`;
    }
}
