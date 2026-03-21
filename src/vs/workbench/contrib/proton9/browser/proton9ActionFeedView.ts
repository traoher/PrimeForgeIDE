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
import { IP9ActionEntry } from '../common/proton9Types.js';

export class Proton9ActionFeedView extends ViewPane {
	private readonly disposables = this._register(new DisposableStore());
	private bodyElement: HTMLElement | undefined;
	private renderedActiveTabId: string | undefined;
	private activeActionListElement: HTMLElement | undefined;

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
		this.renderActions();
	}

	private handleRuntimeEvent(event: P9RuntimeEvent): void {
		if (!this.bodyElement) {
			return;
		}

		if (event.kind === 'action-entry' && this.patchActionEntry(event.tabId, event.entry)) {
			return;
		}

		this.renderActions();
	}

	private renderActions(): void {
		if (!this.bodyElement) {
			return;
		}

		this.disposables.clear();
		this.bodyElement.replaceChildren();
		this.activeActionListElement = undefined;

		const activeSession = this.runtimeService.getActiveSession();
		if (!activeSession) {
			this.renderedActiveTabId = undefined;
			dom.append(this.bodyElement, dom.$('div.proton9-empty', undefined, nls.localize('proton9.actions.empty', "Action feed appears here when Proton9 uses tools.")));
			return;
		}

		this.renderedActiveTabId = activeSession.tabId;
		const card = dom.append(this.bodyElement, dom.$('.proton9-card'));
		dom.append(card, dom.$('div.proton9-card-title', undefined, nls.localize('proton9.actions.header', "Action Feed")));
		dom.append(card, dom.$('div.proton9-meta-line', undefined, `${activeSession.title} | ${activeSession.slotId}`));

		const list = dom.append(card, dom.$('.proton9-action-list'));
		this.activeActionListElement = list;
		for (const entry of this.runtimeService.getActionEntries(activeSession.tabId)) {
			this.renderActionEntryDom(list, entry);
		}
	}

	private patchActionEntry(tabId: string, entry: IP9ActionEntry): boolean {
		if (tabId !== this.renderedActiveTabId || !this.activeActionListElement) {
			return false;
		}

		this.renderActionEntryDom(this.activeActionListElement, entry);
		return true;
	}

	private renderActionEntryDom(container: HTMLElement, entry: IP9ActionEntry): void {
		const item = dom.append(container, dom.$(`div.proton9-action-entry proton9-action-${entry.kind}`));
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
			}
		}
	}
}
