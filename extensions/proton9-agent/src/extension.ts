/**
 * Proton9 Agent Extension — Server Launcher
 *
 * This extension is responsible ONLY for:
 *   1. Spawning the P9 Python server (agent/core/server.py)
 *   2. Piping server stdout/stderr to a VS Code Output Channel
 *   3. Killing the server when the IDE closes
 *
 * All WebSocket communication, UI, and task handling is done by the
 * native PIDE integration (src/vs/workbench/contrib/proton9/).
 */
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { spawn, ChildProcess } from 'child_process';

let serverProcess: ChildProcess | null = null;
let outputChannel: vscode.OutputChannel;

function startP9Server(extensionPath: string): ChildProcess | null {
    const ideRoot = path.resolve(extensionPath, '..', '..');
    const workspaceRoot = path.resolve(ideRoot, '..');
    const preferredAgentDir = path.join(workspaceRoot, 'P9AE', 'runtime', 'agent');
    const transitionalAgentDir = path.join(workspaceRoot, 'runtime', 'agent');
    const legacyAgentDir = path.join(extensionPath, '..', '..', 'agent');
    const agentDir =
        [preferredAgentDir, transitionalAgentDir, legacyAgentDir].find(candidate => fs.existsSync(candidate)) ??
        legacyAgentDir;
    const entryPoint = path.join(agentDir, 'core', 'server.py');

    outputChannel.appendLine('[Pide] Starting P9 server...');
    outputChannel.appendLine('[Pide] Agent dir: ' + agentDir);
    outputChannel.appendLine('[Pide] Entry point: ' + entryPoint);

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
    console.log('[Proton9] Extension activating (server-launcher mode)...');

    outputChannel = vscode.window.createOutputChannel('Proton9 Agent', { log: true });

    // Launch the P9 server — native PIDE will auto-connect via P9BackendClient
    serverProcess = startP9Server(context.extensionPath);
    if (serverProcess) {
        outputChannel.appendLine('[Pide] Server launched. Native PIDE will auto-connect.');
    } else {
        outputChannel.appendLine('[Pide] Server failed to launch. Native PIDE will retry connection.');
    }

    // Register a reconnect command (restarts the server)
    context.subscriptions.push(
        vscode.commands.registerCommand('Proton9.restartServer', () => {
            if (serverProcess && !serverProcess.killed) {
                serverProcess.kill();
                outputChannel.appendLine('[Pide] Killed existing server for restart.');
            }
            serverProcess = startP9Server(context.extensionPath);
        })
    );

    console.log('[Proton9] Extension activated (server-launcher only)');
}

export function deactivate(): void {
    try {
        if (serverProcess && !serverProcess.killed) {
            serverProcess.kill();
            outputChannel?.appendLine('[Pide] P9 server stopped.');
        }
    } catch { /* swallow */ }
}
