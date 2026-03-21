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

export class Proton9StatusView extends ViewPane {
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
		this._register(this.runtimeService.onDidChangeState(() => this.renderStatus()));
	}

	protected override renderBody(container: HTMLElement): void {
		super.renderBody(container);
		container.classList.add('proton9-pane');
		this.bodyElement = dom.append(container, dom.$('.proton9-pane-body'));
		this.renderStatus();
	}

	private renderStatus(): void {
		if (!this.bodyElement) {
			return;
		}

		this.disposables.clear();
		this.bodyElement.replaceChildren();

		const activeSession = this.runtimeService.getActiveSession();
		const connection = this.runtimeService.getConnectionState();
		const status = this.runtimeService.getStatusSnapshot();

		const card = dom.append(this.bodyElement, dom.$('.proton9-card'));
		dom.append(card, dom.$('div.proton9-card-title', undefined, nls.localize('proton9.status.header', "Status")));
		dom.append(card, dom.$('div.proton9-meta-line', undefined, `backend: ${connection.connected ? 'connected' : 'disconnected'}`));
		if (activeSession) {
			dom.append(card, dom.$('div.proton9-meta-line', undefined, `active: ${activeSession.title} (${activeSession.slotId})`));
			dom.append(card, dom.$('div.proton9-meta-line', undefined, `state: ${activeSession.status}`));
		}
		if (status.lastTool) {
			dom.append(card, dom.$('div.proton9-meta-line', undefined, `last tool: ${status.lastTool}`));
		}
		if (status.lastCommand) {
			dom.append(card, dom.$('div.proton9-meta-line', undefined, `last command: ${status.lastCommand}`));
		}
		if (status.lastResourcePath) {
			dom.append(card, dom.$('div.proton9-meta-line', undefined, `last file: ${status.lastResourcePath}`));
		}
		dom.append(card, dom.$('div.proton9-meta-line', undefined, `diagnostics: ${status.diagnosticCount}`));

		if (connection.lastError) {
			dom.append(card, dom.$('div.proton9-error-banner', undefined, connection.lastError));
		}

		const toolbar = dom.append(card, dom.$('.proton9-toolbar'));
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
}
