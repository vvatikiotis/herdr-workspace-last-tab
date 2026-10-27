# Workspace Last Tab

Agreed behavior for the event-driven plugin redesign. This describes requirements, not the current implementation.

## Tracking

- A Herdr plugin startup hook launches one event listener per session. It subscribes to server events and processes tab-focus events sequentially, without polling or a shell-started background watcher.
- Individual event hooks must not update history: Herdr runs those hooks concurrently without guaranteeing execution order.
- The listener exits when its server connection closes. Herdr does not supervise startup commands or automatically restart a failed listener.
- Maintain independent `{current_tab_id, last_tab_id}` history for each workspace, isolated by Herdr session.
- Initialize a newly tracked workspace with its active tab as current and no last tab.
- On a change to a different active tab, make the former current tab the last tab if it still exists in that workspace; otherwise clear the last tab.
- Repeated observations of the same active tab do not change history.
- Closing a workspace deletes its entire history without affecting other workspaces.

## Switching and closure

- The shortcut targets only the focused workspace's distinct, existing last tab.
- Without a valid last tab, the shortcut does nothing.
- Successful switches update history so repeated presses toggle between the two tabs.
- Closing the last tab clears the last-tab reference. Do not fall back to an older tab.
- For A → B → C, closing C and activating B leaves current B and no last tab. Do not retain a longer recency list.

## Restart

- Preserve history across Herdr server restarts when the referenced workspaces and tabs are restored.
- Remove references to workspaces and tabs that no longer exist.
- If restored active B differs from saved current A, make A the last tab if A still exists in that workspace; otherwise clear the last tab.

## Implementation constraints

- Store plugin-owned history under `HERDR_PLUGIN_STATE_DIR`.
- Keep histories isolated by Herdr session; do not assume the directory itself is session-specific.

## Verified implementation choices

- Herdr 0.9.5 startup hooks launch commands asynchronously and do not supervise them. Individual event hooks also run concurrently, so they cannot serialize history updates.
- The listener subscribes to `tab.focused`, `workspace.metadata_updated`, `tab.closed`, and `workspace.closed`. Focus events are ordered within their subscription; different event types have no shared ordering guarantee. Live snapshots prune references to closed objects.
- Shortcut actions send a request through the private `workspace_last_tab_control` metadata token (five-second TTL), then wait for completion. The listener uses a second marker to ensure another focus drain before processing the request, using Herdr 0.9.5's focus-before-metadata subscription batch order. Only the listener advances history. Markers carry request identifiers, not saved history; duplicate or expired markers are ignored. The real-session test must be rerun on Herdr upgrades because this barrier depends on the server's batch implementation.
- Focus history uses event payloads rather than replacing the active tab with the latest snapshot on every event. Mock-socket tests cover queued intermediate tab changes; the disposable Herdr test covers real event delivery and plugin-action dispatch.
- `HERDR_PLUGIN_STATE_DIR` is shared by sessions. Hex-encoded socket paths provide session-specific subdirectories and survive server restarts.
- State is JSON, with one listener as the sole writer, OS file locks preventing conflicting writes/duplicate listeners, and atomic replacement through a sibling file in the state directory.
- A separate per-session action lock serializes complete shortcut invocations until completion acknowledgment, so a later action cannot overtake delayed focus events from an earlier action. Request-specific focus or acknowledgment failures do not terminate tracking in other workspaces.
- Subscription EOF exits normally. Other socket/protocol errors (including `events_lost`) fail explicitly; startup reconciliation cannot reconstruct missing tab changes. See README.md for lifecycle limitations.
