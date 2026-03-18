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
        webview.onDidReceiveMessage(async (msg) => {
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

                    // Collect diagnostics (lint errors, warnings) for context
                    let diagnostics: { path: string; line: number; severity: string; message: string; source: string }[] = [];
                    try {
                        const allDiags = vscode.languages.getDiagnostics();
                        for (const [uri, diags] of allDiags) {
                            for (const d of diags) {
                                if (d.severity <= vscode.DiagnosticSeverity.Warning) {
                                    diagnostics.push({
                                        path: uri.fsPath,
                                        line: d.range.start.line + 1,
                                        severity: d.severity === vscode.DiagnosticSeverity.Error ? 'error' : 'warning',
                                        message: d.message,
                                        source: d.source || '',
                                    });
                                }
                            }
                        }
                        // Cap at 30 diagnostics to avoid bloating the prompt
                        if (diagnostics.length > 30) diagnostics = diagnostics.slice(0, 30);
                        if (diagnostics.length > 0) {
                            console.log(`[Proton9] Sending ${diagnostics.length} diagnostics with task`);
                        }
                    } catch { /* ignore diagnostics errors */ }

                    // Parse @mentions from task text — resolve files and inject contents
                    let mentionedFiles: { path: string; content: string }[] = [];
                    try {
                        const mentionPattern = /@([\w\-./\\]+\.\w+)/g;
                        let match;
                        while ((match = mentionPattern.exec(msg.task)) !== null) {
                            const ref = match[1];
                            // Resolve relative to workspace
                            const wsRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '';
                            const candidates = [
                                vscode.Uri.file(ref),
                                vscode.Uri.file(require('path').join(wsRoot, ref)),
                            ];
                            for (const uri of candidates) {
                                try {
                                    const stat = await vscode.workspace.fs.stat(uri);
                                    if (stat.type === vscode.FileType.File) {
                                        const bytes = await vscode.workspace.fs.readFile(uri);
                                        const text = Buffer.from(bytes).toString('utf8');
                                        mentionedFiles.push({
                                            path: uri.fsPath,
                                            content: text.substring(0, 5000),
                                        });
                                        break;
                                    }
                                } catch { /* file not found, try next candidate */ }
                            }
                            if (mentionedFiles.length >= 5) break; // cap at 5 files
                        }
                        if (mentionedFiles.length > 0) {
                            console.log(`[Proton9] @mentions: resolved ${mentionedFiles.length} files`);
                        }
                    } catch { /* ignore mention errors */ }

                    this.forge.runTask(msg.task, workDir, maxIter, activeFile, msg.sessionId, diagnostics.length > 0 ? diagnostics : undefined, mentionedFiles.length > 0 ? mentionedFiles : undefined, msg.slot_id);
                    break;
                }
                case 'stop':
                    this.forge.stopTask(msg.slot_id);
                    break;
                case 'set_model':
                    this.forge.setModel(msg.provider || '', msg.model || '');
                    break;
                case 'query_models':
                    this.forge.queryModels(msg.provider || '');
                    break;
                case 'request_diagnostics': {
                    // Live lint feedback: fetch diagnostics for a specific file after agent edits it
                    const diagPath = msg.path || '';
                    if (diagPath) {
                        // Wait 1.5s for VS Code linter to re-analyze
                        setTimeout(() => {
                            try {
                                const uri = vscode.Uri.file(diagPath);
                                const fileDiags = vscode.languages.getDiagnostics(uri);
                                const errors: { path: string; line: number; severity: string; message: string; source: string }[] = [];
                                for (const d of fileDiags) {
                                    if (d.severity <= vscode.DiagnosticSeverity.Warning) {
                                        errors.push({
                                            path: diagPath,
                                            line: d.range.start.line + 1,
                                            severity: d.severity === vscode.DiagnosticSeverity.Error ? 'error' : 'warning',
                                            message: d.message,
                                            source: d.source || '',
                                        });
                                    }
                                }
                                if (errors.length > 0) {
                                    console.log(`[Proton9] Live lint: ${errors.length} issues in ${diagPath}`);
                                    this.forge.sendDiagnosticsUpdate(diagPath, errors);
                                }
                            } catch (e) {
                                console.log('[Proton9] Live lint fetch failed:', e);
                            }
                        }, 1500);
                    }
                    break;
                }
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
                                events: msg.events || [],
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
                case 'delete_chat': {
                    // Delete chat JSON file from .proton9/chats/
                    const wsFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                    if (wsFolder && msg.sessionId) {
                        const fs = require('fs');
                        const path = require('path');
                        const chatsDir = path.join(wsFolder, '.proton9', 'chats');
                        try {
                            const files = fs.readdirSync(chatsDir);
                            const match = files.find((f: string) => f.startsWith(msg.sessionId));
                            if (match) {
                                fs.unlinkSync(path.join(chatsDir, match));
                                console.log('[Proton9] Chat deleted:', match);
                            }
                        } catch (err) {
                            console.error('[Proton9] Chat delete error:', err);
                        }
                    }
                    // Also tell server to clean up SQLite session data
                    this.forge.send({ type: 'delete_chat', session_id: msg.sessionId || '' });
                    break;
                }
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
                case 'save_slot_state': {
                    // Persist per-tab chat HTML to .proton9/slots/
                    const wsFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                    if (wsFolder && msg.slotId) {
                        const fs = require('fs');
                        const path = require('path');
                        const slotsDir = path.join(wsFolder, '.proton9', 'slots');
                        try {
                            fs.mkdirSync(slotsDir, { recursive: true });
                            const filepath = path.join(slotsDir, `${msg.slotId}.json`);
                            fs.writeFileSync(filepath, JSON.stringify({
                                slotId: msg.slotId,
                                html: msg.html || '',
                                provider: msg.provider || '',
                                model: msg.model || '',
                                inputText: msg.inputText || '',
                                savedAt: new Date().toISOString(),
                            }, null, 2), 'utf-8');
                        } catch (err) {
                            console.error('[Proton9] Slot save error:', err);
                        }
                    }
                    break;
                }
                case 'load_slots': {
                    // Load saved slot states and send to webview
                    const wsFolder2 = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                    if (wsFolder2) {
                        const fs = require('fs');
                        const path = require('path');
                        const slotsDir = path.join(wsFolder2, '.proton9', 'slots');
                        const savedSlots: any[] = [];
                        try {
                            if (fs.existsSync(slotsDir)) {
                                const files = fs.readdirSync(slotsDir).filter((f: string) => f.endsWith('.json'));
                                for (const f of files) {
                                    try {
                                        const data = JSON.parse(fs.readFileSync(path.join(slotsDir, f), 'utf-8'));
                                        savedSlots.push(data);
                                    } catch { /* skip corrupt files */ }
                                }
                            }
                        } catch { /* ignore */ }
                        try { webview.postMessage({ type: 'saved_slots', slots: savedSlots }); } catch {}
                    }
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
        .artifact-card { background:var(--vscode-editor-background); border:1px solid var(--vscode-widget-border, #444); border-radius:6px; margin:4px 0; font-size:12px; overflow:hidden; }
        .artifact-card .artifact-header { display:flex; align-items:center; justify-content:space-between; padding:8px 10px; background:rgba(180,120,255,0.1); border-bottom:1px solid var(--vscode-widget-border, #333); cursor:pointer; }
        .artifact-card .artifact-title { font-weight:bold; color:#c586c0; flex:1; }
        .artifact-card .artifact-badge { font-size:10px; padding:1px 6px; border-radius:3px; background:rgba(180,120,255,0.2); color:#c586c0; margin-right:8px; }
        .artifact-card .artifact-body { max-height:400px; overflow-y:auto; padding:8px 12px; display:none; white-space:pre-wrap; font-size:11px; line-height:1.5; color:var(--vscode-foreground); }
        .artifact-card .artifact-body.open { display:block; }
        .artifact-card .artifact-actions button { background:none; border:1px solid var(--vscode-widget-border, #555); color:var(--vscode-foreground); padding:2px 8px; border-radius:3px; font-size:11px; cursor:pointer; }
        .artifact-card .artifact-actions button:hover { background:rgba(255,255,255,0.1); }
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
        #usage-btn { background:transparent; border:1px solid var(--vscode-widget-border, #444); color:#4ec9b0; padding:2px 6px; font-size:11px; cursor:pointer; border-radius:3px; margin-left:4px; min-width:unset; white-space:nowrap; }
        #usage-btn:hover { background:#4ec9b022; color:#4ec9b0; }
        #usage-panel { display:none; position:absolute; top:36px; left:8px; right:8px; background:var(--vscode-editorWidget-background, #252526); border:1px solid var(--vscode-widget-border, #444); border-radius:6px; z-index:100; max-height:400px; overflow-y:auto; box-shadow:0 4px 16px rgba(0,0,0,0.5); padding:12px; font-size:12px; }
        #usage-panel h3 { margin:0 0 8px; font-size:13px; color:var(--vscode-foreground); }
        #usage-panel .period { margin-bottom:10px; }
        #usage-panel .period-header { font-weight:600; color:#4ec9b0; font-size:12px; margin-bottom:4px; display:flex; justify-content:space-between; }
        #usage-panel .period-header .total { color:#dcdcaa; font-weight:400; }
        #usage-panel .model-row { display:flex; justify-content:space-between; padding:2px 0 2px 12px; color:var(--vscode-descriptionForeground); font-size:11px; border-left:2px solid #333; }
        #usage-panel .model-row .name { color:#569cd6; }
        #usage-panel .no-data { color:var(--vscode-descriptionForeground); font-style:italic; }
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
        .history-item .delete-btn { background:none; border:none; color:var(--vscode-descriptionForeground); cursor:pointer; font-size:14px; padding:0 4px; opacity:0.5; min-width:unset; }
        .history-item .delete-btn:hover { color:#f44747; opacity:1; }
        /* Agent Tab Bar */
        #agent-tabs { display:flex; gap:2px; padding:4px 8px; background:var(--vscode-sideBarSectionHeader-background); border-bottom:1px solid var(--vscode-sideBarSectionHeader-border, #333); overflow-x:auto; overflow-y:hidden; scrollbar-width:thin; scrollbar-color:rgba(78,201,176,0.3) transparent; }
        #agent-tabs::-webkit-scrollbar { height:3px; }
        #agent-tabs::-webkit-scrollbar-thumb { background:rgba(78,201,176,0.3); border-radius:3px; }
        .agent-tab { display:flex; align-items:center; gap:4px; padding:6px 8px; background:rgba(78,201,176,0.1); border:1px solid rgba(78,201,176,0.15); border-radius:4px; cursor:pointer; font-size:11px; font-weight:600; color:var(--vscode-descriptionForeground); transition:all 0.15s; user-select:none; flex-shrink:0; min-width:60px; }
        .agent-tab:hover { background:rgba(78,201,176,0.2); color:var(--vscode-foreground); }
        .agent-tab.active { background:rgba(78,201,176,0.25); border-color:#4ec9b0; color:var(--vscode-foreground); }
        .tab-dot { width:6px; height:6px; border-radius:50%; }
        .agent-tab .tab-dot.running { background:#dcdcaa; animation:pulse 1s infinite; }
        .agent-tab .tab-dot.idle { background:#4ec9b0; }
        .agent-tab .tab-dot.done { background:#888; }
        .agent-tab .tab-close { background:none; border:none; color:var(--vscode-descriptionForeground); cursor:pointer; font-size:12px; padding:0 2px; opacity:0; line-height:1; }
        .agent-tab:hover .tab-close { opacity:0.7; }
        .agent-tab .tab-close:hover { opacity:1; color:#f44747; }
        #add-slot-btn { background:rgba(78,201,176,0.1); border:1px dashed #4ec9b0; color:#4ec9b0; padding:5px 12px; border-radius:4px; font-size:12px; font-weight:700; cursor:pointer; white-space:nowrap; letter-spacing:0.5px; }
        #add-slot-btn:hover { background:rgba(78,201,176,0.25); border-style:solid; }
        .slot-info { font-size:10px; color:var(--vscode-descriptionForeground); padding:2px 12px 4px; background:var(--vscode-editor-background); border-bottom:1px solid var(--vscode-widget-border, #333); display:none; }
        .slot-info.visible { display:flex; align-items:center; gap:8px; }
        .slot-info .provider-badge { font-size:10px; padding:1px 6px; border-radius:3px; background:rgba(78,201,176,0.15); color:#4ec9b0; }
        .slot-info .elapsed { color:#dcdcaa; }
    </style>
</head>
<body>
    <div id="app">
        <div id="status-bar"><span id="status-dot" class="dot ${initialConnected ? 'connected' : 'disconnected'}"></span><span id="status-text">${initialConnected ? 'Ready' : 'Connecting...'}</span><span id="model-label"></span><button id="usage-btn" title="Token Usage">📊</button><button id="new-chat-btn" title="New Chat">+ New</button><button id="history-btn" title="Chat History">☰</button><button id="gear-btn" title="Settings">⚙</button></div>
        <div id="agent-tabs">
            <div class="agent-tab active" data-slot="p9-1"><span class="tab-dot idle"></span>P9-1<button class="tab-close" title="Close">×</button></div>
            <button id="add-slot-btn" title="New Agent Slot">+ P9</button>
        </div>
        <div id="slot-info" class="slot-info"><span class="provider-badge" id="slot-provider"></span><span class="elapsed" id="slot-elapsed"></span></div>
        <div id="history-panel"></div>
        <div id="usage-panel"></div>
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
            // Request saved slot states from previous session
            vscode.postMessage({ type: 'load_slots' });
            let currentFeedEl = null;      // current Action Feed wrapper
            let currentFeedBody = null;    // its body (contains steps)
            let currentPrompt = '';        // current task prompt
            let currentAnswer = '';        // task answer/summary
            let stepCount = 0;
            let cachedModels = [];         // [{id, context_window}, ...]
            let streamBuffer = '';          // accumulate LLM tokens for saving
            var eventLog = [];              // all UI events for session replay

            // ─── Per-Slot Container Architecture ───
            const MAX_SLOTS = 9;
            const agentTabs = document.getElementById('agent-tabs');
            const addSlotBtn = document.getElementById('add-slot-btn');
            const slotInfoEl = document.getElementById('slot-info');
            const slotProviderEl = document.getElementById('slot-provider');
            const slotElapsedEl = document.getElementById('slot-elapsed');
            let activeSlot = 'p9-1';
            let slotCounter = 1;
            let slotTimerInterval = null;
            let slotInput = { 'p9-1': '' }; // per-slot input text cache

            // Per-slot state — each slot is a mini VM
            let slots = {};
            function initSlot(slotId) {
                // Create a container div for this slot's messages
                var container = document.createElement('div');
                container.className = 'slot-content';
                container.dataset.slot = slotId;
                container.style.display = (slotId === activeSlot) ? 'block' : 'none';
                messagesEl.appendChild(container);
                slots[slotId] = {
                    container: container,
                    streamingEl: null,
                    feedEl: null,
                    feedBody: null,
                    stepCount: 0,
                    streamBuffer: '',
                    running: false,
                    provider: '',
                    model: '',
                    active: false,
                    started: 0,
                    prompt: '',
                    answer: '',
                };
            }
            // Initialize P9-1
            initSlot('p9-1');

            function getSlot(slotId) {
                if (!slots[slotId]) initSlot(slotId);
                return slots[slotId];
            }

            function switchTab(slotId) {
                // Save current input text
                slotInput[activeSlot] = inputEl.value;
                // Hide all slot containers
                Object.keys(slots).forEach(function(sid) {
                    slots[sid].container.style.display = 'none';
                });
                activeSlot = slotId;
                // Show target slot's container
                var slot = getSlot(slotId);
                slot.container.style.display = 'block';
                // Restore input text
                inputEl.value = slotInput[slotId] || '';
                // Update tab highlight
                document.querySelectorAll('.agent-tab').forEach(function(t) { t.classList.remove('active'); });
                var tab = document.querySelector('.agent-tab[data-slot="' + slotId + '"]');
                if (tab) tab.classList.add('active');
                updateSlotInfo();
                scrollToBottom();
                // Restore per-slot running state
                setRunning(!!slot.running);
                if (slot.running) { setStatus('running', 'Running...'); } else { setStatus('connected', 'Ready'); }
                // Point shared refs at active slot for legacy compat
                streamingEl = slot.streamingEl;
                currentFeedEl = slot.feedEl;
                currentFeedBody = slot.feedBody;
                stepCount = slot.stepCount;
                streamBuffer = slot.streamBuffer;
            }

            function addTab(slotId, provider, model) {
                // Enforce max slots
                if (Object.keys(slots).length >= MAX_SLOTS) {
                    addMessage('Maximum ' + MAX_SLOTS + ' agent tabs allowed.', 'error');
                    return;
                }
                var tab = document.createElement('div');
                tab.className = 'agent-tab';
                tab.dataset.slot = slotId;
                tab.innerHTML = '<span class="tab-dot idle"></span>' + slotId.toUpperCase() + '<button class="tab-close" title="Close">×</button>';
                tab.addEventListener('click', function(e) { if (!(e.target instanceof HTMLButtonElement)) switchTab(slotId); });
                tab.querySelector('.tab-close').addEventListener('click', function(e) { e.stopPropagation(); closeTab(slotId); });
                agentTabs.insertBefore(tab, addSlotBtn);
                initSlot(slotId);
                slots[slotId].provider = provider || '';
                slots[slotId].model = model || '';
                slotInput[slotId] = '';
                switchTab(slotId);
            }

            function closeTab(slotId) {
                if (slotId === 'p9-1') return; // Don't close the primary tab
                if (slots[slotId] && slots[slotId].running) {
                    vscode.postMessage({ type: 'send_ws', payload: JSON.stringify({ type: 'stop_slot', slot_id: slotId }) });
                }
                var tab = document.querySelector('.agent-tab[data-slot="' + slotId + '"]');
                if (tab) tab.remove();
                // Remove container from DOM
                if (slots[slotId] && slots[slotId].container) {
                    slots[slotId].container.remove();
                }
                delete slots[slotId];
                delete slotInput[slotId];
                if (activeSlot === slotId) {
                    var remaining = Object.keys(slots);
                    if (remaining.length > 0) switchTab(remaining[0]);
                }
            }

            function updateSlotInfo() {
                var slot = slots[activeSlot];
                if (slot && slot.provider) {
                    slotProviderEl.textContent = slot.provider + '/' + slot.model;
                    slotInfoEl.classList.add('visible');
                } else {
                    slotInfoEl.classList.remove('visible');
                }
            }

            function updateTabDot(slotId, state) {
                var tab = document.querySelector('.agent-tab[data-slot="' + slotId + '"] .tab-dot');
                if (tab) { tab.className = 'tab-dot ' + state; }
            }

            // +P9 button: spawn new slot with current provider/model selection
            addSlotBtn.addEventListener('click', () => {
                slotCounter++;
                const newSlotId = 'p9-' + slotCounter;
                const provider = providerSelect ? providerSelect.value : 'deepseek';
                const model = modelSelect ? modelSelect.value : '';
                addTab(newSlotId, provider, model);
                // Pre-fill info bar
                updateSlotInfo();
            });

            // Tab click delegation for the default P9-1 tab
            document.querySelector('.agent-tab[data-slot=\"p9-1\"]').addEventListener('click', (e) => { if (!(e.target instanceof HTMLButtonElement)) switchTab('p9-1'); });
            document.querySelector('.agent-tab[data-slot=\"p9-1\"] .tab-close').addEventListener('click', (e) => { e.stopPropagation(); /* Don't allow closing the primary tab */ });

            // Start elapsed timer
            slotTimerInterval = setInterval(function() {
                var slot = slots[activeSlot];
                if (slot && slot.active && slot.started) {
                    const elapsed = Math.round(Date.now() / 1000 - slot.started);
                    const mins = Math.floor(elapsed / 60);
                    const secs = elapsed % 60;
                    slotElapsedEl.textContent = '⏱ ' + mins + ':' + String(secs).padStart(2, '0');
                } else {
                    slotElapsedEl.textContent = '';
                }
            }, 1000);

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
                // Update active session's messages, events, and title
                var active = sessions.find(function(s) { return s.id === activeSessionId; });
                if (active) {
                    active.messages = chatMessages;
                    active.events = eventLog;
                    active.title = deriveTitle(chatMessages);
                }
                vscode.setState({ sessions: sessions, activeSessionId: activeSessionId });
            }

            function loadSession(sessionId) {
                var session = sessions.find(function(s) { return s.id === sessionId; });
                if (!session) return;
                activeSessionId = sessionId;
                chatMessages = session.messages || [];
                eventLog = session.events || [];
                messagesEl.innerHTML = '';
                currentPrompt = '';
                currentAnswer = '';
                stepCount = 0;
                streamBuffer = '';
                currentFeedEl = null;
                currentFeedBody = null;
                // Replay events to rebuild full UI
                if (eventLog.length > 0) {
                    replayEvents(eventLog);
                } else {
                    // Fallback: old format — just text messages
                    chatMessages.forEach(function(m) { addMessage(m.text, m.type, true); });
                }
                saveState();
                renderHistory();
                // Tell server to recontext to this session's ensemble
                vscode.postMessage({ type: 'switch_session', sessionId: sessionId });
            }

            function replayEvents(events) {
                for (var i = 0; i < events.length; i++) {
                    var evt = events[i];
                    switch (evt.type) {
                        case 'user_message':
                            addMessage(evt.text, 'user', true);
                            break;
                        case 'assistant_text':
                            addMessage(evt.text, 'assistant', true);
                            break;
                        case 'system_message':
                            addMessage(evt.text, 'system', true);
                            break;
                        case 'error_message':
                            addMessage(evt.text, 'error', true);
                            break;
                        case 'action_feed_start':
                            currentPrompt = evt.prompt || '';
                            createActionFeed();
                            break;
                        case 'action':
                            addAction(evt.step, evt.tool, evt.args);
                            break;
                        case 'result':
                            updateLastAction(evt.success, evt.output, evt.error);
                            break;
                        case 'action_feed_end':
                            finalizeActionFeed(evt.result || {});
                            break;
                        case 'completion':
                            addCompletionCard(evt.data);
                            break;
                        case 'file_changed':
                            addFileChangeCard(evt.path, evt.diff, evt.tool, evt.snapshot, evt.is_new);
                            break;
                        case 'artifact':
                            addArtifactCard(evt.title, evt.content, evt.artifact_type);
                            break;
                    }
                }
                scrollToBottom();
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
                eventLog = [];
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
                    var titleSpan = document.createElement('span');
                    titleSpan.className = 'title';
                    titleSpan.textContent = s.title || 'New Chat';
                    var countSpan = document.createElement('span');
                    countSpan.className = 'count';
                    countSpan.textContent = count + ' msg';
                    var delBtn = document.createElement('button');
                    delBtn.className = 'delete-btn';
                    delBtn.textContent = '\u2715';
                    delBtn.title = 'Delete this chat';
                    delBtn.addEventListener('click', function(e) {
                        e.stopPropagation();
                        deleteSession(s.id);
                    });
                    el.appendChild(titleSpan);
                    el.appendChild(countSpan);
                    el.appendChild(delBtn);
                    el.addEventListener('click', function() {
                        loadSession(s.id);
                        panel.classList.remove('open');
                    });
                    panel.appendChild(el);
                });
            }

            function deleteSession(sessionId) {
                sessions = sessions.filter(function(s) { return s.id !== sessionId; });
                vscode.postMessage({ type: 'delete_chat', sessionId: sessionId });
                if (sessionId === activeSessionId) {
                    if (sessions.length > 0) {
                        loadSession(sessions[0].id);
                    } else {
                        createNewSession();
                    }
                }
                saveState();
                renderHistory();
            }

            // Restore saved sessions on load
            var savedState = vscode.getState();
            if (savedState && savedState.sessions && savedState.sessions.length) {
                sessions = savedState.sessions;
                activeSessionId = savedState.activeSessionId || sessions[0].id;
                var activeSession = sessions.find(function(s) { return s.id === activeSessionId; });
                if (activeSession) {
                    chatMessages = activeSession.messages || [];
                    eventLog = activeSession.events || [];
                    if (eventLog.length > 0) {
                        replayEvents(eventLog);
                    } else {
                        chatMessages.forEach(function(m) { addMessage(m.text, m.type, true); });
                    }
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
                    // Close usage panel if open
                    var up = document.getElementById('usage-panel');
                    if (up) up.style.display = 'none';
                });
            }

            // ─── Usage Panel ───
            window._persistentUsage = {};
            var usageBtn = document.getElementById('usage-btn');
            var usagePanel = document.getElementById('usage-panel');
            if (usageBtn && usagePanel) {
                usageBtn.addEventListener('click', function() {
                    var isOpen = usagePanel.style.display === 'block';
                    usagePanel.style.display = isOpen ? 'none' : 'block';
                    if (!isOpen) renderUsagePanel();
                    // Close other panels
                    if (historyPanel) historyPanel.classList.remove('open');
                });
            }
            function renderUsagePanel() {
                var panel = document.getElementById('usage-panel');
                if (!panel) return;
                var data = window._persistentUsage || {};
                var periods = ['day', 'week', 'month', 'year', 'lifetime'];
                var labels = { day: '📅 Today', week: '📆 This Week', month: '🗓️ This Month', year: '📊 This Year', lifetime: '♾️ All Time' };
                var fmtTok = function(n) { return n >= 1000000 ? (n/1000000).toFixed(1)+'M' : n >= 1000 ? (n/1000).toFixed(1)+'K' : String(n); };
                var fmtCost = function(c) { return c < 0.01 ? '$'+c.toFixed(4) : '$'+c.toFixed(2); };
                var html = '<h3>📊 Token Usage</h3>';
                var hasAny = false;
                for (var pi = 0; pi < periods.length; pi++) {
                    var p = periods[pi];
                    var pd = data[p];
                    if (!pd || pd.total_tokens === 0) continue;
                    hasAny = true;
                    html += '<div class="period">';
                    html += '<div class="period-header"><span>' + labels[p] + '</span>';
                    html += '<span class="total">' + fmtTok(pd.total_tokens) + ' tok · ' + fmtCost(pd.cost_usd || 0) + ' · ' + pd.task_count + ' tasks</span></div>';
                    var models = pd.models || {};
                    var modelKeys = Object.keys(models);
                    for (var mi = 0; mi < modelKeys.length; mi++) {
                        var mk = modelKeys[mi];
                        var md = models[mk];
                        html += '<div class="model-row"><span class="name">' + mk + '</span>';
                        html += '<span>' + fmtTok(md.input_tokens) + ' in / ' + fmtTok(md.output_tokens) + ' out · ' + fmtCost(md.cost_usd || 0) + ' · ' + md.task_count + ' tasks</span></div>';
                    }
                    html += '</div>';
                }
                if (!hasAny) html += '<div class="no-data">No usage recorded yet. Run a task to start tracking.</div>';
                // Add session info
                var sess = window._lastTokenUpdate;
                if (sess && (sess.input + sess.output) > 0) {
                    html += '<div class="period"><div class="period-header"><span>⚡ Current Session</span>';
                    html += '<span class="total">' + fmtTok(sess.input + sess.output) + ' tok · ' + fmtCost(sess.cost || 0) + '</span></div>';
                    html += '<div class="model-row"><span class="name">In: ' + fmtTok(sess.input) + '</span><span>Out: ' + fmtTok(sess.output) + '</span></div></div>';
                }
                panel.innerHTML = html;
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
                anthropic: ['claude-sonnet-4-20250514', 'claude-3-5-sonnet-latest', 'claude-3-5-haiku-latest']
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
                var s = getSlot(activeSlot);
                s.prompt = task;
                s.streamBuffer = '';
                s.stepCount = 0;
                streamBuffer = '';
                stepCount = 0;
                addMessageToSlot(task, 'user', activeSlot);
                eventLog.push({ type: 'user_message', text: task, ts: Date.now() });
                inputEl.value = '';
                vscode.postMessage({ type: 'run_task', task: task, sessionId: activeSessionId, slot_id: activeSlot });
            }

            sendBtn.addEventListener('click', sendTask);
            inputEl.addEventListener('keydown', function(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendTask(); } });
            stopBtn.addEventListener('click', function() {
                vscode.postMessage({ type: 'stop', slot_id: activeSlot });
                addMessageToSlot('Stop requested for ' + activeSlot.toUpperCase() + '...', 'system', activeSlot);
            });

            window.addEventListener('message', function(event) {
                const msg = event.data;
                if (!msg || !msg.type) return;
                // ─── Per-slot routing: route events to correct slot container ───
                var evSlot = msg.slot_id || activeSlot;
                switch (msg.type) {
                    case 'connected':
                        setStatus('connected', 'Ready');
                        var cProv = msg.llm_provider || '';
                        var cModel = msg.llm_model || '';
                        if (cProv && providerSelect) { providerSelect.value = cProv; }
                        cachedModels = msg.available_models || [];
                        populateModels(cachedModels, cModel);
                        if (modelLabel) { modelLabel.textContent = cProv && cModel ? cProv + '/' + cModel : cModel || cProv || ''; }
                        // Store into P9-1 slot and active slot
                        var cSlot = getSlot('p9-1');
                        cSlot.provider = cProv; cSlot.model = cModel;
                        if (activeSlot !== 'p9-1') { var acSlot = getSlot(activeSlot); acSlot.provider = cProv; acSlot.model = cModel; }
                        updateSlotInfo();
                        if (msg.persistent_usage) { window._persistentUsage = msg.persistent_usage; }
                        if (msg.last_session && msg.last_session.objective) {
                            var ls = msg.last_session;
                            var timeAgo = '';
                            try {
                                var diff = Date.now() - new Date(ls.timestamp).getTime();
                                var mins = Math.floor(diff / 60000);
                                if (mins < 60) { timeAgo = mins + 'm ago'; }
                                else if (mins < 1440) { timeAgo = Math.floor(mins/60) + 'h ago'; }
                                else { timeAgo = Math.floor(mins/1440) + 'd ago'; }
                            } catch(e) { timeAgo = ''; }
                            var card = document.createElement('div');
                            card.className = 'continue-card';
                            card.innerHTML = '<div style="padding:10px;background:var(--vscode-editor-background);border:1px solid var(--vscode-focusBorder);border-radius:6px;margin:8px 0;">'
                                + '<div style="font-size:12px;color:var(--vscode-descriptionForeground);margin-bottom:4px;">📋 Last session' + (timeAgo ? ' (' + timeAgo + ')' : '') + '</div>'
                                + '<div style="font-size:13px;margin-bottom:8px;">' + (ls.objective || '').substring(0, 120) + '</div>'
                                + '<div style="display:flex;gap:8px;">'
                                + '<button id="continue-btn" style="flex:1;padding:6px 12px;background:var(--vscode-button-background);color:var(--vscode-button-foreground);border:none;border-radius:4px;cursor:pointer;font-size:12px;">▶ Continue</button>'
                                + '<button id="dismiss-btn" style="padding:6px 12px;background:transparent;color:var(--vscode-descriptionForeground);border:1px solid var(--vscode-widget-border);border-radius:4px;cursor:pointer;font-size:12px;">✕</button>'
                                + '</div></div>';
                            chatEl.appendChild(card);
                            chatEl.scrollTop = chatEl.scrollHeight;
                            var contBtn = document.getElementById('continue-btn');
                            var disBtn = document.getElementById('dismiss-btn');
                            if (contBtn) {
                                contBtn.addEventListener('click', function() {
                                    inputEl.value = 'Continue my previous task: ' + (ls.objective || '').substring(0, 200);
                                    card.remove();
                                    inputEl.focus();
                                });
                            }
                            if (disBtn) {
                                disBtn.addEventListener('click', function() { card.remove(); });
                            }
                        }
                        break;
                    case 'disconnected': setStatus('disconnected', 'Disconnected'); setRunning(false); break;
                    case 'task_started': {
                        var tsS = getSlot(evSlot);
                        tsS.running = true; tsS.active = true; tsS.started = Date.now() / 1000;
                        createActionFeedInSlot(evSlot);
                        updateTabDot(evSlot, 'running');
                        if (evSlot === activeSlot) {
                            setRunning(true); setStatus('running', 'Running...');
                            var ub = document.getElementById('usage-panel'); if(ub){ub.style.display='none';}
                        }
                        updateSlotInfo();
                        break;
                    }
                    case 'slot_started':
                        var sid = msg.slot_id || '';
                        if (sid && !slots[sid]) { addTab(sid, msg.provider || '', msg.model || ''); }
                        else if (sid && slots[sid]) { slots[sid].provider = msg.provider || ''; slots[sid].model = msg.model || ''; slots[sid].active = true; slots[sid].started = Date.now() / 1000; }
                        updateTabDot(sid, 'running');
                        if (sid === activeSlot) updateSlotInfo();
                        break;
                    case 'slot_complete':
                        var csid = msg.slot_id || '';
                        if (csid && slots[csid]) { slots[csid].active = false; slots[csid].running = false; }
                        updateTabDot(csid, 'done');
                        if (csid === activeSlot) { setRunning(false); setStatus('connected', 'Ready'); }
                        break;
                    case 'action': {
                        endStreamingInSlot(evSlot);
                        addActionInSlot(msg.step, msg.tool, msg.args, evSlot);
                        break;
                    }
                    case 'result': {
                        updateLastActionInSlot(msg.success, msg.output, msg.error, evSlot);
                        break;
                    }
                    case 'llm_token': {
                        appendStreamInSlot(msg.text || '', evSlot);
                        var lts = getSlot(evSlot);
                        lts.streamBuffer += (msg.text || '');
                        if (evSlot === activeSlot) { streamBuffer = lts.streamBuffer; }
                        break;
                    }
                    case 'token_update':
                        window._lastTokenUpdate = { input: msg.input_tokens||0, output: msg.output_tokens||0, cost: msg.cost_usd||0 };
                        break;
                    case 'task_complete': {
                        var tcS = getSlot(evSlot);
                        endStreamingInSlot(evSlot);
                        tcS.running = false; tcS.active = false;
                        updateTabDot(evSlot, 'idle');
                        if (evSlot === activeSlot) { setRunning(false); setStatus('connected', 'Ready'); }
                        var taskResult = msg.result || {};
                        finalizeActionFeedInSlot(taskResult, evSlot);
                        if (msg.persistent_usage) { window._persistentUsage = msg.persistent_usage; }
                        var responseText = tcS.streamBuffer || taskResult.summary || '(no response)';
                        addMessageToSlot(responseText, 'assistant', evSlot);
                        tcS.streamBuffer = '';
                        if (evSlot === activeSlot) { streamBuffer = ''; }
                        addCompletionCardToSlot(taskResult, evSlot);
                        // Persist this slot's chat state
                        saveSlotState(evSlot);
                        break;
                    }
                    case 'task_error': {
                        var teS = getSlot(evSlot);
                        endStreamingInSlot(evSlot);
                        teS.running = false; teS.active = false;
                        updateTabDot(evSlot, 'idle');
                        if (evSlot === activeSlot) { setRunning(false); setStatus('connected', 'Ready'); }
                        addMessageToSlot('Error: ' + (msg.error || 'Unknown'), 'error', evSlot);
                        teS.streamBuffer = '';
                        if (evSlot === activeSlot) { streamBuffer = ''; }
                        break;
                    }
                    case 'error': addMessageToSlot('Server: ' + (msg.message || msg.error || 'Unknown error'), 'error', activeSlot); break;
                    case 'model_changed':
                        var mProv = msg.provider || '';
                        var mModel = msg.model || '';
                        var mLabel = mProv && mModel ? mProv + '/' + mModel : mModel || mProv || '?';
                        // Store per-slot
                        var mcSlot = getSlot(evSlot);
                        mcSlot.provider = mProv;
                        mcSlot.model = mModel;
                        addMessageToSlot('\u2705 Model switched to ' + mLabel, 'system', evSlot);
                        if (evSlot === activeSlot) {
                            if (modelLabel) { modelLabel.textContent = mLabel; }
                            if (mProv && providerSelect) { providerSelect.value = mProv; }
                            if (mModel && modelSelect) { modelSelect.value = mModel; }
                            updateSlotInfo();
                        }
                        break;
                    case 'models_list':
                        if (msg.provider === providerSelect.value) {
                            var curVal = modelSelect ? modelSelect.value : '';
                            populateModels(msg.models || [], curVal);
                        }
                        break;
                    case 'file_changed':
                        addFileChangeCardToSlot(msg.path, msg.diff, msg.tool, msg.snapshot, msg.is_new, evSlot);
                        break;
                    case 'file_reverted':
                        // Mark the diff card as reverted
                        var cards = document.querySelectorAll('.diff-card[data-path="' + (msg.path || '') + '"]');
                        cards.forEach(function(c) { c.classList.add('reverted'); });
                        addMessage('↩ ' + (msg.message || 'File reverted'), 'system');
                        // Re-open the reverted file to refresh editor
                        vscode.postMessage({ type: 'open_file', path: msg.path });
                        break;
                    case 'task_artifact':
                        addArtifactCardToSlot(msg.title, msg.content, msg.artifact_type, evSlot);
                        break;
                    case 'saved_slots':
                        // Restore saved tab states from previous session
                        var savedSlots = msg.slots || [];
                        savedSlots.forEach(function(ss) {
                            if (!ss.slotId || ss.slotId === 'p9-1') {
                                // Restore P9-1 content directly
                                if (ss.slotId === 'p9-1' && ss.html) {
                                    var p1Slot = getSlot('p9-1');
                                    p1Slot.container.innerHTML = ss.html;
                                    p1Slot.provider = ss.provider || '';
                                    p1Slot.model = ss.model || '';
                                    if (ss.inputText) slotInput['p9-1'] = ss.inputText;
                                }
                                return;
                            }
                            // Create tab and restore content
                            var restoredCounter = parseInt(ss.slotId.replace('p9-', '')) || 0;
                            if (restoredCounter > slotCounter) slotCounter = restoredCounter;
                            addTab(ss.slotId, ss.provider || '', ss.model || '');
                            var rSlot = getSlot(ss.slotId);
                            if (ss.html) rSlot.container.innerHTML = ss.html;
                            if (ss.inputText) slotInput[ss.slotId] = ss.inputText;
                        });
                        // Switch back to P9-1 after restore
                        switchTab('p9-1');
                        break;
                }
            });

            function setStatus(state, text) { statusDot.className = 'dot ' + state; statusText.textContent = text; }
            function setRunning(running) { isRunning = running; sendBtn.classList.toggle('hidden', running); stopBtn.classList.toggle('hidden', !running); inputEl.disabled = running; }

            // Diff Card for File Changes
            var fileSnapshots = {};

            function addArtifactCard(title, content, artifactType) {
                var card = document.createElement('div');
                card.className = 'artifact-card';
                var header = document.createElement('div');
                header.className = 'artifact-header';
                var badge = document.createElement('span');
                badge.className = 'artifact-badge';
                badge.textContent = (artifactType || 'plan').toUpperCase();
                var titleSpan = document.createElement('span');
                titleSpan.className = 'artifact-title';
                titleSpan.textContent = title || 'Artifact';
                var actions = document.createElement('div');
                actions.className = 'artifact-actions';
                var toggleBtn = document.createElement('button');
                toggleBtn.textContent = '>';
                toggleBtn.title = 'Toggle content';
                var copyBtn = document.createElement('button');
                copyBtn.textContent = 'Copy';
                copyBtn.title = 'Copy content';
                actions.appendChild(toggleBtn);
                actions.appendChild(copyBtn);
                header.appendChild(badge);
                header.appendChild(titleSpan);
                header.appendChild(actions);
                var body = document.createElement('div');
                body.className = 'artifact-body';
                body.textContent = content || '';
                card.appendChild(header);
                card.appendChild(body);
                var _sc = (slots[activeSlot] && slots[activeSlot].container) || messagesEl; _sc.appendChild(card);
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
                copyBtn.addEventListener('click', function(e) {
                    e.stopPropagation();
                    navigator.clipboard.writeText(content || '').then(function() {
                        copyBtn.textContent = 'Copied!';
                        setTimeout(function() { copyBtn.textContent = 'Copy'; }, 1500);
                    });
                });
            }

            function addFileChangeCard(filePath, diff, tool, snapshot, isNew) {
                if (filePath && typeof fileSnapshots !== 'undefined' && fileSnapshots) fileSnapshots[filePath] = snapshot;
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
                var _sc = (slots[activeSlot] && slots[activeSlot].container) || messagesEl; _sc.appendChild(card);
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

            // ─── Per-Slot DOM Functions (each targets the correct slot's container) ───
            function addMessageToSlot(text, type, slotId) {
                var slot = getSlot(slotId);
                var el = document.createElement('div');
                el.className = 'message ' + type;
                el.textContent = text;
                slot.container.appendChild(el);
                if (slotId === activeSlot) scrollToBottom();
            }

            function appendStreamInSlot(text, slotId) {
                var slot = getSlot(slotId);
                if (!slot.streamingEl) {
                    slot.streamingEl = document.createElement('div');
                    slot.streamingEl.className = 'message streaming';
                    slot.streamingEl.innerHTML = '<span class="stream-text"></span><span class="cursor"></span>';
                    slot.container.appendChild(slot.streamingEl);
                    if (slotId === activeSlot) { streamingEl = slot.streamingEl; }
                }
                slot.streamingEl.querySelector('.stream-text').textContent += text;
                if (slotId === activeSlot) scrollToBottom();
            }

            function endStreamingInSlot(slotId) {
                var slot = getSlot(slotId);
                if (slot.streamingEl) {
                    slot.streamingEl.classList.remove('streaming');
                    var c = slot.streamingEl.querySelector('.cursor');
                    if (c) c.remove();
                    slot.streamingEl = null;
                    if (slotId === activeSlot) { streamingEl = null; }
                }
            }

            function createActionFeedInSlot(slotId) {
                var slot = getSlot(slotId);
                slot.stepCount = 0;
                slot.feedEl = document.createElement('div');
                slot.feedEl.className = 'action-feed-wrapper';
                var header = document.createElement('div');
                header.className = 'action-feed-header';
                header.innerHTML = '<span class="chevron">▶</span> <span class="feed-title">Action Feed (0 steps)</span>';
                var copyBtn = document.createElement('button');
                copyBtn.className = 'copy-btn';
                copyBtn.textContent = '📋 Copy';
                copyBtn.addEventListener('click', function(e) { e.stopPropagation(); copyActionFeedInSlot(slotId); });
                header.appendChild(copyBtn);
                var feedEl = slot.feedEl;
                header.addEventListener('click', function() {
                    feedEl.classList.toggle('expanded');
                    header.querySelector('.chevron').textContent = feedEl.classList.contains('expanded') ? '▼' : '▶';
                });
                slot.feedBody = document.createElement('div');
                slot.feedBody.className = 'action-feed-body';
                if (slot.prompt) {
                    var promptEl = document.createElement('div');
                    promptEl.className = 'action-feed-prompt';
                    promptEl.innerHTML = '<span class="action-feed-prompt-label">Prompt:</span>' + esc(slot.prompt);
                    slot.feedBody.appendChild(promptEl);
                }
                slot.feedEl.appendChild(header);
                slot.feedEl.appendChild(slot.feedBody);
                slot.container.appendChild(slot.feedEl);
                if (slotId === activeSlot) {
                    currentFeedEl = slot.feedEl;
                    currentFeedBody = slot.feedBody;
                    stepCount = slot.stepCount;
                    scrollToBottom();
                }
            }

            function addActionInSlot(step, tool, args, slotId) {
                var slot = getSlot(slotId);
                slot.stepCount++;
                if (!slot.feedBody) createActionFeedInSlot(slotId);
                var titleEl = slot.feedEl.querySelector('.feed-title');
                if (titleEl) titleEl.textContent = 'Action Feed (' + slot.stepCount + ' steps)';
                var el = document.createElement('div');
                el.className = 'action-step';
                var argsText = args ? Object.keys(args).map(function(k) { return k + ': ' + String(args[k]).slice(0, 80); }).join(', ') : '';
                el.innerHTML = '<div class="action-header"><span class="step-num">[' + step + ']</span> <span class="tool-name">' + esc(tool || 'thinking') + '</span><span style="flex:1"></span><span class="chevron">+</span></div><div class="action-body">' + esc(argsText) + '</div>';
                el.querySelector('.action-header').addEventListener('click', function() { el.classList.toggle('expanded'); el.querySelector('.chevron').textContent = el.classList.contains('expanded') ? '-' : '+'; });
                slot.feedBody.appendChild(el);
                if (slotId === activeSlot) { stepCount = slot.stepCount; scrollToBottom(); }
            }

            function updateLastActionInSlot(success, output, error, slotId) {
                var slot = getSlot(slotId);
                var container = slot.feedBody || slot.container;
                var steps = container.querySelectorAll('.action-step');
                if (!steps.length) return;
                var last = steps[steps.length - 1];
                var tn = last.querySelector('.tool-name');
                if (tn) tn.classList.add(success ? 'success' : 'error');
                var body = last.querySelector('.action-body');
                if (body) body.textContent = success ? (output || '(no output)').slice(0, 500) : 'ERROR: ' + (error || 'unknown');
            }

            function finalizeActionFeedInSlot(result, slotId) {
                var slot = getSlot(slotId);
                if (!slot.feedEl) return;
                slot.answer = result.summary || '';
                var titleEl = slot.feedEl.querySelector('.feed-title');
                if (titleEl) titleEl.textContent = 'Action Feed (' + slot.stepCount + ' steps)';
            }

            function copyActionFeedInSlot(slotId) {
                var slot = getSlot(slotId);
                var parts = [];
                if (slot.prompt) parts.push('Prompt:\\n' + slot.prompt);
                if (slot.answer) parts.push('Answer:\\n' + slot.answer);
                var steps = slot.feedBody ? slot.feedBody.querySelectorAll('.action-step') : [];
                if (steps.length) {
                    var stepLines = [];
                    steps.forEach(function(s) {
                        var h = s.querySelector('.action-header');
                        var b = s.querySelector('.action-body');
                        stepLines.push((h ? h.textContent.replace(/[+-]$/, '').trim() : '') + '\\n' + (b ? b.textContent : ''));
                    });
                    parts.push('Action Feed (' + steps.length + ' steps):\\n' + stepLines.join('\\n\\n' + '-'.repeat(60) + '\\n\\n'));
                }
                var text = parts.join('\\n\\n' + '='.repeat(60) + '\\n\\n');
                navigator.clipboard.writeText(text);
            }

            function addCompletionCardToSlot(result, slotId) {
                var slot = getSlot(slotId);
                var el = document.createElement('div');
                var stopped = result.task_complete === false;
                el.className = 'message ' + (stopped ? 'error' : 'complete');
                var lines = [];
                lines.push(stopped ? '⚠ Task Stopped' : '✅ Task Complete');
                lines.push('━━━━━━━━━━━━━━━━━━━━━━━━━');
                if (result.summary) lines.push(result.summary);
                var files = result.files_changed || [];
                if (files.length) { lines.push(''); lines.push('📁 Files Changed (' + files.length + '):'); files.forEach(function(f) { lines.push('  • ' + f.replace(/[\\\\\\/]/g, '/').split('/').pop()); }); }
                var actions = result.actions || [];
                if (actions.length) { var successes = actions.filter(function(a) { return a.success; }).length; lines.push(''); lines.push('⚡ Steps: ' + actions.length + ' | ✅ ' + successes + '/' + actions.length); }
                var usage = result.usage || {};
                var inTok = usage.total_input_tokens || 0;
                var outTok = usage.total_output_tokens || 0;
                if (inTok || outTok) {
                    var fmt = function(n) { return n >= 1000 ? (n/1000).toFixed(1) + 'K' : n; };
                    var tokLine = '🔢 Tokens: ' + fmt(inTok) + ' in / ' + fmt(outTok) + ' out';
                    if (result.elapsed) { tokLine += ' · ⏱ ' + result.elapsed + 's'; }
                    lines.push(tokLine);
                }
                el.textContent = lines.join('\\n');
                el.style.whiteSpace = 'pre-wrap';
                el.style.fontFamily = 'var(--vscode-editor-font-family)';
                el.style.fontSize = '12px';
                el.style.lineHeight = '1.6';
                slot.container.appendChild(el);
                if (slotId === activeSlot) scrollToBottom();
            }

            function saveSlotState(slotId) {
                var slot = getSlot(slotId);
                vscode.postMessage({
                    type: 'save_slot_state',
                    slotId: slotId,
                    html: slot.container.innerHTML,
                    provider: slot.provider || '',
                    model: slot.model || '',
                    inputText: slotInput[slotId] || '',
                });
            }

            function addArtifactCardToSlot(title, content, artifactType, slotId) {
                var slot = getSlot(slotId);
                var card = document.createElement('div');
                card.className = 'artifact-card';
                var header = document.createElement('div');
                header.className = 'artifact-header';
                var badge = document.createElement('span');
                badge.className = 'artifact-badge';
                badge.textContent = (artifactType || 'plan').toUpperCase();
                var titleSpan = document.createElement('span');
                titleSpan.className = 'artifact-title';
                titleSpan.textContent = title || 'Artifact';
                var actions = document.createElement('div');
                actions.className = 'artifact-actions';
                var toggleBtn = document.createElement('button');
                toggleBtn.textContent = '>';
                var copyBtn = document.createElement('button');
                copyBtn.textContent = 'Copy';
                actions.appendChild(toggleBtn);
                actions.appendChild(copyBtn);
                header.appendChild(badge);
                header.appendChild(titleSpan);
                header.appendChild(actions);
                var body = document.createElement('div');
                body.className = 'artifact-body';
                body.textContent = content || '';
                card.appendChild(header);
                card.appendChild(body);
                slot.container.appendChild(card);
                if (slotId === activeSlot) scrollToBottom();
                toggleBtn.addEventListener('click', function(e) { e.stopPropagation(); body.style.display = body.style.display === 'none' ? 'block' : 'none'; toggleBtn.textContent = body.style.display === 'none' ? '>' : 'v'; });
                copyBtn.addEventListener('click', function(e) { e.stopPropagation(); navigator.clipboard.writeText((title || '') + '\\n' + (content || '')); copyBtn.textContent = '✓'; setTimeout(function() { copyBtn.textContent = 'Copy'; }, 1500); });
            }

            function addFileChangeCardToSlot(path, diff, tool, snapshot, isNew, slotId) {
                var slot = getSlot(slotId);
                // Create a simple file change notification in the slot container
                var card = document.createElement('div');
                card.className = 'diff-card';
                card.dataset.path = path || '';
                var basename = (path || '').replace(/[\\\\\\/]/g, '/').split('/').pop() || path;
                var toolLabel = tool || 'file_write';
                var header = document.createElement('div');
                header.className = 'diff-header';
                header.innerHTML = '<span class="diff-file">' + esc(basename) + ' (' + esc(toolLabel) + ')</span>';
                var body = document.createElement('div');
                body.className = 'diff-body';
                body.style.display = 'none';
                body.textContent = diff || '(no diff)';
                card.appendChild(header);
                card.appendChild(body);
                slot.container.appendChild(card);
                header.addEventListener('click', function() { body.style.display = body.style.display === 'none' ? 'block' : 'none'; });
                if (slotId === activeSlot) {
                    scrollToBottom();
                    vscode.postMessage({ type: 'open_file', path: path });
                    vscode.postMessage({ type: 'request_diagnostics', path: path });
                }
            }
            function addMessage(text, type, skipSave) {
                var el = document.createElement('div');
                el.className = 'message ' + type;
                el.textContent = text;
                var targetContainer = (slots[activeSlot] && slots[activeSlot].container) || messagesEl;
                targetContainer.appendChild(el);
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
                stepCount = 0;
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
                var _sc = (slots[activeSlot] && slots[activeSlot].container) || messagesEl; _sc.appendChild(currentFeedEl);
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
                    var tokLine = '🔢 Tokens: ' + fmt(inTok) + ' in / ' + fmt(outTok) + ' out';
                    if (result.elapsed) { tokLine += ' · ⏱ ' + result.elapsed + 's'; }
                    lines.push(tokLine);
                } else if (result.elapsed) {
                    lines.push('⏱ ' + result.elapsed + 's');
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
                var _sc = (slots[activeSlot] && slots[activeSlot].container) || messagesEl; _sc.appendChild(el);
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
