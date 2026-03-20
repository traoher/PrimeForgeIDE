/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

export const P9_VIEW_CONTAINER_ID = 'workbench.view.proton9';
export const P9_CHAT_VIEW_ID = 'workbench.view.proton9.chat';
export const P9_ACTION_FEED_VIEW_ID = 'workbench.view.proton9.actionFeed';
export const P9_STATUS_VIEW_ID = 'workbench.view.proton9.status';

export type P9SessionStatus = 'idle' | 'running' | 'stopped' | 'error';

export interface IP9NativeSession {
	tabId: string;
	slotId: string;
	sessionId: string;
	clientId: string;
	title: string;
	workspacePath?: string;
	provider?: string;
	model?: string;
	status: P9SessionStatus;
	lastActiveAt: number;
	activeEnsembleId?: string;
}

export interface IP9RunTaskPayload {
	type: 'run_task';
	slot_id: string;
	client_id: string;
	session_id: string;
	working_dir?: string;
	task: string;
	provider?: string;
	model?: string;
}

export interface IP9StopTaskPayload {
	type: 'stop_slot';
	slot_id: string;
}

export interface IP9SwitchSessionPayload {
	type: 'switch_session';
	client_id: string;
	session_id: string;
}
