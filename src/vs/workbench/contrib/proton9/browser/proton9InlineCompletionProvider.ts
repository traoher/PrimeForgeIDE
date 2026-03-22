/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { CancellationToken } from '../../../../base/common/cancellation.js';
import { Disposable } from '../../../../base/common/lifecycle.js';
import { P9BackendClient } from './proton9BackendClient.js';

/**
 * Proton9 native inline completion provider.
 *
 * Sends {type: 'complete', prefix, suffix, language, file} to the backend
 * and listens for {type: 'completion_result', completion} responses.
 *
 * 300ms debounce, 3s timeout, 30-line context window.
 */
export class P9InlineCompletionProvider extends Disposable {
	private enabled = true;
	private lastRequestTime = 0;

	private static readonly DEBOUNCE_MS = 300;
	private static readonly TIMEOUT_MS = 3000;
	private static readonly CONTEXT_LINES = 30;
	private static readonly SKIP_LANGUAGES = new Set([
		'plaintext', 'log', 'scminput', 'git-commit', 'git-rebase',
		'search-result', 'output',
	]);

	constructor(
		private readonly backendClient: P9BackendClient,
	) {
		super();
	}

	setEnabled(enabled: boolean): void {
		this.enabled = enabled;
	}

	isEnabled(): boolean {
		return this.enabled;
	}

	async provideInlineCompletions(
		languageId: string,
		filePath: string,
		prefix: string,
		suffix: string,
		token: CancellationToken,
	): Promise<string | null> {
		if (!this.enabled || !this.backendClient.isConnected()) {
			return null;
		}

		if (P9InlineCompletionProvider.SKIP_LANGUAGES.has(languageId)) {
			return null;
		}

		// Debounce: wait 300ms from last keystroke
		const now = Date.now();
		this.lastRequestTime = now;
		await new Promise<void>(resolve => setTimeout(resolve, P9InlineCompletionProvider.DEBOUNCE_MS));
		if (this.lastRequestTime !== now || token.isCancellationRequested) {
			return null;
		}

		if (prefix.trim().length < 3) {
			return null;
		}

		return this.requestCompletion(filePath, languageId, prefix, suffix, token);
	}

	private requestCompletion(
		filePath: string,
		language: string,
		prefix: string,
		suffix: string,
		token: CancellationToken,
	): Promise<string | null> {
		return new Promise<string | null>((resolve) => {
			if (!this.backendClient.isConnected()) {
				resolve(null);
				return;
			}

			let resolved = false;

			const timeout = setTimeout(() => {
				if (!resolved) {
					resolved = true;
					handler.dispose();
					resolve(null);
				}
			}, P9InlineCompletionProvider.TIMEOUT_MS);

			// Listen for completion_result
			const handler = this.backendClient.onDidReceiveEvent((event) => {
				if (event.type === 'completion_result' && !resolved) {
					resolved = true;
					clearTimeout(timeout);
					handler.dispose();
					resolve(event.data?.completion || null);
				}
			});

			token.onCancellationRequested(() => {
				if (!resolved) {
					resolved = true;
					clearTimeout(timeout);
					handler.dispose();
					resolve(null);
				}
			});

			// Send completion request
			this.backendClient.sendRaw({
				type: 'complete',
				file: filePath,
				language,
				prefix: prefix.substring(Math.max(0, prefix.length - 2000)),
				suffix: suffix.substring(0, 500),
			});
		});
	}
}
