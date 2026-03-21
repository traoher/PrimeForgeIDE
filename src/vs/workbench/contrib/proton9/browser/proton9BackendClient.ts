/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { Emitter } from '../../../../base/common/event.js';
import { Disposable } from '../../../../base/common/lifecycle.js';
import { IP9ClearSessionPayload, IP9DeleteSessionPayload, IP9NativeSession, IP9RunTaskPayload, IP9StopTaskPayload, IP9SwitchSessionPayload } from '../common/proton9Types.js';

export interface IP9BackendEvent {
	type: string;
	data: any;
}

export class P9BackendClient extends Disposable {
	private static readonly DEFAULT_URL = 'ws://localhost:9321';
	private static readonly READY_MESSAGE = 'Proton9 server ready';

	private socket: WebSocket | undefined;
	private readonly _onDidReceiveEvent = this._register(new Emitter<IP9BackendEvent>());
	readonly onDidReceiveEvent = this._onDidReceiveEvent.event;

	private readonly _onDidChangeConnection = this._register(new Emitter<boolean>());
	readonly onDidChangeConnection = this._onDidChangeConnection.event;
	private handshakeVerified = false;

	async ensureConnected(): Promise<void> {
		if (this.socket?.readyState === WebSocket.OPEN && this.handshakeVerified) {
			return;
		}

		if (this.socket?.readyState === WebSocket.CONNECTING) {
			return new Promise((resolve, reject) => {
				const onConnected = (event: IP9BackendEvent) => {
					if (event.type !== 'connected') {
						return;
					}
					cleanup();
					resolve();
				};
				const onError = (event: IP9BackendEvent) => {
					if (event.type !== 'error') {
						return;
					}
					cleanup();
					reject(new Error(String(event.data?.message ?? 'Failed to connect to Proton9 backend.')));
				};
				const cleanup = () => {
					connectedDisposable.dispose();
					errorDisposable.dispose();
				};
				const connectedDisposable = this.onDidReceiveEvent(onConnected);
				const errorDisposable = this.onDidReceiveEvent(onError);
			});
		}

		this.handshakeVerified = false;
		this.socket = new WebSocket(P9BackendClient.DEFAULT_URL);
		this.socket.addEventListener('close', () => {
			this.handshakeVerified = false;
			this._onDidChangeConnection.fire(false);
		});
		this.socket.addEventListener('error', () => {
			this.handshakeVerified = false;
			this._onDidChangeConnection.fire(false);
		});
		this.socket.addEventListener('message', event => {
			try {
				const data = JSON.parse(String(event.data));
				if (String(data?.type ?? '') === 'connected') {
					const readyMessage = String(data?.message ?? '');
					if (!this.isCompatibleBackend(readyMessage)) {
						const message = `Incompatible backend on port 9321: ${readyMessage || 'unknown server'}. Start Proton9/core/server.py, not PrimeForge/core/server.py.`;
						this.handshakeVerified = false;
						this._onDidReceiveEvent.fire({
							type: 'error',
							data: { type: 'error', message },
						});
						this.socket?.close(1000, 'incompatible backend');
						this._onDidChangeConnection.fire(false);
						return;
					}
					this.handshakeVerified = true;
					this._onDidChangeConnection.fire(true);
				}
				this._onDidReceiveEvent.fire({
					type: String(data?.type ?? 'unknown'),
					data,
				});
			} catch {
				this._onDidReceiveEvent.fire({
					type: 'raw',
					data: event.data,
				});
			}
		});

		return new Promise((resolve, reject) => {
			const onConnected = (event: IP9BackendEvent) => {
				if (event.type !== 'connected') {
					return;
				}
				cleanup();
				resolve();
			};
			const onError = (event: IP9BackendEvent) => {
				if (event.type !== 'error') {
					return;
				}
				cleanup();
				reject(new Error(String(event.data?.message ?? 'Failed to connect to Proton9 backend.')));
			};
			const onClose = () => {
				cleanup();
				reject(new Error('Disconnected from Proton9 backend.'));
			};
			const cleanup = () => {
				connectedDisposable.dispose();
				errorDisposable.dispose();
				this.socket?.removeEventListener('close', onClose);
			};
			const connectedDisposable = this.onDidReceiveEvent(onConnected);
			const errorDisposable = this.onDidReceiveEvent(onError);
			this.socket?.addEventListener('close', onClose);
		});
	}

	isConnected(): boolean {
		return this.socket?.readyState === WebSocket.OPEN;
	}

	async runTask(session: IP9NativeSession, task: string): Promise<void> {
		await this.ensureConnected();
		this.send(this.createRunTaskPayload(session, task));
	}

	async stopTask(session: IP9NativeSession): Promise<void> {
		await this.ensureConnected();
		this.send(this.createStopPayload(session));
	}

	async switchSession(session: IP9NativeSession): Promise<void> {
		await this.ensureConnected();
		this.send(this.createSwitchSessionPayload(session));
	}

	async clearSession(session: IP9NativeSession): Promise<void> {
		await this.ensureConnected();
		this.send(this.createClearSessionPayload(session));
	}

	async deleteSession(session: IP9NativeSession): Promise<void> {
		await this.ensureConnected();
		this.send(this.createDeleteSessionPayload(session));
	}

	private send(payload: object): void {
		if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
			throw new Error('Proton9 backend is not connected.');
		}
		this.socket.send(JSON.stringify(payload));
	}

	private isCompatibleBackend(readyMessage: string): boolean {
		return readyMessage === P9BackendClient.READY_MESSAGE || /proton9/i.test(readyMessage);
	}

	private createRunTaskPayload(session: IP9NativeSession, task: string): IP9RunTaskPayload {
		return {
			type: 'run_task',
			slot_id: session.slotId,
			client_id: session.clientId,
			session_id: session.sessionId,
			working_dir: session.workspacePath,
			task,
			provider: session.provider,
			model: session.model,
		};
	}

	private createStopPayload(session: IP9NativeSession): IP9StopTaskPayload {
		return {
			type: 'stop_slot',
			slot_id: session.slotId,
		};
	}

	private createSwitchSessionPayload(session: IP9NativeSession): IP9SwitchSessionPayload {
		return {
			type: 'switch_session',
			client_id: session.clientId,
			session_id: session.sessionId,
		};
	}

	private createClearSessionPayload(session: IP9NativeSession): IP9ClearSessionPayload {
		return {
			type: 'clear_context',
			client_id: session.clientId,
		};
	}

	private createDeleteSessionPayload(session: IP9NativeSession): IP9DeleteSessionPayload {
		return {
			type: 'delete_chat',
			client_id: session.clientId,
			session_id: session.sessionId,
		};
	}

	override dispose(): void {
		this.socket?.close();
		super.dispose();
	}
}
