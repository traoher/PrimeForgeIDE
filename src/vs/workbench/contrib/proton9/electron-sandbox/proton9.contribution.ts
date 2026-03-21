/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import * as nls from '../../../../nls.js';
import { Codicon } from '../../../../base/common/codicons.js';
import { SyncDescriptor } from '../../../../platform/instantiation/common/descriptors.js';
import { InstantiationType, registerSingleton } from '../../../../platform/instantiation/common/extensions.js';
import { Registry } from '../../../../platform/registry/common/platform.js';
import { registerIcon } from '../../../../platform/theme/common/iconRegistry.js';
import { IP9SessionService } from '../common/proton9Service.js';
import { IWorkbenchContribution, WorkbenchPhase, registerWorkbenchContribution2 } from '../../../common/contributions.js';
import { ViewPaneContainer } from '../../../browser/parts/views/viewPaneContainer.js';
import { Extensions as ViewContainerExtensions, IViewContainersRegistry, IViewsRegistry, ViewContainerLocation } from '../../../common/views.js';
import { P9SessionService } from '../browser/proton9SessionService.js';
import { P9_ACTION_FEED_VIEW_ID, P9_CHAT_VIEW_ID, P9_SESSIONS_VIEW_ID, P9_STATUS_VIEW_ID, P9_VIEW_CONTAINER_ID } from '../common/proton9Types.js';
import '../browser/proton9RuntimeService.js';
import { Proton9ActionFeedView } from '../browser/proton9ActionFeedView.js';
import { Proton9ChatView } from '../browser/proton9ChatView.js';
import { Proton9SessionsView } from '../browser/proton9SessionsView.js';
import { Proton9StatusView } from '../browser/proton9StatusView.js';

registerSingleton(IP9SessionService, P9SessionService, InstantiationType.Delayed);

const proton9ViewIcon = registerIcon('proton9-view-icon', Codicon.commentDiscussion, nls.localize('proton9ViewIcon', "Icon for the Proton9 view container."));

const proton9ViewContainer = Registry.as<IViewContainersRegistry>(ViewContainerExtensions.ViewContainersRegistry).registerViewContainer({
	id: P9_VIEW_CONTAINER_ID,
	title: nls.localize2('proton9', "Proton9"),
	icon: proton9ViewIcon,
	ctorDescriptor: new SyncDescriptor(ViewPaneContainer, [P9_VIEW_CONTAINER_ID, { mergeViewWithContainerWhenSingleView: true }]),
	storageId: P9_VIEW_CONTAINER_ID,
	hideIfEmpty: false,
	order: 8,
}, ViewContainerLocation.Sidebar);

Registry.as<IViewsRegistry>(ViewContainerExtensions.ViewsRegistry).registerViews([{
	id: P9_SESSIONS_VIEW_ID,
	name: nls.localize2('proton9Sessions', "P9 Sessions"),
	containerIcon: proton9ViewIcon,
	canToggleVisibility: true,
	canMoveView: true,
	ctorDescriptor: new SyncDescriptor(Proton9SessionsView),
}, {
	id: P9_CHAT_VIEW_ID,
	name: nls.localize2('proton9Chat', "P9 Chat"),
	containerIcon: proton9ViewIcon,
	canToggleVisibility: true,
	canMoveView: true,
	ctorDescriptor: new SyncDescriptor(Proton9ChatView),
}, {
	id: P9_ACTION_FEED_VIEW_ID,
	name: nls.localize2('proton9Actions', "P9 Actions"),
	containerIcon: proton9ViewIcon,
	canToggleVisibility: true,
	canMoveView: true,
	ctorDescriptor: new SyncDescriptor(Proton9ActionFeedView),
}, {
	id: P9_STATUS_VIEW_ID,
	name: nls.localize2('proton9Status', "P9 Status"),
	containerIcon: proton9ViewIcon,
	canToggleVisibility: true,
	canMoveView: true,
	ctorDescriptor: new SyncDescriptor(Proton9StatusView),
}], proton9ViewContainer);

class Proton9BootstrapContribution implements IWorkbenchContribution {
	static readonly ID = 'workbench.contrib.proton9.bootstrap';

	constructor(
		@IP9SessionService private readonly p9SessionService: IP9SessionService,
	) {
		if (!this.p9SessionService.getActiveSession()) {
			this.p9SessionService.createSession();
		}
	}
}

registerWorkbenchContribution2(Proton9BootstrapContribution.ID, Proton9BootstrapContribution, WorkbenchPhase.AfterRestored);
