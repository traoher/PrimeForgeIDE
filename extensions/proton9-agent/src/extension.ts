import * as vscode from 'vscode';
import { ForgeWebSocket } from './websocket';
import { SidebarProvider } from './sidebar';
import { StatusBarController } from './statusBar';

let forge: ForgeWebSocket;
let statusBar: StatusBarController;
let sidebarProvider: SidebarProvider;

export function activate(context: vscode.ExtensionContext): void {
    try {
        console.log('[Proton9] Extension activating...');

        // Create Output Channel for displaying results (bypasses broken webview postMessage)
        const outputChannel = vscode.window.createOutputChannel('Proton9 Agent', { log: true });

        forge = new ForgeWebSocket(() =>
            vscode.workspace.getConfiguration('Proton9').get<string>('serverUrl', 'ws://localhost:9321')
        );

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
                    case 'llm_token':
                        outputChannel.append(msg.text || '');
                        break;
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
                        vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ||
                        'c:\\DATA\\PrimeNexus\\Proton9';
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
                        vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ||
                        'c:\\DATA\\PrimeNexus\\Proton9';
                    const maxIter = vscode.workspace
                        .getConfiguration('Proton9')
                        .get<number>('maxIterations', 50);
                    forge.runTask(task, workDir, maxIter);
                    vscode.window.showInformationMessage('[P9] Task sent: ' + task.substring(0, 50));
                }
            }),

            vscode.commands.registerCommand('Proton9.stopTask', () => {
                forge.stopTask();
            })
        );

        context.subscriptions.push(
            forge.onEvent((msg) => {
                vscode.window.showInformationMessage(`[P9 DEBUG] Event: ${msg.type}`);
                if (msg.type === 'connected') {
                    const wsFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                    if (wsFolder) {
                        forge.setWorkspace(wsFolder);
                    }
                }
            })
        );

        forge.connect();
        vscode.window.showInformationMessage('[P9 DEBUG] forge.connect() called');
        console.log('[Proton9] Extension activated successfully');
    } catch (err) {
        console.error('[Proton9] Activation failed:', err);
        vscode.window.showErrorMessage(`Proton9 failed to activate: ${err}`);
    }
}

export function deactivate(): void {
    try {
        forge?.dispose();
        statusBar?.dispose();
        sidebarProvider?.dispose();
    } catch { /* swallow */ }
}
