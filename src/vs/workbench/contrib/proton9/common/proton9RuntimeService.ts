/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { Event } from '../../../../base/common/event.js';
import { createDecorator } from '../../../../platform/instantiation/common/instantiation.js';
import { IP9ActionEntry, IP9ConnectionState, IP9NativeSession, IP9RuntimeStatusSnapshot, IP9TranscriptEntry } from './proton9Types.js';

export const IP9RuntimeService = createDecorator<IP9RuntimeService>('p9RuntimeService');

export type P9RuntimeEvent =
	| { kind: 'sessions'; tabId?: string }
	| { kind: 'connection' }
	| { kind: 'status'; tabId?: string }
	| { kind: 'transcript-entry'; tabId: string; entry: IP9TranscriptEntry }
	| { kind: 'assistant-chunk'; tabId: string; entryId: string; text: string }
	| { kind: 'action-entry'; tabId: string; entry: IP9ActionEntry };

export interface IP9RuntimeService {
	readonly _serviceBrand: undefined;
	readonly onDidChangeState: Event<P9RuntimeEvent>;

	getSessions(): readonly IP9NativeSession[];
	getActiveSession(): IP9NativeSession | undefined;
	getConnectionState(): IP9ConnectionState;
	getStatusSnapshot(): IP9RuntimeStatusSnapshot;
	getTranscriptEntries(tabId: string): readonly IP9TranscriptEntry[];
	getActionEntries(tabId: string): readonly IP9ActionEntry[];
	getComposerDraft(tabId: string): string;

	canCreateSession(): boolean;
	createSession(): IP9NativeSession;
	setActiveSession(tabId: string): Promise<void>;
	renameSession(tabId: string, title: string): void;
	clearSession(tabId: string): Promise<void>;
	deleteSession(tabId: string): Promise<void>;
	updateComposerDraft(tabId: string, draft: string): void;

	connect(): Promise<void>;
	runTask(tabId: string, task: string): Promise<void>;
	stopTask(tabId: string): Promise<void>;

	openResource(path: string): Promise<void>;
	showTerminal(): void;
	revealSearch(): Promise<void>;
}
