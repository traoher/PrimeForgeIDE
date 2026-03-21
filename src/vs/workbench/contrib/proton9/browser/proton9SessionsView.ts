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
import { IP9RuntimeService } from '../common/proton9RuntimeService.js';

export class Proton9SessionsView extends ViewPane {
	private readonly disposables = this._register(new DisposableStore());
	private bodyElement: HTMLElement | undefined;

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
		this._register(this.runtimeService.onDidChangeState(() => this.renderSessions()));
	}

	protected override renderBody(container: HTMLElement): void {
		super.renderBody(container);
		container.classList.add('proton9-pane');
		this.bodyElement = dom.append(container, dom.$('.proton9-pane-body'));
		this.renderSessions();
	}

	private renderSessions(): void {
		if (!this.bodyElement) {
			return;
		}

		this.disposables.clear();
		this.bodyElement.replaceChildren();

		const sessions = this.runtimeService.getSessions();
		const connection = this.runtimeService.getConnectionState();
		const activeSession = this.runtimeService.getActiveSession();
		const status = this.runtimeService.getStatusSnapshot();

		const header = dom.append(this.bodyElement, dom.$('.proton9-pane-header'));
		const titleRow = dom.append(header, dom.$('.proton9-title-row'));
		this.renderSessionTabs(titleRow, sessions, activeSession?.tabId);

		const controls = dom.append(header, dom.$('.proton9-toolbar'));
		const connectButton = dom.append(controls, dom.$('button.proton9-button.proton9-button-primary', { type: 'button' }, connection.connected ? nls.localize('proton9.sessions.reconnect', "Reconnect") : nls.localize('proton9.sessions.connect', "Connect")));
		this.disposables.add(dom.addDisposableListener(connectButton, dom.EventType.CLICK, async () => {
			await this.runtimeService.connect();
		}));

		dom.append(this.bodyElement, dom.$('div.proton9-meta-line', undefined, `backend: ${connection.connected ? 'connected' : 'disconnected'}`));
		if (connection.lastError) {
			dom.append(this.bodyElement, dom.$('div.proton9-error-banner', undefined, connection.lastError));
		}

		if (activeSession) {
			const activeCard = dom.append(this.bodyElement, dom.$('.proton9-card'));
			const activeHeader = dom.append(activeCard, dom.$('.proton9-pane-header'));
			dom.append(activeHeader, dom.$('div.proton9-card-title', undefined, activeSession.title));
			dom.append(activeHeader, dom.$('span.proton9-meta-chip', undefined, `${this.formatSessionLabel(activeSession.slotId)} | ${activeSession.status}`));
			dom.append(activeCard, dom.$('div.proton9-meta-line', undefined, `session: ${activeSession.sessionId}`));
			dom.append(activeCard, dom.$('div.proton9-meta-line', undefined, `diagnostics: ${status.diagnosticCount}`));
			const actions = dom.append(activeCard, dom.$('.proton9-toolbar'));
			const renameButton = dom.append(actions, dom.$('button.proton9-button', { type: 'button' }, nls.localize('proton9.sessions.rename', "Rename")));
			const clearButton = dom.append(actions, dom.$('button.proton9-button', { type: 'button' }, nls.localize('proton9.sessions.clear', "Clear")));
			const deleteButton = dom.append(actions, dom.$('button.proton9-button.proton9-button-danger', { type: 'button' }, nls.localize('proton9.sessions.delete', "Delete")));

			this.disposables.add(dom.addDisposableListener(renameButton, dom.EventType.CLICK, () => {
				const nextTitle = window.prompt('Rename Proton9 session', activeSession.title);
				if (nextTitle) {
					this.runtimeService.renameSession(activeSession.tabId, nextTitle);
				}
			}));
			this.disposables.add(dom.addDisposableListener(clearButton, dom.EventType.CLICK, async () => {
				await this.runtimeService.clearSession(activeSession.tabId);
			}));
			this.disposables.add(dom.addDisposableListener(deleteButton, dom.EventType.CLICK, async () => {
				await this.runtimeService.deleteSession(activeSession.tabId);
			}));
		}

		if (!sessions.length) {
			dom.append(this.bodyElement, dom.$('div.proton9-empty', undefined, nls.localize('proton9.sessions.empty', "No Proton9 sessions yet.")));
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
}
