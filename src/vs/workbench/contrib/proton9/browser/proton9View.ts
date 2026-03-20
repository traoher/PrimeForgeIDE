/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import * as dom from '../../../../base/browser/dom.js';
import { DisposableStore } from '../../../../base/common/lifecycle.js';
import * as nls from '../../../../nls.js';
import { IInstantiationService } from '../../../../platform/instantiation/common/instantiation.js';
import { IThemeService } from '../../../../platform/theme/common/themeService.js';
import { IContextMenuService } from '../../../../platform/contextview/browser/contextView.js';
import { IKeybindingService } from '../../../../platform/keybinding/common/keybinding.js';
import { IOpenerService } from '../../../../platform/opener/common/opener.js';
import { IConfigurationService } from '../../../../platform/configuration/common/configuration.js';
import { IContextKeyService } from '../../../../platform/contextkey/common/contextkey.js';
import { IViewDescriptorService } from '../../../common/views.js';
import { IViewPaneOptions, ViewPane } from '../../../browser/parts/views/viewPane.js';
import { IP9SessionService } from '../common/proton9Service.js';
import { P9BackendClient } from './proton9BackendClient.js';

export class Proton9SessionView extends ViewPane {
	private readonly disposables = this._register(new DisposableStore());
	private readonly backendClient = this._register(new P9BackendClient());
	private bodyElement: HTMLElement | undefined;
	private readonly sessionLogs = new Map<string, string[]>();
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
		@IP9SessionService private readonly p9SessionService: IP9SessionService,
	) {
		super(options, keybindingService, contextMenuService, configurationService, contextKeyService, viewDescriptorService, instantiationService, openerService, themeService);
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

				this.appendLog(activeSession.tabId, `> ${task}`);
				this.p9SessionService.updateSessionStatus(activeSession.tabId, 'running');
				try {
					await this.backendClient.runTask(activeSession, task);
				} catch (error) {
					this.p9SessionService.updateSessionStatus(activeSession.tabId, 'error');
					this.appendLog(activeSession.tabId, `[error] ${error instanceof Error ? error.message : String(error)}`);
				}
			}));

			this.disposables.add(dom.addDisposableListener(stopButton, dom.EventType.CLICK, async () => {
				try {
					await this.backendClient.stopTask(activeSession);
				} catch (error) {
					this.appendLog(activeSession.tabId, `[error] ${error instanceof Error ? error.message : String(error)}`);
				}
			}));

			const output = dom.append(activeCard, dom.$('pre.proton9-session-output'));
			output.textContent = this.getLogText(activeSession.tabId);
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
						this.appendLog(session.tabId, `[error] ${error instanceof Error ? error.message : String(error)}`);
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
				this.appendLog(activeSession.tabId, `[error] ${error instanceof Error ? error.message : String(error)}`);
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
				this.appendLog(session.tabId, '[system] connected to Proton9 backend');
				break;
			case 'task_started':
				this.p9SessionService.updateSessionStatus(session.tabId, 'running');
				this.appendLog(session.tabId, `[task] ${String(data?.task ?? 'task started')}`);
				break;
			case 'llm_token':
				this.appendLog(session.tabId, String(data?.text ?? ''));
				break;
			case 'info':
				this.appendLog(session.tabId, `[info] ${String(data?.message ?? '')}`);
				break;
			case 'task_complete':
				this.p9SessionService.updateSessionStatus(session.tabId, 'idle');
				this.appendLog(session.tabId, `[done] ${String(data?.result?.summary ?? 'task complete')}`);
				break;
			case 'slot_complete':
				this.p9SessionService.updateSessionStatus(session.tabId, 'idle');
				this.appendLog(session.tabId, '[slot] complete');
				break;
			case 'error':
			case 'task_error':
				this.p9SessionService.updateSessionStatus(session.tabId, 'error');
				this.appendLog(session.tabId, `[error] ${String(data?.message ?? data?.error ?? 'unknown error')}`);
				break;
		}
	}

	private appendLog(tabId: string, line: string): void {
		const entries = this.sessionLogs.get(tabId) ?? [];
		if (line) {
			entries.push(line);
		}
		this.sessionLogs.set(tabId, entries.slice(-200));
		this.renderSessions();
	}

	private getLogText(tabId: string): string {
		return (this.sessionLogs.get(tabId) ?? []).join('\n');
	}
}
