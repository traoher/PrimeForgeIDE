/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import './media/proton9.css';
import * as dom from '../../../../base/browser/dom.js';
import { URI } from '../../../../base/common/uri.js';
import { DisposableStore } from '../../../../base/common/lifecycle.js';
import * as nls from '../../../../nls.js';
import { IClipboardService } from '../../../../platform/clipboard/common/clipboardService.js';
import { IInstantiationService } from '../../../../platform/instantiation/common/instantiation.js';
import { IThemeService } from '../../../../platform/theme/common/themeService.js';
import { IHoverService } from '../../../../platform/hover/browser/hover.js';
import { IContextMenuService } from '../../../../platform/contextview/browser/contextView.js';
import { IKeybindingService } from '../../../../platform/keybinding/common/keybinding.js';
import { IOpenerService } from '../../../../platform/opener/common/opener.js';
import { IQuickInputService } from '../../../../platform/quickinput/common/quickInput.js';
import { IConfigurationService } from '../../../../platform/configuration/common/configuration.js';
import { IContextKeyService } from '../../../../platform/contextkey/common/contextkey.js';
import { IViewDescriptorService } from '../../../common/views.js';
import { IViewPaneOptions, ViewPane } from '../../../browser/parts/views/viewPane.js';
import { IP9RuntimeService, P9RuntimeEvent } from '../common/proton9RuntimeService.js';
import { IP9ActionEntry, IP9NativeSession, IP9TranscriptEntry } from '../common/proton9Types.js';

type P9SectionKey = 'actions' | 'status';

interface IP9ContentSegment {
	kind: 'text' | 'code';
	text: string;
	language?: string;
}

export class Proton9SessionsView extends ViewPane {
	private readonly disposables = this._register(new DisposableStore());
	private readonly expandedSections: Record<P9SectionKey, boolean> = {
		actions: false,
		status: false,
	};
	private bodyElement: HTMLElement | undefined;
	private transcriptViewport: HTMLElement | undefined;
	private preserveComposerFocus = false;
	private preservedComposerSelectionStart: number | undefined;
	private preservedComposerSelectionEnd: number | undefined;

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
		@IClipboardService private readonly clipboardService: IClipboardService,
		@IP9RuntimeService private readonly runtimeService: IP9RuntimeService,
		@IQuickInputService private readonly quickInputService: IQuickInputService,
	) {
		super(options, keybindingService, contextMenuService, configurationService, contextKeyService, viewDescriptorService, instantiationService, openerService, themeService, hoverService);
		this._register(this.runtimeService.onDidChangeState(event => this.handleRuntimeEvent(event)));
	}

	protected override renderBody(container: HTMLElement): void {
		super.renderBody(container);
		container.classList.add('proton9-pane');
		this.bodyElement = dom.append(container, dom.$('.proton9-pane-body'));
		this.renderSessions();
	}

	private handleRuntimeEvent(event: P9RuntimeEvent): void {
		switch (event.kind) {
			case 'assistant-chunk':
			case 'transcript-entry':
				this.renderSessions(true);
				break;
			default:
				this.renderSessions();
				break;
		}
	}

	private renderSessions(forceStickTranscriptToBottom = false): void {
		if (!this.bodyElement) {
			return;
		}

		const activeElement = this.bodyElement.ownerDocument.activeElement;
		if (activeElement instanceof HTMLTextAreaElement && activeElement.classList.contains('proton9-input')) {
			this.preserveComposerFocus = true;
			this.preservedComposerSelectionStart = activeElement.selectionStart ?? activeElement.value.length;
			this.preservedComposerSelectionEnd = activeElement.selectionEnd ?? activeElement.value.length;
		} else {
			this.preserveComposerFocus = false;
			this.preservedComposerSelectionStart = undefined;
			this.preservedComposerSelectionEnd = undefined;
		}

		const previousTranscriptViewport = this.transcriptViewport;
		const previousBottomOffset = previousTranscriptViewport ? previousTranscriptViewport.scrollHeight - previousTranscriptViewport.scrollTop : 0;
		const shouldStickTranscriptToBottom = forceStickTranscriptToBottom || this.isNearBottom(previousTranscriptViewport);

		this.disposables.clear();
		this.bodyElement.replaceChildren();
		this.transcriptViewport = undefined;

		const sessions = this.runtimeService.getSessions();
		const connection = this.runtimeService.getConnectionState();
		const activeSession = this.runtimeService.getActiveSession();
		const status = this.runtimeService.getStatusSnapshot(activeSession?.tabId);
		const activeActions = activeSession ? this.runtimeService.getActionEntries(activeSession.tabId) : [];

		const shell = dom.append(this.bodyElement, dom.$('.proton9-master-shell'));
		const header = dom.append(shell, dom.$('.proton9-master-header'));
		const titleRow = dom.append(header, dom.$('.proton9-title-row'));
		this.renderSessionTabs(titleRow, sessions, activeSession?.tabId);

		const controls = dom.append(header, dom.$('.proton9-toolbar.proton9-master-controls'));
		if (connection.connected) {
			const connectedLabel = dom.append(controls, dom.$('span.proton9-connected-label', undefined, nls.localize('proton9.sessions.connected', "Connected")));
			connectedLabel.style.color = '#4caf50';
			connectedLabel.style.fontWeight = '600';
			connectedLabel.style.padding = '2px 8px';
		} else if (connection.connecting) {
			const connectingLabel = dom.append(controls, dom.$('span.proton9-connecting-label', undefined, nls.localize('proton9.sessions.connecting', "Connecting...")));
			connectingLabel.style.color = '#ff9800';
			connectingLabel.style.fontWeight = '600';
			connectingLabel.style.padding = '2px 8px';
		} else {
			const connectButton = dom.append(controls, dom.$('button.proton9-button.proton9-button-primary', { type: 'button' }, nls.localize('proton9.sessions.connect', "Connect")));
			this.disposables.add(dom.addDisposableListener(connectButton, dom.EventType.CLICK, async () => {
				await this.runtimeService.connect();
			}));
		}
		const renameButton = dom.append(controls, dom.$('button.proton9-button', { type: 'button' }, nls.localize('proton9.sessions.rename', "Rename")));
		renameButton.toggleAttribute('disabled', !activeSession);
		this.disposables.add(dom.addDisposableListener(renameButton, dom.EventType.CLICK, async () => {
			if (!activeSession) {
				return;
			}
			const inputBox = this.quickInputService.createInputBox();
			inputBox.title = nls.localize('proton9.sessions.renameTitle', "Rename Session");
			inputBox.placeholder = nls.localize('proton9.sessions.renamePlaceholder', "Enter a new name for this session");
			inputBox.value = activeSession.title;
			inputBox.onDidAccept(() => {
				const nextTitle = inputBox.value.trim();
				if (nextTitle) {
					this.runtimeService.renameSession(activeSession.tabId, nextTitle);
				}
				inputBox.dispose();
			});
			inputBox.onDidHide(() => inputBox.dispose());
			inputBox.show();
		}));
		const clearButton = dom.append(controls, dom.$('button.proton9-button', { type: 'button' }, nls.localize('proton9.sessions.clear', "Clear")));
		clearButton.toggleAttribute('disabled', !activeSession);
		this.disposables.add(dom.addDisposableListener(clearButton, dom.EventType.CLICK, async () => {
			if (activeSession) {
				await this.runtimeService.clearSession(activeSession.tabId);
			}
		}));
		const deleteButton = dom.append(controls, dom.$('button.proton9-button.proton9-button-danger', { type: 'button' }, nls.localize('proton9.sessions.delete', "Delete")));
		deleteButton.toggleAttribute('disabled', !activeSession);
		this.disposables.add(dom.addDisposableListener(deleteButton, dom.EventType.CLICK, async () => {
			if (activeSession) {
				await this.runtimeService.deleteSession(activeSession.tabId);
			}
		}));
		dom.append(controls, dom.$(`span.proton9-meta-chip.proton9-state-chip.proton9-state-${activeSession?.status ?? 'idle'}`, undefined, activeSession ? this.formatStateLabel(activeSession.status) : 'Ready'));

		const metaRow = dom.append(shell, dom.$('.proton9-master-meta'));
		const backendLabel = connection.connected ? 'connected' : connection.connecting ? 'connecting' : 'disconnected';
		dom.append(metaRow, dom.$('span.proton9-meta-line', undefined, `backend: ${backendLabel}`));
		if (activeSession) {
			dom.append(metaRow, dom.$('span.proton9-meta-line', undefined, `session: ${activeSession.sessionId}`));
			if (activeSession.model || activeSession.provider) {
				dom.append(metaRow, dom.$('span.proton9-meta-line', undefined, `model: ${activeSession.model ?? activeSession.provider}`));
			}
			if (status.currentLane) {
				dom.append(metaRow, dom.$('span.proton9-meta-line', undefined, `lane: ${status.currentLane}`));
			}
			if (status.terminalState) {
				const terminalLabel = status.blockId ? `${status.terminalState} (${status.blockId})` : status.terminalState;
				dom.append(metaRow, dom.$('span.proton9-meta-line', undefined, `task: ${terminalLabel}`));
			}
			dom.append(metaRow, dom.$('span.proton9-meta-line', undefined, `diagnostics: ${status.diagnosticCount}`));
		}
		if (connection.lastError && !connection.connecting) {
			dom.append(shell, dom.$('div.proton9-error-banner', undefined, connection.lastError));
		}

		if (!sessions.length) {
			dom.append(shell, dom.$('div.proton9-empty', undefined, nls.localize('proton9.sessions.empty', "No Proton9 sessions yet.")));
			return;
		}

		if (!activeSession) {
			dom.append(shell, dom.$('div.proton9-empty', undefined, nls.localize('proton9.sessions.select', "Select a Proton9 session to begin.")));
			return;
		}

		const transcriptViewport = dom.append(shell, dom.$('.proton9-transcript-viewport'));
		this.transcriptViewport = transcriptViewport;
		const transcriptEntries = this.runtimeService.getTranscriptEntries(activeSession.tabId).filter(entry => entry.kind !== 'summary' && entry.kind !== 'info');
		if (!transcriptEntries.length) {
			dom.append(transcriptViewport, dom.$('div.proton9-empty', undefined, nls.localize('proton9.transcript.empty', "Messages will appear here as you work with Proton9.")));
		} else {
			for (const entry of transcriptEntries) {
				this.renderTranscriptEntryDom(transcriptViewport, entry);
			}
		}

		const utilityControls = dom.append(shell, dom.$('.proton9-utility-controls'));
		this.renderUtilityToggle(utilityControls, 'actions', nls.localize('proton9.master.actions', "Actions"), activeActions.length);
		this.renderUtilityToggle(utilityControls, 'status', nls.localize('proton9.master.status', "Status"));

		if (this.expandedSections.actions || this.expandedSections.status) {
			const utilityStack = dom.append(shell, dom.$('.proton9-utility-stack'));
			if (this.expandedSections.actions) {
				this.renderActionsSection(utilityStack, activeActions);
			}
			if (this.expandedSections.status) {
				this.renderStatusSection(utilityStack, activeSession, connection.connected, connection.lastError, status);
			}
		}

		this.renderComposer(shell, activeSession);

		if (!this.transcriptViewport) {
			return;
		}
		if (shouldStickTranscriptToBottom) {
			this.transcriptViewport.scrollTop = this.transcriptViewport.scrollHeight;
		} else {
			this.transcriptViewport.scrollTop = Math.max(0, this.transcriptViewport.scrollHeight - previousBottomOffset);
		}
	}

	private renderSessionTabs(container: HTMLElement, sessions: ReturnType<IP9RuntimeService['getSessions']>, activeTabId: string | undefined): void {
		const strip = dom.append(container, dom.$('.proton9-session-tabs'));
		for (const session of sessions) {
			const tabButton = dom.append(strip, dom.$(`button.proton9-button.proton9-session-tab${activeTabId === session.tabId ? '.proton9-button-primary' : ''}`, {
				type: 'button',
				title: `${session.title} (${session.slotId})`,
			}, this.formatSessionLabel(session.slotId)));
			this.disposables.add(dom.addDisposableListener(tabButton, dom.EventType.CLICK, async () => {
				await this.runtimeService.setActiveSession(session.tabId);
			}));
		}

		const createButton = dom.append(strip, dom.$('button.proton9-button.proton9-session-tab.proton9-session-tab-add', {
			type: 'button',
			title: nls.localize('proton9.sessions.new', "New session"),
		}, '+'));
		createButton.toggleAttribute('disabled', !this.runtimeService.canCreateSession());
		this.disposables.add(dom.addDisposableListener(createButton, dom.EventType.CLICK, () => {
			try {
				this.runtimeService.createSession();
			} catch (error) {
				console.error(error);
			}
		}));
	}

	private formatSessionLabel(slotId: string): string {
		const match = /(\d+)$/.exec(slotId);
		return match ? `S${match[1]}` : slotId.toUpperCase();
	}

	private formatStateLabel(status: IP9NativeSession['status']): string {
		switch (status) {
			case 'running':
				return 'Running';
			case 'error':
				return 'Error';
			case 'stopped':
				return 'Stopped';
			default:
				return 'Ready';
		}
	}

	private renderTranscriptEntryDom(container: HTMLElement, entry: IP9TranscriptEntry): void {
		const item = dom.append(container, dom.$(`div.proton9-message.proton9-message-${entry.kind}`));
		const header = dom.append(item, dom.$('.proton9-entry-header'));
		dom.append(header, dom.$(`div.proton9-entry-kind.proton9-kind-${entry.kind}`, undefined, this.formatTranscriptKind(entry.kind)));
		dom.append(header, dom.$('div.proton9-meta-line', undefined, new Date(entry.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })));

		const content = dom.append(item, dom.$('.proton9-rich-content'));

		// Normalize escape sequences before splitting into segments
		// so both text and code blocks get clean content
		const normalizedText = this.normalizeText(entry.text);

		for (const segment of this.parseContentSegments(normalizedText)) {
			if (segment.kind === 'text') {
				this.renderMarkdownText(content, segment.text);
				continue;
			}

			const codeBlock = dom.append(content, dom.$('.proton9-code-block'));
			const codeHeader = dom.append(codeBlock, dom.$('.proton9-code-header'));
			const langLabel = segment.language ? `code:${segment.language}` : 'code:text';
			dom.append(codeHeader, dom.$('span.proton9-meta-chip.proton9-code-lang', undefined, langLabel));
			const copyButton = dom.append(codeHeader, dom.$('button.proton9-button.proton9-code-copy', { type: 'button' }, nls.localize('proton9.code.copy', "Copy")));
			this.disposables.add(dom.addDisposableListener(copyButton, dom.EventType.CLICK, async () => {
				await this.clipboardService.writeText(segment.text);
				copyButton.textContent = nls.localize('proton9.code.copied', "Copied");
				window.setTimeout(() => {
					copyButton.textContent = nls.localize('proton9.code.copy', "Copy");
				}, 1200);
			}));
			const pre = dom.append(codeBlock, dom.$('pre.proton9-code-pre'));
			const codeEl = dom.append(pre, dom.$('code.proton9-code-content'));
			this.highlightCode(codeEl, segment.text, segment.language);
		}
	}

	private parseContentSegments(text: string): IP9ContentSegment[] {
		if (!text) {
			return [{ kind: 'text', text: '' }];
		}

		const segments: IP9ContentSegment[] = [];
		const codeFencePattern = /```([\w-]+)?\n?([\s\S]*?)```/g;
		let lastIndex = 0;
		let match: RegExpExecArray | null;

		while ((match = codeFencePattern.exec(text)) !== null) {
			if (match.index > lastIndex) {
				segments.push({
					kind: 'text',
					text: text.slice(lastIndex, match.index).trim(),
				});
			}
			segments.push({
				kind: 'code',
				language: match[1]?.trim() || undefined,
				text: match[2].replace(/\n$/, ''),
			});
			lastIndex = match.index + match[0].length;
		}

		if (lastIndex < text.length) {
			segments.push({
				kind: 'text',
				text: text.slice(lastIndex).trim(),
			});
		}

		return segments.filter(segment => segment.text.length > 0);
	}

	private renderUtilityToggle(container: HTMLElement, key: P9SectionKey, label: string, count?: number): void {
		const suffix = typeof count === 'number' && count > 0 ? ` (${count})` : '';
		const button = dom.append(container, dom.$(`button.proton9-button.proton9-utility-toggle${this.expandedSections[key] ? '.proton9-button-primary' : ''}`, { type: 'button' }, `${label}${suffix}`));
		this.disposables.add(dom.addDisposableListener(button, dom.EventType.CLICK, () => {
			this.expandedSections[key] = !this.expandedSections[key];
			this.renderSessions();
		}));
	}

	private renderActionsSection(container: HTMLElement, actions: readonly IP9ActionEntry[]): void {
		const section = dom.append(container, dom.$('.proton9-section'));
		dom.append(section, dom.$('div.proton9-card-title', undefined, nls.localize('proton9.master.actionsSection', "Actions")));
		if (!actions.length) {
			dom.append(section, dom.$('div.proton9-empty', undefined, nls.localize('proton9.master.actionsEmpty', "No tool activity yet.")));
			return;
		}

		const list = dom.append(section, dom.$('.proton9-action-list'));
		for (const entry of [...actions].reverse()) {
			const item = dom.append(list, dom.$(`div.proton9-action-entry.proton9-action-${entry.kind}`));
			const header = dom.append(item, dom.$('div.proton9-entry-header'));
			dom.append(header, dom.$('div.proton9-entry-kind', undefined, entry.kind === 'action' ? 'Action' : 'Result'));
			dom.append(header, dom.$('div.proton9-action-label', undefined, entry.label));
			dom.append(item, dom.$('div.proton9-entry-text', undefined, entry.detail));

			if (entry.command || entry.resourcePath) {
				const toolbar = dom.append(item, dom.$('.proton9-toolbar'));
				if (entry.command) {
					dom.append(toolbar, dom.$('span.proton9-meta-chip', undefined, entry.command));
				}
				if (entry.resourcePath) {
					const openButton = dom.append(toolbar, dom.$('button.proton9-button', { type: 'button' }, nls.localize('proton9.actions.openFile', "Open File")));
					this.disposables.add(dom.addDisposableListener(openButton, dom.EventType.CLICK, async () => {
						await this.runtimeService.openResource(entry.resourcePath!);
					}));
					const revealButton = dom.append(toolbar, dom.$('button.proton9-button', { type: 'button' }, nls.localize('proton9.actions.openLocation', "Open Location")));
					this.disposables.add(dom.addDisposableListener(revealButton, dom.EventType.CLICK, () => {
						const filePath = entry.resourcePath!;
						const dirPath = filePath.replace(/[\\/][^\\/]+$/, '');
						this.openerService.open(URI.file(dirPath), { openExternal: true });
					}));
				}
			}
		}
	}

	private renderStatusSection(container: HTMLElement, activeSession: IP9NativeSession, connected: boolean, lastError: string | undefined, status: ReturnType<IP9RuntimeService['getStatusSnapshot']>): void {
		const section = dom.append(container, dom.$('.proton9-section'));
		dom.append(section, dom.$('div.proton9-card-title', undefined, nls.localize('proton9.master.statusSection', "Status")));
		const grid = dom.append(section, dom.$('.proton9-status-grid'));
		this.renderKeyValue(grid, 'Backend', connected ? 'Connected' : 'Disconnected');
		this.renderKeyValue(grid, 'State', this.formatStateLabel(activeSession.status));
		if (status.currentLane) {
			this.renderKeyValue(grid, 'Lane', status.currentLane);
		}
		if (status.routingEventId) {
			this.renderKeyValue(grid, 'Route event', status.routingEventId);
		}
		if (status.routingReason) {
			this.renderKeyValue(grid, 'Route reason', status.routingReason);
		}
		if (status.terminalState) {
			this.renderKeyValue(grid, 'Terminal state', status.terminalState);
		}
		if (status.terminalEventId) {
			this.renderKeyValue(grid, 'Terminal event', status.terminalEventId);
		}
		if (status.blockId) {
			this.renderKeyValue(grid, 'Block ID', status.blockId);
		}
		if (status.blockCategory) {
			this.renderKeyValue(grid, 'Block category', status.blockCategory);
		}
		if (status.requiredAction) {
			this.renderKeyValue(grid, 'Required action', status.requiredAction);
		}
		if (status.lastTool) {
			this.renderKeyValue(grid, 'Last tool', status.lastTool);
		}
		if (status.lastCommand) {
			this.renderKeyValue(grid, 'Last command', status.lastCommand);
		}
		if (status.lastResourcePath) {
			this.renderKeyValue(grid, 'Last file', status.lastResourcePath);
		}
		this.renderKeyValue(grid, 'Diagnostics', String(status.diagnosticCount));

		if (lastError) {
			dom.append(section, dom.$('div.proton9-error-banner', undefined, lastError));
		}

		const toolbar = dom.append(section, dom.$('.proton9-toolbar'));
		const terminalButton = dom.append(toolbar, dom.$('button.proton9-button', { type: 'button' }, nls.localize('proton9.status.terminal', "Show Terminal")));
		const searchButton = dom.append(toolbar, dom.$('button.proton9-button', { type: 'button' }, nls.localize('proton9.status.search', "Open Search")));
		const resourceButton = dom.append(toolbar, dom.$('button.proton9-button', { type: 'button' }, nls.localize('proton9.status.resource', "Open Last File")));
		resourceButton.toggleAttribute('disabled', !status.lastResourcePath);
		this.disposables.add(dom.addDisposableListener(terminalButton, dom.EventType.CLICK, () => {
			this.runtimeService.showTerminal();
		}));
		this.disposables.add(dom.addDisposableListener(searchButton, dom.EventType.CLICK, async () => {
			await this.runtimeService.revealSearch();
		}));
		this.disposables.add(dom.addDisposableListener(resourceButton, dom.EventType.CLICK, async () => {
			if (status.lastResourcePath) {
				await this.runtimeService.openResource(status.lastResourcePath);
			}
		}));
	}

	private renderKeyValue(container: HTMLElement, label: string, value: string): void {
		const row = dom.append(container, dom.$('.proton9-status-row'));
		dom.append(row, dom.$('span.proton9-status-key', undefined, label));
		dom.append(row, dom.$('span.proton9-status-value', { title: value }, value));
	}

	private renderComposer(container: HTMLElement, activeSession: IP9NativeSession): void {
		const composer = dom.append(container, dom.$('.proton9-master-composer'));
		const input = dom.append(composer, dom.$('textarea.proton9-input', {
			rows: 4,
			placeholder: nls.localize('proton9.chat.placeholder', "Type a task for this Proton9 session..."),
		})) as HTMLTextAreaElement;
		input.value = this.runtimeService.getComposerDraft(activeSession.tabId);
		this.disposables.add(dom.addDisposableListener(input, dom.EventType.INPUT, () => {
			this.runtimeService.updateComposerDraft(activeSession.tabId, input.value);
		}));
		this.disposables.add(dom.addDisposableListener(input, dom.EventType.KEY_DOWN, async event => {
			const keyboardEvent = event as KeyboardEvent;
			if (keyboardEvent.isComposing || keyboardEvent.key !== 'Enter' || keyboardEvent.shiftKey) {
				return;
			}
			keyboardEvent.preventDefault();
			await this.submitTask(activeSession.tabId, input);
		}));

		const footer = dom.append(composer, dom.$('.proton9-master-composer-footer'));
		dom.append(footer, dom.$('div.proton9-meta-line', undefined, nls.localize('proton9.chat.sendHint', "Press Enter to send. Press Shift+Enter for a new line.")));
		const actions = dom.append(footer, dom.$('.proton9-toolbar'));
		const sendButton = dom.append(actions, dom.$('button.proton9-button.proton9-button-primary', { type: 'button' }, nls.localize('proton9.chat.run', "Send")));
		const stopButton = dom.append(actions, dom.$('button.proton9-button', { type: 'button' }, nls.localize('proton9.chat.stop', "Stop")));
		stopButton.toggleAttribute('disabled', activeSession.status !== 'running');
		this.disposables.add(dom.addDisposableListener(sendButton, dom.EventType.CLICK, async () => {
			await this.submitTask(activeSession.tabId, input);
		}));
		this.disposables.add(dom.addDisposableListener(stopButton, dom.EventType.CLICK, async () => {
			await this.runtimeService.stopTask(activeSession.tabId);
		}));

		if (this.preserveComposerFocus) {
			input.focus();
			const selectionStart = Math.min(this.preservedComposerSelectionStart ?? input.value.length, input.value.length);
			const selectionEnd = Math.min(this.preservedComposerSelectionEnd ?? input.value.length, input.value.length);
			input.setSelectionRange(selectionStart, selectionEnd);
		}
	}

	private async submitTask(tabId: string, input: HTMLTextAreaElement): Promise<void> {
		const task = input.value.trim();
		if (!task) {
			return;
		}
		input.value = '';
		this.runtimeService.updateComposerDraft(tabId, '');
		await this.runtimeService.runTask(tabId, task);
	}

	private formatTranscriptKind(kind: IP9TranscriptEntry['kind']): string {
		switch (kind) {
			case 'user':
				return 'You';
			case 'assistant':
				return 'P9';
			case 'summary':
				return 'Summary';
			case 'error':
				return 'Error';
			default:
				return 'Info';
		}
	}

	private normalizeText(text: string): string {
		// Phase 1: JSON-style escape sequences
		let result = text
			.replace(/\\n/g, '\n')
			.replace(/\\"/g, '"');

		// Phase 2: LaTeX math delimiters → remove
		result = result
			.replace(/\\\(/g, '')     // \( → remove
			.replace(/\\\)/g, '')     // \) → remove
			.replace(/\\\[/g, '')     // \[ → remove
			.replace(/\\\]/g, '');    // \] → remove

		// Phase 3: LaTeX commands → Unicode symbols
		const latexMap: Array<[RegExp, string]> = [
			// Operators
			[/\\times\b/g, '×'],
			[/\\div\b/g, '÷'],
			[/\\cdot\b/g, '·'],
			[/\\pm\b/g, '±'],
			[/\\mp\b/g, '∓'],

			// Comparisons
			[/\\leq\b/g, '≤'],
			[/\\geq\b/g, '≥'],
			[/\\neq\b/g, '≠'],
			[/\\approx\b/g, '≈'],
			[/\\equiv\b/g, '≡'],
			[/\\sim\b/g, '∼'],

			// Arrows
			[/\\to\b/g, '→'],
			[/\\rightarrow\b/g, '→'],
			[/\\leftarrow\b/g, '←'],
			[/\\Rightarrow\b/g, '⇒'],
			[/\\Leftarrow\b/g, '⇐'],

			// Dots & misc
			[/\\dots\b/g, '…'],
			[/\\cdots\b/g, '⋯'],
			[/\\ldots\b/g, '…'],
			[/\\infty\b/g, '∞'],
			[/\\partial\b/g, '∂'],
			[/\\nabla\b/g, '∇'],

			// Greek letters (common)
			[/\\alpha\b/g, 'α'],
			[/\\beta\b/g, 'β'],
			[/\\gamma\b/g, 'γ'],
			[/\\delta\b/g, 'δ'],
			[/\\epsilon\b/g, 'ε'],
			[/\\theta\b/g, 'θ'],
			[/\\lambda\b/g, 'λ'],
			[/\\mu\b/g, 'μ'],
			[/\\pi\b/g, 'π'],
			[/\\sigma\b/g, 'σ'],
			[/\\phi\b/g, 'φ'],
			[/\\omega\b/g, 'ω'],
			[/\\Delta\b/g, 'Δ'],
			[/\\Sigma\b/g, 'Σ'],
			[/\\Pi\b/g, 'Π'],
			[/\\Omega\b/g, 'Ω'],

			// Set notation
			[/\\in\b/g, '∈'],
			[/\\notin\b/g, '∉'],
			[/\\subset\b/g, '⊂'],
			[/\\supset\b/g, '⊃'],
			[/\\cup\b/g, '∪'],
			[/\\cap\b/g, '∩'],
			[/\\emptyset\b/g, '∅'],
			[/\\forall\b/g, '∀'],
			[/\\exists\b/g, '∃'],

			// Big operators
			[/\\sum\b/g, 'Σ'],
			[/\\prod\b/g, 'Π'],
			[/\\int\b/g, '∫'],

			// Misc
			[/\\not=/g, '≠'],
			[/\\langle\b/g, '⟨'],
			[/\\rangle\b/g, '⟩'],
		];

		for (const [pattern, replacement] of latexMap) {
			result = result.replace(pattern, replacement);
		}

		// Phase 4: \boxed{content} → content, \frac{a}{b} → a/b, \sqrt{x} → √x
		result = result
			.replace(/\\boxed\{([^}]*)\}/g, '$1')
			.replace(/\\frac\{([^}]*)\}\{([^}]*)\}/g, '$1/$2')
			.replace(/\\sqrt\{([^}]*)\}/g, '√$1')
			.replace(/\\text\{([^}]*)\}/g, '$1');

		// Phase 5: Strip tool call descriptions (redundant with Actions panel)
		// Matches "Calling tool_name({...})" including nested braces
		result = result.replace(/Calling\s+\w+\(\{[\s\S]*?\}\)/g, '');

		// Clean up leftover double newlines from stripped content
		result = result.replace(/\n{3,}/g, '\n\n');

		return result;
	}

	private renderMarkdownText(container: HTMLElement, text: string): void {
		if (!text) {
			return;
		}

		// Normalize escape sequences from LLM output
		// Only handle \n and \" — avoid \t which breaks LaTeX (\times, \theta, etc.)
		const normalized = text;

		// Split into paragraphs on double newlines
		const paragraphs = normalized.split(/\n{2,}/);

		for (const paragraph of paragraphs) {
			const trimmed = paragraph.trim();
			if (!trimmed) {
				continue;
			}

			// Check if this paragraph is a group of list items
			const lines = trimmed.split('\n');
			const isListBlock = lines.every(line => /^(\s*[-*]\s|^\s*\d+[.)]\s)/.test(line.trim()) || !line.trim());
			const isHeaderLine = lines.length === 1 && /^#{1,4}\s/.test(trimmed);

			if (isHeaderLine) {
				// ## Header → styled heading
				const level = (trimmed.match(/^(#+)/) ?? ['', '#'])[1].length;
				const headerText = trimmed.replace(/^#+\s*/, '');
				const el = dom.append(container, dom.$(`div.proton9-md-heading.proton9-md-h${Math.min(level, 4)}`));
				this.renderInlineMarkdown(el, headerText);
			} else if (isListBlock) {
				// - item or 1. item → list
				const list = dom.append(container, dom.$('div.proton9-md-list'));
				for (const line of lines) {
					const trimmedLine = line.trim();
					if (!trimmedLine) {
						continue;
					}
					const listText = trimmedLine.replace(/^[-*]\s+/, '').replace(/^\d+[.)]\s+/, '');
					const item = dom.append(list, dom.$('div.proton9-md-list-item'));
					dom.append(item, dom.$('span.proton9-md-bullet', undefined, '•'));
					const itemContent = dom.append(item, dom.$('span'));
					this.renderInlineMarkdown(itemContent, listText);
				}
			} else {
				// Regular paragraph — render line by line for single \n breaks
				const block = dom.append(container, dom.$('div.proton9-entry-text'));
				for (let i = 0; i < lines.length; i++) {
					if (i > 0) {
						block.appendChild(document.createElement('br'));
					}
					this.renderInlineMarkdown(block, lines[i]);
				}
			}
		}
	}

	private renderInlineMarkdown(container: HTMLElement, text: string): void {
		// Handle **bold** and *italic*
		const boldPattern = /\*\*(.+?)\*\*/g;
		let lastIndex = 0;
		let match: RegExpExecArray | null;

		while ((match = boldPattern.exec(text)) !== null) {
			if (match.index > lastIndex) {
				container.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
			}
			const strong = dom.append(container, dom.$('strong.proton9-md-bold'));
			strong.textContent = match[1];
			lastIndex = match.index + match[0].length;
		}

		if (lastIndex < text.length) {
			container.appendChild(document.createTextNode(text.slice(lastIndex)));
		}
	}

	private isNearBottom(element: HTMLElement | undefined): boolean {
		if (!element) {
			return true;
		}
		return element.scrollHeight - element.scrollTop - element.clientHeight < 48;
	}

	private highlightCode(container: HTMLElement, code: string, language?: string): void {
		const tokens = this.tokenizeCode(code, language);
		for (const token of tokens) {
			if (token.kind === 'plain') {
				container.appendChild(document.createTextNode(token.text));
			} else {
				const span = dom.append(container, dom.$(`span.proton9-hl-${token.kind}`));
				span.textContent = token.text;
			}
		}
	}

	private tokenizeCode(code: string, language?: string): Array<{ kind: string; text: string }> {
		const lang = (language ?? '').toLowerCase();
		const tokens: Array<{ kind: string; text: string }> = [];
		let remaining = code;

		const commentSingle = lang === 'python' || lang === 'ruby' || lang === 'shell' || lang === 'bash' || lang === 'sh' || lang === 'yaml' ? '#' : '//';
		const pyKeywords = /\b(def|class|return|import|from|if|elif|else|for|while|try|except|finally|with|as|raise|pass|break|continue|and|or|not|in|is|lambda|yield|async|await|None|True|False|self|print)\b/g;
		const jsKeywords = /\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|try|catch|finally|throw|new|delete|typeof|instanceof|class|extends|import|export|from|default|async|await|yield|this|super|true|false|null|undefined|void|of|in)\b/g;
		const genericKeywords = /\b(if|else|for|while|return|class|function|import|export|true|false|null|void|int|str|bool|float|string|number|def|const|let|var)\b/g;

		let keywordPattern: RegExp;
		if (lang === 'python' || lang === 'py') {
			keywordPattern = pyKeywords;
		} else if (lang === 'javascript' || lang === 'js' || lang === 'typescript' || lang === 'ts' || lang === 'jsx' || lang === 'tsx') {
			keywordPattern = jsKeywords;
		} else {
			keywordPattern = genericKeywords;
		}

		while (remaining.length > 0) {
			// Comments (single-line)
			const commentIdx = remaining.indexOf(commentSingle);
			// Strings
			const singleQuoteIdx = remaining.indexOf("'");
			const doubleQuoteIdx = remaining.indexOf('"');
			const backtickIdx = remaining.indexOf('`');

			const candidates: Array<{ idx: number; kind: string; end: () => number }> = [];

			if (commentIdx >= 0) {
				candidates.push({ idx: commentIdx, kind: 'comment', end: () => {
					const nl = remaining.indexOf('\n', commentIdx);
					return nl >= 0 ? nl : remaining.length;
				}});
			}
			if (singleQuoteIdx >= 0) {
				candidates.push({ idx: singleQuoteIdx, kind: 'string', end: () => {
					const close = remaining.indexOf("'", singleQuoteIdx + 1);
					return close >= 0 ? close + 1 : remaining.length;
				}});
			}
			if (doubleQuoteIdx >= 0) {
				candidates.push({ idx: doubleQuoteIdx, kind: 'string', end: () => {
					const close = remaining.indexOf('"', doubleQuoteIdx + 1);
					return close >= 0 ? close + 1 : remaining.length;
				}});
			}
			if (backtickIdx >= 0) {
				candidates.push({ idx: backtickIdx, kind: 'string', end: () => {
					const close = remaining.indexOf('`', backtickIdx + 1);
					return close >= 0 ? close + 1 : remaining.length;
				}});
			}

			if (candidates.length === 0) {
				// No more special tokens — process remainder for keywords/numbers
				this.tokenizePlainSegment(remaining, keywordPattern, tokens);
				break;
			}

			candidates.sort((a, b) => a.idx - b.idx);
			const first = candidates[0];

			// Process plain text before the special token
			if (first.idx > 0) {
				this.tokenizePlainSegment(remaining.slice(0, first.idx), keywordPattern, tokens);
			}

			const endPos = first.end();
			tokens.push({ kind: first.kind, text: remaining.slice(first.idx, endPos) });
			remaining = remaining.slice(endPos);
		}

		return tokens;
	}

	private tokenizePlainSegment(text: string, keywordPattern: RegExp, tokens: Array<{ kind: string; text: string }>): void {
		const combinedPattern = new RegExp(`(${keywordPattern.source})|(\\b\\d+(?:\\.\\d+)?\\b)`, 'g');
		let lastIndex = 0;
		let match: RegExpExecArray | null;

		while ((match = combinedPattern.exec(text)) !== null) {
			if (match.index > lastIndex) {
				tokens.push({ kind: 'plain', text: text.slice(lastIndex, match.index) });
			}

			if (match[1]) {
				tokens.push({ kind: 'keyword', text: match[0] });
			} else {
				tokens.push({ kind: 'number', text: match[0] });
			}

			lastIndex = match.index + match[0].length;
		}

		if (lastIndex < text.length) {
			tokens.push({ kind: 'plain', text: text.slice(lastIndex) });
		}
	}
}
