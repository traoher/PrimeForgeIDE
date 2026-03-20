# Proton9 Native Contrib Scaffold

This folder is the starting point for native Proton9 workbench integration inside `PrimeForgeIDE`.

## Current Scope

The scaffold establishes the first native structure for:

- a session model that preserves `P9-1 ... P9-9`
- a native session service contract
- a local session store
- a WebSocket-backed client that matches the Proton9 server contract
- a bootstrap contribution for desktop startup
- a minimal visible native workbench view for Proton9 sessions

## Folder Map

- `common/proton9Types.ts`
  Defines the shared session and message payload types.
- `common/proton9Service.ts`
  Defines the native session service interface.
- `browser/proton9SessionStore.ts`
  Holds native tab/session state and persists it through workbench storage.
- `browser/proton9SessionService.ts`
  Implements session creation, activation, update, removal, and restore-aware slot allocation.
- `browser/proton9BackendClient.ts`
  Manages WebSocket connection plus payloads for `run_task`, `stop_slot`, and `switch_session`.
- `browser/proton9View.ts`
  Renders a minimal native Proton9 session view backed by `IP9SessionService` and the backend client.
- `electron-sandbox/proton9.contribution.ts`
  Registers the service, native view container, native view, and bootstraps an initial P9 session.

## Intended Next Steps

1. Expand the native view into a richer chat surface and action feed.
2. Improve streamed token rendering and per-session transcript handling.
3. Route more native UI events to the Proton9 backend using `slot_id`, `session_id`, and `client_id`.
4. Add restore-time session reconciliation with backend state and slot activity.
