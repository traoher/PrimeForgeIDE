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
import { IP9RuntimeService, P9RuntimeEvent } from '../common/proton9RuntimeService.js';
import { IP9TranscriptEntry } from '../common/proton9Types.js';

export class Proton9ChatView extends ViewPane {
	private readonly disposables = this._register(new DisposableStore());
	private bodyElement: HTMLElement | undefined;
	private renderedActiveTabId: string | undefined;
	private activeTranscriptListElement: HTMLElement | undefined;
	private readonly activeTranscriptTextElements = new Map<string, HTMLElement>();

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
		@IP9RuntimeService private readonly runtimeService: IP9RuntimeService,
	) {
		super(options, keybindingService, contextMenuService, configurationService, contextKeyService, viewDescriptorService, instantiationService, openerService, themeService, hoverService);
		this._register(this.runtimeService.onDidChangeState(event => this.handleRuntimeEvent(event)));
	}

	protected override renderBody(container: HTMLElement): void {
		super.renderBody(container);
		container.classList.add('proton9-pane');
		this.bodyElement = dom.append(container, dom.$('.proton9-pane-body'));
		this.renderChat();
	}

	private handleRuntimeEvent(event: P9RuntimeEvent): void {
		if (!this.bodyElement) {
			return;
		}

		switch (event.kind) {
			case 'transcript-entry':
				if (!this.patchTranscriptEntry(event.tabId, event.entry)) {
					this.renderChat();
				}
				break;
			case 'assistant-chunk':
				if (event.tabId === this.renderedActiveTabId) {
					const textElement = this.activeTranscriptTextElements.get(event.entryId);
					if (textElement) {
						textElement.textContent = event.text;
						return;
					}
				}
				this.renderChat();
				break;
			default:
				this.renderChat();
				break;
		}
	}

	private renderChat(): void {
		if (!this.bodyElement) {
			return;
		}

		this.disposables.clear();
		this.bodyElement.replaceChildren();
		this.activeTranscriptListElement = undefined;
		this.activeTranscriptTextElements.clear();

		const activeSession = this.runtimeService.getActiveSession();
		if (!activeSession) {
			this.renderedActiveTabId = undefined;
			dom.append(this.bodyElement, dom.$('div.proton9-empty', undefined, nls.localize('proton9.chat.empty', "Select a Proton9 session to start chatting.")));
			return;
		}

		this.renderedActiveTabId = activeSession.tabId;
		const card = dom.append(this.bodyElement, dom.$('.proton9-card'));
		const titleRow = dom.append(card, dom.$('.proton9-title-row'));
		dom.append(titleRow, dom.$('div.proton9-card-title.proton9-title-label', {
			title: `${activeSession.title} (${activeSession.slotId})`,
		}, `${activeSession.title} (${activeSession.slotId})`));
		this.renderSessionTabs(titleRow, activeSession.tabId);
		dom.append(card, dom.$('div.proton9-meta-line', undefined, `status: ${activeSession.status}`));

		const composer = dom.append(card, dom.$('.proton9-composer'));
		const input = dom.append(composer, dom.$('textarea.proton9-input', {
			rows: 6,
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

		dom.append(composer, dom.$('div.proton9-meta-line', undefined, nls.localize('proton9.chat.sendHint', "Press Enter to send. Press Shift+Enter for a new line.")));

		const actions = dom.append(composer, dom.$('.proton9-toolbar'));
		const runButton = dom.append(actions, dom.$('button.proton9-button.proton9-button-primary', { type: 'button' }, nls.localize('proton9.chat.run', "Send")));
		const stopButton = dom.append(actions, dom.$('button.proton9-button', { type: 'button' }, nls.localize('proton9.chat.stop', "Stop")));

		this.disposables.add(dom.addDisposableListener(runButton, dom.EventType.CLICK, async () => {
			await this.submitTask(activeSession.tabId, input);
		}));
		this.disposables.add(dom.addDisposableListener(stopButton, dom.EventType.CLICK, async () => {
			await this.runtimeService.stopTask(activeSession.tabId);
		}));

		const transcriptSection = dom.append(card, dom.$('.proton9-section'));
		dom.append(transcriptSection, dom.$('h4', undefined, nls.localize('proton9.chat.transcript', "Transcript")));
		const transcriptList = dom.append(transcriptSection, dom.$('.proton9-transcript-list'));
		this.activeTranscriptListElement = transcriptList;

		for (const entry of this.runtimeService.getTranscriptEntries(activeSession.tabId)) {
			this.renderTranscriptEntryDom(transcriptList, entry);
		}
	}

	private patchTranscriptEntry(tabId: string, entry: IP9TranscriptEntry): boolean {
		if (tabId !== this.renderedActiveTabId || !this.activeTranscriptListElement) {
			return false;
		}

		this.renderTranscriptEntryDom(this.activeTranscriptListElement, entry);
		return true;
	}

	private renderTranscriptEntryDom(container: HTMLElement, entry: IP9TranscriptEntry): void {
		const item = dom.append(container, dom.$(`div.proton9-transcript-entry.proton9-transcript-${entry.kind}`));
		const header = dom.append(item, dom.$('div.proton9-entry-header'));
		dom.append(header, dom.$('div.proton9-entry-kind', undefined, this.formatTranscriptKind(entry.kind)));
		const textElement = dom.append(item, dom.$('div.proton9-entry-text', undefined, entry.text));
		if (entry.kind === 'assistant' && this.renderedActiveTabId) {
			this.activeTranscriptTextElements.set(entry.id, textElement);
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

	private renderSessionTabs(container: HTMLElement, activeTabId: string): void {
		const strip = dom.append(container, dom.$('.proton9-session-tabs'));
		for (const session of this.runtimeService.getSessions()) {
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
			title: nls.localize('proton9.chat.newSession', "New session"),
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
