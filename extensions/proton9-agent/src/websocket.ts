import * as vscode from 'vscode';
import WebSocket from 'ws';

export class ForgeWebSocket {
    private ws: WebSocket | null = null;
    private listeners: Array<(msg: any) => void> = [];
    private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    private _connected = false;
    private _serverVersion = '';

    constructor(private getUrl: () => string) { }

    get connected(): boolean {
        return this._connected;
    }

    get serverVersion(): string {
        return this._serverVersion;
    }

    onEvent(cb: (msg: any) => void): vscode.Disposable {
        this.listeners.push(cb);
        return new vscode.Disposable(() => {
            this.listeners = this.listeners.filter(l => l !== cb);
        });
    }

    connect(): void {
        this.cleanup();
        const url = this.getUrl();
        console.log('[Proton9] Connecting to:', url);
        try {
            this.ws = new WebSocket(url);

            this.ws.on('open', () => {
                console.log('[Proton9] WebSocket OPEN');
                // Emit connected immediately from open event
                // (server will also send a 'connected' message, but this ensures sidebar updates)
                this._connected = true;
                this.emit({ type: 'connected', version: 'connecting...' });
            });

            this.ws.on('message', (raw: WebSocket.Data) => {
                console.log('[Proton9] RAW message received, length:', raw.toString().length);
                try {
                    const data = JSON.parse(raw.toString());
                    console.log('[Proton9] Parsed message type:', data.type);
                    if (data.type === 'connected') {
                        this._connected = true;
                        this._serverVersion = data.version || '';
                        console.log('[Proton9] Server connected, version:', this._serverVersion);
                    }
                    this.emit(data);
                } catch (e) {
                    console.error('[Proton9] Message parse error:', e);
                }
            });

            this.ws.on('close', (code: number, reason: Buffer) => {
                console.log('[Proton9] WebSocket CLOSED, code:', code, 'reason:', reason?.toString());
                this._connected = false;
                this.emit({ type: 'disconnected' });
                this.scheduleReconnect();
            });

            this.ws.on('error', (err: Error) => {
                console.error('[Proton9] WS error:', err.message);
                this._connected = false;
            });
        } catch (e) {
            console.error('[Proton9] Connect exception:', e);
            this.scheduleReconnect();
        }
    }

    // Stable client ID for session continuity across reconnects
    public clientId: string = '';

    send(msg: object): void {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            // Auto-inject client_id into every outgoing message
            const enriched = { ...msg, client_id: this.clientId };
            this.ws.send(JSON.stringify(enriched));
        }
    }

    runTask(task: string, workingDir: string, maxIterations: number, activeFile?: object, sessionId?: string, diagnostics?: object[], mentionedFiles?: object[], slotId?: string): void {
        this.send({ type: 'run_task', task, working_dir: workingDir, max_iterations: maxIterations, active_file: activeFile || null, session_id: sessionId || '', diagnostics: diagnostics || [], mentioned_files: mentionedFiles || [], slot_id: slotId || 'p9-1' });
    }

    stopTask(slotId?: string): void {
        if (slotId) {
            this.send({ type: 'stop_slot', slot_id: slotId });
        } else {
            this.send({ type: 'stop' });
        }
    }

    setWorkspace(path: string): void {
        this.send({ type: 'set_workspace', path });
    }

    setModel(provider: string, model: string, slotId?: string): void {
        this.send({ type: 'set_model', provider, model, slot_id: slotId || '' });
    }

    queryModels(provider: string): void {
        this.send({ type: 'query_models', provider });
    }

    sendDiagnosticsUpdate(path: string, diagnostics: object[]): void {
        this.send({ type: 'diagnostics_update', path, diagnostics });
    }

    dispose(): void {
        this.cleanup();
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
    }

    private emit(msg: any): void {
        for (const cb of this.listeners) {
            try { cb(msg); } catch { /* swallow */ }
        }
    }

    private scheduleReconnect(): void {
        if (!this.reconnectTimer) {
            this.reconnectTimer = setTimeout(() => {
                this.reconnectTimer = null;
                this.connect();
            }, 3000);
        }
    }

    private cleanup(): void {
        if (this.ws) {
            try { this.ws.close(); } catch { /* ignore */ }
            this.ws = null;
        }
    }
}
