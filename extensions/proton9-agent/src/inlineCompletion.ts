import * as vscode from 'vscode';
import { ForgeWebSocket } from './websocket';

/**
 * Proton9 Inline Completion Provider
 * 
 * Provides AI-powered code suggestions as ghost text (Tab to accept).
 * Uses Gemini flash-lite model via the P9 server for fast completions.
 */

let pendingRequest: { cancel: () => void } | null = null;
let lastRequestTime = 0;
const DEBOUNCE_MS = 300;
const CONTEXT_LINES = 30;

export class InlineCompletionProvider implements vscode.InlineCompletionItemProvider {
    private forge: ForgeWebSocket;
    private enabled: boolean = true;

    constructor(forge: ForgeWebSocket) {
        this.forge = forge;
    }

    async provideInlineCompletionItems(
        document: vscode.TextDocument,
        position: vscode.Position,
        context: vscode.InlineCompletionContext,
        token: vscode.CancellationToken
    ): Promise<vscode.InlineCompletionItem[]> {
        if (!this.enabled || !this.forge.connected) {
            return [];
        }

        // Skip for non-code files
        const skipLanguages = new Set([
            'plaintext', 'log', 'scminput', 'git-commit', 'git-rebase',
            'search-result', 'output',
        ]);
        if (skipLanguages.has(document.languageId)) {
            return [];
        }

        // Debounce — wait 300ms from last keystroke
        const now = Date.now();
        lastRequestTime = now;
        await new Promise(resolve => setTimeout(resolve, DEBOUNCE_MS));
        if (lastRequestTime !== now || token.isCancellationRequested) {
            return [];
        }

        // Gather context: lines before and after cursor
        const lineNum = position.line;
        const startLine = Math.max(0, lineNum - CONTEXT_LINES);
        const endLine = Math.min(document.lineCount - 1, lineNum + 5);

        const prefix = document.getText(
            new vscode.Range(startLine, 0, lineNum, position.character)
        );
        const suffix = document.getText(
            new vscode.Range(lineNum, position.character, endLine, document.lineAt(endLine).text.length)
        );

        if (prefix.trim().length < 3) {
            return []; // Too little context
        }

        // Request completion from P9 server
        try {
            const completion = await this.requestCompletion(
                document.uri.fsPath,
                document.languageId,
                prefix,
                suffix,
                token,
            );

            if (!completion || token.isCancellationRequested) {
                return [];
            }

            // Create inline completion item
            const item = new vscode.InlineCompletionItem(
                completion,
                new vscode.Range(position, position),
            );

            return [item];
        } catch {
            return [];
        }
    }

    private requestCompletion(
        filePath: string,
        language: string,
        prefix: string,
        suffix: string,
        token: vscode.CancellationToken,
    ): Promise<string | null> {
        return new Promise<string | null>((resolve) => {
            if (!this.forge.connected) {
                resolve(null);
                return;
            }

            // Cancel any previous pending request
            if (pendingRequest) {
                pendingRequest.cancel();
            }

            let resolved = false;
            const timeout = setTimeout(() => {
                if (!resolved) {
                    resolved = true;
                    resolve(null);
                }
            }, 3000); // 3s timeout

            // Listen for completion response
            const handler = this.forge.onEvent((msg: any) => {
                if (msg.type === 'completion_result' && !resolved) {
                    resolved = true;
                    clearTimeout(timeout);
                    handler.dispose();
                    resolve(msg.completion || null);
                }
            });

            token.onCancellationRequested(() => {
                if (!resolved) {
                    resolved = true;
                    clearTimeout(timeout);
                    handler.dispose();
                    resolve(null);
                }
            });

            pendingRequest = {
                cancel: () => {
                    if (!resolved) {
                        resolved = true;
                        clearTimeout(timeout);
                        handler.dispose();
                        resolve(null);
                    }
                },
            };

            // Send completion request via WebSocket
            this.forge.send({
                type: 'complete',
                file: filePath,
                language: language,
                prefix: prefix.substring(Math.max(0, prefix.length - 2000)),
                suffix: suffix.substring(0, 500),
            });
        });
    }

    setEnabled(enabled: boolean) {
        this.enabled = enabled;
    }
}
