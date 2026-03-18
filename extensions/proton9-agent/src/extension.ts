import * as vscode from 'vscode';
import * as path from 'path';
import { spawn, ChildProcess } from 'child_process';
import { ForgeWebSocket } from './websocket';
import { SidebarProvider } from './sidebar';
import { StatusBarController } from './statusBar';
import { ChatPanel } from './chatPanel';
import { InlineCompletionProvider } from './inlineCompletion';

let forge: ForgeWebSocket;
let statusBar: StatusBarController;
let sidebarProvider: SidebarProvider;
let serverProcess: ChildProcess | null = null;
let serverOutputChannel: vscode.OutputChannel;

function startP9Server(extensionPath: string, outputChannel: vscode.OutputChannel): ChildProcess | null {
    const agentDir = path.join(extensionPath, '..', '..', 'agent');
    const entryPoint = path.join(agentDir, 'core', 'server.py');

    outputChannel.appendLine('[Pide] Starting P9 server...');
    outputChannel.appendLine('[Pide] Agent dir: ' + agentDir);

    try {
        const proc = spawn('python', [entryPoint], {
            cwd: agentDir,
            env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
            stdio: ['ignore', 'pipe', 'pipe'],
        });

        proc.stdout?.on('data', (data: Buffer) => {
            const lines = data.toString().trim();
            if (lines) {
                outputChannel.appendLine('[P9] ' + lines);
            }
        });

        proc.stderr?.on('data', (data: Buffer) => {
            const lines = data.toString().trim();
            if (lines) {
                outputChannel.appendLine('[P9 ERR] ' + lines);
            }
        });

        proc.on('error', (err) => {
            outputChannel.appendLine('[Pide] Failed to start P9: ' + err.message);
            vscode.window.showErrorMessage('Proton9: Failed to start agent server: ' + err.message);
        });

        proc.on('exit', (code, signal) => {
            outputChannel.appendLine('[Pide] P9 server exited (code=' + code + ' signal=' + signal + ')');
            serverProcess = null;
        });

        outputChannel.appendLine('[Pide] P9 server spawned (PID: ' + proc.pid + ')');
        return proc;
    } catch (err) {
        outputChannel.appendLine('[Pide] Spawn error: ' + err);
        return null;
    }
}

export function activate(context: vscode.ExtensionContext): void {
    try {
        console.log('[Proton9] Extension activating...');

        // Create Output Channel for displaying results (bypasses broken webview postMessage)
        const outputChannel = vscode.window.createOutputChannel('Proton9 Agent', { log: true });

        forge = new ForgeWebSocket(() =>
            vscode.workspace.getConfiguration('Proton9').get<string>('serverUrl', 'ws://localhost:9321')
        );

        // Stable client ID for session continuity (persists across restarts)
        let clientId = context.globalState.get<string>('proton9.clientId');
        if (!clientId) {
            clientId = 'p9-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
            context.globalState.update('proton9.clientId', clientId);
        }
        forge.clientId = clientId;
        console.log('[Proton9] Client ID:', clientId);

        // Token buffering for Output channel (avoids word-per-line noise)
        let tokenBuffer = '';
        let tokenFlushTimer: ReturnType<typeof setTimeout> | null = null;

        // Log ALL agent events to the Output Channel
        context.subscriptions.push(
            forge.onEvent((msg) => {
                switch (msg.type) {
                    case 'task_started':
                        outputChannel.show(true);
                        outputChannel.appendLine('═══════════════════════════════════════════');
                        outputChannel.appendLine('▶ Task Started');
                        outputChannel.appendLine('═══════════════════════════════════════════');
                        break;
                    case 'action':
                        outputChannel.appendLine('[Step ' + (msg.step || '?') + '] 🔧 ' + (msg.tool || 'thinking'));
                        if (msg.args) {
                            const argStr = Object.keys(msg.args).map(k => '  ' + k + ': ' + String(msg.args[k]).substring(0, 120)).join('\n');
                            outputChannel.appendLine(argStr);
                        }
                        break;
                    case 'result':
                        if (msg.success) {
                            outputChannel.appendLine('  ✅ ' + (msg.output || '(ok)').substring(0, 200));
                        } else {
                            outputChannel.appendLine('  ❌ ' + (msg.error || 'unknown error'));
                        }
                        break;
                    case 'llm_token': {
                        // Buffer tokens to avoid word-per-line noise in Output channel
                        if (!tokenBuffer) { tokenBuffer = ''; }
                        tokenBuffer += (msg.text || '');
                        if (tokenFlushTimer) { clearTimeout(tokenFlushTimer); }
                        // Flush on newlines, sentence end, or after 500ms
                        if (tokenBuffer.includes('\n') || /[.!?]\s*$/.test(tokenBuffer)) {
                            outputChannel.append(tokenBuffer);
                            tokenBuffer = '';
                        } else {
                            tokenFlushTimer = setTimeout(() => {
                                if (tokenBuffer) {
                                    outputChannel.append(tokenBuffer);
                                    tokenBuffer = '';
                                }
                            }, 500);
                        }
                        break;
                    }
                    case 'task_complete':
                        outputChannel.appendLine('');
                        outputChannel.appendLine('═══════════════════════════════════════════');
                        outputChannel.appendLine('✅ Task Complete');
                        if (msg.result?.summary) outputChannel.appendLine(msg.result.summary);
                        if (msg.result?.files_changed?.length) {
                            outputChannel.appendLine('Files: ' + msg.result.files_changed.join(', '));
                        }
                        outputChannel.appendLine('═══════════════════════════════════════════');
                        vscode.window.showInformationMessage('Proton9: Task complete!');
                        break;
                    case 'task_error':
                        outputChannel.appendLine('');
                        outputChannel.appendLine('❌ Task Error: ' + (msg.error || 'Unknown'));
                        vscode.window.showErrorMessage('Proton9: ' + (msg.error || 'Task failed'));
                        break;
                }
            })
        );

        sidebarProvider = new SidebarProvider(context.extensionUri, forge);

        // Track active text editor so sidebar can reference it even when webview has focus
        SidebarProvider.lastActiveEditor = vscode.window.activeTextEditor;
        context.subscriptions.push(
            vscode.window.onDidChangeActiveTextEditor((editor) => {
                if (editor) {
                    SidebarProvider.lastActiveEditor = editor;
                }
            })
        );

        context.subscriptions.push(
            vscode.window.registerWebviewViewProvider(SidebarProvider.viewType, sidebarProvider, {
                webviewOptions: { retainContextWhenHidden: true },
            })
        );

        statusBar = new StatusBarController(forge);
        context.subscriptions.push(statusBar);

        context.subscriptions.push(
            vscode.commands.registerCommand('Proton9.sendTask', async () => {
                const task = await vscode.window.showInputBox({
                    prompt: 'Enter task for Proton9',
                    placeHolder: 'e.g. "Fix the login bug"',
                });
                if (task) {
                    const workDir =
                        vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '';
                    const maxIter = vscode.workspace
                        .getConfiguration('Proton9')
                        .get<number>('maxIterations', 50);
                    forge.runTask(task, workDir, maxIter);
                }
            }),

            vscode.commands.registerCommand('Proton9.stop', () => {
                forge.stopTask();
            }),

            vscode.commands.registerCommand('Proton9.reconnect', () => {
                forge.connect();
            }),

            vscode.commands.registerCommand('Proton9.runTaskDirect', (task: string) => {
                console.log('[Proton9] runTaskDirect called with:', task);
                if (task && typeof task === 'string') {
                    const workDir =
                        vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '';
                    const maxIter = vscode.workspace
                        .getConfiguration('Proton9')
                        .get<number>('maxIterations', 50);
                    forge.runTask(task, workDir, maxIter);
                    vscode.window.showInformationMessage('[P9] Task sent: ' + task.substring(0, 50));
                }
            }),

            vscode.commands.registerCommand('Proton9.openChat', () => {
                // Open P9's web UI in VS Code's Simple Browser (guaranteed to work)
                const guiUrl = 'http://localhost:9322';
                vscode.commands.executeCommand('simpleBrowser.show', guiUrl);
            }),

            vscode.commands.registerCommand('Proton9.stopTask', () => {
                forge.stopTask();
            }),

            vscode.commands.registerCommand('Proton9.newSlot', () => {
                const slotNum = (context.globalState.get<number>('proton9.slotCounter') || 1) + 1;
                context.globalState.update('proton9.slotCounter', slotNum);
                const slotId = 'p9-' + slotNum;

                const panel = vscode.window.createWebviewPanel(
                    'proton9.slot',
                    '🤖 ' + slotId.toUpperCase(),
                    vscode.ViewColumn.Beside,
                    { enableScripts: true, retainContextWhenHidden: true }
                );

                panel.webview.html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body { font-family:var(--vscode-font-family); color:var(--vscode-foreground); background:var(--vscode-editor-background); height:100vh; display:flex; flex-direction:column; padding:16px; }
    h2 { font-size:16px; color:#4ec9b0; margin-bottom:12px; }
    .badge { display:inline-block; padding:2px 8px; border-radius:3px; background:rgba(78,201,176,0.15); color:#4ec9b0; font-size:11px; font-weight:600; margin-bottom:12px; }
    label { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.3px; display:block; margin-bottom:4px; margin-top:12px; }
    select, input[type=text], textarea { width:100%; padding:6px 10px; border:1px solid var(--vscode-input-border, #444); background:var(--vscode-input-background); color:var(--vscode-input-foreground); font-family:var(--vscode-font-family); font-size:12px; border-radius:4px; outline:none; }
    select:focus, input:focus, textarea:focus { border-color:var(--vscode-focusBorder); }
    textarea { min-height:80px; resize:vertical; margin-top:4px; }
    .launch-btn { width:100%; padding:8px; margin-top:16px; background:var(--vscode-button-background); color:var(--vscode-button-foreground); border:none; border-radius:4px; font-size:13px; font-weight:600; cursor:pointer; }
    .launch-btn:hover { background:var(--vscode-button-hoverBackground); }
    #status { margin-top:12px; font-size:12px; color:var(--vscode-descriptionForeground); }
    #messages { flex:1; overflow-y:auto; margin-top:12px; border-top:1px solid var(--vscode-widget-border, #333); padding-top:8px; }
    .msg { padding:6px 10px; margin:4px 0; border-radius:4px; font-size:12px; }
    .msg.system { background:var(--vscode-editor-background); border:1px solid var(--vscode-widget-border, #444); }
    .msg.user { background:var(--vscode-button-background); color:var(--vscode-button-foreground); }
</style>
</head>
<body>
    <h2>🤖 ${slotId.toUpperCase()}</h2>
    <span class="badge">Agent Slot — Detached</span>
    <label>Provider</label>
    <select id="provider">
        <option value="deepseek">DeepSeek</option>
        <option value="gemini">Gemini</option>
        <option value="anthropic">Anthropic</option>
        <option value="openai">OpenAI</option>
    </select>
    <label>Task</label>
    <textarea id="task" placeholder="Describe the task for this agent..."></textarea>
    <button class="launch-btn" id="launch">▶ Launch Agent</button>
    <div id="status"></div>
    <div id="messages"></div>
    <script>
        const vscode = acquireVsCodeApi();
        const launchBtn = document.getElementById('launch');
        const statusEl = document.getElementById('status');
        const messagesEl = document.getElementById('messages');
        launchBtn.addEventListener('click', () => {
            const task = document.getElementById('task').value;
            const provider = document.getElementById('provider').value;
            if (!task.trim()) return;
            vscode.postMessage({ type: 'launch_slot', task, provider, slot_id: '${slotId}' });
            statusEl.textContent = '⏳ Launching...';
            launchBtn.disabled = true;
            launchBtn.textContent = '⏳ Running...';
        });
        window.addEventListener('message', (event) => {
            const msg = event.data;
            if (msg.type === 'slot_update') {
                statusEl.textContent = msg.status || '';
            } else if (msg.type === 'slot_message') {
                const div = document.createElement('div');
                div.className = 'msg ' + (msg.role || 'system');
                div.textContent = msg.text || '';
                messagesEl.appendChild(div);
                messagesEl.scrollTop = messagesEl.scrollHeight;
            }
        });
    </script>
</body>
</html>`;

                // Handle messages from the slot panel
                panel.webview.onDidReceiveMessage((msg: any) => {
                    if (msg.type === 'launch_slot') {
                        const workDir = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '';
                        forge.send({
                            type: 'run_task',
                            task: msg.task,
                            working_dir: workDir,
                            provider: msg.provider,
                            slot_id: msg.slot_id,
                        });
                        panel.webview.postMessage({ type: 'slot_update', status: '🚀 Agent launched on ' + msg.provider });
                        vscode.window.showInformationMessage('P9: Agent slot ' + msg.slot_id + ' launched on ' + msg.provider);
                    }
                });

                // Forward relevant events to the slot panel
                const eventSub = forge.onEvent((msg: any) => {
                    if (msg.slot_id === slotId) {
                        if (msg.type === 'slot_complete') {
                            panel.webview.postMessage({ type: 'slot_update', status: '✅ Complete' });
                            panel.webview.postMessage({ type: 'slot_message', role: 'system', text: '✅ Task complete' });
                        } else if (msg.type === 'action') {
                            panel.webview.postMessage({ type: 'slot_message', role: 'system', text: '[Step ' + msg.step + '] ' + msg.tool });
                        }
                    }
                });

                panel.onDidDispose(() => { eventSub.dispose(); });
            })
        );

        // Inline Autocomplete — Tab to accept ghost text
        const autocompleteProvider = new InlineCompletionProvider(forge);
        context.subscriptions.push(
            vscode.languages.registerInlineCompletionItemProvider(
                { pattern: '**' },
                autocompleteProvider,
            )
        );
        context.subscriptions.push(
            vscode.commands.registerCommand('Proton9.toggleAutocomplete', () => {
                const config = vscode.workspace.getConfiguration('Proton9');
                const current = config.get<boolean>('autocomplete.enabled', true);
                config.update('autocomplete.enabled', !current, true);
                autocompleteProvider.setEnabled(!current);
                vscode.window.showInformationMessage(
                    `Proton9 Autocomplete: ${!current ? 'ON' : 'OFF'}`
                );
            })
        );

        context.subscriptions.push(
            forge.onEvent((msg) => {
                if (msg.type === 'connected') {
                    const wsFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                    if (wsFolder) {
                        forge.setWorkspace(wsFolder);
                    }
                }
            })
        );

        // Stage C: Auto-launch P9 server, then connect
        serverProcess = startP9Server(context.extensionPath, outputChannel);
        if (serverProcess) {
            outputChannel.appendLine('[Pide] Waiting 3s for P9 to start...');
            setTimeout(() => {
                forge.connect();
                console.log('[Proton9] Connecting to auto-launched P9 server');
            }, 3000);
        } else {
            // Fallback: try to connect to existing server
            forge.connect();
        }
        console.log('[Proton9] Extension activated successfully');
    } catch (err) {
        console.error('[Proton9] Activation failed:', err);
        vscode.window.showErrorMessage(`Proton9 failed to activate: ${err}`);
    }
}

export function deactivate(): void {
    try {
        // Kill P9 server when Pide closes
        if (serverProcess && !serverProcess.killed) {
            serverProcess.kill();
            serverOutputChannel?.appendLine('[Pide] P9 server stopped.');
        }
        forge?.dispose();
        statusBar?.dispose();
        sidebarProvider?.dispose();
    } catch { /* swallow */ }
}
