/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { Emitter } from '../../../../base/common/event.js';
import { Disposable } from '../../../../base/common/lifecycle.js';
import { URI } from '../../../../base/common/uri.js';
import { IEditorService } from '../../../services/editor/common/editorService.js';
import { IEmbedderTerminalOptions, IEmbedderTerminalPty, IEmbedderTerminalService } from '../../../services/terminal/common/embedderTerminalService.js';
import { IViewsService } from '../../../services/views/common/viewsService.js';
import { IStorageService } from '../../../../platform/storage/common/storage.js';
import { INotificationService } from '../../../../platform/notification/common/notification.js';
import { IMarkerData, IMarkerService, MarkerSeverity } from '../../../../platform/markers/common/markers.js';
import { InstantiationType, registerSingleton } from '../../../../platform/instantiation/common/extensions.js';
import { IP9SessionService } from '../common/proton9Service.js';
import { IP9RuntimeService, P9RuntimeEvent } from '../common/proton9RuntimeService.js';
import { IP9ActionEntry, IP9ConnectionState, IP9NativeSession, IP9RuntimeStatusSnapshot, IP9TranscriptEntry } from '../common/proton9Types.js';
import { P9BackendClient } from './proton9BackendClient.js';
import { P9RuntimeStore } from './proton9RuntimeStore.js';

const P9_MARKER_OWNER = 'proton9';
const MAX_ENTRIES = 200;
const SEARCH_VIEW_CONTAINER_ID = 'workbench.view.search';

function createEntryId(prefix: string): string {
	return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

class Proton9TerminalPty extends Disposable implements IEmbedderTerminalPty {
	private readonly _onDidWrite = this._register(new Emitter<string>());
	readonly onDidWrite = this._onDidWrite.event;

	private readonly _onDidClose = this._register(new Emitter<void | number>());
	readonly onDidClose = this._onDidClose.event;

	open(): void {
		// no-op
	}

	close(): void {
		this._onDidClose.fire(0);
	}

	write(text: string): void {
		this._onDidWrite.fire(text);
	}
}

export class P9RuntimeService extends Disposable implements IP9RuntimeService {
	declare readonly _serviceBrand: undefined;

	private readonly _onDidChangeState = this._register(new Emitter<P9RuntimeEvent>());
	readonly onDidChangeState = this._onDidChangeState.event;

	private readonly backendClient = this._register(new P9BackendClient());
	private readonly runtimeStore: P9RuntimeStore;
	private readonly activeAssistantEntryIds = new Map<string, string>();
	private readonly connectionState: IP9ConnectionState = { connected: false };
	private lastTool: string | undefined;
	private lastCommand: string | undefined;
	private lastResourcePath: string | undefined;
	private terminalPty: Proton9TerminalPty | undefined;
	private hasCreatedTerminal = false;

	constructor(
		@IP9SessionService private readonly p9SessionService: IP9SessionService,
		@IStorageService storageService: IStorageService,
		@IEditorService private readonly editorService: IEditorService,
		@IEmbedderTerminalService private readonly embedderTerminalService: IEmbedderTerminalService,
		@IViewsService private readonly viewsService: IViewsService,
		@INotificationService private readonly notificationService: INotificationService,
		@IMarkerService private readonly markerService: IMarkerService,
	) {
		super();
		this.runtimeStore = this._register(new P9RuntimeStore(storageService));
		this.runtimeStore.syncSessions(this.p9SessionService.getSessions().map(session => session.tabId));
		this._register(this.p9SessionService.onDidChangeSessions(() => {
			this.runtimeStore.syncSessions(this.p9SessionService.getSessions().map(session => session.tabId));
			this._onDidChangeState.fire({ kind: 'sessions' });
		}));
		this._register(this.backendClient.onDidChangeConnection(connected => {
			this.connectionState.connected = connected;
			this.connectionState.lastEventAt = Date.now();
			if (!connected) {
				this.connectionState.lastError = this.connectionState.lastError ?? 'Disconnected from Proton9 backend.';
			}
			this._onDidChangeState.fire({ kind: 'connection' });
		}));
		this._register(this.backendClient.onDidReceiveEvent(event => {
			this.connectionState.lastEventAt = Date.now();
			if (event.type !== 'error' && event.type !== 'task_error') {
				this.connectionState.lastError = undefined;
			}
			this.handleBackendEvent(event.type, event.data);
		}));
	}

	getSessions(): readonly IP9NativeSession[] {
		return this.p9SessionService.getSessions();
	}

	getActiveSession(): IP9NativeSession | undefined {
		return this.p9SessionService.getActiveSession();
	}

	getConnectionState(): IP9ConnectionState {
		return { ...this.connectionState };
	}

	getStatusSnapshot(): IP9RuntimeStatusSnapshot {
		return {
			lastTool: this.lastTool,
			lastCommand: this.lastCommand,
			lastResourcePath: this.lastResourcePath,
			diagnosticCount: this.markerService.read({ owner: P9_MARKER_OWNER }).length,
		};
	}

	getTranscriptEntries(tabId: string): readonly IP9TranscriptEntry[] {
		return this.runtimeStore.getTranscriptEntries(tabId);
	}

	getActionEntries(tabId: string): readonly IP9ActionEntry[] {
		return this.runtimeStore.getActionEntries(tabId);
	}

	getComposerDraft(tabId: string): string {
		return this.runtimeStore.getDraft(tabId);
	}

	canCreateSession(): boolean {
		return this.p9SessionService.canCreateSession();
	}

	createSession(): IP9NativeSession {
		const session = this.p9SessionService.createSession();
		this._onDidChangeState.fire({ kind: 'sessions', tabId: session.tabId });
		return session;
	}

	async setActiveSession(tabId: string): Promise<void> {
		this.p9SessionService.setActiveSession(tabId);
		this._onDidChangeState.fire({ kind: 'sessions', tabId });
		const session = this.p9SessionService.getActiveSession();
		if (session && this.connectionState.connected) {
			try {
				await this.backendClient.switchSession(session);
			} catch (error) {
				this.reportError(error);
			}
		}
	}

	renameSession(tabId: string, title: string): void {
		this.p9SessionService.renameSession(tabId, title);
		this._onDidChangeState.fire({ kind: 'sessions', tabId });
	}

	async clearSession(tabId: string): Promise<void> {
		const session = this.getSessionOrThrow(tabId);
		if (this.connectionState.connected) {
			try {
				await this.backendClient.clearSession(session);
			} catch (error) {
				this.reportError(error);
			}
		}

		this.clearSessionMarkers(tabId);
		this.runtimeStore.clearSessionState(tabId);
		this.activeAssistantEntryIds.delete(tabId);
		this.p9SessionService.updateSessionStatus(tabId, 'idle');
		this._onDidChangeState.fire({ kind: 'sessions', tabId });
	}

	async deleteSession(tabId: string): Promise<void> {
		const session = this.getSessionOrThrow(tabId);
		if (this.connectionState.connected) {
			try {
				await this.backendClient.deleteSession(session);
			} catch (error) {
				this.reportError(error);
			}
		}

		this.clearSessionMarkers(tabId);
		this.runtimeStore.removeSessionState(tabId);
		this.activeAssistantEntryIds.delete(tabId);
		this.p9SessionService.removeSession(tabId);

		const activeSession = this.p9SessionService.getActiveSession();
		if (activeSession && this.connectionState.connected) {
			try {
				await this.backendClient.switchSession(activeSession);
			} catch (error) {
				this.reportError(error);
			}
		}

		this._onDidChangeState.fire({ kind: 'sessions', tabId });
	}

	updateComposerDraft(tabId: string, draft: string): void {
		this.runtimeStore.setDraft(tabId, draft);
	}

	async connect(): Promise<void> {
		try {
			await this.backendClient.ensureConnected();
			const activeSession = this.getActiveSession();
			if (activeSession) {
				await this.backendClient.switchSession(activeSession);
			}
		} catch (error) {
			this.reportError(error);
			throw error;
		}
	}

	async runTask(tabId: string, task: string): Promise<void> {
		const session = this.getSessionOrThrow(tabId);
		const trimmedTask = task.trim();
		if (!trimmedTask) {
			return;
		}

		this.appendTranscriptEntry(tabId, 'user', trimmedTask);
		this.runtimeStore.setDraft(tabId, '');
		this.p9SessionService.updateSessionStatus(tabId, 'running');
		this._onDidChangeState.fire({ kind: 'sessions', tabId });

		try {
			await this.backendClient.runTask(session, trimmedTask);
		} catch (error) {
			this.p9SessionService.updateSessionStatus(tabId, 'error');
			this.appendTranscriptEntry(tabId, 'error', this.errorMessage(error));
			this.reportError(error);
		}
	}

	async stopTask(tabId: string): Promise<void> {
		const session = this.getSessionOrThrow(tabId);
		try {
			await this.backendClient.stopTask(session);
		} catch (error) {
			this.reportError(error);
			this.appendTranscriptEntry(tabId, 'error', this.errorMessage(error));
		}
	}

	async openResource(path: string): Promise<void> {
		if (!path) {
			return;
		}

		await this.editorService.openEditor({
			resource: URI.file(path),
			options: { pinned: true },
		});
	}

	showTerminal(): void {
		this.ensureTerminal();
	}

	async revealSearch(): Promise<void> {
		await this.viewsService.openViewContainer(SEARCH_VIEW_CONTAINER_ID, true);
	}

	private handleBackendEvent(type: string, data: any): void {
		const session = this.resolveSessionForEvent(data);
		if (!session && type !== 'connected') {
			return;
		}

		switch (type) {
			case 'connected':
				this.connectionState.connected = true;
				this.connectionState.lastError = undefined;
				if (session) {
					this.appendTranscriptEntry(session.tabId, 'info', 'Connected to Proton9 backend.');
				}
				this._onDidChangeState.fire({ kind: 'connection' });
				break;
			case 'task_started':
				if (!session) {
					return;
				}
				this.p9SessionService.updateSessionStatus(session.tabId, 'running');
				this.ensureAssistantEntry(session.tabId);
				this._onDidChangeState.fire({ kind: 'sessions', tabId: session.tabId });
				break;
			case 'llm_token':
				if (!session) {
					return;
				}
				this.appendAssistantChunk(session.tabId, String(data?.text ?? ''));
				break;
			case 'action':
				if (!session) {
					return;
				}
				this.lastTool = typeof data?.tool === 'string' ? data.tool : undefined;
				this.lastCommand = typeof data?.args?.command === 'string' ? data.args.command : undefined;
				const actionEntry = this.appendActionEntry(session.tabId, 'action', `Tool: ${String(data?.tool ?? '?')}`, this.formatActionDetail(data), data?.args);
				this.appendTerminalLine(`[${session.slotId}] ACTION ${actionEntry.label}${actionEntry.command ? ` :: ${actionEntry.command}` : ''}`);
				this._onDidChangeState.fire({ kind: 'status', tabId: session.tabId });
				break;
			case 'result':
				if (!session) {
					return;
				}
				const detail = String(data?.output ?? data?.error ?? '');
				const resultEntry = this.appendActionEntry(session.tabId, 'result', data?.success ? 'Result: success' : 'Result: failure', detail);
				this.appendTerminalLine(`[${session.slotId}] RESULT ${data?.success ? 'ok' : 'error'}${detail ? ` :: ${detail}` : ''}`);
				if (!data?.success) {
					this.applyErrorMarker(detail, resultEntry.resourcePath);
				}
				this._onDidChangeState.fire({ kind: 'status', tabId: session.tabId });
				break;
			case 'info':
			case 'task_info':
				if (!session) {
					return;
				}
				this.appendTranscriptEntry(session.tabId, 'info', String(data?.message ?? ''));
				break;
			case 'task_complete':
				if (!session) {
					return;
				}
				this.p9SessionService.updateSessionStatus(session.tabId, 'idle');
				this.finishAssistantEntry(session.tabId);
				this.appendTranscriptEntry(session.tabId, 'summary', String(data?.result?.summary ?? 'Task complete.'));
				this._onDidChangeState.fire({ kind: 'sessions', tabId: session.tabId });
				break;
			case 'slot_complete':
				if (!session) {
					return;
				}
				this.p9SessionService.updateSessionStatus(session.tabId, 'idle');
				this.finishAssistantEntry(session.tabId);
				this.appendTranscriptEntry(session.tabId, 'info', 'Slot complete.');
				this._onDidChangeState.fire({ kind: 'sessions', tabId: session.tabId });
				break;
			case 'error':
			case 'task_error':
				if (!session) {
					return;
				}
				this.p9SessionService.updateSessionStatus(session.tabId, 'error');
				this.finishAssistantEntry(session.tabId);
				const message = String(data?.message ?? data?.error ?? 'unknown error');
				this.connectionState.lastError = message;
				this.appendTranscriptEntry(session.tabId, 'error', message);
				this.applyErrorMarker(message);
				this.notificationService.error(message);
				this._onDidChangeState.fire({ kind: 'sessions', tabId: session.tabId });
				break;
		}
	}

	private resolveSessionForEvent(data: any): IP9NativeSession | undefined {
		const slotId = typeof data?.slot_id === 'string' ? data.slot_id : undefined;
		if (slotId) {
			return this.p9SessionService.getSessions().find(candidate => candidate.slotId === slotId);
		}
		return this.p9SessionService.getActiveSession();
	}

	private appendTranscriptEntry(tabId: string, kind: IP9TranscriptEntry['kind'], text: string): IP9TranscriptEntry {
		const entry: IP9TranscriptEntry = {
			id: createEntryId(kind),
			kind,
			text,
			timestamp: Date.now(),
		};
		const entries = [...this.runtimeStore.getTranscriptEntries(tabId), entry].slice(-MAX_ENTRIES);
		this.runtimeStore.setTranscriptEntries(tabId, entries);
		this._onDidChangeState.fire({ kind: 'transcript-entry', tabId, entry });
		return entry;
	}

	private ensureAssistantEntry(tabId: string): void {
		if (this.activeAssistantEntryIds.has(tabId)) {
			return;
		}

		const entry: IP9TranscriptEntry = {
			id: createEntryId('assistant'),
			kind: 'assistant',
			text: '',
			timestamp: Date.now(),
		};
		const entries = [...this.runtimeStore.getTranscriptEntries(tabId), entry].slice(-MAX_ENTRIES);
		this.runtimeStore.setTranscriptEntries(tabId, entries);
		this.activeAssistantEntryIds.set(tabId, entry.id);
		this._onDidChangeState.fire({ kind: 'transcript-entry', tabId, entry });
	}

	private appendAssistantChunk(tabId: string, chunk: string): void {
		if (!chunk) {
			return;
		}

		this.ensureAssistantEntry(tabId);
		const activeId = this.activeAssistantEntryIds.get(tabId);
		const entries = this.runtimeStore.getTranscriptEntries(tabId).map(entry => ({ ...entry }));
		const target = entries.find(entry => entry.id === activeId);
		if (!target || !activeId) {
			return;
		}

		target.text += chunk;
		this.runtimeStore.setTranscriptEntries(tabId, entries);
		this._onDidChangeState.fire({ kind: 'assistant-chunk', tabId, entryId: activeId, text: target.text });
	}

	private finishAssistantEntry(tabId: string): void {
		this.activeAssistantEntryIds.delete(tabId);
	}

	private appendActionEntry(tabId: string, kind: IP9ActionEntry['kind'], label: string, detail: string, source?: unknown): IP9ActionEntry {
		const metadata = this.extractActionMetadata(detail, source);
		const entry: IP9ActionEntry = {
			id: createEntryId(kind),
			kind,
			label,
			detail,
			timestamp: Date.now(),
			resourcePath: metadata.resourcePath,
			command: metadata.command,
		};
		const entries = [...this.runtimeStore.getActionEntries(tabId), entry].slice(-MAX_ENTRIES);
		this.runtimeStore.setActionEntries(tabId, entries);

		if (entry.resourcePath) {
			this.lastResourcePath = entry.resourcePath;
		}
		if (entry.command) {
			this.lastCommand = entry.command;
		}

		this._onDidChangeState.fire({ kind: 'action-entry', tabId, entry });
		return entry;
	}

	private formatActionDetail(data: any): string {
		const parts: string[] = [];
		if (typeof data?.step === 'number') {
			parts.push(`step ${data.step}`);
		}
		if (data?.args && typeof data.args === 'object') {
			parts.push(JSON.stringify(data.args));
		}
		return parts.join(' | ');
	}

	private extractActionMetadata(detail: string, source?: unknown): { resourcePath?: string; command?: string } {
		const args = typeof source === 'object' && source !== null ? source as Record<string, unknown> : undefined;
		const command = this.extractCommand(args, detail);
		const resourcePath = this.extractResourcePath(args, detail);
		return { command, resourcePath };
	}

	private extractCommand(args: Record<string, unknown> | undefined, detail: string): string | undefined {
		if (typeof args?.command === 'string') {
			return args.command;
		}
		const match = detail.match(/"command":"([^"]+)"/);
		return match?.[1];
	}

	private extractResourcePath(args: Record<string, unknown> | undefined, detail: string): string | undefined {
		const directKeys = ['path', 'file', 'file_path', 'resource', 'working_dir'];
		for (const key of directKeys) {
			const value = args?.[key];
			if (typeof value === 'string' && this.looksLikePath(value)) {
				return value;
			}
		}

		const pathMatch = detail.match(/([A-Za-z]:[\\/][^:"|<>\r\n]+|\/[^:"|<>\r\n]+)/);
		return pathMatch?.[1];
	}

	private looksLikePath(value: string): boolean {
		return /^[A-Za-z]:[\\/]/.test(value) || value.startsWith('/');
	}

	private clearSessionMarkers(tabId: string): void {
		const resources = this.runtimeStore.getActionEntries(tabId)
			.map(entry => entry.resourcePath)
			.filter((value): value is string => Boolean(value))
			.map(resourcePath => URI.file(resourcePath));
		if (resources.length) {
			this.markerService.remove(P9_MARKER_OWNER, resources);
		}
	}

	private applyErrorMarker(message: string, fallbackPath?: string): void {
		const locationMatch = message.match(/([A-Za-z]:[\\/][^:\r\n]+|\/[^:\r\n]+):(\d+)(?::(\d+))?/);
		const path = locationMatch?.[1] ?? fallbackPath;
		if (!path) {
			return;
		}

		const line = locationMatch ? Number.parseInt(locationMatch[2], 10) : 1;
		const column = locationMatch?.[3] ? Number.parseInt(locationMatch[3], 10) : 1;
		const marker: IMarkerData = {
			severity: MarkerSeverity.Error,
			message,
			source: 'Proton9',
			startLineNumber: Number.isFinite(line) && line > 0 ? line : 1,
			startColumn: Number.isFinite(column) && column > 0 ? column : 1,
			endLineNumber: Number.isFinite(line) && line > 0 ? line : 1,
			endColumn: (Number.isFinite(column) && column > 0 ? column : 1) + 1,
		};
		this.markerService.changeOne(P9_MARKER_OWNER, URI.file(path), [marker]);
	}

	private appendTerminalLine(line: string): void {
		this.ensureTerminal();
		this.terminalPty?.write(`${line}\r\n`);
	}

	private ensureTerminal(): void {
		if (!this.terminalPty) {
			this.terminalPty = this._register(new Proton9TerminalPty());
		}
		if (!this.hasCreatedTerminal) {
			const options: IEmbedderTerminalOptions = {
				name: 'Proton9 Activity',
				pty: this.terminalPty,
			};
			this.embedderTerminalService.createTerminal(options);
			this.hasCreatedTerminal = true;
		}
	}

	private getSessionOrThrow(tabId: string): IP9NativeSession {
		const session = this.p9SessionService.getSessions().find(candidate => candidate.tabId === tabId);
		if (!session) {
			throw new Error(`Unknown Proton9 session: ${tabId}`);
		}
		return session;
	}

	private reportError(error: unknown): void {
		const message = this.errorMessage(error);
		this.connectionState.lastError = message;
		this._onDidChangeState.fire({ kind: 'connection' });
		this.notificationService.error(message);
	}

	private errorMessage(error: unknown): string {
		return error instanceof Error ? error.message : String(error);
	}
}

registerSingleton(IP9RuntimeService, P9RuntimeService, InstantiationType.Delayed);
