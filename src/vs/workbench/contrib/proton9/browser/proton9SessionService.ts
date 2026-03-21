/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { Emitter } from '../../../../base/common/event.js';
import { Disposable } from '../../../../base/common/lifecycle.js';
import { IStorageService } from '../../../../platform/storage/common/storage.js';
import { IP9SessionService } from '../common/proton9Service.js';
import { IP9NativeSession, P9_MAX_SESSIONS, P9SessionStatus } from '../common/proton9Types.js';
import { P9SessionStore } from './proton9SessionStore.js';

function createId(prefix: string): string {
	return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
}

export class P9SessionService extends Disposable implements IP9SessionService {
	declare readonly _serviceBrand: undefined;
	private readonly _onDidChangeSessions = this._register(new Emitter<void>());
	readonly onDidChangeSessions = this._onDidChangeSessions.event;

	private readonly store: P9SessionStore;

	constructor(
		@IStorageService storageService: IStorageService,
	) {
		super();
		this.store = this._register(new P9SessionStore(storageService));
	}

	getSessions(): readonly IP9NativeSession[] {
		return this.store.getSessions();
	}

	getActiveSession(): IP9NativeSession | undefined {
		return this.store.getActiveSession();
	}

	canCreateSession(): boolean {
		return this.store.getSessions().length < P9_MAX_SESSIONS;
	}

	createSession(): IP9NativeSession {
		const sessionCount = this.getNextSessionOrdinal();
		if (!sessionCount) {
			throw new Error(`Proton9 is limited to ${P9_MAX_SESSIONS} sessions.`);
		}

		const session: IP9NativeSession = {
			tabId: createId('p9tab'),
			slotId: `p9-${sessionCount}`,
			sessionId: createId('sess'),
			clientId: createId('client'),
			title: `P9-${sessionCount}`,
			status: 'idle',
			lastActiveAt: Date.now(),
		};

		this.store.addSession(session);
		this.store.setActiveSession(session.tabId);
		this._onDidChangeSessions.fire();
		return session;
	}

	setActiveSession(tabId: string): void {
		this.store.setActiveSession(tabId);
		this.store.updateSession(tabId, { lastActiveAt: Date.now() });
		this._onDidChangeSessions.fire();
	}

	updateSession(tabId: string, update: Partial<IP9NativeSession>): void {
		this.store.updateSession(tabId, {
			...update,
			lastActiveAt: Date.now(),
		});
		this._onDidChangeSessions.fire();
	}

	renameSession(tabId: string, title: string): void {
		const trimmedTitle = title.trim();
		if (!trimmedTitle) {
			return;
		}

		this.store.updateSession(tabId, {
			title: trimmedTitle,
			lastActiveAt: Date.now(),
		});
		this._onDidChangeSessions.fire();
	}

	updateSessionStatus(tabId: string, status: P9SessionStatus): void {
		this.store.updateSession(tabId, {
			status,
			lastActiveAt: Date.now(),
		});
		this._onDidChangeSessions.fire();
	}

	removeSession(tabId: string): void {
		this.store.removeSession(tabId);
		this._onDidChangeSessions.fire();
	}

	private getNextSessionOrdinal(): number | undefined {
		const ordinals = new Set(this.store.getSessions()
			.map(session => Number.parseInt(session.slotId.replace(/^p9-/, ''), 10))
			.filter(value => Number.isFinite(value) && value > 0));

		for (let ordinal = 1; ordinal <= P9_MAX_SESSIONS; ordinal++) {
			if (!ordinals.has(ordinal)) {
				return ordinal;
			}
		}

		return undefined;
	}
}
