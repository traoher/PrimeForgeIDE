/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { createDecorator } from '../../../../platform/instantiation/common/instantiation.js';
import { Event } from '../../../../base/common/event.js';
import { IP9NativeSession, P9SessionStatus } from './proton9Types.js';

export const IP9SessionService = createDecorator<IP9SessionService>('p9SessionService');

export interface IP9SessionService {
	readonly _serviceBrand: undefined;
	readonly onDidChangeSessions: Event<void>;

	getSessions(): readonly IP9NativeSession[];
	getActiveSession(): IP9NativeSession | undefined;
	createSession(): IP9NativeSession;
	setActiveSession(tabId: string): void;
	updateSession(tabId: string, update: Partial<IP9NativeSession>): void;
	updateSessionStatus(tabId: string, status: P9SessionStatus): void;
	removeSession(tabId: string): void;
}
