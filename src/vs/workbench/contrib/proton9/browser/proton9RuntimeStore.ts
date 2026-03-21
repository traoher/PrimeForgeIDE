/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { Disposable } from '../../../../base/common/lifecycle.js';
import { IStorageService, StorageScope, StorageTarget } from '../../../../platform/storage/common/storage.js';
import { Memento } from '../../../common/memento.js';
import { IP9ActionEntry, IP9SessionRuntimeState, IP9TranscriptEntry } from '../common/proton9Types.js';

interface IP9RuntimeStoreState {
	sessionState?: Record<string, IP9SessionRuntimeState>;
}

function createEmptySessionState(): IP9SessionRuntimeState {
	return {
		transcript: [],
		actions: [],
		draft: '',
	};
}

export class P9RuntimeStore extends Disposable {
	private static readonly STORAGE_ID = 'proton9.runtimeState';

	private readonly memento: Memento;
	private readonly state: IP9RuntimeStoreState;
	private readonly sessionState = new Map<string, IP9SessionRuntimeState>();

	constructor(storageService: IStorageService) {
		super();
		this.memento = new Memento(P9RuntimeStore.STORAGE_ID, storageService);
		this.state = this.memento.getMemento(StorageScope.WORKSPACE, StorageTarget.MACHINE);
		this.restore();
	}

	getSessionState(tabId: string): IP9SessionRuntimeState {
		return this.sessionState.get(tabId) ?? createEmptySessionState();
	}

	getTranscriptEntries(tabId: string): readonly IP9TranscriptEntry[] {
		return this.getSessionState(tabId).transcript;
	}

	setTranscriptEntries(tabId: string, transcript: readonly IP9TranscriptEntry[]): void {
		const current = this.getSessionState(tabId);
		this.sessionState.set(tabId, {
			...current,
			transcript: transcript.map(entry => ({ ...entry })),
		});
		this.save();
	}

	getActionEntries(tabId: string): readonly IP9ActionEntry[] {
		return this.getSessionState(tabId).actions;
	}

	setActionEntries(tabId: string, actions: readonly IP9ActionEntry[]): void {
		const current = this.getSessionState(tabId);
		this.sessionState.set(tabId, {
			...current,
			actions: actions.map(entry => ({ ...entry })),
		});
		this.save();
	}

	getDraft(tabId: string): string {
		return this.getSessionState(tabId).draft;
	}

	setDraft(tabId: string, draft: string): void {
		const current = this.getSessionState(tabId);
		this.sessionState.set(tabId, {
			...current,
			draft,
		});
		this.save();
	}

	clearSessionState(tabId: string): void {
		this.sessionState.set(tabId, createEmptySessionState());
		this.save();
	}

	removeSessionState(tabId: string): void {
		this.sessionState.delete(tabId);
		this.save();
	}

	syncSessions(validTabIds: readonly string[]): void {
		const validIds = new Set(validTabIds);
		let changed = false;
		for (const tabId of this.sessionState.keys()) {
			if (!validIds.has(tabId)) {
				this.sessionState.delete(tabId);
				changed = true;
			}
		}
		if (changed) {
			this.save();
		}
	}

	private restore(): void {
		const storedState = this.state.sessionState ?? {};
		for (const [tabId, sessionState] of Object.entries(storedState)) {
			if (!tabId || !sessionState) {
				continue;
			}
			this.sessionState.set(tabId, {
				transcript: Array.isArray(sessionState.transcript) ? sessionState.transcript.filter(Boolean).map(entry => ({ ...entry })) : [],
				actions: Array.isArray(sessionState.actions) ? sessionState.actions.filter(Boolean).map(entry => ({ ...entry })) : [],
				draft: typeof sessionState.draft === 'string' ? sessionState.draft : '',
			});
		}
	}

	private save(): void {
		this.state.sessionState = {};
		for (const [tabId, sessionState] of this.sessionState) {
			this.state.sessionState[tabId] = {
				transcript: sessionState.transcript.map(entry => ({ ...entry })),
				actions: sessionState.actions.map(entry => ({ ...entry })),
				draft: sessionState.draft,
			};
		}
		this.memento.saveMemento();
	}
}
