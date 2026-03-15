import * as vscode from 'vscode';
import { ForgeWebSocket } from './websocket';

export class StatusBarController {
    private item: vscode.StatusBarItem;
    private disposables: vscode.Disposable[] = [];

    constructor(private forge: ForgeWebSocket) {
        this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
        this.item.command = 'Proton9.sendTask';
        this.updateDisplay();
        this.item.show();

        this.disposables.push(
            forge.onEvent((msg) => {
                switch (msg.type) {
                    case 'connected':
                        this.updateDisplay();
                        break;
                    case 'disconnected':
                        this.updateDisplay();
                        break;
                    case 'task_started':
                        this.item.text = '$(sync~spin) Proton9: Running...';
                        break;
                    case 'action':
                        this.item.text = `$(sync~spin) Proton9: Step ${msg.step || '?'}`;
                        this.item.tooltip = `Tool: ${msg.tool || 'thinking'}`;
                        break;
                    case 'task_complete':
                        this.item.text = '$(check) Proton9: Done';
                        setTimeout(() => this.updateDisplay(), 5000);
                        break;
                    case 'task_error':
                        this.item.text = '$(error) Proton9: Error';
                        setTimeout(() => this.updateDisplay(), 8000);
                        break;
                }
            })
        );
    }

    private updateDisplay(): void {
        if (this.forge.connected) {
            this.item.text = '$(zap) Proton9';
            this.item.tooltip = `Connected (v${this.forge.serverVersion})`;
            this.item.backgroundColor = undefined;
        } else {
            this.item.text = '$(debug-disconnect) Proton9';
            this.item.tooltip = 'Disconnected — click to retry';
            this.item.command = 'Proton9.reconnect';
            this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
        }
    }

    dispose(): void {
        this.item.dispose();
        this.disposables.forEach(d => d.dispose());
    }
}
