/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import './media/proton9.css';
import * as dom from '../../../../base/browser/dom.js';
import { DisposableStore } from '../../../../base/common/lifecycle.js';
import * as nls from '../../../../nls.js';
import { IInstantiationService } from '../../../../platform/instantiation/common/instantiation.js';
import { IThemeService } from '../../../../platform/theme/common/themeService.js';
import { IHoverService } from '../../../../platform/hover/browser/hover.js';
import { IContextMenuService } from '../../../../platform/contextview/browser/contextView.js';
import { IKeybindingService } from '../../../../platform/keybinding/common/keybinding.js';
import { IOpenerService } from '../../../../platform/opener/common/opener.js';
import { IConfigurationService } from '../../../../platform/configuration/common/configuration.js';
import { IContextKeyService } from '../../../../platform/contextkey/common/contextkey.js';
import { IViewDescriptorService } from '../../../common/views.js';
import { IViewPaneOptions, ViewPane } from '../../../browser/parts/views/viewPane.js';
import { IP9SessionService } from '../common/proton9Service.js';
import { IP9ActionEntry, IP9TranscriptEntry } from '../common/proton9Types.js';
import { P9BackendClient } from './proton9BackendClient.js';

export class Proton9SessionView extends ViewPane {
	private readonly disposables = this._register(new DisposableStore());
	private readonly backendClient = this._register(new P9BackendClient());
	private bodyElement: HTMLElement | undefined;
	private readonly sessionTranscripts = new Map<string, IP9TranscriptEntry[]>();
	private readonly sessionActions = new Map<string, IP9ActionEntry[]>();
	private readonly activeAssistantEntryIds = new Map<string, string>();
	private renderedActiveTabId: string | undefined;
	private activeTranscriptListElement: HTMLElement | undefined;
	private activeActionListElement: HTMLElement | undefined;
	private readonly activeTranscriptTextElements = new Map<string, HTMLElement>();
	private isConnected = false;

	constructor(
		options: IViewPaneOptions,
		@IInstantiationService instantiationService: IInstantiationService,
		@IViewDescriptorService viewDescriptorService: IViewDescriptorService,
		@IContextKeyService contextKeyService: IContextKeyService,
		@IConfigurationService configurationService: IConfigurationService,
		@IContextMenuService contextMenuService: IContextMenuService,
		@IKeybindingService keybindingService: IKeybindingService,
		@IOpenerService openerService: IOpenerService,
		@IThemeService themeService: IThemeService,
		@IHoverService hoverService: IHoverService,
		@IP9SessionService private readonly p9SessionService: IP9SessionService,
	) {
		super(options, keybindingService, contextMenuService, configurationService, contextKeyService, viewDescriptorService, instantiationService, openerService, themeService, hoverService);
		this._register(this.p9SessionService.onDidChangeSessions(() => this.renderSessions()));
		this._register(this.backendClient.onDidChangeConnection(connected => {
			this.isConnected = connected;
			this.renderSessions();
		}));
		this._register(this.backendClient.onDidReceiveEvent(event => {
			this.handleBackendEvent(event.type, event.data);
		}));
	}

	protected override renderBody(container: HTMLElement): void {
		super.renderBody(container);
		container.classList.add('proton9-session-view');
		this.bodyElement = dom.append(container, dom.$('.proton9-session-view-body'));
		this.renderSessions();
	}

	private renderSessions(): void {
		if (!this.bodyElement) {
			return;
		}

		this.disposables.clear();
		this.bodyElement.replaceChildren();

		const header = dom.append(this.bodyElement, dom.$('.proton9-session-header'));
		dom.append(header, dom.$('h3', undefined, nls.localize('proton9.sessions.title', "Proton9 Sessions")));

		const createButton = dom.append(header, dom.$('button.proton9-session-create', { type: 'button' }, nls.localize('proton9.sessions.new', "New Session")));
		this.disposables.add(dom.addDisposableListener(createButton, dom.EventType.CLICK, () => {
			this.p9SessionService.createSession();
		}));

		const activeSession = this.p9SessionService.getActiveSession();
		const connectionLine = dom.append(this.bodyElement, dom.$('.proton9-session-connection', undefined, `backend: ${this.isConnected ? 'connected' : 'disconnected'}`));
		if (!this.isConnected) {
			const connectButton = dom.append(connectionLine, dom.$('button.proton9-session-connect', { type: 'button' }, nls.localize('proton9.sessions.connect', "Connect")));
			this.disposables.add(dom.addDisposableListener(connectButton, dom.EventType.CLICK, async () => {
				await this.connectBackend();
			}));
		}

		if (activeSession) {
			this.renderedActiveTabId = activeSession.tabId;
			const activeCard = dom.append(this.bodyElement, dom.$('.proton9-session-active'));
			dom.append(activeCard, dom.$('div.proton9-session-active-title', undefined, `${activeSession.title} (${activeSession.slotId})`));
			dom.append(activeCard, dom.$('div.proton9-session-active-meta', undefined, `status: ${activeSession.status}`));
			dom.append(activeCard, dom.$('div.proton9-session-active-meta', undefined, `session: ${activeSession.sessionId}`));

			const composer = dom.append(activeCard, dom.$('.proton9-session-composer'));
			const input = dom.append(composer, dom.$('textarea.proton9-session-input', {
				rows: 5,
				placeholder: nls.localize('proton9.sessions.placeholder', "Type a task for this Proton9 session..."),
			})) as HTMLTextAreaElement;

			const actions = dom.append(composer, dom.$('.proton9-session-actions'));
			const runButton = dom.append(actions, dom.$('button.proton9-session-run', { type: 'button' }, nls.localize('proton9.sessions.run', "Run Task")));
			const stopButton = dom.append(actions, dom.$('button.proton9-session-stop', { type: 'button' }, nls.localize('proton9.sessions.stop', "Stop")));

			this.disposables.add(dom.addDisposableListener(runButton, dom.EventType.CLICK, async () => {
				const task = input.value.trim();
				if (!task) {
					return;
				}

				this.appendTranscriptEntry(activeSession.tabId, 'user', task);
				input.value = '';
				this.p9SessionService.updateSessionStatus(activeSession.tabId, 'running');
				try {
					await this.backendClient.runTask(activeSession, task);
				} catch (error) {
					this.p9SessionService.updateSessionStatus(activeSession.tabId, 'error');
					this.appendTranscriptEntry(activeSession.tabId, 'error', error instanceof Error ? error.message : String(error));
				}
			}));

			this.disposables.add(dom.addDisposableListener(stopButton, dom.EventType.CLICK, async () => {
				try {
					await this.backendClient.stopTask(activeSession);
				} catch (error) {
					this.appendTranscriptEntry(activeSession.tabId, 'error', error instanceof Error ? error.message : String(error));
				}
			}));

			const transcriptSection = dom.append(activeCard, dom.$('.proton9-session-transcript'));
			dom.append(transcriptSection, dom.$('h4', undefined, nls.localize('proton9.sessions.transcript', "Transcript")));
			const transcriptList = dom.append(transcriptSection, dom.$('.proton9-session-transcript-list'));
			this.activeTranscriptListElement = transcriptList;
			this.activeTranscriptTextElements.clear();
			for (const entry of this.getTranscriptEntries(activeSession.tabId)) {
				this.renderTranscriptEntryDom(transcriptList, entry);
			}

			const actionSection = dom.append(activeCard, dom.$('.proton9-session-actions-feed'));
			dom.append(actionSection, dom.$('h4', undefined, nls.localize('proton9.sessions.actions', "Action Feed")));
			const actionList = dom.append(actionSection, dom.$('.proton9-session-actions-list'));
			this.activeActionListElement = actionList;
			for (const entry of this.getActionEntries(activeSession.tabId)) {
				this.renderActionEntryDom(actionList, entry);
			}
		} else {
			this.renderedActiveTabId = undefined;
			this.activeTranscriptListElement = undefined;
			this.activeActionListElement = undefined;
			this.activeTranscriptTextElements.clear();
		}

		const list = dom.append(this.bodyElement, dom.$('ul.proton9-session-list'));
		for (const session of this.p9SessionService.getSessions()) {
			const item = dom.append(list, dom.$('li.proton9-session-item'));
			const isActive = activeSession?.tabId === session.tabId;
			const label = `${session.title}  ${isActive ? '• active' : ''}`;
			const button = dom.append(item, dom.$('button.proton9-session-switch', { type: 'button' }, label));
			this.disposables.add(dom.addDisposableListener(button, dom.EventType.CLICK, async () => {
				this.p9SessionService.setActiveSession(session.tabId);
				if (this.isConnected) {
					try {
						await this.backendClient.switchSession(session);
					} catch (error) {
						this.appendTranscriptEntry(session.tabId, 'error', error instanceof Error ? error.message : String(error));
					}
				}
			}));
			dom.append(item, dom.$('div.proton9-session-meta', undefined, `${session.slotId} | ${session.status}`));
		}

		if (!this.p9SessionService.getSessions().length) {
			dom.append(this.bodyElement, dom.$('div.proton9-session-empty', undefined, nls.localize('proton9.sessions.empty', "No Proton9 sessions yet.")));
		}
	}

	private async connectBackend(): Promise<void> {
		try {
			await this.backendClient.ensureConnected();
			const activeSession = this.p9SessionService.getActiveSession();
			if (activeSession) {
				await this.backendClient.switchSession(activeSession);
			}
		} catch (error) {
			const activeSession = this.p9SessionService.getActiveSession();
			if (activeSession) {
				this.appendTranscriptEntry(activeSession.tabId, 'error', error instanceof Error ? error.message : String(error));
			}
		}
	}

	private handleBackendEvent(type: string, data: any): void {
		const slotId = typeof data?.slot_id === 'string' ? data.slot_id : undefined;
		const session = slotId ? this.p9SessionService.getSessions().find(candidate => candidate.slotId === slotId) : this.p9SessionService.getActiveSession();
		if (!session) {
			return;
		}

		switch (type) {
			case 'connected':
				this.appendTranscriptEntry(session.tabId, 'info', 'Connected to Proton9 backend.');
				break;
			case 'task_started':
				this.p9SessionService.updateSessionStatus(session.tabId, 'running');
				this.ensureAssistantEntry(session.tabId);
				break;
			case 'llm_token':
				this.appendAssistantChunk(session.tabId, String(data?.text ?? ''));
				break;
			case 'action':
				this.appendActionEntry(session.tabId, 'action', `Tool: ${String(data?.tool ?? '?')}`, this.formatActionDetail(data));
				break;
			case 'result':
				this.appendActionEntry(session.tabId, 'result', data?.success ? 'Result: success' : 'Result: failure', String(data?.output ?? data?.error ?? ''));
				break;
			case 'info':
			case 'task_info':
				this.appendTranscriptEntry(session.tabId, 'info', String(data?.message ?? ''));
				break;
			case 'task_complete':
				this.p9SessionService.updateSessionStatus(session.tabId, 'idle');
				this.finishAssistantEntry(session.tabId);
				this.appendTranscriptEntry(session.tabId, 'summary', String(data?.result?.summary ?? 'Task complete.'));
				break;
			case 'slot_complete':
				this.p9SessionService.updateSessionStatus(session.tabId, 'idle');
				this.finishAssistantEntry(session.tabId);
				this.appendTranscriptEntry(session.tabId, 'info', 'Slot complete.');
				break;
			case 'error':
			case 'task_error':
				this.p9SessionService.updateSessionStatus(session.tabId, 'error');
				this.finishAssistantEntry(session.tabId);
				this.appendTranscriptEntry(session.tabId, 'error', String(data?.message ?? data?.error ?? 'unknown error'));
				break;
		}
	}

	private appendTranscriptEntry(tabId: string, kind: IP9TranscriptEntry['kind'], text: string): void {
		if (!text) {
			return;
		}

		const entries = this.sessionTranscripts.get(tabId) ?? [];
		entries.push({
			id: this.createEntryId(kind),
			kind,
			text,
			timestamp: Date.now(),
		});
		const trimmedEntries = entries.slice(-200);
		this.sessionTranscripts.set(tabId, trimmedEntries);
		const newEntry = trimmedEntries[trimmedEntries.length - 1];
		if (!this.patchTranscriptEntry(tabId, newEntry)) {
			this.renderSessions();
		}
	}

	private ensureAssistantEntry(tabId: string): void {
		if (this.activeAssistantEntryIds.has(tabId)) {
			return;
		}

		const entries = this.sessionTranscripts.get(tabId) ?? [];
		const entry: IP9TranscriptEntry = {
			id: this.createEntryId('assistant'),
			kind: 'assistant',
			text: '',
			timestamp: Date.now(),
		};
		entries.push(entry);
		const trimmedEntries = entries.slice(-200);
		this.sessionTranscripts.set(tabId, trimmedEntries);
		this.activeAssistantEntryIds.set(tabId, entry.id);
		if (!this.patchTranscriptEntry(tabId, entry)) {
			this.renderSessions();
		}
	}

	private appendAssistantChunk(tabId: string, chunk: string): void {
		if (!chunk) {
			return;
		}

		this.ensureAssistantEntry(tabId);
		const activeId = this.activeAssistantEntryIds.get(tabId);
		const entries = this.sessionTranscripts.get(tabId) ?? [];
		const target = entries.find(entry => entry.id === activeId);
		if (!target) {
			return;
		}

		target.text += chunk;
		if (tabId === this.renderedActiveTabId) {
			const textElement = this.activeTranscriptTextElements.get(target.id);
			if (textElement) {
				textElement.textContent = target.text;
				return;
			}
		}
		this.renderSessions();
	}

	private finishAssistantEntry(tabId: string): void {
		this.activeAssistantEntryIds.delete(tabId);
	}

	private appendActionEntry(tabId: string, kind: IP9ActionEntry['kind'], label: string, detail: string): void {
		const entries = this.sessionActions.get(tabId) ?? [];
		entries.push({
			id: this.createEntryId(kind),
			kind,
			label,
			detail,
			timestamp: Date.now(),
		});
		const trimmedEntries = entries.slice(-200);
		this.sessionActions.set(tabId, trimmedEntries);
		const newEntry = trimmedEntries[trimmedEntries.length - 1];
		if (!this.patchActionEntry(tabId, newEntry)) {
			this.renderSessions();
		}
	}

	private getTranscriptEntries(tabId: string): readonly IP9TranscriptEntry[] {
		return this.sessionTranscripts.get(tabId) ?? [];
	}

	private getActionEntries(tabId: string): readonly IP9ActionEntry[] {
		return this.sessionActions.get(tabId) ?? [];
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

	private createEntryId(prefix: string): string {
		return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
	}

	private patchTranscriptEntry(tabId: string, entry: IP9TranscriptEntry): boolean {
		if (tabId !== this.renderedActiveTabId || !this.activeTranscriptListElement) {
			return false;
		}

		this.renderTranscriptEntryDom(this.activeTranscriptListElement, entry);
		return true;
	}

	private patchActionEntry(tabId: string, entry: IP9ActionEntry): boolean {
		if (tabId !== this.renderedActiveTabId || !this.activeActionListElement) {
			return false;
		}

		this.renderActionEntryDom(this.activeActionListElement, entry);
		return true;
	}

	private renderTranscriptEntryDom(container: HTMLElement, entry: IP9TranscriptEntry): void {
		const item = dom.append(container, dom.$(`div.proton9-transcript-entry proton9-transcript-${entry.kind}`));
		const header = dom.append(item, dom.$('div.proton9-transcript-header'));
		dom.append(header, dom.$('div.proton9-transcript-kind', undefined, this.formatTranscriptKind(entry.kind)));
		const textElement = dom.append(item, dom.$('div.proton9-transcript-text', undefined, entry.text));
		if (entry.kind === 'assistant' && this.renderedActiveTabId) {
			this.activeTranscriptTextElements.set(entry.id, textElement);
		}
	}

	private renderActionEntryDom(container: HTMLElement, entry: IP9ActionEntry): void {
		const item = dom.append(container, dom.$(`div.proton9-action-entry proton9-action-${entry.kind}`));
		const header = dom.append(item, dom.$('div.proton9-action-header'));
		dom.append(header, dom.$('div.proton9-action-kind', undefined, entry.kind === 'action' ? 'Action' : 'Result'));
		dom.append(header, dom.$('div.proton9-action-label', undefined, entry.label));
		dom.append(item, dom.$('div.proton9-action-detail', undefined, entry.detail));
	}

	private formatTranscriptKind(kind: IP9TranscriptEntry['kind']): string {
		switch (kind) {
			case 'user':
				return 'You';
			case 'assistant':
				return 'Proton9';
			case 'info':
				return 'Info';
			case 'summary':
				return 'Summary';
			case 'error':
				return 'Error';
		}
	}
}
