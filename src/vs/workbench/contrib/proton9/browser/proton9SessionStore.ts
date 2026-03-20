/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { Disposable } from '../../../../base/common/lifecycle.js';
import { IStorageService, StorageScope, StorageTarget } from '../../../../platform/storage/common/storage.js';
import { Memento } from '../../../common/memento.js';
import { IP9NativeSession } from '../common/proton9Types.js';

export class P9SessionStore extends Disposable {
	private static readonly STORAGE_ID = 'proton9.nativeSessions';

	private readonly sessions = new Map<string, IP9NativeSession>();
	private activeTabId: string | undefined;
	private readonly memento: Memento;
	private readonly state: {
		sessions?: IP9NativeSession[];
		activeTabId?: string;
	};

	constructor(storageService: IStorageService) {
		super();
		this.memento = new Memento(P9SessionStore.STORAGE_ID, storageService);
		this.state = this.memento.getMemento(StorageScope.WORKSPACE, StorageTarget.MACHINE);
		this.restore();
	}

	getSessions(): readonly IP9NativeSession[] {
		return Array.from(this.sessions.values()).sort((a, b) => a.slotId.localeCompare(b.slotId));
	}

	getActiveSession(): IP9NativeSession | undefined {
		return this.activeTabId ? this.sessions.get(this.activeTabId) : undefined;
	}

	addSession(session: IP9NativeSession): void {
		this.sessions.set(session.tabId, session);
		if (!this.activeTabId) {
			this.activeTabId = session.tabId;
		}
		this.save();
	}

	setActiveSession(tabId: string): void {
		if (this.sessions.has(tabId)) {
			this.activeTabId = tabId;
			this.save();
		}
	}

	updateSession(tabId: string, update: Partial<IP9NativeSession>): void {
		const current = this.sessions.get(tabId);
		if (!current) {
			return;
		}

		this.sessions.set(tabId, {
			...current,
			...update,
			tabId: current.tabId,
		});
		this.save();
	}

	removeSession(tabId: string): void {
		this.sessions.delete(tabId);
		if (this.activeTabId === tabId) {
			this.activeTabId = this.getSessions()[0]?.tabId;
		}
		this.save();
	}

	private restore(): void {
		const storedSessions = Array.isArray(this.state.sessions) ? this.state.sessions : [];
		for (const session of storedSessions) {
			if (session?.tabId && session?.slotId && session?.sessionId && session?.clientId) {
				this.sessions.set(session.tabId, session);
			}
		}

		const storedActiveTabId = this.state.activeTabId;
		if (storedActiveTabId && this.sessions.has(storedActiveTabId)) {
			this.activeTabId = storedActiveTabId;
		} else {
			this.activeTabId = this.getSessions()[0]?.tabId;
		}
	}

	private save(): void {
		this.state.sessions = this.getSessions().map(session => ({ ...session }));
		this.state.activeTabId = this.activeTabId;
		this.memento.saveMemento();
	}
}
